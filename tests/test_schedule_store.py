"""Tests for learning when the portal publishes readings.

When readings appear is not documented, differs between meters, and drifts
later as more are rolled out — so a fixed hour chosen today is wrong eventually
and the start has to steer itself.
"""

from __future__ import annotations

from datetime import date, timedelta

from homeassistant.core import HomeAssistant

from custom_components.yvw.const import ADAPTIVE_LATEST_MINUTES
from custom_components.yvw.schedule_store import ScheduleStore, adjust

TWO_AM = 2 * 60


def test_readings_already_waiting_move_the_start_earlier() -> None:
    """Found on the first attempt says nothing about how long they had been there."""
    assert adjust(TWO_AM, timedelta(0)) == TWO_AM - 30


def test_a_long_hunt_moves_the_start_later() -> None:
    """More than an hour of attempts is an hour of requests finding nothing."""
    assert adjust(TWO_AM, timedelta(hours=2)) == TWO_AM + 30


def test_finding_them_shortly_after_starting_is_left_alone() -> None:
    """This is the intended state: begin shortly before they appear."""
    assert adjust(TWO_AM, timedelta(minutes=20)) == TWO_AM


def test_the_start_never_moves_before_midnight() -> None:
    """Earlier than midnight would belong to the day before."""
    assert adjust(10, timedelta(0)) == 0


def test_the_start_never_drifts_past_mid_morning() -> None:
    """Something else is wrong by then, and chasing it would hide that."""
    assert adjust(ADAPTIVE_LATEST_MINUTES, timedelta(hours=3)) == ADAPTIVE_LATEST_MINUTES


async def test_what_is_learned_survives_a_restart(hass: HomeAssistant) -> None:
    """Days of observation would be lost otherwise."""
    store = ScheduleStore(hass)
    await store.async_load()
    await store.async_record("entry", TWO_AM, timedelta(0), date(2026, 9, 1))

    reopened = ScheduleStore(hass)
    await reopened.async_load()

    assert reopened.get("entry").minutes == TWO_AM - 30


async def test_the_start_moves_at_most_once_a_day(hass: HomeAssistant) -> None:
    """A restart, or a second poll, must not count as another morning."""
    store = ScheduleStore(hass)
    await store.async_load()
    today = date(2026, 9, 1)

    await store.async_record("entry", TWO_AM, timedelta(0), today)
    await store.async_record("entry", TWO_AM, timedelta(0), today)
    await store.async_record("entry", TWO_AM, timedelta(0), today)

    assert store.get("entry").minutes == TWO_AM - 30


async def test_it_walks_towards_the_hour_readings_appear(hass: HomeAssistant) -> None:
    """Several mornings of adjustment should converge rather than oscillate."""
    store = ScheduleStore(hass)
    await store.async_load()
    minutes = 6 * 60
    published_at = 3 * 60  # the meter publishes at three in the morning

    for day in range(1, 8):
        start = store.get("entry").minutes if store.get("entry") else minutes
        # Found immediately if the readings were already there, otherwise after
        # however long it takes for them to appear.
        took = timedelta(0) if start >= published_at else timedelta(
            minutes=published_at - start
        )
        await store.async_record("entry", start, took, date(2026, 9, day))

    settled = store.get("entry").minutes
    # Within half an hour of publication, and never before it by more than a step.
    assert published_at - 60 <= settled <= published_at + 30


def test_the_learned_time_reads_as_a_clock() -> None:
    """It is shown to a person on the options screen."""
    from custom_components.yvw.schedule_store import LearnedStart

    assert LearnedStart(minutes=90).clock == "01:30"
    assert LearnedStart(minutes=0).clock == "00:00"
