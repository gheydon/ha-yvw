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

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import VolumeConverter

from .api import UsageReading
from .const import DOMAIN

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
) -> int:
    """Append readings to the meter's statistics, returning how many were added.

    Readings at or before the newest stored hour are skipped, so a poll that
    overlaps what is already recorded is a no-op rather than a duplicate.
    """
    if not readings:
        return 0

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

    statistics: list[StatisticData] = []
    for reading in sorted(readings, key=lambda item: item.start):
        if last_start is not None and reading.start.timestamp() <= last_start:
            continue
        running_sum += reading.litres
        statistics.append(
            StatisticData(start=reading.start, state=reading.litres, sum=running_sum)
        )

    if not statistics:
        return 0

    _LOGGER.debug(
        "Adding %s hourly statistics to %s (through %s)",
        len(statistics),
        statistic_id,
        statistics[-1]["start"],
    )
    async_add_external_statistics(hass, _metadata(meter_serial, address), statistics)
    return len(statistics)
