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
from html import unescape
from urllib.parse import parse_qsl, urljoin, urlsplit

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
    r"(first|second|third|fourth|fifth|sixth)Hidden", re.IGNORECASE
)
_DIGIT_ORDER = ("first", "second", "third", "fourth", "fifth", "sixth")

CODE_LENGTH = len(_DIGIT_ORDER)

# frontdoor.jsp swaps the session id in its query for real cookies and then
# bounces onward from JavaScript rather than with an HTTP redirect, so the
# client has to follow it by hand.
_CLIENT_REDIRECT_RES = (
    re.compile(r"""window\.location\.replace\(\s*['"]([^'"]+)['"]""", re.IGNORECASE),
    re.compile(r"""window\.location(?:\.href)?\s*=\s*['"]([^'"]+)['"]""", re.IGNORECASE),
    # \burl guards against matching the url= inside a retURL= parameter, and the
    # lazy prefix keeps it on the first match rather than the last.
    re.compile(
        r"""<meta[^>]+http-equiv=['"]refresh['"][^>]*"""
        r"""content=['"][^'"]*?\burl\s*=\s*([^'"]+)['"]""",
        re.IGNORECASE,
    ),
)

# Enough to cover frontdoor bouncing into the community and on to the flow.
_MAX_CLIENT_REDIRECTS = 6

# Salesforce serves a login flow from a Visualforce page under /apex/.
_LOGIN_FLOW_RE = re.compile(r"/apex/\w*LoginFlow\w*", re.IGNORECASE)


def describe_url(url: str | None) -> str:
    """Describe a URL for the log without leaking anything sensitive.

    The portal can hand back a URL carrying a one-time session token, which must
    not reach a log file. The path and the parameter names are what matter for
    working out where a sign-in went wrong, so values are masked.
    """
    if not url:
        return "<none>"
    parts = urlsplit(url)
    params = parse_qsl(parts.query, keep_blank_values=True)
    masked = "&".join(f"{name}=<{len(value)} chars>" for name, value in params)
    location = parts.path if not parts.netloc else f"{parts.netloc}{parts.path}"
    return f"{location}?{masked}" if masked else location


def describe_form(html: str) -> str:
    """Summarise a page's form fields, for working out why parsing failed.

    Field names and their shapes are not secrets, and they are the only thing
    that identifies how a portal page expects to be filled in.
    """
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    fields = [
        "{name}[type={type},maxlength={maxlength}]".format(
            name=attrs.get("name", "?"),
            type=attrs.get("type", "text"),
            maxlength=attrs.get("maxlength", "-"),
        )
        for attrs in _parse_inputs(html)
    ]
    return (
        f"title={(title.group(1).strip() if title else '?')!r} "
        f"forms={len(re.findall(r'<form', html, re.IGNORECASE))} "
        f"inputs={len(fields)} :: " + " | ".join(fields[:40])
    )


def find_client_redirect(html: str) -> str | None:
    """Return the target of a JavaScript or meta redirect, if the page has one."""
    for pattern in _CLIENT_REDIRECT_RES:
        match = pattern.search(html)
        if match:
            return unescape(match.group(1))
    return None


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

    digit_names = find_code_fields(inputs)
    if len(digit_names) != CODE_LENGTH:
        raise YvwApiError(
            f"Expected {CODE_LENGTH} code fields on the verification page, "
            f"found {len(digit_names)}"
        )

    digits = [character for character in code if character.isdigit()]
    if len(digits) != CODE_LENGTH:
        raise YvwInvalidCode(f"The code must be {CODE_LENGTH} digits")

    payload: dict[str, str] = {}
    submit_name: str | None = None
    for attrs in inputs:
        name = attrs["name"]
        if name in digit_names:
            continue
        if attrs.get("type", "text").lower() == "submit":
            # Visualforce identifies which button was pressed by its presence.
            if submit_name is None:
                submit_name = name
                payload[name] = attrs.get("value", "")
            continue
        payload[name] = attrs.get("value", "")

    if submit_name is None:
        raise YvwApiError("The verification page had no submit button")

    for name, digit in zip(digit_names, digits, strict=True):
        payload[name] = digit

    return payload


def find_code_fields(inputs: list[dict[str, str]]) -> list[str]:
    """Return the fields the code digits go into, in order.

    The page splits the code one digit per field. Their names were read from a
    browser, where scripts had already rewritten them, so they are located by
    shape rather than trusted to be spelled a particular way: first by the
    ordinal naming the page uses, then by falling back to a run of
    single-character inputs, which is what a code entry looks like anywhere.
    """
    ordinals: dict[str, str] = {}
    for attrs in inputs:
        match = _DIGIT_FIELD_RE.search(attrs["name"])
        if match:
            ordinals.setdefault(match.group(1).lower(), attrs["name"])
    if len(ordinals) == CODE_LENGTH:
        return [ordinals[position] for position in _DIGIT_ORDER]

    single_character = [
        attrs["name"]
        for attrs in inputs
        if attrs.get("maxlength") == "1" and attrs.get("type", "text").lower() != "submit"
    ]
    if len(single_character) == CODE_LENGTH:
        return single_character

    return list(ordinals.values()) or single_character


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

        page_url = result.get("pageUrl")

        _LOGGER.debug(
            "doLogin returned keys=%s mfaType=%s pageUrl=%s cookies=%s",
            sorted(result),
            result.get("mfaType"),
            describe_url(page_url),
            sorted({cookie.key for cookie in self._session.cookie_jar}),
        )

        if not page_url:
            # No verification step; the session cookie should already be set.
            self._code_page_url = None
            return None

        # A relative path without a leading slash would resolve against the bare
        # host and land somewhere else entirely, so anchor it on the login page.
        self._code_page_url = urljoin(LOGIN_PAGE, page_url)

        # Loading the verification page is what makes the portal send the code:
        # the browser gets there by navigation, so nothing is sent until the
        # page is actually fetched. It also mints the Visualforce view state
        # that the eventual postback has to echo back, so the page is kept
        # rather than re-fetched, which would invalidate it and send a second
        # code.
        if not await self._async_load_code_page():
            # The portal completed the sign-in without asking for a code.
            self._code_page_url = None
            return None
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

    async def _async_load_code_page(self) -> bool:
        """Follow the sign-in through to the verification page.

        Returns True once the page carrying the code fields is reached, which is
        also what makes the portal send the code. Returns False if the session
        came out fully signed in without a verification step.

        The hops matter: doLogin hands back a frontdoor.jsp URL that trades its
        session id for cookies and then bounces onward from JavaScript, and the
        community only redirects to the verification flow once those cookies
        exist. Following just the first hop lands on the bounce page and no code
        is ever sent.
        """
        assert self._code_page_url is not None
        url = self._code_page_url

        for hop in range(_MAX_CLIENT_REDIRECTS):
            html, final_url, status = await self._async_get(url)
            has_code_fields = bool(_DIGIT_FIELD_RE.search(html))
            redirect = None if has_code_fields else find_client_redirect(html)

            _LOGGER.debug(
                "Sign-in hop %s: status=%s landed_on=%s bytes=%s has_code_fields=%s "
                "next=%s cookies=%s",
                hop,
                status,
                describe_url(final_url),
                len(html),
                has_code_fields,
                describe_url(redirect),
                sorted({cookie.key for cookie in self._session.cookie_jar}),
            )

            if has_code_fields:
                self._code_page_html = html
                self._code_page_url = final_url
                return True

            if redirect is not None:
                url = urljoin(final_url, redirect)
                continue

            # No onward hop. Landing on the login flow itself means the code
            # was sent but the page could not be read, which must not be
            # mistaken for not needing one: that would skip code entry and fail
            # later with something unrelated.
            if _LOGIN_FLOW_RE.search(final_url):
                _LOGGER.error(
                    "Reached the verification page but could not find the code "
                    "fields. %s",
                    describe_form(html),
                )
                raise YvwApiError(
                    "A code was sent, but the verification page could not be read. "
                    "The portal has probably changed its form; the debug log lists "
                    "the fields it offered"
                )

            if read_cookie(self._session, "sid"):
                _LOGGER.debug("Signed in without a verification step")
                self._code_page_html = None
                return False

            raise YvwApiError(
                "The sign-in stopped at "
                f"{describe_url(final_url)} (status {status}) without reaching "
                "the verification page, so no code was sent"
            )

        raise YvwApiError("The sign-in redirected too many times to follow")
