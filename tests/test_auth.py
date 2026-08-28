"""Tests for the sign-in helpers."""

from __future__ import annotations

import pytest

from custom_components.yvw.api import account_ids_in
from custom_components.yvw.auth import build_code_form
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
