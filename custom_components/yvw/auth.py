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
from urllib.parse import urljoin

import aiohttp

from .aura import (
    MAX_CLIENT_REDIRECTS,
    async_load_page_context,
    build_message,
    describe_url,
    extract_return_value,
    find_client_redirect,
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

# The submit button does not post the form directly. It calls into AJAX4JSF,
# which identifies the request by an AJAXREQUEST parameter naming the container
# and echoes the pressed button's client id as its own value. A plain post is
# accepted but simply re-renders the page without running the action behind the
# button, which looks exactly like a silently refused code.
_A4J_SUBMIT_RE = re.compile(r"A4J\.AJAX\.Submit\(\s*'([^']+)'")
_A4J_PARAMS_RE = re.compile(r"'parameters'\s*:\s*\{([^}]*)\}")
_A4J_PAIR_RE = re.compile(r"'([^']+)'\s*:\s*'([^']*)'")
_A4J_REQUEST_FIELD = "AJAXREQUEST"

CODE_LENGTH = len(_DIGIT_ORDER)

# Salesforce serves a login flow from a Visualforce page under /apex/.
_LOGIN_FLOW_RE = re.compile(r"/apex/\w*LoginFlow\w*", re.IGNORECASE)


def a4j_parameters(onclick: str) -> dict[str, str] | None:
    """Return the parameters an AJAX4JSF submit button would post, if it is one."""
    container = _A4J_SUBMIT_RE.search(onclick)
    if container is None:
        return None
    parameters = {_A4J_REQUEST_FIELD: container.group(1)}
    params = _A4J_PARAMS_RE.search(onclick)
    if params:
        parameters.update(dict(_A4J_PAIR_RE.findall(params.group(1))))
    return parameters


def page_message(html: str) -> str | None:
    """Return whatever the page is telling the user, if anything.

    The portal explains a refusal in the rendered page rather than in any
    structured field, so its own wording is the most reliable account of what
    went wrong and is worth showing rather than paraphrasing.
    """
    body = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    # Tags become line breaks, not spaces, so neighbouring elements do not run
    # together into one sentence.
    text = unescape(re.sub(r"(?s)<[^>]+>", "\n", body))
    lines = [line.strip() for line in re.split(r"[\r\n.]+", text) if line.strip()]
    wanted = re.compile(
        r"\b(incorrect|invalid|expired|not valid|try again|does not match|"
        r"unable|error|wrong)\b",
        re.IGNORECASE,
    )
    for line in lines:
        collapsed = " ".join(line.split())
        if 8 < len(collapsed) <= 200 and wanted.search(collapsed):
            return collapsed
    return None


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
            if submit_name is not None:
                continue
            submit_name = name
            parameters = a4j_parameters(attrs.get("onclick", ""))
            if parameters is None:
                # A plain button identifies itself by being present at all.
                payload[name] = attrs.get("value", "")
            else:
                payload.update(parameters)
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
        chosen = [ordinals[position] for position in _DIGIT_ORDER]
        _LOGGER.debug("Code fields found by ordinal naming: %s", chosen)
        return chosen

    # The visible boxes are the ones a person types into, but the page may post
    # the digits from hidden fields its scripts copy them into, so prefer any
    # hidden candidates over the visible ones.
    single_character = [
        attrs
        for attrs in inputs
        if attrs.get("maxlength") == "1" and attrs.get("type", "text").lower() != "submit"
    ]
    hidden = [a["name"] for a in single_character if a.get("type", "").lower() == "hidden"]
    visible = [a["name"] for a in single_character if a.get("type", "").lower() != "hidden"]
    for label, candidates in (("hidden", hidden), ("visible", visible)):
        if len(candidates) == CODE_LENGTH:
            _LOGGER.debug("Code fields found by shape (%s): %s", label, candidates)
            return candidates

    _LOGGER.warning(
        "Could not identify the code fields. ordinals=%s hidden=%s visible=%s",
        sorted(ordinals.values()),
        hidden,
        visible,
    )
    return list(ordinals.values()) or visible or hidden


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

    @property
    def code_page_html(self) -> str | None:
        """Return the verification page as served, for diagnosing a refusal."""
        return self._code_page_html

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

        # An AJAX postback answers with a page fragment, which may still carry
        # the code fields whether or not the code was taken. Whether the session
        # actually works is the only reliable answer, so ask that instead of
        # reading the response.
        try:
            return await self.async_finish()
        except YvwInvalidCode:
            # Keep the returned form: it carries a fresh view state, so another
            # attempt does not need the portal to send a new code.
            if _DIGIT_FIELD_RE.search(returned):
                self._code_page_html = returned
            message = page_message(returned)
            _LOGGER.debug(
                "The code was not accepted. portal_said=%r posted_to=%s %s",
                message,
                describe_url(target),
                describe_form(returned),
            )
            raise YvwInvalidCode(
                message or "The portal did not accept that verification code"
            ) from None

    async def async_finish(self) -> str:
        """Confirm the session works and return its cookie."""
        try:
            await async_load_page_context(self._session, USAGE_PAGE)
        except YvwAuthError as err:
            raise YvwInvalidCode("The portal did not accept that verification code") from err
        except YvwApiError as err:
            # Salesforce parks a session with an unfinished login flow on
            # loginFlowOnly, so landing there means the code never took.
            if "loginflow" in str(err).lower():
                raise YvwInvalidCode(
                    "The verification step was not completed, so the code was not accepted"
                ) from err
            raise

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

        for hop in range(MAX_CLIENT_REDIRECTS):
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
