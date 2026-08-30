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
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.yvw.api import UsageReading
from custom_components.yvw.const import (
    CATCHUP_RETRY,
    CONF_ACCOUNT_ID,
    CONF_ADDRESS,
    CONF_KEEPALIVE_MINUTES,
    CONF_METER_SERIAL,
    CONF_PROBE_ENABLED,
    CONF_PROBE_STEP_MINUTES,
    CONF_SID,
    DOMAIN,
    EVENT_AUTH_FAILED,
    EVENT_KEEPALIVE,
    EVENT_NEW_READINGS,
    KEEPALIVE_RETRY,
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
        hass, StubApi(), {CONF_KEEPALIVE_MINUTES: 60, CONF_PROBE_ENABLED: True,
                          CONF_PROBE_STEP_MINUTES: 15}
    )
    coordinator.probe_state.survived_minutes = MAX_KEEPALIVE_MINUTES

    assert coordinator.keepalive_interval == timedelta(
        minutes=MAX_KEEPALIVE_MINUTES + 15
    )


def test_a_configured_interval_is_still_capped(hass: HomeAssistant) -> None:
    """Only measuring gets the longer leash."""
    coordinator = build_coordinator(
        hass, StubApi(), {CONF_KEEPALIVE_MINUTES: MAX_KEEPALIVE_MINUTES + 500}
    )

    assert coordinator.keepalive_interval == timedelta(minutes=MAX_KEEPALIVE_MINUTES)


# --- Aiming the poll at when readings appear --------------------------------


def _at(hass: HomeAssistant, hour: int, complete: bool) -> timedelta:
    """Return the wait chosen at a given hour, for a given state of yesterday."""
    coordinator = build_coordinator(hass, StubApi())
    moment = datetime(2026, 8, 30, hour, 0, tzinfo=MELBOURNE)
    with patch("custom_components.yvw.coordinator.datetime") as clock:
        clock.now.return_value = moment
        return coordinator._next_poll(YvwData(yesterday_complete=complete))


def test_before_the_morning_window_it_waits_for_it(hass: HomeAssistant) -> None:
    """Readings for a day are not there at three in the morning."""
    assert _at(hass, 3, complete=False) == timedelta(hours=3)


def test_during_the_window_it_tries_every_ten_minutes(hass: HomeAssistant) -> None:
    """The portal publishes at no time it announces, so keep looking."""
    assert _at(hass, 7, complete=False) == CATCHUP_RETRY


def test_once_yesterday_is_complete_it_stops_until_tomorrow(
    hass: HomeAssistant,
) -> None:
    """Having got what it came for, asking again is wasted traffic."""
    assert _at(hass, 7, complete=True) == timedelta(hours=23)


def test_a_day_that_never_completes_is_given_up_on(hass: HomeAssistant) -> None:
    """A meter that reported only part of a day is not going to finish it.

    Retrying every ten minutes until midnight would ask over eighty times for
    readings that are never coming.
    """
    assert _at(hass, 12, complete=False) == timedelta(hours=18)


async def test_a_poll_sets_the_next_one_from_what_it_found(
    recorder_mock: Recorder, hass: HomeAssistant
) -> None:
    """The schedule follows the data rather than a fixed clock."""
    coordinator = build_coordinator(hass, StubApi())

    await coordinator._async_update_data()

    # The stub returns a partial day, so it should be in catch-up or waiting,
    # never the old blind twelve hours.
    assert coordinator.update_interval != UPDATE_INTERVAL
