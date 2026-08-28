"""Measuring how long a portal session survives untouched.

Yarra Valley Water does not publish the idle timeout on its sessions, and the
cost of guessing runs both ways: ping too often and the integration is noisier
than it needs to be, ping too rarely and the session lapses, which costs the
user a verification code.

The only way to know is to measure. This records the longest gap a session has
actually survived, and the gap that finally killed one, which brackets the real
timeout.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ProbeState:
    """What has been learned about the session timeout so far."""

    survived_minutes: int = 0
    """Longest idle gap a session has come back from."""

    failed_minutes: int | None = None
    """Idle gap that found a session already gone."""

    failed_session_age_minutes: int | None = None
    """How old that session was, which separates an idle timeout from a cap on
    total session life."""

    @property
    def concluded(self) -> bool:
        """Return whether a timeout has been bracketed."""
        return self.failed_minutes is not None

    @property
    def summary(self) -> str:
        """Describe the finding in a line."""
        if not self.concluded:
            if not self.survived_minutes:
                return "no measurements yet"
            return f"survived {self.survived_minutes} min idle so far"
        return (
            f"timed out between {self.survived_minutes} and {self.failed_minutes} "
            f"minutes idle (session was {self.failed_session_age_minutes} min old)"
        )


class ProbeStore:
    """Persist probe results per config entry.

    Kept out of the entry's options deliberately: results change as often as
    every ping, and writing them there would reload the entry each time.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the store."""
        self._store: Store[dict[str, dict]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._states: dict[str, ProbeState] = {}

    async def async_load(self) -> None:
        """Load previously recorded results."""
        data = await self._store.async_load() or {}
        for entry_id, raw in data.items():
            try:
                self._states[entry_id] = ProbeState(**raw)
            except TypeError:
                _LOGGER.debug("Discarding unreadable probe state for %s", entry_id)

    def get(self, entry_id: str) -> ProbeState:
        """Return the state for an entry."""
        return self._states.setdefault(entry_id, ProbeState())

    async def async_record_survived(self, entry_id: str, minutes: int) -> bool:
        """Note a gap the session came back from, returning True if it is a best."""
        state = self.get(entry_id)
        if minutes <= state.survived_minutes:
            return False
        state.survived_minutes = minutes
        await self._async_save()
        return True

    async def async_record_failure(
        self, entry_id: str, minutes: int, session_age_minutes: int
    ) -> ProbeState:
        """Note the gap that found the session gone."""
        state = self.get(entry_id)
        state.failed_minutes = minutes
        state.failed_session_age_minutes = session_age_minutes
        await self._async_save()
        return state

    async def async_forget(self, entry_id: str) -> None:
        """Drop what was measured for an entry that no longer exists.

        Measurements are keyed by config entry, and a removed entry leaves its
        results behind forever otherwise.
        """
        if self._states.pop(entry_id, None) is not None:
            await self._async_save()

    async def _async_save(self) -> None:
        await self._store.async_save(
            {entry_id: asdict(state) for entry_id, state in self._states.items()}
        )
