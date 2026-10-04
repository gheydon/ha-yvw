"""Import metered readings into Home Assistant's long-term statistics.

The portal publishes consumption a day or so after the fact, so the readings can
never be recorded as they happen. Instead they are written straight into the
statistics tables as external statistics, timestamped with the hour they
actually belong to. That is what makes past usage show up on the Water
dashboard, and it is what lets the integration heal gaps after Home Assistant
has been offline.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import VolumeConverter

from .api import UsageReading
from .const import DOMAIN, MAX_HISTORY_DAYS

_LOGGER = logging.getLogger(__name__)

_UNSAFE_ID_CHARS = re.compile(r"[^a-z0-9_]")


def statistic_id_for(meter_serial: str) -> str:
    """Return the external statistic id used for a meter."""
    slug = _UNSAFE_ID_CHARS.sub("_", meter_serial.lower())
    return f"{DOMAIN}:water_consumption_{slug}"


def _metadata(meter_serial: str, address: str) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=f"{address} water consumption",
        source=DOMAIN,
        statistic_id=statistic_id_for(meter_serial),
        unit_class=VolumeConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfVolume.LITERS,
    )


async def async_insert_statistics(
    hass: HomeAssistant,
    meter_serial: str,
    address: str,
    readings: list[UsageReading],
) -> list[UsageReading]:
    """Record readings into the meter's statistics and return the ones added.

    Normally this only appends: readings at or before the newest stored hour are
    skipped, so a poll overlapping what is already recorded costs nothing.

    But the portal is re-read over a rolling window every time, which means a
    stretch of history that went in wrong can be put right rather than left
    there. If any hour in the window disagrees with what is stored, or is
    missing from it, everything from that hour onwards is written again with the
    running total recalculated. Appending alone could never repair that, because
    a sum that is short stays short for every hour that follows it.
    """
    if not readings:
        return []

    statistic_id = statistic_id_for(meter_serial)

    last_stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, statistic_id, True, {"sum"}
    )

    if last_stats and last_stats.get(statistic_id):
        last_row = last_stats[statistic_id][0]
        running_sum = float(last_row.get("sum") or 0.0)
        last_start = last_row["start"]
    else:
        _LOGGER.debug("No existing statistics for %s; starting a new series", statistic_id)
        running_sum = 0.0
        last_start = None

    ordered = sorted(readings, key=lambda item: item.start)
    repair_from = await _async_first_disagreement(hass, statistic_id, ordered)

    if repair_from is not None:
        running_sum, last_start = await _async_total_before(hass, statistic_id, repair_from)
        _LOGGER.warning(
            "Rewriting %s from %s: the stored history disagrees with the portal there",
            statistic_id,
            repair_from,
        )

    statistics: list[StatisticData] = []
    added: list[UsageReading] = []
    for reading in ordered:
        if last_start is not None and reading.start.timestamp() <= last_start:
            continue
        running_sum += reading.litres
        added.append(reading)
        statistics.append(StatisticData(start=reading.start, state=reading.litres, sum=running_sum))

    if not statistics:
        return []

    _LOGGER.debug(
        "Adding %s hourly statistics to %s (through %s)",
        len(statistics),
        statistic_id,
        statistics[-1]["start"],
    )
    async_add_external_statistics(hass, _metadata(meter_serial, address), statistics)
    return added


async def _async_stored_rows(
    hass: HomeAssistant, statistic_id: str, start: datetime, end: datetime
) -> list[dict]:
    """Return the hourly rows already stored for a window."""
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        end,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return rows.get(statistic_id, [])


async def _async_first_disagreement(
    hass: HomeAssistant, statistic_id: str, ordered: list[UsageReading]
) -> datetime | None:
    """Return the first hour the stored history does not match the portal.

    Only hours the portal has actually reported are compared. A stored hour with
    nothing against it is left alone: the meter not reporting an hour is normal
    and is not the same as the record being wrong.
    """
    first = ordered[0].start
    # The window end is exclusive, so reach past the last hour to see it.
    last = ordered[-1].start + timedelta(hours=1)
    stored = await _async_stored_rows(hass, statistic_id, first, last)
    if not stored:
        return None

    by_start = {row["start"]: row for row in stored}
    for reading in ordered:
        row = by_start.get(reading.start.timestamp())
        if row is None:
            # Nothing stored yet for this hour. Only a problem if later hours
            # are stored, which would mean this one was skipped rather than
            # simply not reached yet.
            if reading.start.timestamp() < max(by_start):
                return reading.start
            continue
        state = row.get("state")
        if state is None or abs(float(state) - reading.litres) > 0.001:
            return reading.start
    return None


async def _async_total_before(
    hass: HomeAssistant, statistic_id: str, moment: datetime
) -> tuple[float, float | None]:
    """Return the running total and hour immediately before a point in time."""
    window_start = moment - timedelta(days=MAX_HISTORY_DAYS + 1)
    rows = await _async_stored_rows(hass, statistic_id, window_start, moment)
    earlier = [row for row in rows if row["start"] < moment.timestamp()]
    if not earlier:
        return 0.0, None
    last = max(earlier, key=lambda row: row["start"])
    return float(last.get("sum") or 0.0), last["start"]
