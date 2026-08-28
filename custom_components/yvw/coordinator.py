"""Poll the YVW portal and keep its session alive."""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import UsageReading, YvwApi
from .const import (
    CONF_KEEPALIVE_MINUTES,
    DEFAULT_KEEPALIVE_MINUTES,
    DOMAIN,
    KEEPALIVE_JITTER,
    MAX_HISTORY_DAYS,
    UPDATE_INTERVAL,
)
from .exceptions import YvwAuthError, YvwError
from .statistics import async_insert_statistics

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
        self._cancel_keepalive: Callable[[], None] | None = None
        self._session_started: datetime | None = None
        self._last_contact: datetime | None = None

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
    def last_contact(self) -> datetime | None:
        """Return when the portal was last successfully contacted."""
        return self._last_contact

    @property
    def keepalive_running(self) -> bool:
        """Return whether a keep-alive ping is currently scheduled."""
        return self._cancel_keepalive is not None

    @property
    def keepalive_interval(self) -> timedelta:
        """Return how often to ping the portal."""
        minutes = self.config_entry.options.get(
            CONF_KEEPALIVE_MINUTES, DEFAULT_KEEPALIVE_MINUTES
        )
        return timedelta(minutes=minutes)

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
    def _async_schedule_keepalive(self) -> None:
        """Arm the next ping, at a slightly irregular interval.

        Exact clockwork is the one thing a person browsing their own usage
        never produces, so the delay is jittered a little either side of the
        configured interval.
        """
        seconds = self.keepalive_interval.total_seconds()
        delay = random.uniform(seconds * (1 - KEEPALIVE_JITTER), seconds)
        self._cancel_keepalive = async_call_later(self.hass, delay, self._async_keepalive)

    @callback
    def async_stop_keepalive(self) -> None:
        """Stop pinging the portal."""
        if self._cancel_keepalive is not None:
            self._cancel_keepalive()
            self._cancel_keepalive = None

    async def _async_keepalive(self, _now: datetime) -> None:
        self._cancel_keepalive = None

        # Any successful request resets the portal's idle clock, so a poll that
        # just ran has already done this ping's job. Skipping here keeps the
        # request count to the minimum that holds the session open.
        interval = self.keepalive_interval
        if self._last_contact is not None and dt_util.utcnow() - self._last_contact < interval:
            self._async_schedule_keepalive()
            return

        try:
            await self.api.async_ping(self.account_id)
            self._last_contact = dt_util.utcnow()
        except YvwAuthError:
            # Nothing will revive the session without the user, so stop pinging
            # a dead one and ask them to sign in again.
            _LOGGER.warning(
                "The Yarra Valley Water session expired after %s of keep-alive pings "
                "every %s; re-authentication is needed",
                self._session_age(),
                self.keepalive_interval,
            )
            self.async_stop_keepalive()
            self.config_entry.async_start_reauth(self.hass)
            return
        except YvwError as err:
            # A transient failure is not worth escalating; the next ping retries.
            _LOGGER.debug("Keep-alive ping failed: %s", err)
        else:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "Keep-alive ok, session age %s, portal session clock: %s",
                    self._session_age(),
                    await self.api.async_probe_session_time(),
                )

        self._async_schedule_keepalive()

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
            self.async_stop_keepalive()
            raise ConfigEntryAuthFailed(str(err)) from err
        except YvwError as err:
            raise UpdateFailed(str(err)) from err

        # A successful poll proves the session is healthy again, and counts as
        # contact for the purposes of the idle clock.
        self._last_contact = dt_util.utcnow()
        self.async_start_keepalive()

        added = await async_insert_statistics(
            self.hass, self.meter_serial, self.address, readings
        )
        if added:
            _LOGGER.debug("Recorded %s new hourly readings for %s", added, self.meter_serial)

        return self._summarise(readings)

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
