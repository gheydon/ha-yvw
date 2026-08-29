"""Tests for the long-term statistics import.

The running sum is cumulative and never rewritten, so a duplicated or
double-counted hour corrupts every figure after it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.yvw.api import UsageReading
from custom_components.yvw.const import MAX_HISTORY_DAYS
from custom_components.yvw.statistics import async_insert_statistics, statistic_id_for

MELBOURNE = ZoneInfo("Australia/Melbourne")
METER = "YAW0000001"
ADDRESS = "1 Example St, Suburb, Vic, 3000"


def readings(start: datetime, litres: list[float]) -> list[UsageReading]:
    return [
        UsageReading(start=start + timedelta(hours=index), litres=value)
        for index, value in enumerate(litres)
    ]


async def _stored(hass: HomeAssistant, statistic_id: str) -> list[dict]:
    await async_wait_recording_done(hass)
    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    return stats.get(statistic_id, [])


def test_statistic_id_is_derived_from_the_meter() -> None:
    """External statistic ids must be lower case and free of punctuation."""
    assert statistic_id_for("YAW-000/001") == "yvw:water_consumption_yaw_000_001"


async def test_first_import_starts_the_running_sum_at_zero(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A new meter begins its cumulative total from nothing."""
    start = datetime(2026, 8, 20, 1, 0, tzinfo=MELBOURNE)

    added = await async_insert_statistics(hass, METER, ADDRESS, readings(start, [10, 20, 30]))

    assert len(added) == 3
    rows = await _stored(hass, statistic_id_for(METER))
    assert [row["state"] for row in rows] == [10, 20, 30]
    assert [row["sum"] for row in rows] == [10, 30, 60]


async def test_a_second_import_continues_the_running_sum(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The next day's readings must extend the total, not restart it."""
    start = datetime(2026, 8, 20, 1, 0, tzinfo=MELBOURNE)
    await async_insert_statistics(hass, METER, ADDRESS, readings(start, [10, 20]))
    await async_wait_recording_done(hass)

    added = await async_insert_statistics(
        hass, METER, ADDRESS, readings(start + timedelta(hours=2), [5])
    )

    assert len(added) == 1
    rows = await _stored(hass, statistic_id_for(METER))
    assert [row["sum"] for row in rows] == [10, 30, 35]


async def test_replayed_hours_are_not_counted_twice(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Every poll re-fetches 30 days, so overlap is the normal case."""
    start = datetime(2026, 8, 20, 1, 0, tzinfo=MELBOURNE)
    await async_insert_statistics(hass, METER, ADDRESS, readings(start, [10, 20]))
    await async_wait_recording_done(hass)

    added = await async_insert_statistics(hass, METER, ADDRESS, readings(start, [10, 20, 30]))

    assert len(added) == 1
    rows = await _stored(hass, statistic_id_for(METER))
    assert [row["sum"] for row in rows] == [10, 30, 60]


async def test_nothing_new_is_a_no_op(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A poll that finds no fresh hours must not write anything."""
    start = datetime(2026, 8, 20, 1, 0, tzinfo=MELBOURNE)
    await async_insert_statistics(hass, METER, ADDRESS, readings(start, [10]))
    await async_wait_recording_done(hass)

    assert await async_insert_statistics(hass, METER, ADDRESS, readings(start, [10])) == []


async def test_no_readings_writes_nothing(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A dead session or an empty window must not touch the statistics."""
    assert await async_insert_statistics(hass, METER, ADDRESS, []) == []


async def test_a_long_gap_is_filled_in_when_the_user_signs_back_in(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Losing the session for weeks should cost nothing but the sign-in.

    The portal serves a rolling window, so as long as the gap is inside it, the
    next successful poll picks up everything missed and the running total
    carries on from where it stopped.
    """
    start = datetime(2026, 8, 1, 0, 0, tzinfo=MELBOURNE)

    # Two days recorded, then the session lapses and nothing is collected.
    before = [
        UsageReading(start=start + timedelta(hours=hour), litres=10.0)
        for hour in range(48)
    ]
    assert len(await async_insert_statistics(hass, METER, ADDRESS, before)) == 48
    await async_wait_recording_done(hass)

    # Twenty days later the user signs in again, and the portal hands back the
    # whole window it still holds — including everything missed.
    catch_up = [
        UsageReading(start=start + timedelta(hours=hour), litres=10.0)
        for hour in range(24 * 22)
    ]
    added = await async_insert_statistics(hass, METER, ADDRESS, catch_up)
    await async_wait_recording_done(hass)

    # Only the hours not already held are added, and none are duplicated.
    assert len(added) == 24 * 22 - 48

    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        None,
        {statistic_id_for(METER)},
        "hour",
        None,
        {"sum", "state"},
    )
    rows = stats[statistic_id_for(METER)]
    assert len(rows) == 24 * 22
    # The running total continues rather than restarting at the gap.
    assert rows[-1]["sum"] == pytest.approx(24 * 22 * 10.0)


async def test_readings_older_than_the_window_cannot_be_recovered(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The portal only serves a rolling window, so a longer gap loses data.

    Worth pinning: it is the reason re-authenticating promptly matters.
    """
    assert MAX_HISTORY_DAYS == 30
