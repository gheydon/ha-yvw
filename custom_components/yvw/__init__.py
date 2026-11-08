"""The Yarra Valley Water integration."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.util import dt as dt_util

from .api import YvwApi
from .aura import YvwAuraClient
from .const import (
    CONF_ACCOUNT_ID,
    CONF_ADDRESS,
    CONF_COOKIES,
    CONF_METER_SERIAL,
    CONF_SID,
    CONF_SIGNED_IN_AT,
    PORTAL_TIMEZONE,
)
from .context_store import ContextStore
from .coordinator import YvwConfigEntry, YvwCoordinator
from .exceptions import YvwAuthError, YvwError
from .probe import ProbeStore
from .schedule_store import ScheduleStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: YvwConfigEntry) -> bool:
    """Set up Yarra Valley Water from a config entry."""
    # A dedicated session keeps the portal's cookies out of the shared jar.
    session = async_create_clientsession(hass)
    contexts = ContextStore(hass)
    await contexts.async_load()

    async def _remember(aura) -> None:
        await contexts.async_save(entry.entry_id, aura)

    kept = contexts.get(entry.entry_id)
    # Which of these happens decides whether a restart needs the user, so it is
    # worth saying plainly rather than leaving it to be inferred from silence.
    _LOGGER.debug(
        "Starting %s",
        "from the context kept before the last shutdown"
        if kept
        else "with no kept context; a portal page will have to be loaded",
    )

    client = YvwAuraClient(
        session,
        entry.data[CONF_SID],
        entry.data.get(CONF_COOKIES),
        context=kept,
        on_context=_remember,
    )

    # Readings are timestamped in the portal's local time, not UTC.
    portal_tz = await dt_util.async_get_time_zone(PORTAL_TIMEZONE)
    if portal_tz is None:  # pragma: no cover - the tz database always has this
        raise RuntimeError(f"Unknown timezone {PORTAL_TIMEZONE}")

    api = YvwApi(client, portal_tz)
    meter_serial = await _async_current_meter_serial(hass, entry, api)

    probe = ProbeStore(hass)
    await probe.async_load()

    schedule = ScheduleStore(hass)
    await schedule.async_load()

    coordinator = YvwCoordinator(
        hass,
        entry,
        api,
        account_id=entry.data[CONF_ACCOUNT_ID],
        meter_serial=meter_serial,
        address=entry.data[CONF_ADDRESS],
        portal_tz=portal_tz,
        probe=probe,
        schedule=schedule,
        signed_in_at=dt_util.parse_datetime(entry.data.get(CONF_SIGNED_IN_AT) or ""),
    )

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.async_stop_keepalive)
    entry.async_on_unload(coordinator.async_stop_watchdog)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    coordinator.async_start_keepalive()
    coordinator.async_start_watchdog()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_current_meter_serial(
    hass: HomeAssistant, entry: YvwConfigEntry, api: YvwApi
) -> str:
    """Return the serial of the meter the account currently has.

    The serial kept at setup is only a starting point. When Yarra Valley Water
    replaces a meter (a digital meter going in for a mechanical one, say) the
    account gets a new serial, and the portal answers every usage request for
    the old one with "Not available" from then on, however the window is cut.
    So the account is asked each time the entry is set up, and a change is
    followed and written back to the entry.

    If the portal cannot be reached the kept serial is used; the first refresh
    will report the outage in the usual way.
    """
    kept: str = entry.data[CONF_METER_SERIAL]
    try:
        site = await api.async_get_account(entry.data[CONF_ACCOUNT_ID])
    except YvwAuthError:
        raise
    except YvwError as err:
        _LOGGER.debug("Could not check the meter serial with the portal (%s); using %s", err, kept)
        return kept
    if not site.meter_serial or site.meter_serial == kept:
        return kept
    _LOGGER.warning(
        "The meter on account %s is now %s (was %s); following the new meter",
        entry.data[CONF_ACCOUNT_ID],
        site.meter_serial,
        kept,
    )
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_METER_SERIAL: site.meter_serial}
    )
    return site.meter_serial


async def async_unload_entry(hass: HomeAssistant, entry: YvwConfigEntry) -> bool:
    """Unload a Yarra Valley Water config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: YvwConfigEntry) -> None:
    """Reload when the options change, so a new interval takes effect."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_remove_entry(hass: HomeAssistant, entry: YvwConfigEntry) -> None:
    """Forget anything kept for an entry being removed."""
    probe = ProbeStore(hass)
    await probe.async_load()
    await probe.async_forget(entry.entry_id)

    contexts = ContextStore(hass)
    await contexts.async_load()
    await contexts.async_forget(entry.entry_id)

    schedule = ScheduleStore(hass)
    await schedule.async_load()
    await schedule.async_forget(entry.entry_id)
