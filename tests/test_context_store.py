"""Tests for remembering the Aura context across restarts.

A restart begins with nothing in hand, and the page load that would supply a
fresh context is the request the portal is most likely to turn away. Keeping the
last one that worked is what lets a restart carry on rather than asking the user
for an SMS code.
"""

from __future__ import annotations

from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.yvw.aura import AuraContext
from custom_components.yvw.const import DOMAIN
from custom_components.yvw.context_store import ContextStore

ENTRY = "an-entry-id"
AURA = AuraContext(
    context={"fwuid": "FW1", "app": "siteforce:communityApp", "loaded": {}},
    token="header.body.signature",
)


async def test_a_context_survives_a_restart(hass: HomeAssistant) -> None:
    """This is the whole point: a new process starts with something usable."""
    store = ContextStore(hass)
    await store.async_load()
    await store.async_save(ENTRY, AURA)

    reopened = ContextStore(hass)
    await reopened.async_load()

    assert reopened.get(ENTRY) == AURA


async def test_nothing_kept_yet_is_not_an_error(hass: HomeAssistant) -> None:
    """A first run has nothing, and must simply load a page as before."""
    store = ContextStore(hass)
    await store.async_load()

    assert store.get(ENTRY) is None


async def test_an_unchanged_context_is_not_rewritten(hass: HomeAssistant) -> None:
    """Page loads are frequent; rewriting identical data each time is waste."""
    store = ContextStore(hass)
    await store.async_load()
    await store.async_save(ENTRY, AURA)

    writes: list[None] = []
    original = store._async_write

    async def counted() -> None:
        writes.append(None)
        await original()

    store._async_write = counted
    await store.async_save(ENTRY, AURA)

    assert writes == []


async def test_a_new_token_replaces_the_old_one(hass: HomeAssistant) -> None:
    """Tokens are reissued on each page load, and the newest is the one to keep."""
    store = ContextStore(hass)
    await store.async_load()
    await store.async_save(ENTRY, AURA)

    newer = AuraContext(context=AURA.context, token="a.newer.token")
    await store.async_save(ENTRY, newer)

    assert store.get(ENTRY) == newer


async def test_removing_an_entry_forgets_its_context(
    recorder_mock: Recorder, hass: HomeAssistant, custom_integration
) -> None:
    """A stored token is a credential; it should not outlive its entry."""
    store = ContextStore(hass)
    await store.async_load()
    await store.async_save(ENTRY, AURA)

    entry = MockConfigEntry(domain=DOMAIN, entry_id=ENTRY)
    await store.async_forget(entry.entry_id)

    reopened = ContextStore(hass)
    await reopened.async_load()
    assert reopened.get(ENTRY) is None
