"""Config flow for Yarra Valley Water.

Signing in mirrors the portal: an email and password, then a verification code
sent by SMS. The credentials are used once, here, to obtain a session cookie and
are never written to the config entry — the portal demands a fresh code on every
sign-in, so storing a password would buy nothing.
"""

from __future__ import annotations

import logging
from pathlib import Path
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

from .api import AccountInfo, AccountSummary, YvwApi
from .aura import YvwAuraClient
from .auth import CODE_LENGTH, YvwLogin
from .const import (
    CATCHUP_UNTIL_HOUR,
    CONF_ACCOUNT_ID,
    CONF_ADDRESS,
    CONF_CATCHUP_FROM_HOUR,
    CONF_COOKIES,
    CONF_INCLUDE_SESSION,
    CONF_KEEPALIVE_MINUTES,
    CONF_METER_SERIAL,
    CONF_PROBE_ENABLED,
    CONF_PROBE_STEP_MINUTES,
    CONF_SID,
    CONF_SIGNED_IN_AT,
    DEFAULT_CATCHUP_FROM_HOUR,
    DEFAULT_INCLUDE_SESSION,
    DEFAULT_KEEPALIVE_MINUTES,
    DEFAULT_PROBE_STEP_MINUTES,
    DOMAIN,
    MAX_KEEPALIVE_MINUTES,
    MIN_KEEPALIVE_MINUTES,
    PORTAL_TIMEZONE,
)
from .exceptions import (
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
        self._cookies: dict[str, str] = {}
        self._summaries: list[AccountSummary] = []
        self._site: AccountInfo | None = None
        self._code_error: str | None = None

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
            except YvwInvalidCode as err:
                # The portal explains a refusal in its own words; passing them
                # through beats a generic message that hides whether the code
                # was wrong, stale or already used.
                self._code_error = str(err)
                errors["base"] = "invalid_code"
                await self._async_dump_code_page()
            except YvwCannotConnect:
                errors["base"] = "cannot_connect"
            except YvwError:
                _LOGGER.exception("Unexpected error verifying the YVW code")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="mfa",
            data_schema=STEP_MFA_SCHEMA,
            errors=errors,
            description_placeholders={
                "digits": str(CODE_LENGTH),
                "detail": self._code_error or "",
            },
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
            # The id alone is refused on a fresh client, so keep the rest
            # of what signing in established alongside it.
            self._cookies = self._login.session_cookies
            self._summaries = await self._async_discover(self._sid)
        except YvwError as err:
            _LOGGER.error("Signed in but could not read the account: %s", err)
            return self.async_abort(reason="no_account")

        if self.source == "reauth":
            # Reauth replaces a dead session; the property does not change.
            return await self._async_store(self._get_reauth_entry().data[CONF_ACCOUNT_ID])

        if not self._summaries:
            # Nothing to choose between, so ask for the number instead of
            # abandoning a sign-in that otherwise worked.
            _LOGGER.debug("The portal listed no accounts; asking for the number")
            return await self.async_step_account()

        if len(self._summaries) == 1:
            return await self._async_store(self._summaries[0].account_id)

        return await self.async_step_site()

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the water account number when it cannot be discovered."""
        errors: dict[str, str] = {}

        if user_input is not None:
            account_id = "".join(
                character
                for character in user_input[CONF_ACCOUNT_ID]
                if character.isdigit()
            )
            if not account_id:
                errors[CONF_ACCOUNT_ID] = "invalid_account"
            else:
                assert self._sid is not None
                try:
                    site = await self._async_read_account(self._sid, account_id)
                except YvwError as err:
                    _LOGGER.debug("Could not read account %s: %s", account_id, err)
                    errors[CONF_ACCOUNT_ID] = "unknown_account"
                else:
                    return await self._async_store(site.account_id)

        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema({vol.Required(CONF_ACCOUNT_ID): str}),
            errors=errors,
        )

    async def async_step_site(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask which property to follow."""
        if user_input is not None:
            return await self._async_store(user_input[CONF_ACCOUNT_ID])

        # Current accounts first: a closed one has no meter still reporting.
        ordered = sorted(self._summaries, key=lambda site: not site.active)
        options = [
            SelectOptionDict(value=site.account_id, label=site.label) for site in ordered
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
        """Read the chosen property's meter details and persist the entry."""
        assert self._sid is not None
        try:
            site = await self._async_read_account(self._sid, account_id)
        except YvwError as err:
            _LOGGER.error("Could not read account %s: %s", account_id, err)
            return self.async_abort(reason="no_account")

        if not site.meter_serial:
            return self.async_abort(reason="no_meter")

        data = {
            CONF_SID: self._sid,
            # The id alone is refused by the portal on a fresh client, so what
            # else sign-in established is kept with it.
            CONF_COOKIES: self._cookies,
            CONF_ACCOUNT_ID: site.account_id,
            CONF_METER_SERIAL: site.meter_serial,
            CONF_ADDRESS: site.address,
            CONF_SIGNED_IN_AT: dt_util.utcnow().isoformat(),
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

    async def _async_dump_code_page(self) -> None:
        """Save the verification page when a code is refused, under debug logging.

        A refusal the portal declines to explain can only be understood from the
        page itself: which fields its scripts fill, and how. Written locally and
        only when debug logging is on, since it is a page from the user's own
        account.
        """
        if not _LOGGER.isEnabledFor(logging.DEBUG) or self._login is None:
            return
        html = self._login.code_page_html
        if not html:
            return
        path = self.hass.config.path(f"{DOMAIN}_verification_page.html")
        try:
            await self.hass.async_add_executor_job(
                lambda: Path(path).write_text(html, encoding="utf-8")
            )
        except OSError as err:
            _LOGGER.debug("Could not save the verification page: %s", err)
        else:
            _LOGGER.warning(
                "Saved the verification page to %s (%s bytes) for diagnosis. "
                "It is from your account, so review it before sharing",
                path,
                len(html),
            )

    async def _async_api(self, sid: str) -> YvwApi:
        """Build an API client for a freshly signed-in session."""
        session = async_create_clientsession(self.hass)
        portal_tz = await dt_util.async_get_time_zone(PORTAL_TIMEZONE)
        return YvwApi(YvwAuraClient(session, sid), portal_tz)

    async def _async_discover(self, sid: str) -> list[AccountSummary]:
        """Ask the portal which properties this login covers."""
        api = await self._async_api(sid)
        return await api.async_list_accounts()

    async def _async_read_account(self, sid: str, account_id: str) -> AccountInfo:
        """Read one account by number."""
        api = await self._async_api(sid)
        return await api.async_get_account(account_id)


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
        options = self.config_entry.options

        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_KEEPALIVE_MINUTES: int(user_input[CONF_KEEPALIVE_MINUTES]),
                    CONF_PROBE_ENABLED: user_input[CONF_PROBE_ENABLED],
                    CONF_PROBE_STEP_MINUTES: int(user_input[CONF_PROBE_STEP_MINUTES]),
                    CONF_INCLUDE_SESSION: user_input[CONF_INCLUDE_SESSION],
                    CONF_CATCHUP_FROM_HOUR: int(user_input[CONF_CATCHUP_FROM_HOUR]),
                }
            )

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_KEEPALIVE_MINUTES,
                    default=options.get(CONF_KEEPALIVE_MINUTES, DEFAULT_KEEPALIVE_MINUTES),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_KEEPALIVE_MINUTES,
                        max=MAX_KEEPALIVE_MINUTES,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="minutes",
                    )
                ),
                vol.Required(
                    CONF_PROBE_ENABLED,
                    default=options.get(CONF_PROBE_ENABLED, False),
                ): bool,
                vol.Required(
                    CONF_PROBE_STEP_MINUTES,
                    default=options.get(CONF_PROBE_STEP_MINUTES, DEFAULT_PROBE_STEP_MINUTES),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=1,
                        max=60,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="minutes",
                    )
                ),
                vol.Required(
                    CONF_CATCHUP_FROM_HOUR,
                    default=options.get(
                        CONF_CATCHUP_FROM_HOUR, DEFAULT_CATCHUP_FROM_HOUR
                    ),
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0,
                        max=CATCHUP_UNTIL_HOUR - 1,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(
                    CONF_INCLUDE_SESSION,
                    default=options.get(CONF_INCLUDE_SESSION, DEFAULT_INCLUDE_SESSION),
                ): bool,
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={"findings": self._findings()},
        )

    def _findings(self) -> str:
        """Describe what calibration has measured so far."""
        entry = self.config_entry
        coordinator = getattr(entry, "runtime_data", None)
        if coordinator is None:
            return "Not running."
        return f"Measured so far: {coordinator.probe_state.summary}."
