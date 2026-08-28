"""Tests for polling behaviour."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.yvw.api import UsageReading
from custom_components.yvw.const import (
    CONF_ACCOUNT_ID,
    CONF_ADDRESS,
    CONF_METER_SERIAL,
    CONF_SID,
    DOMAIN,
    EVENT_NEW_READINGS,
)
from custom_components.yvw.coordinator import YvwCoordinator
from custom_components.yvw.exceptions import YvwAuthError

MELBOURNE = ZoneInfo("Australia/Melbourne")
ACCOUNT = "1234567890"
METER = "YAW0000001"
ADDRESS = "1 Example St, Suburb, Vic, 3000"


class StubApi:
    """Stand in for the portal."""

    def __init__(self, readings: list[UsageReading] | None = None, error=None) -> None:
        self.readings = readings or []
        self.error = error
        self.pings = 0

    async def async_get_hourly_usage(self, account_id, meter_serial, start_date, end_date):
        if self.error:
            raise self.error
        return self.readings

    async def async_ping(self, account_id: str) -> None:
        self.pings += 1

    async def async_probe_session_time(self) -> str | None:
        return None


def build_coordinator(hass: HomeAssistant, api: StubApi) -> YvwCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SID: "session",
            CONF_ACCOUNT_ID: ACCOUNT,
            CONF_METER_SERIAL: METER,
            CONF_ADDRESS: ADDRESS,
        },
        unique_id=ACCOUNT,
    )
    entry.add_to_hass(hass)
    return YvwCoordinator(
        hass, entry, api, ACCOUNT, METER, ADDRESS, portal_tz=MELBOURNE
    )


def hourly(count: int, litres: float = 10.0) -> list[UsageReading]:
    start = datetime(2026, 8, 20, 0, 0, tzinfo=MELBOURNE)
    return [
        UsageReading(start=start + timedelta(hours=index), litres=litres)
        for index in range(count)
    ]


async def test_new_readings_fire_an_event(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Automations need a trigger for freshly recorded consumption."""
    events: list[Event] = []
    hass.bus.async_listen(EVENT_NEW_READINGS, events.append)
    coordinator = build_coordinator(hass, StubApi(hourly(3)))

    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    await hass.async_block_till_done()

    assert len(events) == 1
    data = events[0].data
    assert data["count"] == 3
    assert data["litres"] == 30
    assert data["meter_serial"] == METER
    assert data["statistic_id"] == f"{DOMAIN}:water_consumption_yaw0000001"
    assert data["first_hour"] == "2026-08-20T00:00:00+10:00"
    assert data["last_hour"] == "2026-08-20T02:00:00+10:00"


async def test_no_event_when_nothing_is_new(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Every poll re-reads 30 days, so most polls add nothing."""
    events: list[Event] = []
    coordinator = build_coordinator(hass, StubApi(hourly(2)))
    await coordinator.async_refresh()
    await async_wait_recording_done(hass)

    hass.bus.async_listen(EVENT_NEW_READINGS, events.append)
    await coordinator.async_refresh()
    await async_wait_recording_done(hass)
    await hass.async_block_till_done()

    assert events == []


async def test_a_dead_session_asks_for_reauthentication(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A lapsed session must surface, not be recorded as no usage."""
    coordinator = build_coordinator(hass, StubApi(error=YvwAuthError("gone")))

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    assert coordinator.keepalive_running is False


async def test_a_poll_counts_as_contact_so_no_ping_is_needed(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Pinging right after a poll would be a wasted request."""
    api = StubApi(hourly(1))
    coordinator = build_coordinator(hass, api)
    await coordinator.async_refresh()
    await async_wait_recording_done(hass)

    await coordinator._async_keepalive(datetime.now(MELBOURNE))

    assert api.pings == 0


async def test_only_complete_days_are_totalled(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A partial day would read as a sudden drop in consumption."""
    coordinator = build_coordinator(hass, StubApi(hourly(24)))

    await coordinator.async_refresh()

    assert coordinator.data.last_full_day == date(2026, 8, 20)
    assert coordinator.data.last_full_day_litres == 240
