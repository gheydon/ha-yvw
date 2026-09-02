"""Learning when the portal publishes a day's readings.

The hour readings appear is not documented, is not the same for every meter,
and does not stay put: as more digital meters are rolled out the overnight batch
they belong to takes longer, so a time that is right today drifts later over
months. Asking each person to find their own hour and revisit it is not a good
answer.

So the start steers itself. Finding the readings on the very first attempt means
they were already waiting and the looking could have begun earlier; taking more
than an hour of attempts means it began too early and spent the difference on
requests that found nothing. Anything in between is the intended state and is
left alone.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    ADAPTIVE_EARLIEST_MINUTES,
    ADAPTIVE_LATEST_MINUTES,
    ADAPTIVE_STEP,
    ADAPTIVE_TARGET_MAX,
    SCHEDULE_STORAGE_KEY,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class LearnedStart:
    """When to start looking, as learned from how the last few days went."""

    minutes: int
    """Minutes after midnight."""

    learned_on: str = ""
    """The day the last adjustment was made, so it moves at most once a day."""

    @property
    def clock(self) -> str:
        """Describe the time in a form a person reads."""
        return f"{self.minutes // 60:02d}:{self.minutes % 60:02d}"


def adjust(minutes: int, took: timedelta) -> int:
    """Return where to start looking tomorrow, given how today went.

    ``took`` is how long after the window opened the readings were found.
    """
    if took <= timedelta(0):
        # Already waiting when the looking began, so it can begin earlier.
        moved = minutes - int(ADAPTIVE_STEP.total_seconds() // 60)
    elif took > ADAPTIVE_TARGET_MAX:
        # A long run of empty attempts: it began before there was anything.
        moved = minutes + int(ADAPTIVE_STEP.total_seconds() // 60)
    else:
        return minutes
    return max(ADAPTIVE_EARLIEST_MINUTES, min(moved, ADAPTIVE_LATEST_MINUTES))


class ScheduleStore:
    """Persist the learned start for each config entry."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the store."""
        self._store: Store[dict[str, dict]] = Store(
            hass, STORAGE_VERSION, SCHEDULE_STORAGE_KEY
        )
        self._starts: dict[str, LearnedStart] = {}

    async def async_load(self) -> None:
        """Load what has been learned so far."""
        data = await self._store.async_load() or {}
        for entry_id, raw in data.items():
            try:
                self._starts[entry_id] = LearnedStart(**raw)
            except TypeError:
                _LOGGER.debug("Discarding unreadable schedule for %s", entry_id)

    def get(self, entry_id: str) -> LearnedStart | None:
        """Return the learned start for an entry, if there is one."""
        return self._starts.get(entry_id)

    async def async_record(
        self, entry_id: str, minutes: int, took: timedelta, today: date
    ) -> LearnedStart:
        """Note how today went and return where to start tomorrow."""
        learned = self._starts.get(entry_id) or LearnedStart(minutes=minutes)
        if learned.learned_on == today.isoformat():
            return learned

        moved = adjust(learned.minutes, took)
        learned.minutes = moved
        learned.learned_on = today.isoformat()
        self._starts[entry_id] = learned
        await self._async_write()
        return learned

    async def async_forget(self, entry_id: str) -> None:
        """Drop what was learned for an entry that no longer exists."""
        if self._starts.pop(entry_id, None) is not None:
            await self._async_write()

    async def _async_write(self) -> None:
        await self._store.async_save(
            {entry_id: asdict(start) for entry_id, start in self._starts.items()}
        )
