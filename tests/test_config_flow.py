"""Tests for setting the integration up.

The promise the readme makes — that credentials are never stored — is worth
holding to a test rather than to a careful reading of the code.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.yvw.api import AccountInfo
from custom_components.yvw.const import (
    CONF_ACCOUNT_ID,
    CONF_ADDRESS,
    CONF_METER_SERIAL,
    CONF_SID,
    DOMAIN,
)
from custom_components.yvw.exceptions import YvwInvalidAuth, YvwInvalidCode

EMAIL = "someone@example.invalid"
PASSWORD = "correct-horse-battery-staple"
CODE = "373054"
ACCOUNT = "1234567890"
SITE = AccountInfo(
    account_id=ACCOUNT,
    address="1 Example St, Suburb, Vic, 3000",
    meter_serial="YAW0000001",
    has_usage=True,
)


def _login(**kwargs) -> AsyncMock:
    login = AsyncMock()
    login.async_submit_credentials.return_value = "sms"
    login.async_submit_code.return_value = "session-value"
    login.async_finish.return_value = "session-value"
    login.code_page_url = "https://myaccount.yvw.com.au/myaccount/apex/MALoginFlowVFPage"
    for key, value in kwargs.items():
        setattr(login, key, value)
    return login


def _api(account_ids: list[str] | None = None) -> AsyncMock:
    api = AsyncMock()
    api.async_list_account_ids.return_value = (
        [ACCOUNT] if account_ids is None else account_ids
    )
    api.async_get_account.return_value = SITE
    return api


async def _run(hass: HomeAssistant, login: AsyncMock, api: AsyncMock):
    with (
        patch("custom_components.yvw.config_flow.YvwLogin", return_value=login),
        patch("custom_components.yvw.config_flow.YvwApi", return_value=api),
        patch("custom_components.yvw.config_flow.YvwAuraClient"),
        patch("custom_components.yvw.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: EMAIL, CONF_PASSWORD: PASSWORD}
        )
        if result["type"] is FlowResultType.FORM and result["step_id"] == "mfa":
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"code": CODE, "resend": False}
            )
        if result["type"] is FlowResultType.FORM and result["step_id"] == "site":
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {CONF_ACCOUNT_ID: ACCOUNT}
            )
        return result


async def test_signing_in_creates_the_entry(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Email, code, property: the whole path the portal makes you walk."""
    result = await _run(hass, _login(), _api())

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == SITE.address
    assert result["data"] == {
        CONF_SID: "session-value",
        CONF_ACCOUNT_ID: ACCOUNT,
        CONF_METER_SERIAL: "YAW0000001",
        CONF_ADDRESS: SITE.address,
    }


async def test_no_credential_is_written_to_the_entry(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """The readme promises this; hold it to a test.

    Only the session survives setup. Nothing that could be used to sign in
    again may be written anywhere in the entry.
    """
    result = await _run(hass, _login(), _api())
    stored = repr(result["data"]).lower()

    assert PASSWORD.lower() not in stored
    assert EMAIL.lower() not in stored
    assert CODE not in stored
    assert not any("pass" in key.lower() for key in result["data"])
    assert not any("user" in key.lower() for key in result["data"])


async def test_a_rejected_password_is_reported(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """A wrong password should say so rather than failing obscurely."""
    login = _login()
    login.async_submit_credentials.side_effect = YvwInvalidAuth("no")

    result = await _run(hass, login, _api())

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_a_rejected_code_is_reported(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """Codes are single use, so this is the common case on a retry."""
    login = _login()
    login.async_submit_code.side_effect = YvwInvalidCode("nope")

    result = await _run(hass, login, _api())

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "mfa"
    assert result["errors"] == {"base": "invalid_code"}


async def test_the_account_number_is_asked_for_when_undiscoverable(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """The portal will not always name the account, so setup asks."""
    result = await _run(hass, _login(), _api(account_ids=[]))

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "account"


@pytest.mark.parametrize("entered", ["1234 567 890", "1234567890"])
async def test_a_typed_account_number_is_accepted(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    custom_integration,
    entered: str,
) -> None:
    """People copy the number off a bill, spaces and all."""
    api = _api(account_ids=[])
    with (
        patch("custom_components.yvw.config_flow.YvwLogin", return_value=_login()),
        patch("custom_components.yvw.config_flow.YvwApi", return_value=api),
        patch("custom_components.yvw.config_flow.YvwAuraClient"),
        patch("custom_components.yvw.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_USERNAME: EMAIL, CONF_PASSWORD: PASSWORD}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": CODE, "resend": False}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ACCOUNT_ID: entered}
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ACCOUNT_ID] == ACCOUNT
