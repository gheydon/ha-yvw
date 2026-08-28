"""Tests for the sign-in helpers."""

from __future__ import annotations

import pytest

from custom_components.yvw.api import account_ids_in
from custom_components.yvw.auth import build_code_form, find_client_redirect
from custom_components.yvw.exceptions import YvwApiError, YvwInvalidCode

CODE_PAGE = """
<form id="mfapage:theForm" method="post" action="/myaccount/apex/MALoginFlowVFPage">
  <input type="hidden" name="mfapage:theForm" value="mfapage:theForm" />
  <input type="hidden" name="com.salesforce.visualforce.ViewState" value="STATE" />
  <input type="hidden" name="com.salesforce.visualforce.ViewStateMAC" value="MAC" />
  <input type="hidden" name="mfapage:theForm:page:firstHidden:hidden" value="" />
  <input type="hidden" name="mfapage:theForm:page:secondHidden:hidden" value="" />
  <input type="hidden" name="mfapage:theForm:page:thirdHidden:hidden" value="" />
  <input type="hidden" name="mfapage:theForm:page:fourthHidden:hidden" value="" />
  <input type="hidden" name="mfapage:theForm:page:fifthHidden:hidden" value="" />
  <input type="hidden" name="mfapage:theForm:page:sixthHidden:hidden" value="" />
  <input type="submit" name="mfapage:theForm:page:j_id73:j_id82:submit" value="Submit" />
</form>
"""


def test_code_is_split_across_the_six_digit_fields() -> None:
    """The page takes one digit per field, in order."""
    payload = build_code_form(CODE_PAGE, "123456")

    assert payload["mfapage:theForm:page:firstHidden:hidden"] == "1"
    assert payload["mfapage:theForm:page:sixthHidden:hidden"] == "6"


def test_visualforce_state_is_echoed_back() -> None:
    """Visualforce rejects a postback that loses its view state."""
    payload = build_code_form(CODE_PAGE, "123456")

    assert payload["com.salesforce.visualforce.ViewState"] == "STATE"
    assert payload["com.salesforce.visualforce.ViewStateMAC"] == "MAC"
    # The pressed button identifies the action to Visualforce.
    assert "mfapage:theForm:page:j_id73:j_id82:submit" in payload


def test_generated_field_names_are_not_assumed() -> None:
    """Visualforce renames its generated ids between deployments."""
    renamed = CODE_PAGE.replace("j_id73:j_id82", "j_id99:j_id01")

    payload = build_code_form(renamed, "123456")

    assert "mfapage:theForm:page:j_id99:j_id01:submit" in payload


@pytest.mark.parametrize("code", ["12345", "1234567", "abcdef", ""])
def test_a_code_that_is_not_six_digits_is_rejected(code: str) -> None:
    """Catch a mistyped code before spending it against the portal."""
    with pytest.raises(YvwInvalidCode):
        build_code_form(CODE_PAGE, code)


def test_an_unrecognisable_page_is_an_error() -> None:
    """If the portal redesigns the page, say so rather than post nonsense."""
    with pytest.raises(YvwApiError):
        build_code_form("<html><body>Something else</body></html>", "123456")


def test_a_bare_account_id_is_recognised() -> None:
    """The cached payload is sometimes just the selected account."""
    assert account_ids_in("1234567890") == ["1234567890"]


def test_several_accounts_are_found_in_a_json_payload() -> None:
    """A login covering more than one property must offer all of them."""
    payload = '{"accounts":[{"accountId":"1111111111"},{"accountId":"2222222222"}]}'

    assert account_ids_in(payload) == ["1111111111", "2222222222"]


def test_repeated_accounts_are_listed_once() -> None:
    """The payload mentions the selected account in more than one place."""
    payload = '{"accountId":"1111111111","accounts":[{"accountId":"1111111111"}]}'

    assert account_ids_in(payload) == ["1111111111"]


def test_an_opaque_payload_yields_nothing() -> None:
    """Better to abort setup than to invent an account id."""
    assert account_ids_in("not json at all") == []


class FakeResponse:
    """Minimal stand-in for an aiohttp response."""

    def __init__(
        self,
        text: str,
        url: str = "https://myaccount.yvw.com.au/x",
        status: int = 200,
    ) -> None:
        self._text = text
        self.url = url
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return self._text.encode()


class FakeSession:
    """Record what the login flow asks the portal for."""

    def __init__(self, get_body: str = CODE_PAGE, post_body: str = "") -> None:
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []
        self._get_body = get_body
        self._post_body = post_body

    def get(self, url, **kwargs):
        self.gets.append(url)
        return FakeResponse(self._get_body, url=url)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs.get("data") or {}))
        return FakeResponse(self._post_body, url=url)

    @property
    def cookie_jar(self):
        return []


DO_LOGIN_OK = {"mfaType": "SMS", "pageUrl": "/myaccount/apex/MALoginFlowVFPage?retURL=%2F"}


async def _login_with(monkeypatch, session, result=DO_LOGIN_OK):
    from custom_components.yvw import auth as auth_module

    class Ctx:
        context = {"fwuid": "x", "app": "siteforce:loginApp2", "loaded": {}}
        token = ""

    async def fake_context(*args, **kwargs):
        return Ctx()

    monkeypatch.setattr(auth_module, "async_load_page_context", fake_context)
    monkeypatch.setattr(auth_module, "parse_aura_body", lambda text: {})
    monkeypatch.setattr(auth_module, "extract_return_value", lambda parsed: result)

    login = auth_module.YvwLogin(session)
    return login, await login.async_submit_credentials("someone@example.invalid", "pw")


async def test_the_code_page_is_fetched_during_the_password_step(monkeypatch) -> None:
    """Loading that page is what makes the portal send the code.

    Deferring the fetch until the code is submitted means the user is asked for
    a code that was never sent.
    """
    session = FakeSession()
    login, mfa_type = await _login_with(monkeypatch, session)

    assert mfa_type == "SMS"
    assert len(session.gets) == 1
    assert "MALoginFlowVFPage" in session.gets[0]


async def test_submitting_a_code_does_not_refetch_the_page(monkeypatch) -> None:
    """A re-fetch would send a second code and void the view state."""
    session = FakeSession()
    login, _ = await _login_with(monkeypatch, session)
    gets_after_login = len(session.gets)


    async def fake_finish():
        return "sid-value"

    login.async_finish = fake_finish
    await login.async_submit_code("123456")

    assert len(session.gets) == gets_after_login
    # One post for the password, one for the code.
    assert len(session.posts) == 2
    url, posted = session.posts[-1]
    assert "MALoginFlowVFPage" in url
    assert posted["com.salesforce.visualforce.ViewState"] == "STATE"
    assert posted["mfapage:theForm:page:firstHidden:hidden"] == "1"
    assert posted["mfapage:theForm:page:sixthHidden:hidden"] == "6"


async def test_resending_reloads_the_page(monkeypatch) -> None:
    """Reloading is how another code gets sent."""
    session = FakeSession()
    login, _ = await _login_with(monkeypatch, session)

    await login.async_resend_code()

    assert len(session.gets) == 2


async def test_no_verification_step_skips_the_page_fetch(monkeypatch) -> None:
    """Some sign-ins may not challenge; nothing should be fetched then."""
    session = FakeSession()
    login, mfa_type = await _login_with(
        monkeypatch, session, result={"pageUrl": None}
    )

    assert mfa_type is None
    assert session.gets == []
    assert login.code_page_url is None


FRONTDOOR_BOUNCE = (
    '<html><head><title>Redirect</title></head><body>'
    '<script>window.location.replace('
    "'https://myaccount.yvw.com.au/myaccount/s/?amp;x=1');</script>"
    "</body></html>"
)

META_BOUNCE = (
    '<html><head><meta http-equiv="Refresh" '
    'content="0; url=/myaccount/apex/MALoginFlowVFPage?retURL=%2F"></head></html>'
)


def test_a_javascript_bounce_is_recognised() -> None:
    """frontdoor.jsp redirects from script, not with an HTTP status."""
    assert find_client_redirect(FRONTDOOR_BOUNCE) == (
        "https://myaccount.yvw.com.au/myaccount/s/?amp;x=1"
    )


def test_a_meta_refresh_is_recognised() -> None:
    assert find_client_redirect(META_BOUNCE) == (
        "/myaccount/apex/MALoginFlowVFPage?retURL=%2F"
    )


def test_a_page_with_no_bounce_returns_nothing() -> None:
    assert find_client_redirect(CODE_PAGE) is None


class HopSession(FakeSession):
    """Serve a different body per request, like a redirect chain."""

    def __init__(self, bodies: list[str], cookies: list[str] | None = None) -> None:
        super().__init__()
        self._bodies = bodies
        self._cookies = cookies or []

    def get(self, url, **kwargs):
        self.gets.append(url)
        body = self._bodies[min(len(self.gets) - 1, len(self._bodies) - 1)]
        return FakeResponse(body, url=url)

    @property
    def cookie_jar(self):
        class C:
            def __init__(self, key):
                self.key = key
                self.value = "v"

        return [C(name) for name in self._cookies]


async def test_the_frontdoor_bounce_is_followed_to_the_code_page(monkeypatch) -> None:
    """This is what makes the portal send the code.

    Stopping at the bounce page leaves the user staring at a code box for a code
    that was never sent.
    """
    session = HopSession([FRONTDOOR_BOUNCE, META_BOUNCE, CODE_PAGE], cookies=["sid"])
    login, mfa_type = await _login_with(monkeypatch, session)

    assert mfa_type == "SMS"
    assert len(session.gets) == 3
    assert "MALoginFlowVFPage" in session.gets[-1]


async def test_a_signed_in_session_without_a_flow_needs_no_code(monkeypatch) -> None:
    """A trusted device may skip verification entirely."""
    session = HopSession(["<html>no bounce, no code fields</html>"], cookies=["sid"])
    login, mfa_type = await _login_with(monkeypatch, session)

    assert mfa_type is None
    assert login.code_page_url is None


async def test_a_chain_that_stops_short_is_an_error(monkeypatch) -> None:
    """Never present a code box when nothing was sent."""
    from custom_components.yvw.exceptions import YvwApiError as _ApiError

    session = HopSession(["<html>dead end</html>"], cookies=[])
    with pytest.raises(_ApiError, match="without reaching"):
        await _login_with(monkeypatch, session)
