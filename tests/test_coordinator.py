"""Tests for polling behaviour."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.util import dt as dt_util
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
    CONF_PROBE_ENABLED,
    CONF_PROBE_STEP_MINUTES,
    CONF_SID,
    DOMAIN,
    EVENT_AUTH_FAILED,
    EVENT_NEW_READINGS,
    MAX_KEEPALIVE_MINUTES,
)
from custom_components.yvw.coordinator import YvwCoordinator
from custom_components.yvw.exceptions import YvwAuthError
from custom_components.yvw.probe import ProbeState, ProbeStore

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


def build_coordinator(
    hass: HomeAssistant, api: StubApi, options: dict | None = None
) -> YvwCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SID: "session",
            CONF_ACCOUNT_ID: ACCOUNT,
            CONF_METER_SERIAL: METER,
            CONF_ADDRESS: ADDRESS,
        },
        unique_id=ACCOUNT,
        options=options or {},
    )
    entry.add_to_hass(hass)
    return YvwCoordinator(
        hass,
        entry,
        api,
        ACCOUNT,
        METER,
        ADDRESS,
        portal_tz=MELBOURNE,
        probe=ProbeStore(hass),
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


async def test_a_dead_session_fires_an_event(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Recovering needs a person and an SMS code, so it is worth notifying."""
    events: list[Event] = []
    hass.bus.async_listen(EVENT_AUTH_FAILED, events.append)
    coordinator = build_coordinator(hass, StubApi(error=YvwAuthError("gone")))

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["detected_by"] == "poll"
    assert events[0].data["meter_serial"] == METER
    assert events[0].data["address"] == ADDRESS


async def test_a_keepalive_that_finds_a_dead_session_fires_the_event(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Most lapses are noticed by the keep-alive, not by a poll."""
    events: list[Event] = []
    hass.bus.async_listen(EVENT_AUTH_FAILED, events.append)
    api = StubApi()
    api.async_ping = _raise_auth_error
    coordinator = build_coordinator(hass, api)

    await coordinator._async_keepalive(datetime.now(MELBOURNE))
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["detected_by"] == "keepalive"
    assert coordinator.keepalive_running is False


async def _raise_auth_error(account_id: str) -> None:
    raise YvwAuthError("gone")


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


# --- Calibration -----------------------------------------------------------


def test_the_interval_is_the_configured_one_when_not_calibrating(
    hass: HomeAssistant,
) -> None:
    """Nothing should stretch unless asked to."""
    coordinator = build_coordinator(hass, StubApi(), {CONF_KEEPALIVE_MINUTES: 10})

    assert coordinator.calibrating is False
    assert coordinator.keepalive_interval == timedelta(minutes=10)


def test_calibration_tests_a_longer_gap_than_the_last_one_survived(
    hass: HomeAssistant,
) -> None:
    """Each round has to reach further than the last or it learns nothing."""
    coordinator = build_coordinator(
        hass,
        StubApi(),
        {CONF_KEEPALIVE_MINUTES: 10, CONF_PROBE_ENABLED: True, CONF_PROBE_STEP_MINUTES: 5},
    )
    coordinator.probe_state.survived_minutes = 40

    assert coordinator.calibrating is True
    assert coordinator.keepalive_interval == timedelta(minutes=45)


def test_calibration_never_exceeds_the_maximum(hass: HomeAssistant) -> None:
    """A session that never lapses must not stretch without bound."""
    coordinator = build_coordinator(
        hass, StubApi(), {CONF_KEEPALIVE_MINUTES: 10, CONF_PROBE_ENABLED: True}
    )
    coordinator.probe_state.survived_minutes = MAX_KEEPALIVE_MINUTES + 100

    assert coordinator.keepalive_interval == timedelta(minutes=MAX_KEEPALIVE_MINUTES)


def test_a_concluded_measurement_stops_calibrating(hass: HomeAssistant) -> None:
    """Once the timeout is bracketed there is nothing left to measure."""
    coordinator = build_coordinator(
        hass, StubApi(), {CONF_KEEPALIVE_MINUTES: 10, CONF_PROBE_ENABLED: True}
    )
    state = coordinator.probe_state
    state.survived_minutes = 40
    state.failed_minutes = 45

    assert coordinator.calibrating is False
    assert coordinator.keepalive_interval == timedelta(minutes=10)


def test_the_finding_brackets_the_timeout() -> None:
    """The answer is a range: the longest survived and the gap that failed."""
    state = ProbeState(
        survived_minutes=40, failed_minutes=45, failed_session_age_minutes=300
    )

    assert state.concluded is True
    assert "between 40 and 45" in state.summary


async def test_concluding_settles_on_an_interval_inside_the_timeout(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Sitting on the boundary would lapse again; back off inside it."""
    api = StubApi()
    api.async_ping = _raise_auth_error
    coordinator = build_coordinator(
        hass, api, {CONF_KEEPALIVE_MINUTES: 10, CONF_PROBE_ENABLED: True}
    )
    coordinator.probe_state.survived_minutes = 40
    coordinator._last_contact = dt_util.utcnow() - timedelta(minutes=45)

    await coordinator._async_keepalive(datetime.now(MELBOURNE))
    await hass.async_block_till_done()

    state = coordinator.probe_state
    assert state.failed_minutes == 45
    # Calibration switches itself off and keeps clear of the boundary.
    assert coordinator.config_entry.options[CONF_PROBE_ENABLED] is False
    assert coordinator.config_entry.options[CONF_KEEPALIVE_MINUTES] == 30


# --- Keep-alive scheduling --------------------------------------------------


async def test_an_early_wakeup_waits_only_the_time_still_owed(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A poll resets the portal's idle clock, so a ping due now can be skipped.

    Starting a fresh interval when that happens lets the real gap grow towards
    twice the configured one, which is how a session lapses despite a keep-alive
    that looks correctly configured.
    """
    api = StubApi()
    coordinator = build_coordinator(hass, api, {CONF_KEEPALIVE_MINUTES: 10})
    coordinator._last_contact = dt_util.utcnow() - timedelta(minutes=8)

    delays: list[float] = []
    with patch(
        "custom_components.yvw.coordinator.async_call_later",
        side_effect=lambda hass, delay, action: delays.append(delay),
    ):
        await coordinator._async_keepalive(datetime.now(MELBOURNE))

    assert api.pings == 0
    # Two minutes still owed, not another ten.
    assert 100 <= delays[0] <= 130


async def test_a_due_ping_is_sent_and_the_next_is_a_full_interval(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Once the interval really has elapsed, touch the portal."""
    api = StubApi()
    coordinator = build_coordinator(hass, api, {CONF_KEEPALIVE_MINUTES: 10})
    coordinator._last_contact = dt_util.utcnow() - timedelta(minutes=11)

    delays: list[float] = []
    with patch(
        "custom_components.yvw.coordinator.async_call_later",
        side_effect=lambda hass, delay, action: delays.append(delay),
    ):
        await coordinator._async_keepalive(datetime.now(MELBOURNE))

    assert api.pings == 1
    # A fresh interval, jittered no longer than the setting.
    assert 8 * 60 <= delays[0] <= 10 * 60


async def test_calibration_does_not_jitter_the_interval(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Shortening a measured gap would understate the timeout."""
    coordinator = build_coordinator(
        hass, StubApi(), {CONF_KEEPALIVE_MINUTES: 20, CONF_PROBE_ENABLED: True}
    )

    delays: list[float] = []
    with patch(
        "custom_components.yvw.coordinator.async_call_later",
        side_effect=lambda hass, delay, action: delays.append(delay),
    ):
        coordinator._async_schedule_keepalive()

    assert delays[0] == 20 * 60
