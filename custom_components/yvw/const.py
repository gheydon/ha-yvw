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
