"""Config flow for Yarra Valley Water.

Signing in mirrors the portal: an email and password, then a verification code
sent by SMS. The credentials are used once, here, to obtain a session cookie and
are never written to the config entry — the portal demands a fresh code on every
sign-in, so storing a password would buy nothing.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from .api import AccountInfo, YvwApi
from .aura import YvwAuraClient
from .auth import CODE_LENGTH, YvwLogin
from .const import (
    CONF_ACCOUNT_ID,
    CONF_ADDRESS,
    CONF_KEEPALIVE_MINUTES,
    CONF_METER_SERIAL,
    CONF_SID,
    DEFAULT_KEEPALIVE_MINUTES,
    DOMAIN,
    MAX_KEEPALIVE_MINUTES,
    MIN_KEEPALIVE_MINUTES,
    PORTAL_TIMEZONE,
)
from .exceptions import (
    YvwApiError,
    YvwCannotConnect,
    YvwError,
    YvwInvalidAuth,
    YvwInvalidCode,
)

_LOGGER = logging.getLogger(__name__)

CONF_CODE = "code"
CONF_RESEND = "resend"

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)

STEP_MFA_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_CODE, default=""): str,
        vol.Optional(CONF_RESEND, default=False): bool,
    }
)


class YvwConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Yarra Valley Water config flow."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow."""
        return YvwOptionsFlow()

    def __init__(self) -> None:
        """Set up flow state that spans the sign-in steps."""
        self._login: YvwLogin | None = None
        self._sid: str | None = None
        self._sites: dict[str, AccountInfo] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the portal credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_create_clientsession(self.hass)
            self._login = YvwLogin(session)
            try:
                await self._login.async_submit_credentials(
                    user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except YvwInvalidAuth:
                errors["base"] = "invalid_auth"
            except YvwCannotConnect:
                errors["base"] = "cannot_connect"
            except YvwError:
                _LOGGER.exception("Unexpected error signing in to the YVW portal")
                errors["base"] = "unknown"
            else:
                if self._login.code_page_url is None:
                    # The portal skipped the verification step.
                    return await self._async_finish()
                return await self.async_step_mfa()

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the verification code the portal sent by SMS."""
        errors: dict[str, str] = {}

        if user_input is not None:
            assert self._login is not None
            code = user_input.get(CONF_CODE, "").strip()
            try:
                if user_input.get(CONF_RESEND):
                    await self._login.async_resend_code()
                    errors["base"] = "code_resent"
                elif not code:
                    errors["base"] = "code_required"
                else:
                    await self._login.async_submit_code(code)
                    return await self._async_finish()
            except YvwInvalidCode:
                errors["base"] = "invalid_code"
            except YvwCannotConnect:
                errors["base"] = "cannot_connect"
            except YvwError:
                _LOGGER.exception("Unexpected error verifying the YVW code")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="mfa",
            data_schema=STEP_MFA_SCHEMA,
            errors=errors,
            description_placeholders={"digits": str(CODE_LENGTH)},
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle a session that has expired."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask the user to sign in again."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm", data_schema=STEP_USER_SCHEMA
            )
        return await self.async_step_user(user_input)

    async def _async_finish(self) -> ConfigFlowResult:
        """Discover the sites behind the new session and ask which to follow."""
        assert self._login is not None
        try:
            self._sid = await self._login.async_finish()
            self._sites = await self._async_discover(self._sid)
        except YvwError as err:
            _LOGGER.error("Signed in but could not read the account: %s", err)
            return self.async_abort(reason="no_account")

        if not self._sites:
            return self.async_abort(reason="no_account")

        if self.source == "reauth":
            # Reauth is about replacing a dead session, not changing property.
            return await self._async_store(self._get_reauth_entry().data[CONF_ACCOUNT_ID])

        return await self.async_step_site()

    async def async_step_site(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask which property to follow.

        One login can cover several properties, and even with a single one it is
        worth showing the address so it is obvious what is being set up.
        """
        if user_input is not None:
            return await self._async_store(user_input[CONF_ACCOUNT_ID])

        options = [
            SelectOptionDict(
                value=account_id,
                label=f"{site.address} ({account_id})",
            )
            for account_id, site in self._sites.items()
        ]
        schema = vol.Schema(
            {
                vol.Required(CONF_ACCOUNT_ID, default=options[0]["value"]): SelectSelector(
                    SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
                )
            }
        )
        return self.async_show_form(step_id="site", data_schema=schema)

    async def _async_store(self, account_id: str) -> ConfigFlowResult:
        """Persist the chosen site and its session."""
        site = self._sites.get(account_id)
        if site is None:
            return self.async_abort(reason="wrong_account")
        if not site.meter_serial:
            return self.async_abort(reason="no_meter")

        data = {
            CONF_SID: self._sid,
            CONF_ACCOUNT_ID: site.account_id,
            CONF_METER_SERIAL: site.meter_serial,
            CONF_ADDRESS: site.address,
        }

        await self.async_set_unique_id(site.account_id)

        if self.source == "reauth":
            # Replace the dead session in place rather than creating a duplicate.
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=data
            )

        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=site.address, data=data)

    async def _async_discover(self, sid: str) -> dict[str, AccountInfo]:
        """Read every water account this login can see."""
        session = async_create_clientsession(self.hass)
        client = YvwAuraClient(session, sid)
        portal_tz = await dt_util.async_get_time_zone(PORTAL_TIMEZONE)
        api = YvwApi(client, portal_tz)

        account_ids = await api.async_list_account_ids()
        if not account_ids:
            raise YvwApiError("The portal did not report any water accounts")

        sites: dict[str, AccountInfo] = {}
        for account_id in account_ids:
            sites[account_id] = await api.async_get_account(account_id)
        return sites


class YvwOptionsFlow(OptionsFlow):
    """Let the keep-alive interval be tuned without editing code.

    The portal does not publish its idle timeout. A shorter interval is safer
    but talks to Yarra Valley Water more often; a longer one is quieter but
    risks the session lapsing, which costs another SMS code. The right value is
    found by experiment, so it is an option rather than a constant.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_KEEPALIVE_MINUTES: int(user_input[CONF_KEEPALIVE_MINUTES])}
            )

        current = self.config_entry.options.get(
            CONF_KEEPALIVE_MINUTES, DEFAULT_KEEPALIVE_MINUTES
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_KEEPALIVE_MINUTES, default=current): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_KEEPALIVE_MINUTES,
                        max=MAX_KEEPALIVE_MINUTES,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="minutes",
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
