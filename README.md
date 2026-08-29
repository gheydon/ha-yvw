# Yarra Valley Water for Home Assistant

[![HACS: custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)
[![Release](https://img.shields.io/github/v/release/gheydon/ha-yvw?display_name=tag&sort=semver)](https://github.com/gheydon/ha-yvw/releases)
[![Validate](https://github.com/gheydon/ha-yvw/actions/workflows/validate.yml/badge.svg)](https://github.com/gheydon/ha-yvw/actions/workflows/validate.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)

[![Open your Home Assistant instance and open this repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=gheydon&repository=ha-yvw&category=integration)

Brings the hourly readings from a Yarra Valley Water digital meter into Home
Assistant, and writes them into long-term statistics so they show up on the
Water dashboard — including the hours that were recorded before Home Assistant
asked for them.

Requires a digital meter. If the Usage page of
[MyAccount](https://myaccount.yvw.com.au/) shows you an hourly chart, you have
one.

You sign in once with your MyAccount email, password and the code they text you.
**Your password is never saved** — only the session that sign-in produces, which
is what every later request uses. See [What is stored](#what-is-stored).

## Installing

Click the button above, which opens this repository straight in HACS on your own
Home Assistant. Then install "Yarra Valley Water" and restart.

By hand: HACS → three-dot menu → **Custom repositories** → add
`https://github.com/gheydon/ha-yvw` with category **Integration**, then install
"Yarra Valley Water" and restart Home Assistant.

Or copy `custom_components/yvw` into your `config/custom_components/` directory
and restart.

## Setting up

[![Open your Home Assistant instance and start setting up a new integration.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=yvw)

Or: **Settings → Devices & services → Add integration → Yarra Valley Water.**

1. Enter your MyAccount email and password.
2. Enter the verification code Yarra Valley Water texts you. Each code works
   once, so use the newest message.
3. Choose the property to follow, if your login covers more than one.

Properties are read from the portal, the same way its own account switcher does,
so there is nothing to look up. Closed accounts are listed too and labelled as
such, since they no longer have a meter reporting. If the list comes back empty,
setup asks for the account number instead — the 10-digit number on your bill.

## What is stored

**Your email address and password are never saved.** They are sent once, to the
portal's own sign-in, and discarded the moment that request returns. They are
not written to the config entry, not written to disk, and not kept in memory
after setup finishes.

The same goes for the SMS code: it is used once to complete the verification
step and then discarded.

What sign-in produces is a **session**, and that session is the only credential
kept. From that point on it is the only thing used to talk to Yarra Valley
Water — every reading, and every keep-alive, is that session and nothing else.
The integration cannot sign itself in again, by design and by necessity: the
portal issues a fresh SMS code on every sign-in, so a stored password would buy
nothing anyway.

The config entry holds exactly this:

```
sid            the session cookie
cookies        the rest of the cookies signing in established
account_id     your water account number
meter_serial   your meter's serial number
address        the property address, used to name the device
```

The session id alone is not enough: Salesforce pairs it with others set during
sign-in, and a client offering only the id is bounced to the login page — even
through `frontdoor.jsp`. Home Assistant builds a new cookie jar on every
restart, so the whole set is kept. Analytics cookies the portal's pages collect
are deliberately left out.

The CSRF token the portal also requires is not stored at all. It is read from
the portal on each page load, so it is always current.

When the session eventually lapses, Home Assistant asks you to sign in again —
email, password and a fresh code — and the new session replaces the old one in
place. That is the only time your password is needed again.

You can confirm all of this yourself:

```bash
python -c "import json;print(json.load(open('.storage/core.config_entries'))['data'])" | grep -i pass
```

run from your Home Assistant config directory. It finds nothing.

## Using it on the Water dashboard

**Settings → Dashboards → Energy → Water → Add water source**, and pick
**`<your address> water consumption`**.

Past readings will not show on an entity's History graph: they are long-term
statistics, which live on the Water dashboard and under **Developer Tools →
Statistics**. Readings arrive about a day late, so scroll back a day to see
them.

Use that statistic, not the sensors. The sensors exist to show you the latest
figures at a glance; the statistic is the complete hourly history. Adding both
would count the same water twice. Costs come from the tariff you configure on
that dashboard — the portal's own billing figures are not used.

## Sensors

| Sensor | What it shows |
| --- | --- |
| Latest hourly usage | Litres in the most recent hour the meter reported |
| Last full day usage | Litres across the most recent complete day |
| Last reading | When the meter last reported, which is the end of the hour it covers |

## Reacting to new readings

Every poll that records new hours fires a `yvw_new_readings` event, so an
automation can run when fresh consumption arrives rather than polling for it.
Nothing fires when a poll finds no new hours, which is most of them.

```yaml
automation:
  - alias: Warn about a heavy hour
    triggers:
      - trigger: event
        event_type: yvw_new_readings
    conditions:
      - condition: template
        value_template: "{{ trigger.event.data.litres > 200 }}"
    actions:
      - action: notify.persistent_notification
        data:
          message: >
            {{ trigger.event.data.count }} new hours recorded,
            {{ trigger.event.data.litres }} L up to
            {{ trigger.event.data.last_hour }}.
```

The event carries `count`, `litres`, `first_hour`, `last_hour`, `statistic_id`,
`meter_serial`, `account_id`, `address` and `entry_id`. Hours are the start of
the interval, in local time.

A `yvw_auth_failed` event fires when the session lapses. Getting it back needs a
person and an SMS code, so it is worth being told rather than noticing later
that readings stopped:

```yaml
automation:
  - alias: Yarra Valley Water needs signing in again
    triggers:
      - trigger: event
        event_type: yvw_auth_failed
    actions:
      - action: notify.persistent_notification
        data:
          title: Yarra Valley Water session expired
          message: >
            Sign in again to resume water readings
            (lapsed after {{ trigger.event.data.session_age }}).
```

It carries `detected_by` (`poll` or `keepalive`), `session_age`, `last_contact`,
and the same identifying fields. Home Assistant also raises its own repair and
re-authentication prompt at the same moment.

Every keep-alive attempt fires `yvw_keepalive` with its `outcome` — `ok`,
`failed` for something transient, `expired`, or `stalled` when the keep-alive is
found not to have run — along with `idle_minutes`,
`next_minutes`, `session_age`, `calibrating` and `measurement`. That is enough
to follow a timeout measurement from a notification rather than a log. A wake-up
that is skipped because a poll has already touched the portal fires nothing:
nothing was asked of it, so there is no outcome to report.

## Keeping the session alive

The portal has no API keys and no OAuth, and it sends a verification code on
every sign-in. So the integration holds onto the session it was given, touching
the portal periodically to stop it going idle. If the session lapses anyway, a
repair appears under **Settings → System → Repairs** asking you to sign in
again; nothing is recorded as zero usage in the meantime.

A session has been measured surviving a **70 minute** idle gap against the real
portal, so the default is to touch it every 30 minutes: a wide margin, at a
third of the requests the original guess made.

Losing the session matters more than the request count, because getting it back
needs a person and an SMS code. So the integration also watches that the
keep-alive is *running*: a loop that has stopped looks exactly like a healthy one
until the readings quietly stop, which is worth knowing about promptly. If the
portal has not been touched in well over the interval, that is reported as a
`stalled` outcome and the keep-alive is restarted.

How long a session survives untouched is not published, so the integration can
measure it. Turn on **Find the session timeout** under **Configure** and each
keep-alive waits a little longer than the last gap the session came back from,
climbing until one finally lapses. That brackets the real limit — "timed out
between 40 and 45 minutes idle" — and it then sets the interval to sit safely
inside that and switches itself off.

It ends by costing one verification code, which is the whole price of the
measurement, so run it when you are around to sign in again. The result appears
under **Configure** and in the diagnostics download. Until then the default is
to touch the session every 10 minutes, well inside Salesforce's shortest
possible idle timeout.

While measuring, each ping writes a line to the logbook — "session survived 45
minutes idle, testing 50 minutes next" — so a run can be followed from the
Home Assistant interface without reading a log file. A **Last keep-alive**
sensor records when the portal was last touched, and carries the current
interval and the measurement so far as attributes.

Debug logging additionally records how long each session lasted:

```yaml
logger:
  logs:
    custom_components.yvw: debug
```

Requests are made with browser headers and at slightly irregular intervals, and
the integration polls for readings only twice a day, so the traffic looks like
somebody keen on checking their water use rather than a script.

## Including the session in diagnostics

**Configure → Include the session in diagnostics.** Off by default, and it
should stay off unless someone is helping you debug.

Turned on, the diagnostics download carries your **live sign-in session in the
clear**, so that a problem can be reproduced against a real account. Anyone who
reads that file can use your account until the session lapses — and diagnostics
are the sort of thing people paste into public issues without thinking.

When it is on, the file says so in a heading at the top, and Home Assistant logs
a warning each time a download is taken. If you have shared one, sign out of
MyAccount to invalidate the session.

Everything else in the file — account number, address, meter serial — is
redacted whether this is on or off.

## Limits worth knowing

- **The portal only serves about 30 days of hourly history.** A first setup
  backfills up to that, and no further back. History accumulates in Home
  Assistant from then on.
- **Hours the meter did not report are skipped, not stored as zero.** The portal
  pads its response with zero-litre rows; recording those would invent readings.
- **Readings arrive about a day late.** That is the portal, not the integration.
- **Multiple properties on one login are unverified.** The code reads every
  property it can find and asks which to follow, but it has only been run
  against a single-property login. If yours covers several, see below.

## If your login covers several properties

Setup lists them and asks which to follow. Only one property can be followed per
config entry; add the integration again to follow another.

Very large numbers of properties are the one untested case: the portal pages its
account list, and nothing seen so far has needed a second page. If yours does,
the log says so and you can enter the account number by hand instead.

## For Yarra Valley Water

This integration exists because there is no supported way for a customer to get
at their own meter readings. It works by replaying what the MyAccount website
does, which is nobody's idea of a good arrangement — least of all ours. Three
changes would make it unnecessary, and each is modest.

**Offer OAuth 2.0 as an authentication method.** Today the only way in is to
post a password to the site's own sign-in and then complete an SMS verification
step, because that is what the website does. That means a person has to be
present for every sign-in, and it means software holding a session cookie that
is indistinguishable from a browser's. An authorisation-code flow with a
refresh token would let a customer grant read-only access to their own usage,
review it, and revoke it — without any third-party software ever seeing a
password or a verification code. It would be better for you than the present
arrangement, in which the safest available option is still a password prompt.

**Document two endpoints.** Only two are needed to do something useful:

- *list the accounts a login covers* — account number, address, and whether it
  is active
- *fetch metered usage* — an account, a meter, a date range, an interval, and
  readings back

Both already exist behind the website. Publishing them, with a stable contract,
would mean this integration stops depending on internal details that can change
without notice — an undocumented dispatcher, Visualforce form fields, a CSRF
token delivered in a cookie whose name is spelled backwards. When those change,
customers' integrations break and the first you hear of it is a support call.

**Submit your logo to the Home Assistant brands repository.** Home Assistant
shows an integration's icon from
[home-assistant/brands](https://github.com/home-assistant/brands). Yours is not
there, so this integration ships a droplet of its own invention — deliberately
sharing nothing with your mark, because it is not ours to use. Adding
`custom_integrations/yvw/icon.png` would put your actual brand in front of your
own customers. It is a pull request with two images in it, and it should come
from you rather than from us.

Happy to talk: open an issue on this repository, or get in touch with the
maintainer. If any of this is already planned or possible today, we would rather
delete the workarounds than keep them.

## Privacy

This integration does not want to know anything about you.

- **Nothing is sent anywhere except Yarra Valley Water.** The only host it ever
  contacts is `myaccount.yvw.com.au`, to read your own meter. There is no
  server behind this project, no account to create, and nowhere for your data
  to go.
- **No telemetry, no analytics, no crash reporting, no phoning home.** Not
  disabled by default — simply not written.
- **Your readings stay in your Home Assistant.** They go into your own recorder
  database and nowhere else.
- **Your password is never stored**, and the SMS code is used once and
  discarded. See [What is stored](#what-is-stored).
- **Nothing is collected from you by the author.** No usage counts, no install
  pings, no identifiers. If you open an issue, you choose what to put in it.

The one thing to be careful with is the **diagnostics download**. It describes
your account, with the account number, address, meter serial and session
redacted, so it is safe to share as it comes. See
[Including the session in diagnostics](#including-the-session-in-diagnostics)
before changing that.

Nothing here can be taken on trust, and it should not be: the source is short,
and every outbound request is made in `aura.py` and `auth.py`. Reading them is
the only assurance worth having.

## Logo

A droplet holding a valley and a river: a nod to the name rather than anything
belonging to Yarra Valley Water. It shares nothing with their logo — no leaf,
none of their colours, and no imitation of their wordmark.

The images live in `custom_components/yvw/brand/`, which Home Assistant reads
directly as of 2026.3; local brand images take priority over the ones served
from the brands CDN, so there is nothing to submit anywhere and no wait for a
review. Sources are in `assets/`, rendered with:

```bash
inkscape assets/icon.svg -o /tmp/icon.png -w 1024 -h 1024 --export-background-opacity=0
```

then trimmed of surplus transparent space and written out at 256 and 512.

## Development

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
uvx ruff check custom_components tests
```

## Licence

MIT. Not affiliated with or endorsed by Yarra Valley Water.
