"""Tests for setting up and tearing down the integration."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import Recorder
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.yvw.api import UsageReading
from custom_components.yvw.const import (
    CONF_ACCOUNT_ID,
    CONF_ADDRESS,
    CONF_KEEPALIVE_MINUTES,
    CONF_METER_SERIAL,
    CONF_SID,
    DOMAIN,
)

MELBOURNE = ZoneInfo("Australia/Melbourne")
ACCOUNT = "1234567890"
METER = "YAW0000001"
ADDRESS = "1 Example St, Suburb, Vic, 3000"


def entry(**kwargs) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=ADDRESS,
        unique_id=ACCOUNT,
        **kwargs,
        data={
            CONF_SID: "session",
            CONF_ACCOUNT_ID: ACCOUNT,
            CONF_METER_SERIAL: METER,
            CONF_ADDRESS: ADDRESS,
        },
    )


def readings() -> list[UsageReading]:
    start = datetime(2026, 8, 20, 0, 0, tzinfo=MELBOURNE)
    return [
        UsageReading(start=start + timedelta(hours=index), litres=float(index + 1))
        for index in range(24)
    ]


async def _setup(hass: HomeAssistant, config_entry: MockConfigEntry) -> AsyncMock:
    config_entry.add_to_hass(hass)
    api = AsyncMock()
    api.async_get_hourly_usage.return_value = readings()
    api.async_ping.return_value = None
    with (
        patch("custom_components.yvw.YvwAuraClient"),
        patch("custom_components.yvw.YvwApi", return_value=api),
    ):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    return api


async def test_setup_creates_the_sensors(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """The integration should come up and expose its sensors."""
    config_entry = entry()
    await _setup(hass, config_entry)

    assert config_entry.state is ConfigEntryState.LOADED

    states = {
        entity_id: hass.states.get(entity_id).state
        for entity_id in hass.states.async_entity_ids("sensor")
    }
    assert len(states) == 3
    assert states["sensor.1_example_st_suburb_vic_3000_latest_hourly_usage"] == "24.0"
    assert states["sensor.1_example_st_suburb_vic_3000_last_full_day_usage"] == "300.0"


async def test_setup_records_the_history(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Setting up should backfill the statistics the Water dashboard reads."""
    from homeassistant.components.recorder.statistics import statistics_during_period
    from homeassistant.util import dt as dt_util

    await _setup(hass, entry())

    stats = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {f"{DOMAIN}:water_consumption_yaw0000001"},
        "hour",
        None,
        {"state", "sum"},
    )
    rows = stats[f"{DOMAIN}:water_consumption_yaw0000001"]
    assert len(rows) == 24
    assert rows[-1]["sum"] == 300


async def test_unload_stops_the_keepalive(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Nothing should keep talking to the portal after removal."""
    config_entry = entry()
    await _setup(hass, config_entry)
    coordinator = config_entry.runtime_data
    assert coordinator.keepalive_running is True

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.NOT_LOADED
    assert coordinator.keepalive_running is False


async def test_the_keepalive_interval_option_is_honoured(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Raising the interval is how the portal is talked to less often."""
    config_entry = entry(options={CONF_KEEPALIVE_MINUTES: 45})
    await _setup(hass, config_entry)

    assert config_entry.runtime_data.keepalive_interval == timedelta(minutes=45)
