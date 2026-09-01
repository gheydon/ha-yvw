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

> **This is an independent project.** It is not made by, associated with,
> endorsed by, or supported by Yarra Valley Water. Please do not contact them
> about it — their support staff did not write it and cannot help with it.
> Anything to do with this integration belongs in
> [issues on this repository](https://github.com/gheydon/ha-yvw/issues).
> Contact Yarra Valley Water about your water, your bill or your meter, as you
> normally would.

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

Separately from the entry, the integration also keeps the last working Aura
context — the framework descriptor and CSRF token. Obtaining those fresh means
loading a portal page, which is the request most likely to be turned away, so a
restart that can reuse the previous one avoids the problem rather than needing
to survive it. The token carries no expiry of its own and lives as long as the
session behind it. Both are dropped when the integration is removed.

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

## When it looks for readings

A day's readings appear the following morning, at no time the portal publishes.
Rather than polling blindly around the clock, the integration aims at that:

- from **4am** it looks every **ten minutes** for yesterday's readings
- as soon as yesterday is complete it stops, and waits until tomorrow
- after **6 hours** of looking it gives up until tomorrow

Both the hour it starts and how long it keeps looking are under **Configure**,
so they survive upgrades. Starting earlier finds the readings sooner at the cost
of a few more attempts; a longer window suits a meter that publishes late.

When readings actually appear is not documented, and is worth measuring on your
own meter rather than assuming. On the one this was built against, looking at
4am found yesterday complete on the first attempt — so they had arrived some
time before that, and the hour they really land is still unknown. If you set the
start early and the first attempt of the day always succeeds, you can move it
earlier again; if the first few attempts come back empty, you have found roughly
when your meter publishes and can start there.

That last rule matters. A meter that reported only part of a day is never going
to finish it, and without a stopping point the integration would ask every ten
minutes until midnight for readings that are not coming. A window is never
allowed to run past midnight into the next day's.

In the ordinary case that is one or two requests a day, replacing a blind poll
every twelve hours that could sit half a day behind.

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
| Session | Whether the sign-in still works: `active` or `expired` |

The keep-alive has no sensor of its own — when it last ran is an attribute of
the session sensor, which is the thing it exists to protect.

### Seeing the session's history

The session sensor reports `active` or `expired`, which means its **History**
shows exactly how long the session was up, and how long it was down, as a
timeline rather than a graph. Its attributes carry the detail:

| attribute | |
|---|---|
| `since` | when it last changed between working and not |
| `hours_in_state` | how long it has been that way, counted from the sign-in rather than from the last restart |
| `signed_in_at`, `session_age` | when the sign-in was made, and its age |
| `expired_at` | when it lapsed, if it has |
| `last_contact`, `last_keepalive` | when the portal was last touched |
| `keepalive_interval`, `next_poll` | what it is doing next |

`hours_in_state` counts in whole hours deliberately. Counting minutes would
write a history entry every few minutes for a number nobody reads that
precisely.

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

A session has been measured surviving a **115 minute** idle gap against the real
portal without complaint, so the default is to touch it every hour: comfortably
inside what is proven, at a sixth of the requests the original guess made.

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

## If the session lapses while you are away

Nothing is lost, provided you sign in again within a month.

Readings are fetched from a rolling 30 day window and written into statistics by
the hour they belong to, so the next successful poll after signing back in fills
in everything missed. A fortnight away costs one sign-in and nothing else: the
gap is backfilled and the running total carries on from where it stopped.

The limit is the portal's, not this integration's. It only serves the last 30
days, so a gap longer than that loses the excess for good — there is nowhere to
fetch it from.

**So the rule is simple: sign in at least once every 30 days and your history
stays complete.** Nothing else is required of you. Whether the session lasted an
hour or a fortnight makes no difference to the data — only to how often you are
asked for a code.

That is worth being clear about, because it changes what the keep-alive is for.
It is a convenience that saves you sign-ins. It is not what protects your
readings; noticing promptly and signing back in is.

### A limit we cannot rule out

The portal may enforce a maximum session age regardless of activity — a
session that expires, say, 24 hours after sign-in no matter how recently it was
used. Nothing observed so far proves this either way: the longest session seen
alive was four hours old, and every expiry so far has been explained by
something else.

If such a limit exists, **no keep-alive interval can prevent it**, and the
honest consequence is that you would be asked for a code roughly once a day.
Tedious, but it costs nothing in readings: each sign-in backfills whatever was
missed while the session was gone.

The integration is instrumented to tell the two apart when it next happens. Both
the `yvw_auth_failed` event and the stored measurement record the session's
**age** alongside the idle gap that killed it:

- expiring at a consistent age regardless of idle time → a hard limit
- expiring after a long idle gap at any age → an idle timeout

If it does turn out to be a hard limit, chasing a longer keep-alive interval is
pointless and the effort belongs in making re-authentication quick instead —
which is the strongest argument yet for
[an OAuth flow with a refresh token](#yarra-valley-water-could-you-help).

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

## Yarra Valley Water, could you help?

If anyone from Yarra Valley Water reads this, here are four things that would
help, in rough order of how much difference they would make.

This integration exists because there is no supported way for a customer to get
at their own meter readings. It works by replaying what the MyAccount website
does, which is nobody's idea of a good arrangement — least of all ours. None of
what follows is a complaint about the portal, which does its job well. Each item
would simply let us stop working around it.

**Offer OAuth 2.0 as an authentication method.** Today the only way in is to
post a password to the site's own sign-in and then complete an SMS verification
step, because that is what the website does. That means a person has to be
present for every sign-in, and it means software holding a session cookie that
is indistinguishable from a browser's. An authorisation-code flow with a
refresh token would let a customer grant read-only access to their own usage,
review it, and revoke it — without any third-party software ever seeing a
password or a verification code. It would be better for you than the present
arrangement, in which the safest available option is still a password prompt.

While on the subject: **could you tell us the session policy?** How long a
session survives idle, and whether there is a maximum age regardless of
activity. We currently hold sessions open by touching the portal periodically,
having measured by experiment that one survives at least 70 minutes idle. If
there is a maximum age as well, that approach is pointless past it and customers
face a verification code roughly once a day — which is precisely the problem an
OAuth refresh token solves. Either way, knowing the numbers would let this
integration make far fewer requests than guessing does.

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

**None of this is effort spent on us, and none of it is wasted.** If a mobile
app is ever on the roadmap, this is work you will do regardless.
A native app cannot scrape its own website: it needs exactly this — a token
based sign-in it can hold onto, a call that lists the customer's properties, and
a call that returns metered usage for a date range. Everything asked for here is
a strict subset of what an app requires, and an app needs a good deal more
besides: bills, payments, concessions, faults, notifications.

So the question is not whether to build it, but whether the small part of it
that already exists gets written down. Publishing these two first has a
practical advantage too — a handful of technical customers will exercise the
contract, find the awkward edges, and report them, long before an app in the
store depends on it. That is free integration testing by people who are
motivated to be careful, and we would rather do it early than have an app ship
around the same problems we have already worked through.

Put plainly: whatever you build here you keep. It serves your own app, your own
web front end, and any future one, and it costs a customer-facing team nothing
to have a few of us using it first and telling you what we find.

**Submit your logo to the Home Assistant brands repository.** Home Assistant
shows an integration's icon from
[home-assistant/brands](https://github.com/home-assistant/brands). Yours is not
there, so this integration ships a droplet of its own invention — deliberately
sharing nothing with your mark, because it is not ours to use. Adding
`custom_integrations/yvw/icon.png` would put your actual brand in front of your
own customers. It is a pull request with two images in it, and it should come
from you rather than from us.

**Tell us when the previous day's readings are published.** Readings arrive
about a day late, but not at a time we can predict, so this integration polls
blindly and re-reads a window of days each time to be sure it has not missed
any. If you could say when a day's meter data is normally available — even
roughly, "by 6am for the previous day" — every installation could poll once,
shortly after, instead of guessing. That is fewer requests on your servers, not
more, and it is the one item on this list that costs you nothing but a sentence
of documentation.

**And an offer.** I would be glad to work with you on any of this. If there is
an API in progress, I will happily build against it, test it against a real
account and report back before it ships. If an OAuth flow needs a client
registration, I will do the Home Assistant side of it. If you would rather this
integration worked differently — a different name, different branding, a rate
limit it should respect, endpoints it should leave alone — say so and it will.
The aim is for Yarra Valley Water customers to have the best Home Assistant
integration of any water utility, and that is much easier with you than around
you.

Open an issue on this repository, or get in touch with the maintainer. If any of
this is already planned or possible today, we would rather delete the
workarounds than keep them.

To be clear about what this project is: it is written by a customer, for
customers, and it is not affiliated with or endorsed by Yarra Valley Water. We
are not asking you to support it — only to make it unnecessary, or to make it
better together.

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

### Do say hello

A consequence of collecting nothing is that there is no way of knowing whether
anybody is using this. If you are, I would genuinely like to hear about it —
[open an issue](https://github.com/gheydon/ha-yvw/issues) and say so.

It is welcome for its own sake, and it is also useful. How many properties your
login covers, when your meter publishes each morning, whether your session
behaves like mine: those are the things that shaped this integration, and every
one of them was worked out from a single account. A second account is worth more
than any amount of guessing.

Bug reports and unhappy experiences are just as welcome as the other kind.

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
