"""Remembering the Aura context across restarts.

Every request needs a framework descriptor and a CSRF token, and the only way to
obtain them fresh is to load a portal page. A restart therefore begins with
nothing in hand, and if that page load bounces to the login screen — which it
does whenever the portal will not accept the cookies from a client it has not
seen before — there is no way forward but asking the user for an SMS code.

The token does not expire on its own: it carries ``exp: 0``, and lives as long
as the session behind it. So keeping the last one that worked lets a restart
carry on where it left off, without loading a page at all. A descriptor that has
gone stale announces itself, and is recovered from in the ordinary way.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .aura import AuraContext
from .const import CONTEXT_STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


class ContextStore:
    """Persist the last working Aura context for each config entry.

    Kept out of the config entry: it changes on every page load, and writing it
    there would reload the integration each time.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the store."""
        self._store: Store[dict[str, dict[str, Any]]] = Store(
            hass, STORAGE_VERSION, CONTEXT_STORAGE_KEY
        )
        self._contexts: dict[str, AuraContext] = {}

    async def async_load(self) -> None:
        """Load whatever was kept from before."""
        data = await self._store.async_load() or {}
        for entry_id, raw in data.items():
            context, token = raw.get("context"), raw.get("token")
            if isinstance(context, dict) and isinstance(token, str):
                self._contexts[entry_id] = AuraContext(context=context, token=token)

    def get(self, entry_id: str) -> AuraContext | None:
        """Return the context kept for an entry, if there is one."""
        return self._contexts.get(entry_id)

    async def async_save(self, entry_id: str, aura: AuraContext) -> None:
        """Keep a context that has just been established."""
        held = self._contexts.get(entry_id)
        if held is not None and held.token == aura.token and held.context == aura.context:
            return
        self._contexts[entry_id] = aura
        await self._async_write()

    async def async_forget(self, entry_id: str) -> None:
        """Drop what was kept for an entry that no longer exists."""
        if self._contexts.pop(entry_id, None) is not None:
            await self._async_write()

    async def _async_write(self) -> None:
        await self._store.async_save(
            {
                entry_id: {"context": aura.context, "token": aura.token}
                for entry_id, aura in self._contexts.items()
            }
        )
