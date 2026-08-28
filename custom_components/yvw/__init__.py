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
    CONF_METER_SERIAL,
    CONF_SID,
    PORTAL_TIMEZONE,
)
from .coordinator import YvwConfigEntry, YvwCoordinator
from .probe import ProbeStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: YvwConfigEntry) -> bool:
    """Set up Yarra Valley Water from a config entry."""
    # A dedicated session keeps the portal's cookies out of the shared jar.
    session = async_create_clientsession(hass)
    client = YvwAuraClient(session, entry.data[CONF_SID])

    # Readings are timestamped in the portal's local time, not UTC.
    portal_tz = await dt_util.async_get_time_zone(PORTAL_TIMEZONE)
    if portal_tz is None:  # pragma: no cover - the tz database always has this
        raise RuntimeError(f"Unknown timezone {PORTAL_TIMEZONE}")

    probe = ProbeStore(hass)
    await probe.async_load()

    coordinator = YvwCoordinator(
        hass,
        entry,
        YvwApi(client, portal_tz),
        account_id=entry.data[CONF_ACCOUNT_ID],
        meter_serial=entry.data[CONF_METER_SERIAL],
        address=entry.data[CONF_ADDRESS],
        portal_tz=portal_tz,
        probe=probe,
    )

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(coordinator.async_stop_keepalive)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    coordinator.async_start_keepalive()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: YvwConfigEntry) -> bool:
    """Unload a Yarra Valley Water config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: YvwConfigEntry) -> None:
    """Reload when the options change, so a new interval takes effect."""
    await hass.config_entries.async_reload(entry.entry_id)
