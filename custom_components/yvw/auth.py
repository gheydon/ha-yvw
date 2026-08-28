"""Sign in to the MyAccount portal.

The portal has no OAuth and no API credentials: signing in means replaying what
the browser does. That is a password post followed by a verification code sent
by SMS, so it can only be completed with the user present. What comes out is a
session cookie, and that is the only thing worth keeping — the password is used
once here and never stored.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urljoin

import aiohttp

from .aura import (
    async_load_page_context,
    build_message,
    extract_return_value,
    page_headers,
    parse_aura_body,
    read_cookie,
    xhr_headers,
)
from .const import (
    APEX_AUTH_CLASS,
    APEX_AUTH_METHOD,
    AURA_ENDPOINT,
    BASE_URL,
    COMMUNITY_PATH,
    LOGIN_PAGE,
    USAGE_PAGE,
)
from .exceptions import (
    YvwApiError,
    YvwAuthError,
    YvwCannotConnect,
    YvwInvalidAuth,
    YvwInvalidCode,
)

_LOGGER = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=45)

_INPUT_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s">]+))""")
_FORM_ACTION_RE = re.compile(r"<form\b[^>]*\baction\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)

# The code page splits the six digits across separately named fields.
_DIGIT_FIELD_RE = re.compile(
    r":(first|second|third|fourth|fifth|sixth)Hidden:", re.IGNORECASE
)
_DIGIT_ORDER = ("first", "second", "third", "fourth", "fifth", "sixth")

CODE_LENGTH = len(_DIGIT_ORDER)


def _parse_inputs(html: str) -> list[dict[str, str]]:
    """Return every ``<input>`` on a page as a dict of its attributes."""
    inputs = []
    for tag in _INPUT_RE.findall(html):
        attrs = {}
        for match in _ATTR_RE.finditer(tag):
            name = match.group(1).lower()
            attrs[name] = match.group(2) or match.group(3) or match.group(4) or ""
        if attrs.get("name"):
            inputs.append(attrs)
    return inputs


def build_code_form(html: str, code: str) -> dict[str, str]:
    """Build the postback body for the verification-code page.

    Visualforce carries its state in generated hidden fields whose names change
    between deployments, so every field is echoed back as found rather than
    hard-coded, and the digit and submit fields are located by shape.
    """
    inputs = _parse_inputs(html)
    if not inputs:
        raise YvwApiError("The verification page had no form fields")

    payload: dict[str, str] = {}
    digit_fields: dict[str, str] = {}
    submit_name: str | None = None

    for attrs in inputs:
        name = attrs["name"]
        field_type = attrs.get("type", "text").lower()
        if field_type == "submit":
            # Visualforce identifies which button was pressed by its presence.
            if submit_name is None:
                submit_name = name
                payload[name] = attrs.get("value", "")
            continue
        match = _DIGIT_FIELD_RE.search(name)
        if match:
            digit_fields[match.group(1).lower()] = name
            continue
        payload[name] = attrs.get("value", "")

    if len(digit_fields) != CODE_LENGTH:
        raise YvwApiError(
            f"Expected {CODE_LENGTH} code fields on the verification page, "
            f"found {len(digit_fields)}"
        )
    if submit_name is None:
        raise YvwApiError("The verification page had no submit button")

    digits = [character for character in code if character.isdigit()]
    if len(digits) != CODE_LENGTH:
        raise YvwInvalidCode(f"The code must be {CODE_LENGTH} digits")

    for position, digit in zip(_DIGIT_ORDER, digits, strict=True):
        payload[digit_fields[position]] = digit

    return payload


class YvwLogin:
    """Drive the portal's interactive sign-in and hand back a session cookie."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Store the session whose cookie jar will collect the login cookies."""
        self._session = session
        self._code_page_url: str | None = None
        self._code_page_html: str | None = None

    @property
    def code_page_url(self) -> str | None:
        """Return the verification page reached after the password step."""
        return self._code_page_url

    async def async_submit_credentials(self, username: str, password: str) -> str | None:
        """Post the password and return the MFA type the portal chose.

        Returns ``None`` if the portal signed the user straight in without a
        verification step.
        """
        aura = await async_load_page_context(self._session, LOGIN_PAGE)

        payload = {
            "message": build_message(
                APEX_AUTH_CLASS, APEX_AUTH_METHOD, {"username": username, "password": password}
            ),
            "aura.context": json.dumps(aura.context),
            "aura.pageURI": f"{COMMUNITY_PATH}/login/",
            "aura.token": aura.token,
        }

        try:
            async with self._session.post(
                AURA_ENDPOINT,
                params={"r": "1", "aura.ApexAction.execute": "1"},
                data=payload,
                headers=xhr_headers(referer=LOGIN_PAGE),
                timeout=_TIMEOUT,
            ) as response:
                text = await response.text()
        except aiohttp.ClientError as err:
            raise YvwCannotConnect(f"Could not reach the YVW portal: {err}") from err

        try:
            result = extract_return_value(parse_aura_body(text))
        except (YvwApiError, YvwAuthError) as err:
            # The portal reports a bad email or password as a failed action
            # rather than a structured error code.
            raise YvwInvalidAuth("The portal rejected that email address or password") from err

        if not isinstance(result, dict):
            raise YvwInvalidAuth("The portal rejected that email address or password")

        _LOGGER.debug(
            "doLogin returned keys=%s mfaType=%s cookies=%s",
            sorted(result),
            result.get("mfaType"),
            sorted({cookie.key for cookie in self._session.cookie_jar}),
        )

        page_url = result.get("pageUrl")
        if not page_url:
            # No verification step; the session cookie should already be set.
            self._code_page_url = None
            return None

        self._code_page_url = urljoin(BASE_URL, page_url)

        # Loading the verification page is what makes the portal send the code:
        # the browser gets there by navigation, so nothing is sent until the
        # page is actually fetched. It also mints the Visualforce view state
        # that the eventual postback has to echo back, so the page is kept
        # rather than re-fetched, which would invalidate it and send a second
        # code.
        await self._async_load_code_page()
        return result.get("mfaType")

    async def async_resend_code(self) -> None:
        """Ask the portal to send another verification code."""
        if self._code_page_url is None:
            raise YvwApiError("There is no verification step in progress")
        await self._async_load_code_page()

    async def async_submit_code(self, code: str) -> str:
        """Post the verification code and return the session cookie."""
        if self._code_page_url is None or self._code_page_html is None:
            raise YvwApiError("There is no verification step in progress")

        html = self._code_page_html
        payload = build_code_form(html, code)

        action = _FORM_ACTION_RE.search(html)
        target = urljoin(self._code_page_url, action.group(1)) if action else self._code_page_url

        try:
            async with self._session.post(
                target,
                data=payload,
                headers={
                    **page_headers(),
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": BASE_URL,
                    "Referer": self._code_page_url,
                    "Sec-Fetch-Site": "same-origin",
                },
                timeout=_TIMEOUT,
                allow_redirects=True,
            ) as response:
                returned = await response.text()
        except aiohttp.ClientError as err:
            raise YvwCannotConnect(f"Could not reach the YVW portal: {err}") from err

        # A rejected code re-renders the page with a fresh view state. Keeping
        # it lets the user try again without the portal having to text them a
        # new code.
        if _DIGIT_FIELD_RE.search(returned):
            self._code_page_html = returned

        return await self.async_finish()

    async def async_finish(self) -> str:
        """Confirm the session works and return its cookie."""
        try:
            await async_load_page_context(self._session, USAGE_PAGE)
        except YvwAuthError as err:
            raise YvwInvalidCode("The portal did not accept that verification code") from err

        sid = read_cookie(self._session, "sid")
        if not sid:
            raise YvwApiError("Signed in but the portal did not issue a session cookie")
        return sid

    async def _async_get(self, url: str) -> tuple[str, str, int]:
        """Fetch a page, returning its body, the URL it ended on and the status."""
        try:
            async with self._session.get(
                url, headers=page_headers(), timeout=_TIMEOUT, allow_redirects=True
            ) as response:
                return await response.text(), str(response.url), response.status
        except aiohttp.ClientError as err:
            raise YvwCannotConnect(f"Could not reach the YVW portal: {err}") from err

    async def _async_load_code_page(self) -> None:
        """Fetch the verification page, which is what sends the code."""
        assert self._code_page_url is not None
        html, final_url, status = await self._async_get(self._code_page_url)

        reached_the_flow = bool(_DIGIT_FIELD_RE.search(html))
        _LOGGER.debug(
            "Verification page: status=%s landed_on=%s bytes=%s has_code_fields=%s "
            "cookies=%s",
            status,
            final_url,
            len(html),
            reached_the_flow,
            sorted({cookie.key for cookie in self._session.cookie_jar}),
        )

        if not reached_the_flow:
            # Without reaching the flow page the portal never sends anything, so
            # say so rather than presenting a code box that can never be filled.
            raise YvwApiError(
                "The portal did not serve the verification page "
                f"(ended on {final_url} with status {status}), so no code was sent"
            )

        self._code_page_html = html
