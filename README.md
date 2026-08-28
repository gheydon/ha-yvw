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
2. Enter the verification code Yarra Valley Water texts you.
3. Choose the property to follow.

Your password is used once, to sign in. It is **not stored** — only the
resulting session is kept, because the portal demands a fresh code every time
anyone signs in, so a saved password would not let the integration sign itself
back in anyway.

## Using it on the Water dashboard

**Settings → Dashboards → Energy → Water → Add water source**, and pick
**`<your address> water consumption`**.

Use that statistic, not the sensors. The sensors exist to show you the latest
figures at a glance; the statistic is the complete hourly history. Adding both
would count the same water twice. Costs come from the tariff you configure on
that dashboard — the portal's own billing figures are not used.

## Sensors

| Sensor | What it shows |
| --- | --- |
| Latest hourly usage | Litres in the most recent hour the meter reported |
| Last full day usage | Litres across the most recent complete day |
| Last reading | When the meter last reported |

## Keeping the session alive

The portal has no API keys and no OAuth, and it sends a verification code on
every sign-in. So the integration holds onto the session it was given, touching
the portal periodically to stop it going idle. If the session lapses anyway, a
repair appears under **Settings → System → Repairs** asking you to sign in
again; nothing is recorded as zero usage in the meantime.

How long a session survives untouched is not published. The default is to touch
it every 10 minutes, which is well inside Salesforce's shortest possible idle
timeout. If you would rather it talked to Yarra Valley Water less often, raise
the interval under **Configure** on the integration — at the risk of the session
lapsing and needing another code. Turning on debug logging records how long each
session actually lasted, which is the way to find the real number:

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

## Development

```bash
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
uvx ruff check custom_components tests
```

## Licence

MIT. Not affiliated with or endorsed by Yarra Valley Water.
