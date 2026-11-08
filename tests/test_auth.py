"""Tests for the sign-in helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.yvw.api import account_ids_in
from custom_components.yvw.auth import (
    build_code_form,
    code_destination,
    describe_mfa_channel,
    find_client_redirect,
)
from custom_components.yvw.exceptions import YvwApiError, YvwInvalidCode

# Reduced from a real verification page. The portal renders a mobile block
# first and removes it by script on a desktop theme, so both sets of ordinal
# fields are present in the markup a client sees, and only the second set is
# the one to post. View state values are placeholders; the rest keeps the shape
# the portal serves, including the masked destination inside its own span.
CODE_PAGE = (Path(__file__).parent / "fixtures" / "verification_page.html").read_text()


def test_code_is_split_across_the_six_digit_fields() -> None:
    """The page takes one digit per field, in order."""
    payload = build_code_form(CODE_PAGE, "123456")

    assert payload["mfapage:theForm:page:firstHidden"] == "1"
    assert payload["mfapage:theForm:page:sixthHidden"] == "6"


def test_visualforce_state_is_echoed_back() -> None:
    """Visualforce rejects a postback that loses its view state."""
    payload = build_code_form(CODE_PAGE, "123456")

    assert payload["com.salesforce.visualforce.ViewState"] == "STATE"
    assert payload["com.salesforce.visualforce.ViewStateMAC"] == "MAC"
    # The pressed button identifies the action to Visualforce.
    assert "mfapage:theForm:page:j_id73:j_id82" in payload


def test_generated_field_names_are_not_assumed() -> None:
    """Visualforce renames its generated ids between deployments."""
    renamed = CODE_PAGE.replace("j_id73:j_id82", "j_id99:j_id01")

    payload = build_code_form(renamed, "123456")

    assert "mfapage:theForm:page:j_id99:j_id01" in payload


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


# doLogin hands back frontdoor.jsp, which trades its session id for cookies and
# then bounces onward from script.
DO_LOGIN_OK = {
    "mfaType": "SMS",
    "pageUrl": "/myaccount/secur/frontdoor.jsp?sid=abc&retURL=%2Fmyaccount%2Fs%2F",
}


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
    # The chain starts at frontdoor.jsp, which is where the session is minted.
    assert len(session.gets) == 1
    assert "frontdoor.jsp" in session.gets[0]


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
    assert posted["mfapage:theForm:page:firstHidden"] == "1"
    assert posted["mfapage:theForm:page:sixthHidden"] == "6"


async def test_resending_reloads_the_page(monkeypatch) -> None:
    """Reloading is how another code gets sent."""
    session = FakeSession()
    login, _ = await _login_with(monkeypatch, session)

    await login.async_resend_code()

    assert len(session.gets) == 2


async def test_no_verification_step_skips_the_page_fetch(monkeypatch) -> None:
    """Some sign-ins may not challenge; nothing should be fetched then."""
    session = FakeSession()
    login, mfa_type = await _login_with(monkeypatch, session, result={"pageUrl": None})

    assert mfa_type is None
    assert session.gets == []
    assert login.code_page_url is None


FRONTDOOR_BOUNCE = (
    "<html><head><title>Redirect</title></head><body>"
    "<script>window.location.replace("
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
    assert find_client_redirect(META_BOUNCE) == ("/myaccount/apex/MALoginFlowVFPage?retURL=%2F")


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


async def test_an_unreadable_flow_page_is_not_mistaken_for_no_code(monkeypatch) -> None:
    """The code has been sent by this point, so skipping entry strands the user.

    Treating it as 'no verification needed' fails later with something
    unrelated, which is exactly what happened in the field.
    """
    from custom_components.yvw.exceptions import YvwApiError as _ApiError

    session = HopSession(
        [FRONTDOOR_BOUNCE, META_BOUNCE, "<html><body>Enter your code</body></html>"],
        cookies=["sid"],
    )
    with pytest.raises(_ApiError, match="A code was sent"):
        await _login_with(monkeypatch, session)


def test_code_fields_are_found_by_shape_when_the_names_differ() -> None:
    """The ordinal names came from a browser DOM, not the served markup."""
    page = (
        '<form action="/x"><input type="hidden" name="com.salesforce.visualforce.ViewState"'
        ' value="STATE" />'
        + "".join(f'<input type="text" name="code_{index}" maxlength="1" />' for index in range(6))
        + '<input type="submit" name="go" value="Submit" /></form>'
    )

    payload = build_code_form(page, "246810")

    assert [payload[f"code_{index}"] for index in range(6)] == list("246810")
    assert payload["com.salesforce.visualforce.ViewState"] == "STATE"
    assert payload["go"] == "Submit"


async def test_a_refused_code_is_reported_as_such(monkeypatch) -> None:
    """The portal re-renders the form rather than saying no in any other way.

    Reporting it as an unexpected error hides the one thing the user can act
    on, which is that the code did not take.
    """
    session = HopSession([FRONTDOOR_BOUNCE, META_BOUNCE, CODE_PAGE], cookies=["sid"])
    login, _ = await _login_with(monkeypatch, session)
    session._post_body = CODE_PAGE

    # A refused code leaves the session parked on the login flow, so confirming
    # it is what reveals the refusal.
    async def finish_rejects():
        raise YvwInvalidCode("The verification step was not completed")

    login.async_finish = finish_rejects

    with pytest.raises(YvwInvalidCode):
        await login.async_submit_code("123456")

    # The returned form is kept so a retry needs no new code.
    assert login.code_page_html == CODE_PAGE


def test_hidden_code_fields_are_preferred_over_visible_boxes() -> None:
    """Scripts copy what is typed into hidden fields, and those get posted.

    Filling only the visible boxes posts six empty hidden fields, which the
    portal reads as no code at all.
    """
    page = (
        '<form action="/x"><input type="hidden" name="ViewState" value="S" />'
        + "".join(
            f'<input type="text" name="visible_{i}" maxlength="1" />'
            f'<input type="hidden" name="store_{i}" maxlength="1" />'
            for i in range(6)
        )
        + '<input type="submit" name="go" value="Submit" /></form>'
    )

    payload = build_code_form(page, "123456")

    assert [payload[f"store_{i}"] for i in range(6)] == list("123456")
    assert all(payload[f"visible_{i}"] == "" for i in range(6))


def test_the_portals_own_wording_is_extracted() -> None:
    """Its explanation is more use than a generic message of ours."""
    from custom_components.yvw.auth import page_message

    html = (
        "<html><body><h1>Enter your code</h1>"
        '<span class="error">The code you entered is incorrect</span>'
        "<script>var x = 'invalid';</script></body></html>"
    )

    assert page_message(html) == "The code you entered is incorrect"


def test_a_page_with_nothing_to_say_yields_nothing() -> None:
    from custom_components.yvw.auth import page_message

    assert page_message("<html><body><p>Enter your code</p></body></html>") is None


A4J_ONCLICK = (
    "return A4J.AJAX.Submit('mfapage:theForm',event,"
    "{'similarityGroupingId':'mfapage:theForm:page:j_id73:j_id82','html5Validate':true,"
    "'oncomplete':function(request,event,data){checkError()},"
    "'parameters':{'mfapage:theForm:page:j_id73:j_id82':"
    "'mfapage:theForm:page:j_id73:j_id82'} ,"
    "'status':'mfapage:theForm:page:pageStatus'} );"
)


def test_an_ajax_submit_button_is_recognised() -> None:
    """The button calls into AJAX4JSF rather than posting the form."""
    from custom_components.yvw.auth import a4j_parameters

    assert a4j_parameters(A4J_ONCLICK) == {
        "AJAXREQUEST": "mfapage:theForm",
        "mfapage:theForm:page:j_id73:j_id82": "mfapage:theForm:page:j_id73:j_id82",
    }


def test_a_plain_button_is_left_alone() -> None:
    from custom_components.yvw.auth import a4j_parameters

    assert a4j_parameters("doSomethingElse()") is None
    assert a4j_parameters("") is None


def test_the_ajax_parameters_are_posted_instead_of_the_button_label() -> None:
    """Posting the label runs no action: the page just re-renders.

    That is indistinguishable from a refused code, which is what made this hard
    to spot.
    """
    page = (
        '<form action="/myaccount/MALoginFlowVFPage">'
        '<input type="hidden" name="com.salesforce.visualforce.ViewState" value="S" />'
        + "".join(
            f'<input type="hidden" name="mfapage:theForm:page:{ordinal}Hidden" value="" />'
            for ordinal in ("first", "second", "third", "fourth", "fifth", "sixth")
        )
        + f'<input type="submit" name="mfapage:theForm:page:j_id73:j_id82"'
        f' value="Submit" onclick="{A4J_ONCLICK}" /></form>'
    )

    payload = build_code_form(page, "373054")

    assert payload["AJAXREQUEST"] == "mfapage:theForm"
    # The button echoes its own client id, not its label.
    assert payload["mfapage:theForm:page:j_id73:j_id82"] == "mfapage:theForm:page:j_id73:j_id82"
    assert payload["mfapage:theForm:page:firstHidden"] == "3"
    assert payload["mfapage:theForm:page:sixthHidden"] == "4"


@pytest.mark.parametrize(
    ("mfa_type", "expected"),
    [
        ("SMS", " sent by text message"),
        ("sms", " sent by text message"),
        ("EMAIL", " sent by email"),
        ("TOTP", " from your authenticator app"),
        ("authenticator", " from your authenticator app"),
    ],
)
def test_the_channel_the_portal_named_is_spelled_out(mfa_type: str, expected: str) -> None:
    """Only the portal knows which channel an account uses."""
    assert describe_mfa_channel(mfa_type) == expected


@pytest.mark.parametrize("mfa_type", [None, "", "   "])
def test_saying_nothing_beats_naming_the_wrong_channel(mfa_type: str | None) -> None:
    """With no channel named the sentence has to stay silent about where to look."""
    assert describe_mfa_channel(mfa_type) == ""


def test_an_unfamiliar_channel_is_passed_through() -> None:
    """A channel nobody has seen still tells the user more than a guess would."""
    assert describe_mfa_channel("Voice Call") == " sent by voice call"


async def test_the_channel_is_kept_for_the_code_step(monkeypatch) -> None:
    """The code form is shown long after doLogin answered, so it has to be kept."""
    session = FakeSession()
    login, _ = await _login_with(monkeypatch, session)

    assert login.mfa_type == "SMS"


async def test_no_channel_is_kept_when_no_code_was_sent(monkeypatch) -> None:
    """A sign-in that skipped verification has no channel to report."""
    session = FakeSession()
    login, _ = await _login_with(monkeypatch, session, result={"pageUrl": None})

    assert login.mfa_type is None


async def test_a_channel_that_is_not_text_is_ignored(monkeypatch) -> None:
    """The field is remote input, so a surprise type must not reach the wording."""
    session = FakeSession()
    login, mfa_type = await _login_with(
        monkeypatch, session, result={**DO_LOGIN_OK, "mfaType": {"unexpected": True}}
    )

    assert mfa_type is None
    assert login.mfa_type is None
    assert describe_mfa_channel(login.mfa_type) == ""


def test_the_masked_destination_is_read_from_the_page() -> None:
    """Which phone or inbox to check is the one thing only the page knows."""
    assert code_destination(CODE_PAGE) == "*******506"


def test_the_mobile_fields_are_not_mistaken_for_the_code_fields() -> None:
    """The portal renders a mobile set first and strips it by script.

    Both sets are in the markup a client parses, and posting the digits into
    the block a desktop browser would have removed is not what the page does.
    """
    payload = build_code_form(CODE_PAGE, "123456")

    assert payload["mfapage:theForm:page:firstHidden"] == "1"
    assert all("MobileHidden" not in name or payload[name] == "" for name in payload)


def test_an_address_keeps_the_dots_in_it() -> None:
    """Stopping at the first full stop would cut an email address in half."""
    page = (
        '<p>Please type the verification code sent to '
        '<span class="font-family-bold">g****@heydon.com.au.</span></p>'
    )

    assert code_destination(page) == "g****@heydon.com.au"


def test_a_page_that_does_not_say_where_yields_nothing() -> None:
    """Only the wording for a text message has been captured."""
    page = '<p>Open your authenticator app and enter the code it shows.</p>'

    assert code_destination(page) is None


@pytest.mark.parametrize("html", [None, "", "<p></p>", "<p>code sent to </p>"])
def test_nothing_to_read_is_not_an_error(html: str | None) -> None:
    """A page shape nobody has seen must not stop the sign-in."""
    assert code_destination(html) is None


def test_the_channel_and_the_destination_read_as_one_sentence() -> None:
    """Both are known on the code page, and together they say where to look."""
    assert describe_mfa_channel("SMS", "*******506") == " sent by text message to *******506"


def test_a_destination_alone_still_says_where() -> None:
    """The page names the target even when doLogin named no channel."""
    assert describe_mfa_channel(None, "*******506") == " sent to *******506"


def test_an_authenticator_app_is_not_given_a_destination() -> None:
    """A code waiting in an app was not sent anywhere to be read off."""
    assert describe_mfa_channel("TOTP", "*******506") == " from your authenticator app"
