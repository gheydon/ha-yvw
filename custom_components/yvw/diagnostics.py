"""Diagnostics for the Yarra Valley Water integration.

The portal is undocumented, so working out why something misbehaves — a session
that lapses sooner than expected, or a login covering several properties —
means looking at what the portal actually returned. This gathers that in one
download.

Credentials and personal details are redacted: diagnostics get attached to bug
reports. What survives is the shape of the responses and the timing of the
session, which is what the questions are actually about.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .api import account_ids_in
from .const import (
    CONF_ACCOUNT_ID,
    CONF_ADDRESS,
    CONF_COOKIES,
    CONF_INCLUDE_SESSION,
    CONF_METER_SERIAL,
    CONF_SID,
    DEFAULT_INCLUDE_SESSION,
)
from .coordinator import YvwConfigEntry
from .exceptions import YvwError

_LOGGER = logging.getLogger(__name__)

TO_REDACT = {
    CONF_COOKIES,
    CONF_SID,
    CONF_ACCOUNT_ID,
    CONF_ADDRESS,
    CONF_METER_SERIAL,
    "accountId",
    "accountNumber",
    "address",
    "addressline1",
    "billDeliveryEmailAddress",
    "customers",
    "meterBadgeNumber",
    "meterNumber",
    "meterSerialNumber",
    "postcode",
    "premiseId",
    "propertyNumber",
    "serviceLocationId",
    "servicePointId",
    "servicePointInformation",
    "suburb",
}

# The token is a CSRF credential; only its non-secret claims are useful here.
SAFE_TOKEN_CLAIMS = ("typ", "alg", "iat", "exp")


def _token_claims(token: str) -> dict[str, Any]:
    """Decode a token's header, keeping only the claims that are not secrets."""
    segment = token.split(".")[0]
    segment += "=" * (-len(segment) % 4)
    try:
        header = json.loads(base64.urlsafe_b64decode(segment))
    except (ValueError, binascii.Error):
        return {"decoded": False}
    claims: dict[str, Any] = {
        name: header[name] for name in SAFE_TOKEN_CLAIMS if name in header
    }
    claims["decoded"] = True
    # exp of 0 means the token itself never expires; the session governs it.
    claims["expires"] = bool(header.get("exp"))
    return claims


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: YvwConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    aura = coordinator.api.client.aura

    diagnostics: dict[str, Any] = {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "session": {
            "age": str(coordinator.session_age),
            "last_contact": (
                coordinator.last_contact.isoformat() if coordinator.last_contact else None
            ),
            "now": dt_util.utcnow().isoformat(),
            "keepalive_interval": str(coordinator.keepalive_interval),
            "update_interval": str(coordinator.update_interval),
            "keepalive_running": coordinator.keepalive_running,
            "calibrating": coordinator.calibrating,
            "timeout_measurement": coordinator.probe_state.summary,
        },
        "aura": {
            "context_loaded": aura is not None,
            "fwuid": aura.context.get("fwuid") if aura else None,
            "app": aura.context.get("app") if aura else None,
            "token": _token_claims(aura.token) if aura else None,
        },
        "readings": {
            "latest_hour": (
                coordinator.data.latest.start.isoformat()
                if coordinator.data and coordinator.data.latest
                else None
            ),
            "last_full_day": (
                coordinator.data.last_full_day.isoformat()
                if coordinator.data and coordinator.data.last_full_day
                else None
            ),
        },
    }

    # The cached session payload is where a login's other properties would be
    # listed. Nobody involved here has a multi-property login to test against,
    # so this reports the payload's shape in enough detail to work out how to
    # read it, while keeping the numbers themselves out of the file.
    try:
        diagnostics["cache_payload"] = _describe_cache(await coordinator.api.async_raw_cache())
    except YvwError as err:
        diagnostics["cache_payload"] = {"error": str(err)}

    try:
        diagnostics["account"] = async_redact_data(
            await coordinator.api.async_raw_account(coordinator.account_id), TO_REDACT
        )
    except YvwError as err:
        diagnostics["account"] = {"error": str(err)}

    diagnostics["session"]["portal_clock"] = await coordinator.api.async_probe_session_time()

    if entry.options.get(CONF_INCLUDE_SESSION, DEFAULT_INCLUDE_SESSION):
        # Deliberate and temporary. Announced loudly rather than tucked away,
        # because this file is the sort of thing people paste into issues.
        _LOGGER.warning(
            "This diagnostics download contains the live Yarra Valley Water session "
            "cookie in the clear. Anyone who reads it can use the account until the "
            "session lapses. Do not attach it to a public issue. Turn off "
            "'Include the session in diagnostics' under Configure to stop"
        )
        diagnostics["!!! READ THIS FIRST !!!"] = (
            "This file contains a LIVE SESSION for the Yarra Valley Water account "
            "below, in the clear. Anyone who reads it can sign in as you until the "
            "session expires. Do not post it publicly or attach it to an issue. "
            "Sign out of MyAccount, or reload this integration, to invalidate it. "
            "It is here because 'Include the session in diagnostics' is switched "
            "on under Configure. Turn that off when you no longer need it."
        )
        diagnostics["development_session"] = {
            "warning": "live credential, see above",
            CONF_SID: entry.data.get(CONF_SID),
            CONF_ACCOUNT_ID: entry.data.get(CONF_ACCOUNT_ID),
            "aura_token": aura.token if aura else None,
            "how_to_use": (
                "Send this cookie as 'sid' against myaccount.yvw.com.au; the Aura "
                "token is read fresh from a page load, so it need not be reused."
            ),
        }

    return diagnostics


def _describe_cache(value: Any) -> dict[str, Any]:
    """Describe the cached session payload without leaking its contents."""
    described: dict[str, Any] = {
        "type": type(value).__name__,
        "accounts_discovered": len(account_ids_in(value)),
    }

    if isinstance(value, str):
        described["length"] = len(value)
        described["is_bare_account_id"] = value.strip().isdigit()
        try:
            value = json.loads(value)
        except ValueError:
            # An opaque blob could contain anything, so report only that it did
            # not parse. If discovery ever fails here, this is the line to chase.
            described["parses_as_json"] = False
            return described
        described["parses_as_json"] = True

    if isinstance(value, dict | list):
        described["structure"] = async_redact_data(value, TO_REDACT)

    return described
