"""Constants for the Yarra Valley Water integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "yvw"

# --- Portal endpoints -------------------------------------------------------

BASE_URL = "https://myaccount.yvw.com.au"
COMMUNITY_PATH = "/myaccount/s"
AURA_ENDPOINT = f"{BASE_URL}{COMMUNITY_PATH}/sfsites/aura"
USAGE_PAGE = f"{BASE_URL}{COMMUNITY_PATH}/usage"
LOGIN_PAGE = f"{BASE_URL}{COMMUNITY_PATH}/login/"

# Salesforce's own endpoint for how much idle time a session has left. The
# portal's session-timeout warning polls it. Its response shape is not
# documented, so it is only logged, never parsed for control flow.
SESSION_TIME_URL = f"{BASE_URL}/myaccount/_nc_external/system/security/session/SessionTimeServlet"

# Every Apex call is routed through one generic dispatcher.
DISPATCH_CLASS = "MyAccountGenericNavFWController"
DISPATCH_METHOD = "invokeMethod"

CONTROLLER_USAGE = "MyAccountUsageController"
METHOD_GET_USAGE = "getDMUsage"
CONTROLLER_ACCOUNT = "MyAccountGetAccountController"
METHOD_GET_ACCOUNT = "getAccountForSearch"

# Called directly rather than through the dispatcher; returns the account id
# currently selected in the portal, which seeds discovery during setup.
APEX_CACHE_CLASS = "MyAccountPlatformCacheController"
APEX_CACHE_METHOD = "getCache"
APEX_CACHE_KEY = "payload"

# Lists every account the signed-in person holds, with no arguments. This is
# what the portal's account switcher uses.
APEX_ACCOUNTS_CLASS = "MyAccountGetAccountController"
APEX_ACCOUNTS_METHOD = "getAccountsForPerson"

# Called with no arguments: the portal works out which account is meant from the
# session, so its answer names the account when the cache does not.
APEX_BALANCE_CLASS = "MyAccountBalanceController"
APEX_BALANCE_METHOD = "getAccountBalance"

# Sign-in is a direct Apex call too, answering with the MFA type and the URL of
# the verification-code page.
APEX_AUTH_CLASS = "MyAccountCommunityAuthController"
APEX_AUTH_METHOD = "doLogin"

AURA_APP = "siteforce:communityApp"

# MyAccount is a browser-only site with no API contract, and Salesforce edge
# rules are unfriendly to clients that do not look like one. Every request
# therefore carries a consistent set of Chrome headers.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-AU,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Upgrade-Insecure-Requests": "1",
}

# --- Config entry keys ------------------------------------------------------
# Only the session is persisted. The email/password are used once during the
# config flow to sign in and are deliberately never written to the entry.

CONF_SID = "sid"
CONF_ACCOUNT_ID = "account_id"
CONF_METER_SERIAL = "meter_serial"
CONF_ADDRESS = "address"

# Diagnostics normally redact the session. Turning this on puts it in the file
# in the clear, which is occasionally the only way to reproduce a problem
# against a real account — and is why it defaults to off and is announced
# loudly when it is not.
CONF_INCLUDE_SESSION = "include_session_in_diagnostics"
DEFAULT_INCLUDE_SESSION = False

# --- Behaviour --------------------------------------------------------------

# The portal publishes readings roughly a day late, so polling often gains
# nothing. Twice a day is enough to stay current and keeps the request count
# down: every poll is one page load plus one Apex call.
UPDATE_INTERVAL = timedelta(hours=12)

# The session expires server-side on idle, not in the browser: an untouched
# session was observed being refused with ec=302 at the login page after roughly
# half an hour, and the portal ships Salesforce's own session-timeout warning
# component, which only exists because the server enforces the limit. Salesforce
# allows a minimum idle timeout of 15 minutes, so ping inside that. The cost of
# pinging too often is one small POST; the cost of pinging too rarely is making
# the user request another SMS code.
DEFAULT_KEEPALIVE_MINUTES = 10
MIN_KEEPALIVE_MINUTES = 1
MAX_KEEPALIVE_MINUTES = 120

CONF_KEEPALIVE_MINUTES = "keepalive_minutes"

# Fraction of the interval the ping may be brought forward by, so the traffic
# is not perfectly periodic.
KEEPALIVE_JITTER = 0.2

# Calibration. The portal does not publish its idle timeout, so it can be found
# by holding a session open at steadily longer intervals until one lapses. That
# costs a verification code when it succeeds, which is why it is opt-in.
CONF_PROBE_ENABLED = "probe_session_timeout"
CONF_PROBE_STEP_MINUTES = "probe_step_minutes"
DEFAULT_PROBE_STEP_MINUTES = 5

# Once the timeout is known, sit this far inside it rather than on the edge.
PROBE_SAFETY_MARGIN = 0.75

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.probe"

# getDMUsage refuses any window starting more than 30 days before today.
MAX_HISTORY_DAYS = 30

# Readings are timestamped in the portal's local time, not UTC.
PORTAL_TIMEZONE = "Australia/Melbourne"

# usages[].measureStatus for a reading that actually happened. Anything else is
# zero-filled padding and must not be recorded.
STATUS_ACTUAL = "ACTUAL"

# dmUsageResponse.status when the requested window is outside the served range.
STATUS_NOT_AVAILABLE = "Not available"

# Fired once per poll that recorded new hours, so automations can react to
# fresh consumption data rather than polling entities.
EVENT_NEW_READINGS = f"{DOMAIN}_new_readings"

# Fired when the portal session lapses. Re-authenticating needs a person and an
# SMS code, so it is worth being able to notify rather than waiting to notice.
EVENT_AUTH_FAILED = f"{DOMAIN}_auth_failed"

# Fired after every keep-alive attempt, whatever came of it, so the run can be
# followed without reading a log.
EVENT_KEEPALIVE = f"{DOMAIN}_keepalive"
