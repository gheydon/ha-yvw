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
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import UsageReading, YvwApi
from .const import (
    CONF_KEEPALIVE_MINUTES,
    CONF_PROBE_ENABLED,
    CONF_PROBE_STEP_MINUTES,
    DEFAULT_KEEPALIVE_MINUTES,
    DEFAULT_PROBE_STEP_MINUTES,
    DOMAIN,
    EVENT_AUTH_FAILED,
    EVENT_KEEPALIVE,
    EVENT_NEW_READINGS,
    KEEPALIVE_JITTER,
    KEEPALIVE_RETRY,
    MAX_HISTORY_DAYS,
    MAX_KEEPALIVE_MINUTES,
    PROBE_SAFETY_MARGIN,
    UPDATE_INTERVAL,
)
from .exceptions import YvwAuthError, YvwError
from .probe import ProbeState, ProbeStore
from .statistics import async_insert_statistics, statistic_id_for

_LOGGER = logging.getLogger(__name__)

type YvwConfigEntry = ConfigEntry[YvwCoordinator]

HOURS_IN_A_FULL_DAY = 24


@dataclass(slots=True)
class YvwData:
    """The most recent readings, for the sensor entities."""

    latest: UsageReading | None = None
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
        self._cancel_keepalive: Callable[[], None] | None = None
        self._session_started: datetime | None = None
        self._last_contact: datetime | None = None
        self._last_keepalive: datetime | None = None
        self._session_dead = False

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
        return bool(
            self.config_entry.options.get(CONF_PROBE_ENABLED)
        ) and not self.probe_state.concluded

    @property
    def configured_interval_minutes(self) -> int:
        """Return the interval the user asked for."""
        return self.config_entry.options.get(
            CONF_KEEPALIVE_MINUTES, DEFAULT_KEEPALIVE_MINUTES
        )

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
                self._session_dead = True
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

            if self.calibrating:
                await self._probe.async_record_survived(
                    self.config_entry.entry_id, idle_minutes
                )
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
            self._session_dead = True
            self.async_stop_keepalive()
            self._async_fire_auth_failed("poll")
            raise ConfigEntryAuthFailed(str(err)) from err
        except YvwError as err:
            raise UpdateFailed(str(err)) from err

        # A successful poll proves the session is healthy again, and counts as
        # contact for the purposes of the idle clock.
        self._session_dead = False
        self._last_contact = dt_util.utcnow()
        self.async_start_keepalive()

        added = await async_insert_statistics(
            self.hass, self.meter_serial, self.address, readings
        )
        if added:
            _LOGGER.debug("Recorded %s new hourly readings for %s", len(added), self.meter_serial)
            self._async_fire_new_readings(added)

        return self._summarise(readings)

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
                "last_contact": (
                    self._last_contact.isoformat() if self._last_contact else None
                ),
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
        complete_days = [
            day for day, hours in by_day.items() if len(hours) == HOURS_IN_A_FULL_DAY
        ]
        last_full_day = max(complete_days) if complete_days else None

        return YvwData(
            latest=readings[-1],
            last_full_day=last_full_day,
            last_full_day_litres=(
                sum(hour.litres for hour in by_day[last_full_day])
                if last_full_day is not None
                else None
            ),
        )
