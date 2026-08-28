"""Tests for the Aura transport.

The portal issues its CSRF token two different ways depending on the page, and
assuming only one of them breaks sign-in entirely.
"""

from __future__ import annotations

import pytest

from custom_components.yvw.aura import (
    extract_descriptor,
    extract_return_value,
    parse_aura_body,
    token_cookie_name,
)
from custom_components.yvw.exceptions import YvwApiError, YvwAuthError

# How a signed-in page names the cookie holding its token.
AUTHENTICATED_PAGE = (
    '<script>auraConfig = {"mode":"PROD","eikoocnekot":"__Host-ERIC_PROD-12345",'
    '"maxActionsPerXhr":250};\n'
    'cn = auraConfig["eikoocnekot"]; if (cn) { /* read and delete */ }\n'
    'delete auraConfig["eikoocnekot"]</script>'
)

# The login page carries the same bootstrap script but names no cookie.
LOGIN_PAGE = (
    '<script>auraConfig = {"mode":"PROD","exposeCsrfToken":false,'
    '"maxActionsPerXhr":250};\n'
    'cn = auraConfig["eikoocnekot"]; if (cn) { /* read and delete */ }\n'
    'else { if (auraConfig["token"] == null) { auraConfig["csrfV2"] = true } }</script>'
)


def test_a_signed_in_page_names_its_token_cookie() -> None:
    """The token is read from the cookie the page points at."""
    assert token_cookie_name(AUTHENTICATED_PAGE) == "__Host-ERIC_PROD-12345"


def test_the_login_page_names_no_cookie() -> None:
    """The bootstrap script mentions the field even when there is no cookie.

    Matching those mentions is what broke sign-in: the login page runs under
    csrfV2, where an empty token is correct.
    """
    assert token_cookie_name(LOGIN_PAGE) is None


def test_a_page_without_the_bootstrap_names_no_cookie() -> None:
    assert token_cookie_name("<html><body>Nothing here</body></html>") is None


def test_the_framework_descriptor_survives_nested_braces() -> None:
    """The descriptor nests a loaded object, so it cannot be matched lazily."""
    html = (
        '<script src="/myaccount/s/sfsites/l/%7B%22mode%22%3A%22PROD%22%2C'
        "%22fwuid%22%3A%22ABC123%22%2C%22app%22%3A%22siteforce%3AloginApp2%22%2C"
        "%22loaded%22%3A%7B%22APPLICATION%40markup%3A%2F%2Fsiteforce%3AloginApp2%22"
        '%3A%22999%22%7D%2C%22mlr%22%3A1%7D/resources.js"></script>'
    )
    descriptor = extract_descriptor(html)

    assert descriptor["fwuid"] == "ABC123"
    assert descriptor["app"] == "siteforce:loginApp2"
    assert descriptor["loaded"] == {"APPLICATION@markup://siteforce:loginApp2": "999"}


def test_the_anti_hijacking_prefix_is_stripped() -> None:
    assert parse_aura_body('while(1);{"actions":[]}') == {"actions": []}


def test_an_error_wrapper_is_stripped() -> None:
    assert parse_aura_body('*/{"exceptionMessage":"nope"}/*ERROR*/') == {
        "exceptionMessage": "nope"
    }


def test_an_expired_session_is_reported_as_an_auth_failure() -> None:
    """This is the signal that the user has to sign in again."""
    body = {
        "event": {"descriptor": "markup://aura:invalidSession"},
        "exceptionMessage": "Expected 3 tokens in all",
    }
    with pytest.raises(YvwAuthError):
        extract_return_value(body)


def test_a_rejected_login_is_reported_as_an_api_error() -> None:
    """The portal reports a bad password as a failed action, not an event."""
    body = {"actions": [{"state": "ERROR", "error": [{"message": "User Not Found"}]}]}
    with pytest.raises(YvwApiError, match="User Not Found"):
        extract_return_value(body)


def test_the_dispatcher_double_wrapping_is_unwrapped() -> None:
    body = {"actions": [{"state": "SUCCESS", "returnValue": {"returnValue": {"a": 1}}}]}
    assert extract_return_value(body) == {"a": 1}
