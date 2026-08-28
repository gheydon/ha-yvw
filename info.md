# Yarra Valley Water

Hourly readings from a Yarra Valley Water digital meter, written into Home
Assistant's long-term statistics so they appear on the Water dashboard — history
included.

- Backfills the hourly usage the portal still holds (about 30 days)
- Heals gaps after Home Assistant has been offline
- Sign in with your MyAccount email, password and the code they text you
- Your password is never stored, only the resulting session

Requires a digital meter: if the Usage page of MyAccount shows an hourly chart,
you have one.

After installing, add the **`<your address> water consumption`** statistic as a
water source on the Energy dashboard, and set your tariff there.
