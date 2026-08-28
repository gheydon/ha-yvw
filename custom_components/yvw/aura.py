"""Low-level client for the Salesforce Aura endpoint behind the YVW portal.

MyAccount is a Salesforce Experience Cloud site. There is no documented API: the
browser talks to a single Aura endpoint, and every Apex call is routed through
one generic dispatcher (``MyAccountGenericNavFWController.invokeMethod``). This
module reproduces that call and nothing more; the domain-level wrappers live in
``api.py``.

Requests need two things beyond the session cookie, and both are read from the
portal at runtime rather than stored:

* ``fwuid`` and the app descriptor, which identify the deployed framework
  version and rotate whenever Salesforce ships a release;
* a CSRF token, which the portal hands over in one of two ways. Its own
  bootstrap script picks between them like this::

      cn = auraConfig["eikoocnekot"];
      if (cn) { /* read the token out of the cookie named cn, then delete it */ }
      else if (auraConfig["token"] == null) { auraConfig["csrfV2"] = true }

  Signed-in pages take the first branch: an ``eikoocnekot`` field ("tokencookie"
  backwards) names a ``__Host-`` cookie holding the token. Reading it from the
  cookie jar on each page load keeps the token fresh and unstored.

  The login page takes the second branch and issues no token cookie at all. The
  server still insists the ``aura.token`` parameter be *present*, but does not
  check its value, so an empty string is what a browser effectively sends there.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from html import unescape
from typing import Any
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

import aiohttp
from yarl import URL

from .const import (
    AURA_APP,
    AURA_ENDPOINT,
    BASE_URL,
    BROWSER_HEADERS,
    COMMUNITY_PATH,
    DISPATCH_CLASS,
    DISPATCH_METHOD,
    USAGE_PAGE,
)
from .exceptions import YvwApiError, YvwAuthError, YvwCannotConnect

_LOGGER = logging.getLogger(__name__)

# The framework descriptor is embedded in resource URLs as a URL-encoded JSON
# blob: /sfsites/l/%7B...%7D/resources.js. It nests braces, so it is sliced out
# by locating the trailing path rather than matched with a lazy regex.
_DESCRIPTOR_MARKER = "/sfsites/l/"
_DESCRIPTOR_TAILS = ("/resources.js", "/app.js")

# Names the cookie that carries the CSRF token. Matched as a JSON key so it
# cannot latch onto the bootstrap script's own references to the same name.
_TOKEN_COOKIE_RE = re.compile(r'"eikoocnekot"\s*:\s*"([^"]+)"')

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
MAX_CLIENT_REDIRECTS = 6

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=45)


@dataclass(frozen=True, slots=True)
class AuraContext:
    """Everything a request needs beyond the session cookie."""

    context: dict[str, Any]
    token: str


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


def find_client_redirect(html: str) -> str | None:
    """Return the target of a JavaScript or meta redirect, if the page has one."""
    for pattern in _CLIENT_REDIRECT_RES:
        match = pattern.search(html)
        if match:
            return unescape(match.group(1))
    return None


def parse_aura_body(text: str) -> dict[str, Any]:
    """Parse an Aura response body.

    Successful responses may carry a ``while(1);`` anti-JSON-hijacking prefix;
    error responses come wrapped as ``*/{...}/*ERROR*/``.
    """
    body = text.strip()
    if body.startswith("while(1);"):
        body = body[len("while(1);") :]
    if body.startswith("*/"):
        body = body[2:]
    if body.endswith("/*ERROR*/"):
        body = body[: -len("/*ERROR*/")]
    try:
        return json.loads(body.strip())
    except ValueError as err:
        raise YvwApiError("Could not parse the portal response") from err


def extract_descriptor(html: str) -> dict[str, Any]:
    """Pull the Aura framework descriptor out of a portal page."""
    for tail in _DESCRIPTOR_TAILS:
        end = html.find(tail)
        if end == -1:
            continue
        start = html.rfind(_DESCRIPTOR_MARKER, 0, end)
        if start == -1:
            continue
        blob = html[start + len(_DESCRIPTOR_MARKER) : end]
        try:
            return json.loads(unquote(blob))
        except ValueError:
            continue
    raise YvwApiError("Could not find the Aura framework descriptor on the page")


def token_cookie_name(html: str) -> str | None:
    """Return the cookie a page says holds its CSRF token, if it names one.

    Signed-in pages name one. The login page does not, and runs under csrfV2
    instead, where the token value is unchecked.
    """
    match = _TOKEN_COOKIE_RE.search(html)
    return match.group(1) if match else None


def build_context(descriptor: dict[str, Any], app: str = AURA_APP) -> dict[str, Any]:
    """Build the aura.context payload from a page's framework descriptor."""
    return {
        "mode": descriptor.get("mode", "PROD"),
        "fwuid": descriptor["fwuid"],
        "app": descriptor.get("app", app),
        "loaded": descriptor.get("loaded", {}),
        "dn": [],
        "globals": {},
        "uad": True,
    }


def page_headers() -> dict[str, str]:
    """Headers for a page load, as Chrome would send them."""
    return {
        **BROWSER_HEADERS,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }


def xhr_headers(referer: str = USAGE_PAGE) -> dict[str, str]:
    """Headers for the background Aura request the page would make."""
    return {
        **BROWSER_HEADERS,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": BASE_URL,
        "Referer": referer,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }


def build_message(classname: str, method: str, params: dict[str, Any] | None) -> str:
    """Build the Aura ``message`` payload for one Apex call."""
    return json.dumps(
        {
            "actions": [
                {
                    "id": "1;a",
                    "descriptor": "aura://ApexActionController/ACTION$execute",
                    "callingDescriptor": "UNKNOWN",
                    "params": {
                        "namespace": "",
                        "classname": classname,
                        "method": method,
                        "params": params or {},
                        "cacheable": False,
                        "isContinuation": False,
                    },
                }
            ]
        }
    )


def extract_return_value(parsed: dict[str, Any]) -> Any:
    """Unwrap an Aura response, translating failures into exceptions."""
    event = parsed.get("event") or {}
    descriptor = event.get("descriptor", "")
    exception_message = parsed.get("exceptionMessage", "")

    # Both a dead session and a rejected CSRF token surface as invalidSession.
    if "invalidSession" in descriptor or "Guest user access is not allowed" in exception_message:
        raise YvwAuthError("The portal session is no longer valid")

    actions = parsed.get("actions") or []
    if not actions:
        raise YvwApiError(exception_message or "The portal returned no action result")

    action = actions[0]
    if action.get("state") != "SUCCESS":
        errors = action.get("error") or []
        detail = errors[0].get("message") if errors else action.get("state")
        raise YvwApiError(f"The portal rejected the request: {detail}")

    return_value = action.get("returnValue")
    # The dispatcher double-wraps: returnValue.returnValue holds the payload.
    if isinstance(return_value, dict) and "returnValue" in return_value:
        return return_value["returnValue"]
    return return_value


def read_cookie(session: aiohttp.ClientSession, name: str) -> str | None:
    """Return a cookie's value from the session's jar."""
    for cookie in session.cookie_jar:
        if cookie.key == name:
            return cookie.value
    return None


async def async_load_page_context(
    session: aiohttp.ClientSession, url: str, app: str = AURA_APP
) -> AuraContext:
    """Load a portal page and take the Aura context and CSRF token from it.

    Salesforce moves between frontdoor.jsp, the community and any pending login
    flow using script and meta redirects rather than HTTP ones, so the hops are
    followed by hand. Stopping at the first response lands on a bounce page,
    which carries neither the framework descriptor nor a token.
    """
    html = ""
    for _ in range(MAX_CLIENT_REDIRECTS):
        try:
            async with session.get(
                url, headers=page_headers(), timeout=_REQUEST_TIMEOUT, allow_redirects=True
            ) as response:
                # An expired session is bounced to the login page.
                if "/login" in str(response.url) and "/login" not in url:
                    raise YvwAuthError("Session expired: redirected to the login page")
                html = await response.text()
                final_url = str(response.url)
        except aiohttp.ClientError as err:
            raise YvwCannotConnect(f"Could not reach the YVW portal: {err}") from err

        redirect = find_client_redirect(html)
        if redirect is None:
            break
        _LOGGER.debug("Following a client-side redirect to %s", describe_url(redirect))
        url = urljoin(final_url, redirect)
    else:
        raise YvwApiError("The portal redirected too many times to follow")

    cookie_name = token_cookie_name(html)
    if cookie_name is None:
        # The csrfV2 path: no token cookie is issued and the value is not
        # checked, but the parameter still has to be sent. This is how the
        # login page works, so it is normal rather than a failure.
        _LOGGER.debug("Page issued no CSRF token cookie; using the csrfV2 path")
        token = ""
    else:
        token = read_cookie(session, cookie_name)
        if not token:
            raise YvwApiError("The portal named a CSRF token cookie but did not set it")

    return AuraContext(context=build_context(extract_descriptor(html), app), token=token)


class YvwAuraClient:
    """Issue Apex calls against the MyAccount Aura endpoint."""

    def __init__(self, session: aiohttp.ClientSession, sid: str) -> None:
        """Store the session and seed the jar with the portal session cookie."""
        self._session = session
        self._session.cookie_jar.update_cookies({"sid": sid}, response_url=URL(BASE_URL))
        self._aura: AuraContext | None = None

    @property
    def aura(self) -> AuraContext | None:
        """Return the context currently in use, if the page has been loaded."""
        return self._aura

    async def async_refresh(self) -> AuraContext:
        """Reload the usage page to pick up a fresh context and token."""
        self._aura = await async_load_page_context(self._session, USAGE_PAGE)
        return self._aura

    async def async_get_text(self, url: str) -> str | None:
        """Fetch an authenticated URL, returning None if it is unavailable."""
        try:
            async with self._session.get(
                url,
                params={"buster": "1"},
                headers={**BROWSER_HEADERS, "Accept": "*/*", "Referer": USAGE_PAGE},
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                if response.status != 200:
                    return None
                return (await response.text())[:200]
        except aiohttp.ClientError:
            return None

    async def async_invoke(
        self, controller: str, method: str, argument: dict[str, Any] | None = None
    ) -> Any:
        """Call an Apex controller method through the portal's dispatcher."""
        return await self.async_invoke_apex(
            DISPATCH_CLASS,
            DISPATCH_METHOD,
            {"controller": controller, "method": method, "argument": argument or {}},
        )

    async def async_invoke_apex(
        self, classname: str, method: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Call an ``@AuraEnabled`` Apex method directly and return its result."""
        if self._aura is None:
            await self.async_refresh()

        try:
            return await self._async_invoke_once(classname, method, params)
        except (YvwApiError, YvwAuthError):
            # A rotated fwuid and a spent token both fail here and both are
            # cured by reloading the page. If the session itself is gone the
            # reload redirects to the login page and raises YvwAuthError, which
            # is what the caller needs to hear.
            _LOGGER.debug("Retrying %s.%s after refreshing the Aura context", classname, method)
            await self.async_refresh()
            return await self._async_invoke_once(classname, method, params)

    async def _async_invoke_once(
        self, classname: str, method: str, params: dict[str, Any] | None
    ) -> Any:
        assert self._aura is not None
        payload = {
            "message": build_message(classname, method, params),
            "aura.context": json.dumps(self._aura.context),
            "aura.pageURI": f"{COMMUNITY_PATH}/usage",
            "aura.token": self._aura.token,
        }

        try:
            async with self._session.post(
                AURA_ENDPOINT,
                params={"r": "1", "aura.ApexAction.execute": "1"},
                data=payload,
                headers=xhr_headers(),
                timeout=_REQUEST_TIMEOUT,
            ) as response:
                text = await response.text()
        except aiohttp.ClientError as err:
            raise YvwCannotConnect(f"Could not reach the YVW portal: {err}") from err

        return extract_return_value(parse_aura_body(text))
