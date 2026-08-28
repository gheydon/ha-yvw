# Yarra Valley Water for Home Assistant

Brings the hourly readings from a Yarra Valley Water digital meter into Home
Assistant, and writes them into long-term statistics so they show up on the
Water dashboard — including the hours that were recorded before Home Assistant
asked for them.

Requires a digital meter. If the Usage page of
[MyAccount](https://myaccount.yvw.com.au/) shows you an hourly chart, you have
one.

## Installing

HACS → three-dot menu → **Custom repositories** → add
`https://github.com/gheydon/ha-yvw` with category **Integration**, then install
"Yarra Valley Water" and restart Home Assistant.

Or copy `custom_components/yvw` into your `config/custom_components/` directory
and restart.

## Setting up

**Settings → Devices & services → Add integration → Yarra Valley Water.**

1. Enter your MyAccount email and password.
2. Enter the verification code Yarra Valley Water texts you. Each code works
   once, so use the newest message.
3. Choose the property to follow, or enter your account number if asked.

The portal only names your account once its own web app has run and cached it,
which a background client never does, so setup may ask for the number. It is the
10-digit number on your bill, shown on the portal beside your address.

Your password is used once, to sign in. It is **not stored** — only the
resulting session is kept, because the portal demands a fresh code every time
anyone signs in, so a saved password would not let the integration sign itself
back in anyway.

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

## Keeping the session alive

The portal has no API keys and no OAuth, and it sends a verification code on
every sign-in. So the integration holds onto the session it was given, touching
the portal periodically to stop it going idle. If the session lapses anyway, a
repair appears under **Settings → System → Repairs** asking you to sign in
again; nothing is recorded as zero usage in the meantime.

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

Debug logging additionally records how long each session lasted:

```yaml
logger:
  logs:
    custom_components.yvw: debug
```

Requests are made with browser headers and at slightly irregular intervals, and
the integration polls for readings only twice a day, so the traffic looks like
somebody keen on checking their water use rather than a script.

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

Set it up, then go to the integration, open the three-dot menu on the device and
choose **Download diagnostics**. The file describes the shape of the portal's
response — how many properties were found and where they sit in the payload —
with account numbers, addresses, meter serials and session credentials redacted.
Attaching it to an issue is enough to get property selection working properly,
and it does not expose your account.

## Logo

`brands/` holds the integration's mark: a droplet carrying a valley and a river,
which is a nod to the name rather than anything belonging to Yarra Valley Water.
It deliberately shares nothing with their logo — no leaf, none of their colours,
and no imitation of their wordmark.

Home Assistant serves integration logos from the
[brands repository](https://github.com/home-assistant/brands), so the icon will
only appear in the interface once `brands/icon.png` and `icon@2x.png` are
submitted there under `custom_integrations/yvw/`. The files are sized and
transparent, ready to go.

## Development

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
uvx ruff check custom_components tests
```

## Licence

MIT. Not affiliated with or endorsed by Yarra Valley Water.
