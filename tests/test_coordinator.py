"""Tests for polling behaviour."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.yvw.api import UsageReading
from custom_components.yvw.const import (
    CATCHUP_RETRY,
    CONF_ACCOUNT_ID,
    CONF_ADAPTIVE_START,
    CONF_ADDRESS,
    CONF_CATCHUP_FROM_HOUR,
    CONF_CATCHUP_HOURS,
    CONF_KEEPALIVE_MINUTES,
    CONF_METER_SERIAL,
    CONF_PROBE_ENABLED,
    CONF_PROBE_STEP_MINUTES,
    CONF_SID,
    DOMAIN,
    EVENT_AUTH_FAILED,
    EVENT_KEEPALIVE,
    EVENT_NEW_READINGS,
    FAILURE_RETRY,
    KEEPALIVE_RETRY,
    MAX_FAILURE_RETRY,
    MAX_KEEPALIVE_MINUTES,
    MAX_PROBE_MINUTES,
    UPDATE_INTERVAL,
)
from custom_components.yvw.coordinator import YvwCoordinator, YvwData
from custom_components.yvw.exceptions import YvwAuthError, YvwError
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
        UsageReading(start=start + timedelta(hours=index), litres=litres) for index in range(count)
    ]


async def test_new_readings_fire_an_event(recorder_mock: Recorder, hass: HomeAssistant) -> None:
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


async def test_no_event_when_nothing_is_new(recorder_mock: Recorder, hass: HomeAssistant) -> None:
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


async def test_a_dead_session_fires_an_event(recorder_mock: Recorder, hass: HomeAssistant) -> None:
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
    coordinator.probe_state.survived_minutes = MAX_PROBE_MINUTES + 100

    assert coordinator.keepalive_interval == timedelta(minutes=MAX_PROBE_MINUTES)


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
    state = ProbeState(survived_minutes=40, failed_minutes=45, failed_session_age_minutes=300)

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


async def test_a_successful_ping_reports_its_outcome(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Every attempt should be followable without reading a log."""
    events: list[Event] = []
    hass.bus.async_listen(EVENT_KEEPALIVE, events.append)
    coordinator = build_coordinator(hass, StubApi(), {CONF_KEEPALIVE_MINUTES: 10})
    coordinator._last_contact = dt_util.utcnow() - timedelta(minutes=11)

    await coordinator._async_keepalive(datetime.now(MELBOURNE))
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["outcome"] == "ok"
    assert events[0].data["idle_minutes"] == 11
    assert events[0].data["next_minutes"] == 10


async def test_an_expired_session_reports_its_outcome(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """The last message of a calibration run is the one that matters."""
    events: list[Event] = []
    hass.bus.async_listen(EVENT_KEEPALIVE, events.append)
    api = StubApi()
    api.async_ping = _raise_auth_error
    coordinator = build_coordinator(hass, api, {CONF_KEEPALIVE_MINUTES: 10})
    coordinator._last_contact = dt_util.utcnow() - timedelta(minutes=12)

    await coordinator._async_keepalive(datetime.now(MELBOURNE))
    await hass.async_block_till_done()

    assert [e.data["outcome"] for e in events] == ["expired"]
    assert events[0].data["idle_minutes"] == 12


async def test_a_skipped_wakeup_reports_nothing(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Nothing was asked of the portal, so there is no outcome to report."""
    events: list[Event] = []
    hass.bus.async_listen(EVENT_KEEPALIVE, events.append)
    coordinator = build_coordinator(hass, StubApi(), {CONF_KEEPALIVE_MINUTES: 10})
    coordinator._last_contact = dt_util.utcnow() - timedelta(minutes=2)

    await coordinator._async_keepalive(datetime.now(MELBOURNE))
    await hass.async_block_till_done()

    assert events == []


async def test_a_successful_ping_updates_the_sensor(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The sensor reads the time from the coordinator, so it has to be told.

    Without this it sits at unknown for the life of the entry, which is exactly
    how a keep-alive that is working can look like one that never runs.
    """
    coordinator = build_coordinator(hass, StubApi(), {CONF_KEEPALIVE_MINUTES: 10})
    coordinator._last_contact = dt_util.utcnow() - timedelta(minutes=11)
    updates: list[None] = []
    coordinator.async_add_listener(lambda: updates.append(None))

    await coordinator._async_keepalive(datetime.now(MELBOURNE))

    assert coordinator.last_keepalive is not None
    assert updates, "listeners were never told the ping happened"


async def test_an_unexpected_error_still_arms_the_next_ping(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A loop that stops scheduling itself fails silently.

    That is indistinguishable from a session the portal dropped, so anything
    unforeseen must still leave the next ping armed.
    """
    api = StubApi()

    async def explode(account_id: str) -> None:
        raise RuntimeError("something unforeseen")

    api.async_ping = explode
    coordinator = build_coordinator(hass, api, {CONF_KEEPALIVE_MINUTES: 10})
    coordinator._last_contact = dt_util.utcnow() - timedelta(minutes=11)

    delays: list[float] = []
    with patch(
        "custom_components.yvw.coordinator.async_call_later",
        side_effect=lambda hass, delay, action: delays.append(delay),
    ):
        await coordinator._async_keepalive(datetime.now(MELBOURNE))

    assert delays, "the loop stopped after an unexpected error"


async def test_a_failed_ping_is_retried_soon_not_a_whole_interval_later(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A failed ping leaves the session untouched.

    Waiting the full interval again doubles the idle gap, which risks losing a
    session that was fine and, while measuring, reports a gap far longer than
    the one being tested.
    """
    api = StubApi()

    async def refuse(account_id: str) -> None:
        raise YvwError("portal had a moment")

    api.async_ping = refuse
    coordinator = build_coordinator(hass, api, {CONF_KEEPALIVE_MINUTES: 60})
    coordinator._last_contact = dt_util.utcnow() - timedelta(minutes=61)

    delays: list[float] = []
    with patch(
        "custom_components.yvw.coordinator.async_call_later",
        side_effect=lambda hass, delay, action: delays.append(delay),
    ):
        await coordinator._async_keepalive(datetime.now(MELBOURNE))

    assert delays[0] == KEEPALIVE_RETRY.total_seconds()


async def test_a_timed_out_ping_is_also_retried_soon(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A request timeout is what stopped the loop overnight."""
    api = StubApi()

    async def time_out(account_id: str) -> None:
        raise TimeoutError

    api.async_ping = time_out
    coordinator = build_coordinator(hass, api, {CONF_KEEPALIVE_MINUTES: 60})
    coordinator._last_contact = dt_util.utcnow() - timedelta(minutes=61)

    delays: list[float] = []
    with patch(
        "custom_components.yvw.coordinator.async_call_later",
        side_effect=lambda hass, delay, action: delays.append(delay),
    ):
        await coordinator._async_keepalive(datetime.now(MELBOURNE))

    assert delays[0] == KEEPALIVE_RETRY.total_seconds()


async def test_an_expiry_found_during_startup_still_reaches_automations(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Config entries set up before automations are listening.

    A session found expired on the first poll after a restart is exactly that
    case, and is the one most worth being told about — firing it into an empty
    bus loses the alert entirely.
    """
    hass.set_state(CoreState.starting)
    events: list[Event] = []
    hass.bus.async_listen(EVENT_AUTH_FAILED, events.append)
    coordinator = build_coordinator(hass, StubApi(error=YvwAuthError("gone")))

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    await hass.async_block_till_done()

    assert events == [], "fired before anything could be listening"

    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data["detected_by"] == "poll"


# --- Watchdog ---------------------------------------------------------------


async def test_the_watchdog_says_nothing_while_the_keepalive_is_running(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """It must be silent in the normal case or it is just noise."""
    events: list[Event] = []
    hass.bus.async_listen(EVENT_KEEPALIVE, events.append)
    coordinator = build_coordinator(hass, StubApi(), {CONF_KEEPALIVE_MINUTES: 30})
    coordinator._last_contact = dt_util.utcnow() - timedelta(minutes=20)

    await coordinator._async_watchdog(datetime.now(MELBOURNE))
    await hass.async_block_till_done()

    assert events == []


async def test_the_watchdog_reports_a_keepalive_that_has_stopped(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A stopped loop looks exactly like a healthy one until readings stop.

    This is what happened overnight: no pings for nearly seven hours, and
    nothing said so.
    """
    events: list[Event] = []
    hass.bus.async_listen(EVENT_KEEPALIVE, events.append)
    coordinator = build_coordinator(hass, StubApi(), {CONF_KEEPALIVE_MINUTES: 30})
    coordinator._last_contact = dt_util.utcnow() - timedelta(hours=7)

    await coordinator._async_watchdog(datetime.now(MELBOURNE))
    await hass.async_block_till_done()

    assert [e.data["outcome"] for e in events] == ["stalled"]
    assert events[0].data["idle_minutes"] == 420


async def test_the_watchdog_reports_a_stall_once_not_every_tick(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """It runs every few minutes; repeating itself would be noise."""
    events: list[Event] = []
    hass.bus.async_listen(EVENT_KEEPALIVE, events.append)
    coordinator = build_coordinator(hass, StubApi(), {CONF_KEEPALIVE_MINUTES: 30})
    coordinator._last_contact = dt_util.utcnow() - timedelta(hours=7)

    for _ in range(3):
        await coordinator._async_watchdog(datetime.now(MELBOURNE))
    await hass.async_block_till_done()

    assert len(events) == 1


async def test_the_watchdog_restarts_the_keepalive(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Reporting a stall without fixing it leaves the session to lapse."""
    coordinator = build_coordinator(hass, StubApi(), {CONF_KEEPALIVE_MINUTES: 30})
    coordinator._last_contact = dt_util.utcnow() - timedelta(hours=7)

    delays: list[float] = []
    with patch(
        "custom_components.yvw.coordinator.async_call_later",
        side_effect=lambda hass, delay, action: delays.append(delay),
    ):
        await coordinator._async_watchdog(datetime.now(MELBOURNE))

    assert delays and delays[0] <= 1, "the keep-alive was not restarted"


async def test_the_watchdog_leaves_a_dead_session_alone(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The user has already been asked to sign in; nagging adds nothing."""
    events: list[Event] = []
    hass.bus.async_listen(EVENT_KEEPALIVE, events.append)
    coordinator = build_coordinator(hass, StubApi(), {CONF_KEEPALIVE_MINUTES: 30})
    coordinator._last_contact = dt_util.utcnow() - timedelta(hours=7)
    coordinator._session_dead = True

    await coordinator._async_watchdog(datetime.now(MELBOURNE))
    await hass.async_block_till_done()

    assert events == []


def test_calibration_may_climb_past_what_anyone_can_configure(
    hass: HomeAssistant,
) -> None:
    """Stopping at the configurable ceiling would only report that ceiling.

    The point of measuring is to find where the limit actually is, which means
    testing gaps longer than anyone would sensibly run.
    """
    coordinator = build_coordinator(
        hass,
        StubApi(),
        {CONF_KEEPALIVE_MINUTES: 60, CONF_PROBE_ENABLED: True, CONF_PROBE_STEP_MINUTES: 15},
    )
    coordinator.probe_state.survived_minutes = MAX_KEEPALIVE_MINUTES

    assert coordinator.keepalive_interval == timedelta(minutes=MAX_KEEPALIVE_MINUTES + 15)


def test_a_configured_interval_is_still_capped(hass: HomeAssistant) -> None:
    """Only measuring gets the longer leash."""
    coordinator = build_coordinator(
        hass, StubApi(), {CONF_KEEPALIVE_MINUTES: MAX_KEEPALIVE_MINUTES + 500}
    )

    assert coordinator.keepalive_interval == timedelta(minutes=MAX_KEEPALIVE_MINUTES)


# --- Aiming the poll at when readings appear --------------------------------


def _at(hass: HomeAssistant, hour: int, complete: bool, options: dict | None = None) -> timedelta:
    """Return the wait chosen at a given hour, for a given state of yesterday."""
    coordinator = build_coordinator(hass, StubApi(), options)
    moment = datetime(2026, 8, 30, hour, 0, tzinfo=MELBOURNE)
    with patch("custom_components.yvw.coordinator.datetime") as clock:
        clock.now.return_value = moment
        return coordinator._next_poll(YvwData(yesterday_complete=complete))


def test_before_the_morning_window_it_waits_for_it(hass: HomeAssistant) -> None:
    """Readings for a day are not there at three in the morning."""
    assert _at(hass, 3, complete=False) == timedelta(hours=1)


def test_during_the_window_it_tries_every_ten_minutes(hass: HomeAssistant) -> None:
    """The portal publishes at no time it announces, so keep looking."""
    assert _at(hass, 7, complete=False) == CATCHUP_RETRY


def test_once_yesterday_is_complete_it_stops_until_tomorrow(
    hass: HomeAssistant,
) -> None:
    """Having got what it came for, asking again is wasted traffic."""
    assert _at(hass, 7, complete=True) == timedelta(hours=21)


def test_a_day_that_never_completes_is_given_up_on(hass: HomeAssistant) -> None:
    """A meter that reported only part of a day is not going to finish it.

    Retrying every ten minutes until midnight would ask over eighty times for
    readings that are never coming.
    """
    assert _at(hass, 12, complete=False) == timedelta(hours=16)


async def test_a_poll_sets_the_next_one_from_what_it_found(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The schedule follows the data rather than a fixed clock."""
    coordinator = build_coordinator(hass, StubApi())

    await coordinator._async_update_data()

    # The stub returns a partial day, so it should be in catch-up or waiting,
    # never the old blind twelve hours.
    assert coordinator.update_interval != UPDATE_INTERVAL


async def test_losing_the_session_updates_the_entities(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """The sensor reads the state from here, so it has to be told it changed.

    Without this the session sensor keeps saying active until the next poll,
    and the history of when it went down is wrong by however long that was.
    """
    coordinator = build_coordinator(hass, StubApi(error=YvwAuthError("gone")))
    updates: list[None] = []
    coordinator.async_add_listener(lambda: updates.append(None))

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    assert coordinator.session_active is False
    assert updates, "the entities were never told the session had gone"


async def test_the_time_in_state_restarts_when_the_session_does(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """History should show how long it was down, then how long it has been up."""
    coordinator = build_coordinator(hass, StubApi(error=YvwAuthError("gone")))
    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()
    went_down = coordinator.status_since

    coordinator.api = StubApi()
    await coordinator._async_update_data()

    assert coordinator.session_active is True
    # Both transitions land in the same microsecond under test, so the check is
    # that the clock was restarted at all, not that time passed.
    assert coordinator.status_since >= went_down
    assert coordinator.expired_at is None


def test_the_hour_it_starts_looking_can_be_moved(hass: HomeAssistant) -> None:
    """Some meters publish earlier than others, and waiting gains nothing."""
    assert _at(hass, 3, complete=False, options={CONF_CATCHUP_FROM_HOUR: 2}) == (CATCHUP_RETRY)
    assert _at(hass, 3, complete=False, options={CONF_CATCHUP_FROM_HOUR: 6}) == (timedelta(hours=3))


def test_the_window_stops_at_the_end_of_the_day_it_started(
    hass: HomeAssistant,
) -> None:
    """Running past midnight would collide with the next day's window."""
    coordinator = build_coordinator(
        hass, StubApi(), {CONF_CATCHUP_FROM_HOUR: 22, CONF_CATCHUP_HOURS: 12}
    )

    assert coordinator.catchup_hours == 2


def test_how_long_to_keep_looking_can_be_changed(hass: HomeAssistant) -> None:
    """A meter that publishes late needs a longer window than one that does not."""
    # Default is four in the morning for six hours, so ten is too late.
    assert _at(hass, 10, complete=False) == timedelta(hours=18)
    # Given ten hours it is still within the window.
    assert _at(hass, 10, complete=False, options={CONF_CATCHUP_HOURS: 10}) == (CATCHUP_RETRY)


def test_a_restart_does_not_look_like_a_new_session(hass: HomeAssistant) -> None:
    """How long the session has been good spans restarts.

    Starting the clock at startup would report a fortnight-old session as being
    a few minutes old every time Home Assistant is restarted.
    """
    signed_in = dt_util.utcnow() - timedelta(days=3)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, options={})
    entry.add_to_hass(hass)
    coordinator = YvwCoordinator(
        hass,
        entry,
        StubApi(),
        ACCOUNT,
        METER,
        ADDRESS,
        portal_tz=MELBOURNE,
        probe=ProbeStore(hass),
        signed_in_at=signed_in,
    )

    assert coordinator.status_since == signed_in
    assert coordinator.signed_in_at == signed_in


def test_without_a_recorded_sign_in_the_clock_starts_now(
    hass: HomeAssistant,
) -> None:
    """Entries made before the sign-in time was recorded still work."""
    coordinator = build_coordinator(hass, StubApi())

    assert coordinator.status_since is not None
    assert coordinator.signed_in_at is None


async def test_only_the_first_find_of_the_day_teaches(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Later polls would each report a shorter wait than the last.

    Learning from every one would drag the start earlier for no reason.
    """
    from custom_components.yvw.schedule_store import ScheduleStore

    schedule = ScheduleStore(hass)
    await schedule.async_load()
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, options={})
    entry.add_to_hass(hass)
    coordinator = YvwCoordinator(
        hass,
        entry,
        StubApi(),
        ACCOUNT,
        METER,
        ADDRESS,
        portal_tz=MELBOURNE,
        probe=ProbeStore(hass),
        schedule=schedule,
    )

    recorded: list[timedelta] = []
    original = schedule.async_record

    async def counted(entry_id, minutes, took, today):
        recorded.append(took)
        return await original(entry_id, minutes, took, today)

    schedule.async_record = counted

    # Inside a window that nothing interrupted, which is the only time a
    # morning counts as a measurement at all.
    moment = datetime(2026, 8, 30, 5, 0, tzinfo=MELBOURNE)
    coordinator._started_at = moment - timedelta(hours=6)
    with patch("custom_components.yvw.coordinator.datetime") as clock:
        clock.now.return_value = moment
        await coordinator._async_learn_from(YvwData(yesterday_complete=True))
        await coordinator._async_learn_from(YvwData(yesterday_complete=True))

    assert len(recorded) == 1


async def test_nothing_is_learned_when_learning_is_off(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Someone who has set an hour deliberately should keep it."""
    from custom_components.yvw.schedule_store import ScheduleStore

    schedule = ScheduleStore(hass)
    await schedule.async_load()
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ACCOUNT, options={CONF_ADAPTIVE_START: False})
    entry.add_to_hass(hass)
    coordinator = YvwCoordinator(
        hass,
        entry,
        StubApi(),
        ACCOUNT,
        METER,
        ADDRESS,
        portal_tz=MELBOURNE,
        probe=ProbeStore(hass),
        schedule=schedule,
    )

    await coordinator._async_learn_from(YvwData(yesterday_complete=True))

    assert schedule.get(entry.entry_id) is None
    assert coordinator.catchup_from_minutes == coordinator.catchup_from_hour * 60


# --- Recovering from a failed poll ------------------------------------------


async def _fail_at(
    hass: HomeAssistant,
    hour: int,
    *,
    error: Exception,
    previous: timedelta,
    failures: int = 1,
) -> timedelta:
    """Fail a poll at a given hour and return the wait it arms next."""
    coordinator = build_coordinator(hass, StubApi(error=error))
    coordinator.update_interval = previous
    moment = datetime(2026, 8, 30, hour, 0, tzinfo=MELBOURNE)
    with patch("custom_components.yvw.coordinator.datetime") as clock:
        clock.now.return_value = moment
        for _ in range(failures):
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()
    return coordinator.update_interval


async def test_a_failed_poll_inside_the_window_tries_again_in_ten_minutes(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The interval is only recalculated on success, so a failure inherited it.

    Home Assistant re-arms the next poll from update_interval whatever the
    outcome. After a successful morning that interval is a full day, so one
    timed-out request left the integration not looking again until tomorrow —
    entities unavailable and a day of readings missed for a blip that lasted
    forty-five seconds.
    """
    armed = await _fail_at(
        hass, 7, error=YvwError("portal timed out"), previous=timedelta(hours=24)
    )

    assert armed == CATCHUP_RETRY


async def test_a_failure_outside_the_window_does_not_wait_a_whole_day(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Nothing is owed, but everything is unavailable until a poll succeeds."""
    armed = await _fail_at(hass, 12, error=YvwError("portal down"), previous=timedelta(hours=16))

    assert armed == FAILURE_RETRY


async def test_repeated_failures_back_off_but_stay_bounded(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """A portal that is down for hours should not be asked every half hour.

    Nor should the wait grow without limit: the cap is what brings the entities
    back promptly once it recovers.
    """
    armed = await _fail_at(
        hass,
        12,
        error=YvwError("portal down"),
        previous=timedelta(hours=16),
        failures=6,
    )

    assert armed == MAX_FAILURE_RETRY


async def test_backing_off_never_overshoots_the_next_window(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Waiting past the window would miss the morning it was backing off for."""
    armed = await _fail_at(
        hass,
        3,
        error=YvwError("portal down"),
        previous=timedelta(hours=24),
        failures=4,
    )

    # The window opens at four, an hour away — that, not a two hour backoff.
    assert armed == timedelta(hours=1)


async def test_a_recovered_poll_forgets_the_backoff(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Otherwise the next failure would start from the old ladder."""
    api = StubApi(error=YvwError("portal down"))
    coordinator = build_coordinator(hass, api)
    moment = datetime(2026, 8, 30, 12, 0, tzinfo=MELBOURNE)

    with patch("custom_components.yvw.coordinator.datetime") as clock:
        clock.now.return_value = moment
        for _ in range(4):
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()
        api.error = None
        await coordinator._async_update_data()

        api.error = YvwError("portal down again")
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()

    assert coordinator.update_interval == FAILURE_RETRY


async def test_a_session_that_expires_is_not_retried_on_a_backoff(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Nothing but the user can fix it, and reauth restarts polling."""
    coordinator = build_coordinator(hass, StubApi(error=YvwAuthError("expired")))
    coordinator.update_interval = timedelta(hours=24)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    assert coordinator.update_interval == timedelta(hours=24)


async def test_the_session_sensor_still_reports_when_a_poll_fails(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """It answers the question a failed poll raises, so it must not go with it.

    Everything else going unavailable is right — the readings are stale. This
    one says whether the session is gone or the portal merely hiccuped, which is
    exactly what you look for when readings stop.
    """
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

    with patch("custom_components.yvw.YvwApi", return_value=StubApi(hourly(24))):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        coordinator = entry.runtime_data
        coordinator.api = StubApi(error=YvwError("portal timed out"))
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.last_update_success is False
    session = hass.states.get("sensor.1_example_st_suburb_vic_3000_session")
    assert session is not None
    assert session.state == "active"


# --- Evidence from ordinary running -----------------------------------------


async def test_an_ordinary_keepalive_records_the_gap_it_survived(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Every ping that comes back proves the session lasted that long.

    Keeping that only while calibrating threw the evidence away: the options
    screen reported twenty-five minutes while the session was clearing an hour
    every hour, and answering "how long does a session last" meant running a
    measurement that costs a verification code.
    """
    probe = ProbeStore(hass)
    coordinator = build_coordinator(hass, StubApi(), {CONF_KEEPALIVE_MINUTES: 60})
    coordinator._probe = probe
    coordinator._last_contact = dt_util.utcnow() - timedelta(minutes=61)

    assert coordinator.calibrating is False
    await coordinator._async_keepalive(datetime.now(MELBOURNE))

    assert probe.get(coordinator.config_entry.entry_id).survived_minutes == 61


async def test_a_shorter_gap_does_not_lower_the_best(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The figure is the longest gap proven, not the most recent one."""
    probe = ProbeStore(hass)
    coordinator = build_coordinator(hass, StubApi(), {CONF_KEEPALIVE_MINUTES: 10})
    coordinator._probe = probe
    await probe.async_record_survived(coordinator.config_entry.entry_id, 120)
    coordinator._last_contact = dt_util.utcnow() - timedelta(minutes=11)

    await coordinator._async_keepalive(datetime.now(MELBOURNE))

    assert probe.get(coordinator.config_entry.entry_id).survived_minutes == 120


async def test_an_ordinary_keepalive_stays_out_of_the_logbook(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Recording it is not a reason to narrate it; only a run being watched is."""
    coordinator = build_coordinator(hass, StubApi(), {CONF_KEEPALIVE_MINUTES: 60})
    coordinator._probe = ProbeStore(hass)
    coordinator._last_contact = dt_util.utcnow() - timedelta(minutes=61)

    with patch("custom_components.yvw.coordinator.async_log_entry") as logbook:
        await coordinator._async_keepalive(datetime.now(MELBOURNE))

    logbook.assert_not_called()


# --- Only an undisturbed morning is a measurement ---------------------------


async def _learn_at(
    hass: HomeAssistant, hour: int, *, failed_first: bool = False, started: int = 0
) -> int | None:
    """Run a morning and return the start it learned, if it learned one."""
    from custom_components.yvw.schedule_store import ScheduleStore

    schedule = ScheduleStore(hass)
    await schedule.async_load()
    coordinator = build_coordinator(hass, StubApi(), {CONF_CATCHUP_FROM_HOUR: 3})
    coordinator._schedule = schedule
    coordinator._started_at = datetime(2026, 8, 30, started, 0, tzinfo=MELBOURNE)

    moment = datetime(2026, 8, 30, hour, 0, tzinfo=MELBOURNE)
    with patch("custom_components.yvw.coordinator.datetime") as clock:
        clock.now.return_value = moment
        if failed_first:
            coordinator.api = StubApi(error=YvwError("portal timed out"))
            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()
            coordinator.api = StubApi()
        await coordinator._async_learn_from(YvwData(yesterday_complete=True))

    learned = schedule.get(coordinator.config_entry.entry_id)
    return learned.minutes if learned else None


async def test_an_undisturbed_morning_is_learned_from(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The ordinary case still teaches it."""
    assert await _learn_at(hass, 3) == 2 * 60 + 30


async def test_a_morning_with_a_failed_poll_teaches_nothing(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """How long it took then measures the outage, not when readings appeared.

    This is what happened on the night the poll timed out: the readings were
    found hours later by a forced refresh, and that was recorded as the meter
    publishing late.
    """
    assert await _learn_at(hass, 5, failed_first=True) is None


async def test_a_find_outside_the_window_teaches_nothing(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """No scheduled attempt is running, so this is somebody forcing a refresh."""
    assert await _learn_at(hass, 14) is None


async def test_a_morning_home_assistant_slept_through_teaches_nothing(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """Nothing was looking when the window opened, so the elapsed time is idle."""
    assert await _learn_at(hass, 6, started=5) is None


async def test_the_start_time_is_an_entity_not_just_a_dialog(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """It moves on its own, so it is worth seeing without opening the options."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SID: "session",
            CONF_ACCOUNT_ID: ACCOUNT,
            CONF_METER_SERIAL: METER,
            CONF_ADDRESS: ADDRESS,
        },
        unique_id=ACCOUNT,
        options={CONF_CATCHUP_FROM_HOUR: 3},
    )
    entry.add_to_hass(hass)

    with patch("custom_components.yvw.YvwApi", return_value=StubApi(hourly(24))):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    looking = hass.states.get("sensor.1_example_st_suburb_vic_3000_looking_for_readings_from")
    assert looking is not None
    assert looking.state == "3.0"
    assert looking.attributes["clock"] == "03:00"
    assert looking.attributes["learned"] is False
    assert looking.attributes["configured_from_hour"] == 3
    assert looking.attributes["next_window"]


async def test_the_start_time_entity_shows_what_was_learned(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """The learned time is the one actually in use, so it is the one shown."""
    from custom_components.yvw.schedule_store import ScheduleStore

    schedule = ScheduleStore(hass)
    await schedule.async_load()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SID: "session",
            CONF_ACCOUNT_ID: ACCOUNT,
            CONF_METER_SERIAL: METER,
            CONF_ADDRESS: ADDRESS,
        },
        unique_id=ACCOUNT,
        options={CONF_CATCHUP_FROM_HOUR: 1},
    )
    entry.add_to_hass(hass)
    await schedule.async_record(entry.entry_id, 150, timedelta(hours=2), date(2026, 9, 13))

    with (
        patch("custom_components.yvw.YvwApi", return_value=StubApi(hourly(24))),
        patch("custom_components.yvw.ScheduleStore", return_value=schedule),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    looking = hass.states.get("sensor.1_example_st_suburb_vic_3000_looking_for_readings_from")
    assert looking.state == "3.0"
    assert looking.attributes["clock"] == "03:00"
    assert looking.attributes["learned"] is True
    assert looking.attributes["last_moved_on"] == "2026-09-13"


async def test_the_start_time_is_a_number_so_it_can_be_graphed(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """A clock face cannot be plotted, and the trend is the point of it.

    It moves half an hour at a time; which way, and how far it has got, is what
    the sensor is for. So the state is hours after midnight and the readable
    form is an attribute.
    """
    from custom_components.yvw.schedule_store import ScheduleStore

    schedule = ScheduleStore(hass)
    await schedule.async_load()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_SID: "session",
            CONF_ACCOUNT_ID: ACCOUNT,
            CONF_METER_SERIAL: METER,
            CONF_ADDRESS: ADDRESS,
        },
        unique_id=ACCOUNT,
        options={CONF_CATCHUP_FROM_HOUR: 2},
    )
    entry.add_to_hass(hass)
    # Half past midnight, the smallest step above the floor.
    await schedule.async_record(entry.entry_id, 60, timedelta(seconds=1), date(2026, 9, 23))

    with (
        patch("custom_components.yvw.YvwApi", return_value=StubApi(hourly(24))),
        patch("custom_components.yvw.ScheduleStore", return_value=schedule),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    looking = hass.states.get("sensor.1_example_st_suburb_vic_3000_looking_for_readings_from")
    assert float(looking.state) == 0.5
    assert looking.attributes["clock"] == "00:30"
    assert looking.attributes["unit_of_measurement"] == "h"
    assert looking.attributes["state_class"] == "measurement"
