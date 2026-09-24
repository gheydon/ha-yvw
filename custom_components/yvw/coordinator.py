"""Poll the YVW portal and keep its session alive."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo
from typing import Any

from homeassistant.components.logbook import async_log_entry
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import UsageReading, YvwApi
from .const import (
    CATCHUP_RETRY,
    CONF_ADAPTIVE_START,
    CONF_CATCHUP_FROM_HOUR,
    CONF_CATCHUP_HOURS,
    CONF_KEEPALIVE_MINUTES,
    CONF_PROBE_ENABLED,
    CONF_PROBE_STEP_MINUTES,
    DEFAULT_ADAPTIVE_START,
    DEFAULT_CATCHUP_FROM_HOUR,
    DEFAULT_CATCHUP_HOURS,
    DEFAULT_KEEPALIVE_MINUTES,
    DEFAULT_PROBE_STEP_MINUTES,
    DOMAIN,
    EVENT_AUTH_FAILED,
    EVENT_KEEPALIVE,
    EVENT_NEW_READINGS,
    FAILURE_RETRY,
    HOURS_IN_A_DAY,
    KEEPALIVE_JITTER,
    KEEPALIVE_RETRY,
    MAX_FAILURE_RETRY,
    MAX_HISTORY_DAYS,
    MAX_KEEPALIVE_MINUTES,
    MAX_PROBE_MINUTES,
    PROBE_SAFETY_MARGIN,
    UPDATE_INTERVAL,
    WATCHDOG_GRACE,
    WATCHDOG_INTERVAL,
)
from .exceptions import YvwAuthError, YvwError
from .probe import ProbeState, ProbeStore
from .schedule_store import LearnedStart, ScheduleStore, clock
from .statistics import async_insert_statistics, statistic_id_for

_LOGGER = logging.getLogger(__name__)

type YvwConfigEntry = ConfigEntry[YvwCoordinator]

HOURS_IN_A_FULL_DAY = 24


@dataclass(slots=True)
class YvwData:
    """The most recent readings, for the sensor entities."""

    latest: UsageReading | None = None
    yesterday_complete: bool = False
    last_full_day: date | None = None
    last_full_day_litres: float | None = None


class YvwCoordinator(DataUpdateCoordinator[YvwData]):
    """Fetch hourly readings, record them as statistics, and hold the session open."""

    config_entry: YvwConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: YvwConfigEntry,
        api: YvwApi,
        account_id: str,
        meter_serial: str,
        address: str,
        portal_tz: tzinfo,
        probe: ProbeStore,
        signed_in_at: datetime | None = None,
        schedule: ScheduleStore | None = None,
    ) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api
        self.account_id = account_id
        self.meter_serial = meter_serial
        self.address = address
        self._portal_tz = portal_tz
        self._probe = probe
        self._signed_in_at = signed_in_at
        self._schedule = schedule
        # The day the readings were last found, so a morning is only learned
        # from once however many times the coordinator runs.
        self._found_on: date | None = None
        # The day yesterday was last confirmed complete, and how many polls
        # have failed in a row. A failure cannot ask the data whether readings
        # are still owed: stale data claims yesterday is complete all day.
        self._complete_for: date | None = None
        self._failures = 0
        # A morning is only a measurement of when readings appear if nothing
        # interrupted the attempts. These say when this started running, and
        # the last day something did.
        self._started_at = datetime.now(self._portal_tz)
        self._disrupted_on: date | None = None
        self._cancel_keepalive: Callable[[], None] | None = None
        self._session_started: datetime | None = None
        self._last_contact: datetime | None = None
        self._last_keepalive: datetime | None = None
        self._session_dead = False
        self._expired_at: datetime | None = None
        # When the session last changed between working and not. A session that
        # is still good has been good since it was signed in, which is what the
        # user wants to see — not the time since Home Assistant last started.
        self._status_since: datetime = signed_in_at or dt_util.utcnow()
        self._cancel_watchdog: Callable[[], None] | None = None
        self._stall_reported = False

        @callback
        def _dummy_listener() -> None:
            """Keep the coordinator polling even when no entity is subscribed.

            The statistics import is the point of this integration, and it only
            runs from _async_update_data. Without a listener the coordinator
            would never be scheduled.
            """

        self.async_add_listener(_dummy_listener)

    # --- Keep-alive ---------------------------------------------------------

    @property
    def session_age(self) -> timedelta | None:
        """Return how long the current session has been alive."""
        return self._session_age()

    @property
    def signed_in_at(self) -> datetime | None:
        """Return when the user last signed in, if it was recorded."""
        return self._signed_in_at

    @property
    def session_active(self) -> bool:
        """Return whether the portal session is usable."""
        return not self._session_dead

    @property
    def status_since(self) -> datetime:
        """Return when the session last changed between working and not."""
        return self._status_since

    @callback
    def _async_set_session_dead(self, dead: bool) -> None:
        """Record a change in whether the session works, and say so.

        The sensor reads this from the coordinator, so nothing updates unless
        listeners are told — and a history of how long the session was up or
        down is only as good as the moment it was written.
        """
        if dead == self._session_dead:
            return
        self._session_dead = dead
        self._status_since = dt_util.utcnow()
        self._expired_at = dt_util.utcnow() if dead else None
        self.async_update_listeners()

    @property
    def expired_at(self) -> datetime | None:
        """Return when the session was found to have lapsed, if it has."""
        return self._expired_at

    @property
    def last_keepalive(self) -> datetime | None:
        """Return when the portal was last pinged to hold the session open."""
        return self._last_keepalive

    @property
    def last_contact(self) -> datetime | None:
        """Return when the portal was last successfully contacted."""
        return self._last_contact

    @property
    def keepalive_running(self) -> bool:
        """Return whether a keep-alive ping is currently scheduled."""
        return self._cancel_keepalive is not None

    @property
    def probe_state(self) -> ProbeState:
        """Return what has been measured about the session timeout."""
        return self._probe.get(self.config_entry.entry_id)

    @property
    def calibrating(self) -> bool:
        """Return whether the interval is being stretched to find the timeout."""
        return (
            bool(self.config_entry.options.get(CONF_PROBE_ENABLED))
            and not self.probe_state.concluded
        )

    @property
    def learning_start(self) -> bool:
        """Return whether the start steers itself."""
        return bool(self.config_entry.options.get(CONF_ADAPTIVE_START, DEFAULT_ADAPTIVE_START))

    @property
    def learned_start(self) -> LearnedStart | None:
        """Return the start learned from recent mornings, if any."""
        if self._schedule is None or not self.learning_start:
            return None
        return self._schedule.get(self.config_entry.entry_id)

    @property
    def catchup_from_minutes(self) -> int:
        """Return how far into the day the look for readings begins.

        The configured hour is where it starts from; once it has watched a few
        mornings it uses what it learned instead.
        """
        learned = self.learned_start
        if learned is not None:
            return learned.minutes
        return self.catchup_from_hour * 60

    @property
    def looking_from(self) -> str:
        """Return the time the daily look begins, learned or configured."""
        return clock(self.catchup_from_minutes)

    @property
    def next_window(self) -> datetime:
        """Return when the look for readings next begins."""
        now = datetime.now(self._portal_tz)
        return now + self._next_window(now)

    @property
    def catchup_from_hour(self) -> int:
        """Return the configured hour the daily look begins."""
        hour = self.config_entry.options.get(CONF_CATCHUP_FROM_HOUR, DEFAULT_CATCHUP_FROM_HOUR)
        return max(0, min(int(hour), 23))

    @property
    def catchup_hours(self) -> int:
        """Return how long to keep looking each morning."""
        hours = self.config_entry.options.get(CONF_CATCHUP_HOURS, DEFAULT_CATCHUP_HOURS)
        # Past midnight the window would run into the next day's, so it stops
        # at the end of the day it started.
        return max(1, min(int(hours), 24 - (self.catchup_from_minutes // 60)))

    @property
    def configured_interval_minutes(self) -> int:
        """Return the interval the user asked for."""
        return self.config_entry.options.get(CONF_KEEPALIVE_MINUTES, DEFAULT_KEEPALIVE_MINUTES)

    @property
    def keepalive_interval(self) -> timedelta:
        """Return how long to leave the session alone before touching it.

        While calibrating this climbs a step past the longest gap already
        survived, so each ping tests a slightly longer idle period than the last
        until one finds the session gone.
        """
        minutes = self.configured_interval_minutes
        if self.calibrating:
            step = self.config_entry.options.get(
                CONF_PROBE_STEP_MINUTES, DEFAULT_PROBE_STEP_MINUTES
            )
            minutes = max(minutes, self.probe_state.survived_minutes + step)
            # Measuring is allowed further than anyone should configure, or it
            # would only ever report the configurable ceiling back.
            return timedelta(minutes=min(minutes, MAX_PROBE_MINUTES))
        return timedelta(minutes=min(minutes, MAX_KEEPALIVE_MINUTES))

    @callback
    def async_start_keepalive(self) -> None:
        """Begin pinging the portal so the session does not idle out.

        Losing the session costs the user an SMS round trip, so this runs far
        more often than the data poll. The interval is an option because the
        portal's real idle timeout is not published and is worth measuring.
        """
        if self._cancel_keepalive is not None:
            return
        if self._session_started is None:
            self._session_started = dt_util.utcnow()
        self._async_schedule_keepalive()

    @callback
    def _async_schedule_keepalive(self, delay: timedelta | None = None) -> None:
        """Arm the next ping.

        Without a delay this is a fresh interval, jittered a little shorter:
        exact clockwork is the one thing a person browsing their own usage never
        produces. A given delay is used as it stands, which is how a wake-up
        that turns out to be early asks for the remaining time rather than
        starting the wait over.
        """
        if delay is not None:
            seconds = max(1.0, delay.total_seconds())
        else:
            seconds = self.keepalive_interval.total_seconds()
            if not self.calibrating:
                # Jitter only shortens the gap; lengthening one could outlast
                # the timeout being measured.
                seconds = random.uniform(seconds * (1 - KEEPALIVE_JITTER), seconds)
        self._cancel_keepalive = async_call_later(self.hass, seconds, self._async_keepalive)

    @callback
    def async_stop_keepalive(self) -> None:
        """Stop pinging the portal."""
        if self._cancel_keepalive is not None:
            self._cancel_keepalive()
            self._cancel_keepalive = None

    @callback
    def async_start_watchdog(self) -> None:
        """Watch that the keep-alive is actually still running.

        Everything else here reports a session that has gone. Nothing reported a
        keep-alive that had simply stopped, and a stopped loop looks exactly
        like a healthy one until the readings quietly stop. This makes no
        requests: it only compares the clock against when the portal was last
        touched.
        """
        if self._cancel_watchdog is None:
            self._cancel_watchdog = async_track_time_interval(
                self.hass, self._async_watchdog, WATCHDOG_INTERVAL
            )

    @callback
    def async_stop_watchdog(self) -> None:
        """Stop watching."""
        if self._cancel_watchdog is not None:
            self._cancel_watchdog()
            self._cancel_watchdog = None

    async def _async_watchdog(self, _now: datetime) -> None:
        """Notice a keep-alive that has stopped, say so, and restart it.

        Also nudges the entities, so how long the session has been up or down
        keeps counting rather than freezing until the next poll.
        """
        self.async_update_listeners()
        if self._session_dead or self._last_contact is None:
            # Nothing to guard: the user has been asked to sign in again.
            return

        overdue_after = self.keepalive_interval * WATCHDOG_GRACE
        idle = dt_util.utcnow() - self._last_contact
        if idle <= overdue_after:
            self._stall_reported = False
            return

        if self._stall_reported:
            return
        self._stall_reported = True

        idle_minutes = round(idle.total_seconds() / 60)
        _LOGGER.error(
            "The keep-alive has not run for %s minutes, well past its %s interval. "
            "Restarting it; the session may already have lapsed",
            idle_minutes,
            self.keepalive_interval,
        )
        self._async_log_activity(
            f"keep-alive had stopped for {idle_minutes} minutes; restarting it"
        )
        self._async_fire_keepalive("stalled", idle_minutes)

        # Re-arm and try immediately: the session may still be saveable.
        self.async_stop_keepalive()
        self._async_schedule_keepalive(timedelta(seconds=1))

    async def _async_keepalive(self, _now: datetime) -> None:
        """Touch the portal so the session does not go idle.

        Whatever happens in here, the next ping is armed on the way out. A
        keep-alive that stops scheduling itself fails silently and looks exactly
        like a session the portal dropped, so the loop is kept alive even when
        something inside it misbehaves.
        """
        self._cancel_keepalive = None
        reschedule: timedelta | None = None

        try:
            # Any successful request resets the portal's idle clock, so a poll
            # that just ran has already done this ping's job. Skipping keeps the
            # request count to the minimum that holds the session open — but
            # only the time still owed is waited out. Starting a fresh interval
            # here would let the gap grow towards twice what was configured,
            # which is the opposite of the safety margin the setting provides.
            interval = self.keepalive_interval
            idle = dt_util.utcnow() - (self._last_contact or dt_util.utcnow())
            if self._last_contact is not None and idle < interval:
                reschedule = interval - idle
                return

            idle_minutes = round(idle.total_seconds() / 60)

            try:
                await self.api.async_ping(self.account_id)
                self._last_contact = self._last_keepalive = dt_util.utcnow()
                self._stall_reported = False
            except YvwAuthError:
                # Nothing will revive the session without the user, so stop
                # pinging a dead one and ask them to sign in again.
                _LOGGER.warning(
                    "The Yarra Valley Water session expired after %s of keep-alive "
                    "pings every %s; re-authentication is needed",
                    self._session_age(),
                    self.keepalive_interval,
                )
                if self.calibrating:
                    await self._async_conclude_calibration(idle_minutes)
                self._async_set_session_dead(True)
                self._async_fire_keepalive("expired", idle_minutes)
                self._async_fire_auth_failed("keepalive")
                self.config_entry.async_start_reauth(self.hass)
                return
            except YvwError as err:
                # A transient failure is not worth escalating, but it does mean
                # the session went untouched, so try again shortly rather than
                # after another whole interval.
                _LOGGER.debug("Keep-alive ping failed after %s min idle: %s", idle_minutes, err)
                self._async_fire_keepalive("failed", idle_minutes, error=str(err))
                reschedule = KEEPALIVE_RETRY
                return

            # Every ping that comes back proves the session survived that gap,
            # whether or not anyone asked for a measurement. Keeping it only
            # while calibrating threw the evidence away: the options screen
            # reported twenty-five minutes while the session was clearing an
            # hour every hour, and answering how long a session lasts meant
            # running a measurement that ends by costing a verification code.
            proved = await self._probe.async_record_survived(
                self.config_entry.entry_id, idle_minutes
            )
            if proved and not self.calibrating:
                _LOGGER.debug("Session has now survived %s minutes idle untouched", idle_minutes)

            if self.calibrating:
                next_minutes = round(self.keepalive_interval.total_seconds() / 60)
                _LOGGER.info(
                    "Session survived %s minutes idle; next test %s minutes",
                    idle_minutes,
                    next_minutes,
                )
                # While measuring, each ping is a result worth seeing without
                # reading a log file, so it goes in the logbook.
                self._async_log_activity(
                    f"session survived {idle_minutes} minutes idle, "
                    f"testing {next_minutes} minutes next"
                )
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Keep-alive ok, session age %s, portal session clock: %s",
                    self._session_age(),
                    await self.api.async_probe_session_time(),
                )
            self._async_fire_keepalive("ok", idle_minutes)
            # The sensor reads this from the coordinator, so it has to be told.
            self.async_update_listeners()
        except Exception:
            # Anything unforeseen would otherwise end the loop without a word.
            # A request timing out lands here, and left the session untouched,
            # so it is retried soon rather than a whole interval later.
            _LOGGER.exception("Keep-alive failed unexpectedly; retrying shortly")
            reschedule = KEEPALIVE_RETRY
        finally:
            # A dead session is the one case where stopping is correct: the
            # reauth flow restarts this once the user has signed in.
            if self._session_dead:
                self._cancel_keepalive = None
            else:
                self._async_schedule_keepalive(reschedule)

    @callback
    def _async_log_activity(self, message: str) -> None:
        """Write a line to the logbook, so a run can be followed as it happens."""
        async_log_entry(self.hass, self.address, message, DOMAIN)

    async def _async_conclude_calibration(self, idle_minutes: int) -> None:
        """Record the gap that killed the session and settle on a safe interval."""
        age = self._session_age()
        state = await self._probe.async_record_failure(
            self.config_entry.entry_id,
            idle_minutes,
            round(age.total_seconds() / 60) if age else 0,
        )
        safe = max(1, int(state.survived_minutes * PROBE_SAFETY_MARGIN)) or 1
        _LOGGER.warning(
            "Session timeout found: %s. Keep-alive set to %s minutes and "
            "calibration switched off; sign in again to resume",
            state.summary,
            safe,
        )
        self._async_log_activity(
            f"session lapsed after {idle_minutes} minutes idle: {state.summary}. "
            f"Keep-alive set to {safe} minutes; sign in again to resume"
        )
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            options={
                **self.config_entry.options,
                CONF_PROBE_ENABLED: False,
                CONF_KEEPALIVE_MINUTES: safe,
            },
        )

    def _session_age(self) -> timedelta | None:
        """Return how long the current session has been alive."""
        if self._session_started is None:
            return None
        return dt_util.utcnow() - self._session_started

    # --- Polling ------------------------------------------------------------

    async def _async_update_data(self) -> YvwData:
        """Fetch readings, and choose when to look again whatever the outcome.

        Home Assistant re-arms the next poll from the interval left on the
        coordinator, so the failure path has to set one too. Without this it
        inherited whatever the last success chose — after a successful morning,
        a full day — and one timed-out request meant no further attempt until
        tomorrow, with every entity unavailable until then.
        """
        try:
            data = await self._async_poll()
        except ConfigEntryAuthFailed:
            # Only the user can fix this, and signing in again restarts polling.
            raise
        except Exception:
            self._failures += 1
            now = datetime.now(self._portal_tz)
            if self._in_window(now):
                self._disrupted_on = now.date()
            self.update_interval = self._after_failure()
            _LOGGER.debug(
                "Poll failed (%s in a row); looking again in %s",
                self._failures,
                self.update_interval,
            )
            raise
        self._failures = 0
        return data

    async def _async_poll(self) -> YvwData:
        """Fetch readings, append them to statistics, and summarise the latest."""
        today = datetime.now(self._portal_tz).date()
        start_date = today - timedelta(days=MAX_HISTORY_DAYS)

        try:
            readings = await self.api.async_get_hourly_usage(
                self.account_id, self.meter_serial, start_date, today
            )
        except YvwAuthError as err:
            _LOGGER.warning(
                "The Yarra Valley Water session expired after %s; re-authentication is needed",
                self._session_age(),
            )
            self._async_set_session_dead(True)
            self.async_stop_keepalive()
            self._async_fire_auth_failed("poll")
            raise ConfigEntryAuthFailed(str(err)) from err
        except YvwError as err:
            raise UpdateFailed(str(err)) from err

        # A successful poll proves the session is healthy again, and counts as
        # contact for the purposes of the idle clock.
        self._async_set_session_dead(False)
        self._last_contact = dt_util.utcnow()
        self.async_start_keepalive()

        added = await async_insert_statistics(self.hass, self.meter_serial, self.address, readings)
        if added:
            _LOGGER.debug("Recorded %s new hourly readings for %s", len(added), self.meter_serial)
            self._async_fire_new_readings(added)

        data = self._summarise(readings)
        if data.yesterday_complete:
            # What a failure needs to know: whether today's readings are still
            # owed. Stale data cannot answer that, because it says yesterday was
            # complete right through tomorrow.
            self._complete_for = datetime.now(self._portal_tz).date()
        await self._async_learn_from(data)
        self.update_interval = self._next_poll(data)
        _LOGGER.debug(
            "Yesterday %s; next poll in %s",
            "complete" if data.yesterday_complete else "still incomplete",
            self.update_interval,
        )
        return data

    async def _async_learn_from(self, data: YvwData) -> None:
        """Move tomorrow's start based on how long today's readings took.

        Only the moment they are first found says anything: later polls on the
        same day would report a shorter and shorter wait and drag the start
        earlier for no reason.
        """
        if self._schedule is None or not self.learning_start:
            return
        now = datetime.now(self._portal_tz)
        if not data.yesterday_complete or self._found_on == now.date():
            return
        if not self._in_window(now) or self._morning_was_interrupted(now):
            return
        self._found_on = now.date()

        took = now - self._morning(now)

        learned = await self._schedule.async_record(
            self.config_entry.entry_id,
            self.catchup_from_minutes,
            took,
            now.date(),
        )
        _LOGGER.debug(
            "Readings found %s after looking began; starting at %s tomorrow",
            took,
            learned.clock,
        )

    def _morning_was_interrupted(self, now: datetime) -> bool:
        """Return whether anything stopped today's attempts running as intended.

        How long the readings took to find only measures when they appeared if
        the attempts actually ran on cadence from the moment the window opened.
        A poll that failed, or a Home Assistant that was not running, breaks
        that: the elapsed time then measures the interruption instead. Learning
        from it moves the start for the wrong reason — a night the portal timed
        out taught this that the meter had begun publishing hours later.
        """
        return self._disrupted_on == now.date() or self._started_at > self._morning(now)

    def _next_poll(self, data: YvwData) -> timedelta:
        """Return how long to wait before looking for readings again.

        A day's readings appear the following morning, at no time the portal
        publishes. So rather than polling blindly around the clock, this looks
        from early morning every ten minutes until yesterday is complete, then
        waits until tomorrow. A day that never completes is given up on by
        mid-morning: retrying until midnight would only ask repeatedly for
        readings that are not coming.
        """
        now = datetime.now(self._portal_tz)

        if data.yesterday_complete:
            return self._until_tomorrow_morning(now)
        if self._in_window(now):
            return CATCHUP_RETRY
        return self._next_window(now)

    def _after_failure(self) -> timedelta:
        """Return how long to wait after a poll that did not get through.

        Readings still owed today are worth the window's own cadence: that is
        what the window is for, and it already stops itself by mid-morning. With
        nothing owed the only cost of waiting is that the entities stay
        unavailable, so this backs off instead of retrying hard — but never past
        the next window, which would miss the morning it was waiting for.
        """
        now = datetime.now(self._portal_tz)
        if self._complete_for != now.date() and self._in_window(now):
            return CATCHUP_RETRY
        backoff = min(FAILURE_RETRY * 2 ** (self._failures - 1), MAX_FAILURE_RETRY)
        return min(backoff, self._next_window(now))

    def _in_window(self, now: datetime) -> bool:
        """Return whether now is inside today's catch-up window."""
        opens = self._morning(now)
        return opens <= now < opens + timedelta(hours=self.catchup_hours)

    def _next_window(self, now: datetime) -> timedelta:
        """Return the wait until the catch-up window next opens."""
        if now < self._morning(now):
            return self._morning(now) - now
        return self._until_tomorrow_morning(now)

    def _morning(self, now: datetime) -> datetime:
        """Return the start of today's catch-up window."""
        minutes = self.catchup_from_minutes
        return now.replace(hour=minutes // 60, minute=minutes % 60, second=0, microsecond=0)

    def _until_tomorrow_morning(self, now: datetime) -> timedelta:
        """Return the wait until the next catch-up window opens."""
        return self._morning(now + timedelta(days=1)) - now

    @callback
    def _async_fire(self, event_type: str, data: dict[str, Any]) -> None:
        """Fire an event, waiting for startup to finish if it has not.

        Config entries are set up before automations are listening, so an event
        fired during startup reaches nobody. A session found expired on the
        first poll after a restart is exactly that case, and is the one most
        worth being told about.
        """
        if self.hass.state is CoreState.running:
            self.hass.bus.async_fire(event_type, data)
            return

        @callback
        def _fire_when_started(_hass: HomeAssistant) -> None:
            self.hass.bus.async_fire(event_type, data)

        async_at_started(self.hass, _fire_when_started)

    @callback
    def _async_fire_keepalive(
        self, outcome: str, idle_minutes: int, error: str | None = None
    ) -> None:
        """Announce the result of a keep-alive attempt.

        Fired whether the ping worked, failed transiently, or found the session
        gone, so an automation can report each one without reading a log. Not
        fired when a wake-up is skipped because a poll already touched the
        portal — nothing was asked of it, so there is no outcome to report.
        """
        self._async_fire(
            EVENT_KEEPALIVE,
            {
                "entry_id": self.config_entry.entry_id,
                "account_id": self.account_id,
                "address": self.address,
                "outcome": outcome,
                "idle_minutes": idle_minutes,
                "next_minutes": round(self.keepalive_interval.total_seconds() / 60),
                "session_age": str(self._session_age()),
                "calibrating": self.calibrating,
                "measurement": self.probe_state.summary,
                "error": error,
            },
        )

    @callback
    def _async_fire_auth_failed(self, detected_by: str) -> None:
        """Announce that the session has lapsed and a person is needed.

        Recovering means signing in again with an SMS code, so this is worth
        acting on rather than waiting to notice missing readings.
        """
        self._async_fire(
            EVENT_AUTH_FAILED,
            {
                "entry_id": self.config_entry.entry_id,
                "account_id": self.account_id,
                "meter_serial": self.meter_serial,
                "address": self.address,
                "detected_by": detected_by,
                "session_age": str(self._session_age()),
                "last_contact": (self._last_contact.isoformat() if self._last_contact else None),
            },
        )

    @callback
    def _async_fire_new_readings(self, added: list[UsageReading]) -> None:
        """Announce newly recorded hours so automations can act on them."""
        self._async_fire(
            EVENT_NEW_READINGS,
            {
                "entry_id": self.config_entry.entry_id,
                "account_id": self.account_id,
                "meter_serial": self.meter_serial,
                "address": self.address,
                "statistic_id": statistic_id_for(self.meter_serial),
                "count": len(added),
                "litres": round(sum(reading.litres for reading in added), 3),
                "first_hour": added[0].start.isoformat(),
                "last_hour": added[-1].start.isoformat(),
            },
        )

    def _summarise(self, readings: list[UsageReading]) -> YvwData:
        if not readings:
            return YvwData()

        by_day: dict[date, list[UsageReading]] = {}
        for reading in readings:
            by_day.setdefault(reading.start.astimezone(self._portal_tz).date(), []).append(reading)

        # Only a day the meter reported in full is a meaningful daily total;
        # a partial day would read as a sudden drop in consumption.
        complete_days = [day for day, hours in by_day.items() if len(hours) == HOURS_IN_A_FULL_DAY]
        last_full_day = max(complete_days) if complete_days else None

        yesterday = (datetime.now(self._portal_tz) - timedelta(days=1)).date()
        return YvwData(
            latest=readings[-1],
            yesterday_complete=len(by_day.get(yesterday, ())) >= HOURS_IN_A_DAY,
            last_full_day=last_full_day,
            last_full_day_litres=(
                sum(hour.litres for hour in by_day[last_full_day])
                if last_full_day is not None
                else None
            ),
        )
