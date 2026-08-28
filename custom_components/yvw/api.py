"""Domain-level access to the Yarra Valley Water portal.

Wraps the raw Aura calls in ``aura.py`` and normalises the portal's quirks so
the rest of the integration can work with plain readings.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Any

from .aura import YvwAuraClient
from .const import (
    APEX_ACCOUNTS_CLASS,
    APEX_ACCOUNTS_METHOD,
    APEX_BALANCE_CLASS,
    APEX_BALANCE_METHOD,
    APEX_CACHE_CLASS,
    APEX_CACHE_KEY,
    APEX_CACHE_METHOD,
    CONTROLLER_ACCOUNT,
    CONTROLLER_USAGE,
    MAX_HISTORY_DAYS,
    METHOD_GET_ACCOUNT,
    METHOD_GET_USAGE,
    SESSION_TIME_URL,
    STATUS_ACTUAL,
    STATUS_NOT_AVAILABLE,
)
from .exceptions import YvwApiError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UsageReading:
    """One hour of metered consumption."""

    start: datetime
    """Start of the hour the reading covers, in the portal's local time."""

    litres: float

    @property
    def end(self) -> datetime:
        """Return when the meter reported, which is the end of the hour.

        Statistics are filed against the start of the interval, but a reading is
        taken at the end of it: the 23:00 hour is reported at midnight.
        """
        return self.start + timedelta(hours=1)


@dataclass(frozen=True, slots=True)
class AccountSummary:
    """One of the properties a login covers, as the switcher lists them."""

    account_id: str
    address: str
    status: str

    @property
    def active(self) -> bool:
        """Return whether this is a current account rather than a closed one."""
        return self.status.casefold() == "active"

    @property
    def label(self) -> str:
        """Describe the property for a chooser."""
        suffix = "" if self.active else f", {self.status.lower()}"
        return f"{self.address} ({self.account_id}{suffix})"


@dataclass(frozen=True, slots=True)
class AccountInfo:
    """The details of a water account needed to poll it."""

    account_id: str
    address: str
    meter_serial: str | None
    has_usage: bool


class YvwApi:
    """Read account details and metered usage from the portal."""

    def __init__(self, client: YvwAuraClient, portal_tz: tzinfo) -> None:
        """Store the transport and the timezone the portal reports readings in."""
        self._client = client
        self._tz = portal_tz

    async def async_list_accounts(self) -> list[AccountSummary]:
        """Return every property the signed-in person holds.

        This is the call the portal's own account switcher makes, so it answers
        without needing an account number to start from.
        """
        value = await self._client.async_invoke_apex(
            APEX_ACCOUNTS_CLASS, APEX_ACCOUNTS_METHOD, {}
        )
        rows = (value or {}).get("data") or []
        meta = (value or {}).get("meta") or {}

        accounts = [
            AccountSummary(
                account_id=str(row["accountId"]),
                address=(row.get("location") or {}).get("address") or str(row["accountId"]),
                status=str(row.get("status") or "Unknown"),
            )
            for row in rows
            if row.get("accountId")
        ]

        # The response is paged and nothing observed has needed a second page,
        # so say so rather than silently showing a partial list.
        try:
            pages = int(meta.get("totalPages", 1))
        except (TypeError, ValueError):
            pages = 1
        if pages > 1:
            _LOGGER.warning(
                "The portal reports %s pages of accounts; only the first is listed. "
                "Enter the account number by hand if the one you want is missing",
                pages,
            )

        _LOGGER.debug("Portal lists %s account(s)", len(accounts))
        return accounts

    async def async_list_account_ids(self) -> list[str]:
        """Return every water account the signed-in user can see.

        One login can cover several properties — the portal has a "Switch
        accounts" control — so setup has to ask which one to follow. The cached
        session payload is the only place that lists them without making extra
        requests, and it holds either a bare account id or a JSON blob.
        """
        sources = (
            ("cached session payload", APEX_CACHE_CLASS, APEX_CACHE_METHOD,
             {"key": APEX_CACHE_KEY}),
            ("account balance", APEX_BALANCE_CLASS, APEX_BALANCE_METHOD, {}),
        )

        for label, classname, method, params in sources:
            try:
                value = await self._client.async_invoke_apex(classname, method, params)
            except YvwApiError as err:
                _LOGGER.debug("Could not read the %s: %s", label, err)
                continue

            found = account_ids_in(value)
            _LOGGER.debug(
                "Looked for accounts in the %s: %s (%s)",
                label,
                f"found {len(found)}" if found else "found none",
                _describe(value),
            )
            if found:
                return found

        # Last resort: the account search itself, with nothing to search on. The
        # portal resolves the signed-in user's accounts server side, so this may
        # answer even when the cache is opaque.
        try:
            value = await self._client.async_invoke(CONTROLLER_ACCOUNT, METHOD_GET_ACCOUNT, {})
        except YvwApiError as err:
            _LOGGER.debug("Could not read the account search: %s", err)
            return []

        found = account_ids_in(value)
        _LOGGER.debug(
            "Looked for accounts in the account search: %s (%s)",
            f"found {len(found)}" if found else "found none",
            _describe(value),
        )
        return found

    async def async_get_account(self, account_id: str) -> AccountInfo:
        """Return the account's address and meter details."""
        value = await self._client.async_invoke(
            CONTROLLER_ACCOUNT, METHOD_GET_ACCOUNT, {"accountId": account_id}
        )
        accounts = (value or {}).get("accountSearchResponse") or []
        if not accounts:
            raise YvwApiError(f"The portal returned no account matching {account_id}")

        account = accounts[0]
        meters = account.get("meters") or []
        meter_serial = None
        if meters:
            meter_serial = meters[0].get("meterSerialNumber")
        if not meter_serial:
            meter_serial = (account.get("meter") or {}).get("meterNumber")

        return AccountInfo(
            account_id=str(account.get("accountId") or account_id),
            address=(account.get("location") or {}).get("address") or account_id,
            meter_serial=meter_serial,
            has_usage=bool(account.get("hasUsage")),
        )

    async def async_ping(self, account_id: str) -> None:
        """Make a cheap authenticated call to keep the session from idling out."""
        await self._client.async_invoke(
            CONTROLLER_ACCOUNT, METHOD_GET_ACCOUNT, {"accountId": account_id}
        )

    async def async_raw_cache(self) -> Any:
        """Return the portal's cached session payload verbatim, for diagnostics."""
        return await self._client.async_invoke_apex(
            APEX_CACHE_CLASS, APEX_CACHE_METHOD, {"key": APEX_CACHE_KEY}
        )

    async def async_raw_account(self, account_id: str) -> Any:
        """Return the portal's account response verbatim, for diagnostics."""
        return await self._client.async_invoke(
            CONTROLLER_ACCOUNT, METHOD_GET_ACCOUNT, {"accountId": account_id}
        )

    @property
    def client(self) -> YvwAuraClient:
        """Return the underlying transport."""
        return self._client

    async def async_probe_session_time(self) -> str | None:
        """Return whatever Salesforce reports about the session's idle clock.

        The response shape is undocumented, so this only ever feeds a debug log:
        it exists so the real idle timeout can be measured from a running system
        instead of guessed at.
        """
        return await self._client.async_get_text(SESSION_TIME_URL)

    async def async_get_hourly_usage(
        self,
        account_id: str,
        meter_serial: str,
        start_date: date,
        end_date: date,
    ) -> list[UsageReading]:
        """Return hourly readings between two dates, oldest first.

        The portal refuses any window beginning more than ``MAX_HISTORY_DAYS``
        before today, whatever the end date, so the requested range is clamped
        and then split into chunks the portal will accept.
        """
        earliest = end_date - timedelta(days=MAX_HISTORY_DAYS)
        if start_date < earliest:
            _LOGGER.debug(
                "Clamping requested start %s to %s: the portal serves at most %s days",
                start_date,
                earliest,
                MAX_HISTORY_DAYS,
            )
            start_date = earliest

        readings: dict[datetime, UsageReading] = {}
        chunk_start = start_date
        while chunk_start <= end_date:
            # dateFrom may be up to MAX_HISTORY_DAYS before dateTo, so a window
            # spans that many days plus the end day itself.
            chunk_end = min(chunk_start + timedelta(days=MAX_HISTORY_DAYS), end_date)
            for reading in await self._async_get_usage_chunk(
                account_id, meter_serial, chunk_start, chunk_end
            ):
                readings[reading.start] = reading
            chunk_start = chunk_end + timedelta(days=1)

        return [readings[key] for key in sorted(readings)]

    async def _async_get_usage_chunk(
        self, account_id: str, meter_serial: str, start_date: date, end_date: date
    ) -> list[UsageReading]:
        value = await self._client.async_invoke(
            CONTROLLER_USAGE,
            METHOD_GET_USAGE,
            {
                "accountId": account_id,
                "meterSerialNumber": meter_serial,
                "dateFrom": start_date.isoformat(),
                "dateTo": end_date.isoformat(),
                "interval": "hourly",
            },
        )
        return self._parse_usage(value)

    def _parse_usage(self, value: Any) -> list[UsageReading]:
        response = (value or {}).get("dmUsageResponse")
        if response is None:
            raise YvwApiError("The portal response did not contain usage data")

        # An out-of-range window is reported as a status, not an error. It means
        # "no data here", which is very different from "zero litres used".
        if response.get("status") == STATUS_NOT_AVAILABLE:
            _LOGGER.debug("The portal has no usage data for the requested window")
            return []

        readings = []
        for entry in response.get("usages") or []:
            # Hours the meter did not report come back zero-filled. Recording
            # them would fabricate consumption of exactly nothing.
            if entry.get("measureStatus") != STATUS_ACTUAL:
                continue
            reading = self._parse_entry(entry)
            if reading is not None:
                readings.append(reading)
        return readings

    def _parse_entry(self, entry: dict[str, Any]) -> UsageReading | None:
        raw_date = entry.get("usageDate")
        raw_hour = entry.get("hourOfTheDay")
        if not raw_date or not raw_hour:
            return None

        try:
            usage_date = date.fromisoformat(raw_date)
            hour, _, minute = raw_hour.partition(":")
            end_of_hour = datetime.combine(
                usage_date, time(int(hour), int(minute or 0)), tzinfo=self._tz
            )
        except ValueError:
            _LOGGER.debug("Skipping unparseable usage entry: %s %s", raw_date, raw_hour)
            return None

        try:
            litres = float(entry.get("usageInLitres", 0))
        except (TypeError, ValueError):
            return None

        # hourOfTheDay marks the END of the interval: the hour the portal shows
        # as "12pm - 1pm" arrives as 13:00, and midnight-to-1am of one day is
        # reported as 00:00 dated the following day.
        return UsageReading(start=end_of_hour - timedelta(hours=1), litres=litres)


def account_ids_in(value: object) -> list[str]:
    """Pull water account ids out of the portal's cached session payload."""
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.isdigit():
            return [candidate]
        try:
            value = json.loads(candidate)
        except ValueError:
            return []

    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key.lower() in {"accountid", "accountnumber"} and _is_account_id(item):
                    found.append(str(item))
                else:
                    walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    # dict.fromkeys keeps the portal's own ordering, which puts the currently
    # selected account first.
    return list(dict.fromkeys(found))


def _is_account_id(value: object) -> bool:
    return isinstance(value, str | int) and str(value).isdigit()


def _describe(value: object) -> str:
    """Describe a portal response for the log without spilling its contents."""
    if isinstance(value, str):
        return f"str of {len(value)} chars, digits only: {value.strip().isdigit()}"
    if isinstance(value, dict):
        return f"dict with keys {sorted(value)[:12]}"
    if isinstance(value, list):
        return f"list of {len(value)}"
    return type(value).__name__
