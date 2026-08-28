"""Tests for the diagnostics download.

The session is a live credential, so whether it appears in a file people share
must follow the setting exactly.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.yvw.api import UsageReading
from custom_components.yvw.aura import AuraContext
from custom_components.yvw.const import (
    CONF_ACCOUNT_ID,
    CONF_ADDRESS,
    CONF_INCLUDE_SESSION,
    CONF_METER_SERIAL,
    CONF_SID,
    DOMAIN,
)
from custom_components.yvw.diagnostics import async_get_config_entry_diagnostics

MELBOURNE = ZoneInfo("Australia/Melbourne")
SID = "a-live-session-value"


async def _setup(hass: HomeAssistant, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="1234567890",
        options=options or {},
        data={
            CONF_SID: SID,
            CONF_ACCOUNT_ID: "1234567890",
            CONF_METER_SERIAL: "YAW0000001",
            CONF_ADDRESS: "1 Example St, Suburb, Vic, 3000",
        },
    )
    entry.add_to_hass(hass)
    api = AsyncMock()
    start = datetime(2026, 8, 20, 0, 0, tzinfo=MELBOURNE)
    api.async_get_hourly_usage.return_value = [
        UsageReading(start=start + timedelta(hours=i), litres=1.0) for i in range(3)
    ]
    api.async_raw_cache.return_value = "1234567890"
    api.async_raw_account.return_value = {"accountSearchResponse": [{"accountId": "x"}]}
    api.async_probe_session_time.return_value = None
    header = base64.urlsafe_b64encode(
        json.dumps({"typ": "JWT", "alg": "HS256", "iat": 1, "exp": 0}).encode()
    ).decode().rstrip("=")
    api.client = MagicMock()
    api.client.aura = AuraContext(
        context={"fwuid": "FW1", "app": "siteforce:communityApp"},
        token=f"{header}.body.signature",
    )
    with (
        patch("custom_components.yvw.YvwAuraClient"),
        patch("custom_components.yvw.YvwApi", return_value=api),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    return entry


async def test_the_session_is_withheld_by_default(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Diagnostics get attached to public issues, so this must be opt-in."""
    entry = await _setup(hass)

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert SID not in repr(result)
    assert "development_session" not in result


async def test_the_session_is_included_when_asked_for(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Turning it on is deliberate, and the file says so at the top."""
    entry = await _setup(hass, {CONF_INCLUDE_SESSION: True})

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["development_session"][CONF_SID] == SID
    warning = next(key for key in result if "READ THIS FIRST" in key)
    assert "LIVE SESSION" in result[warning]


async def test_the_account_details_are_redacted_either_way(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Whatever the setting, personal details stay out of the file."""
    entry = await _setup(hass, {CONF_INCLUDE_SESSION: True})

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["entry"]["data"][CONF_ADDRESS] == "**REDACTED**"
    assert result["entry"]["data"][CONF_METER_SERIAL] == "**REDACTED**"
