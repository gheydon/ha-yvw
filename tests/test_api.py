"""Tests for the portal's usage parsing.

The portal's conventions are easy to get subtly wrong and the failures are
silent: readings land on the wrong hour, or hours the meter never reported get
recorded as zero consumption. These tests pin the conventions down.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from itertools import pairwise

import pytest

from custom_components.yvw.api import YvwApi
from custom_components.yvw.exceptions import YvwApiError


def usage(usage_date: str, hour: str, litres: float, status: str = "ACTUAL") -> dict:
    """Build one entry as the portal returns it."""
    return {
        "usageDate": usage_date,
        "hourOfTheDay": hour,
        "usageInLitres": litres,
        "usage": litres / 1000,
        "usageMetric": "kl",
        "measureStatus": status,
    }


class StubClient:
    """Record the arguments of each call and replay canned responses."""

    def __init__(self, responses: list[dict] | None = None) -> None:
        self.calls: list[dict] = []
        self._responses = responses or []

    async def async_invoke(self, controller, method, argument=None):
        self.calls.append({"controller": controller, "method": method, "argument": argument})
        if self._responses:
            return self._responses.pop(0)
        return {"dmUsageResponse": {"status": "Success", "usages": []}}


def response(entries: list[dict], status: str = "Success") -> dict:
    return {"dmUsageResponse": {"status": status, "usages": entries}}


async def test_hour_label_is_the_end_of_the_interval(portal_tz) -> None:
    """A reading labelled 13:00 covers 12:00-13:00, so it starts at 12:00."""
    client = StubClient([response([usage("2026-08-27", "13:00", 43)])])
    api = YvwApi(client, portal_tz)

    readings = await api.async_get_hourly_usage(
        "1234567890", "YAW0000001", date(2026, 8, 27), date(2026, 8, 27)
    )

    assert len(readings) == 1
    assert readings[0].start == datetime(2026, 8, 27, 12, 0, tzinfo=portal_tz)
    assert readings[0].litres == 43


async def test_midnight_belongs_to_the_previous_day(portal_tz) -> None:
    """00:00 on the 28th is the 23:00-00:00 hour of the 27th."""
    client = StubClient([response([usage("2026-08-28", "00:00", 3)])])
    api = YvwApi(client, portal_tz)

    readings = await api.async_get_hourly_usage(
        "1234567890", "YAW0000001", date(2026, 8, 27), date(2026, 8, 28)
    )

    assert readings[0].start == datetime(2026, 8, 27, 23, 0, tzinfo=portal_tz)


async def test_unreported_hours_are_skipped_not_recorded_as_zero(portal_tz) -> None:
    """Padding rows carry usage 0; recording them would invent consumption."""
    client = StubClient(
        [
            response(
                [
                    usage("2026-08-27", "13:00", 43),
                    usage("2026-08-26", "13:00", 0, status="NOT AVAILABLE"),
                ]
            )
        ]
    )
    api = YvwApi(client, portal_tz)

    readings = await api.async_get_hourly_usage(
        "1234567890", "YAW0000001", date(2026, 8, 26), date(2026, 8, 27)
    )

    assert [r.litres for r in readings] == [43]


async def test_out_of_range_window_is_not_treated_as_zero_usage(portal_tz) -> None:
    """The portal reports an unavailable window as a status, not an error."""
    client = StubClient([response([], status="Not available")])
    api = YvwApi(client, portal_tz)

    readings = await api.async_get_hourly_usage(
        "1234567890", "YAW0000001", date(2026, 8, 27), date(2026, 8, 27)
    )

    assert readings == []


async def test_requests_are_clamped_to_the_portals_thirty_day_horizon(portal_tz) -> None:
    """Asking for a year of history must not produce a rejected request."""
    client = StubClient()
    api = YvwApi(client, portal_tz)
    end = date(2026, 8, 28)

    await api.async_get_hourly_usage("1234567890", "YAW0000001", end - timedelta(days=365), end)

    assert len(client.calls) == 1
    argument = client.calls[0]["argument"]
    assert argument["dateFrom"] == (end - timedelta(days=30)).isoformat()
    assert argument["dateTo"] == end.isoformat()
    assert argument["interval"] == "hourly"


async def test_readings_are_sorted_and_deduplicated(portal_tz) -> None:
    """Overlapping chunks must not produce the same hour twice."""
    client = StubClient(
        [
            response(
                [
                    usage("2026-08-27", "14:00", 10),
                    usage("2026-08-27", "13:00", 43),
                    usage("2026-08-27", "13:00", 43),
                ]
            )
        ]
    )
    api = YvwApi(client, portal_tz)

    readings = await api.async_get_hourly_usage(
        "1234567890", "YAW0000001", date(2026, 8, 27), date(2026, 8, 27)
    )

    assert [r.start.hour for r in readings] == [12, 13]


async def test_missing_usage_payload_is_an_error(portal_tz) -> None:
    """A response without the usage envelope is a fault, not an empty day."""
    client = StubClient([{}])
    api = YvwApi(client, portal_tz)

    with pytest.raises(YvwApiError):
        await api.async_get_hourly_usage(
            "1234567890", "YAW0000001", date(2026, 8, 27), date(2026, 8, 27)
        )


async def test_account_details_are_read_from_the_meter_list(portal_tz) -> None:
    """Discovery needs the address and the meter serial."""

    class AccountClient(StubClient):
        async def async_invoke(self, controller, method, argument=None):
            return {
                "accountSearchResponse": [
                    {
                        "accountId": "1234567890",
                        "hasUsage": True,
                        "location": {"address": "1 Example St, Suburb, Vic, 3000"},
                        "meters": [{"meterSerialNumber": "YAW0000001"}],
                    }
                ]
            }

    api = YvwApi(AccountClient(), portal_tz)
    account = await api.async_get_account("1234567890")

    assert account.meter_serial == "YAW0000001"
    assert account.address == "1 Example St, Suburb, Vic, 3000"
    assert account.has_usage is True


async def test_a_reading_reports_at_the_end_of_its_hour(portal_tz) -> None:
    """The 23:00 hour is measured at midnight, which is when it was read.

    Statistics are filed against the start of the interval, but "last reading"
    means when the meter reported.
    """
    client = StubClient([response([usage("2026-08-28", "00:00", 3)])])
    api = YvwApi(client, portal_tz)

    readings = await api.async_get_hourly_usage(
        "1234567890", "YAW0000001", date(2026, 8, 27), date(2026, 8, 28)
    )

    assert readings[0].start == datetime(2026, 8, 27, 23, 0, tzinfo=portal_tz)
    assert readings[0].end == datetime(2026, 8, 28, 0, 0, tzinfo=portal_tz)


# --- The two mornings a year that are not twenty-four hours long ------------


async def test_the_morning_clocks_go_forward_keeps_every_hour(portal_tz) -> None:
    """On 4 October 2026 Melbourne jumps 02:00 to 03:00, so 02:00 never happens.

    Stepping back an hour from 03:00 on the clock face lands on that missing
    time, which resolves to the same instant as 03:00 itself. Two readings then
    claim one hour and the dictionary keeps only the last, losing an hour of
    consumption silently.
    """
    client = StubClient(
        [
            response(
                [
                    usage("2026-10-04", "01:00", 10),
                    usage("2026-10-04", "03:00", 20),
                    usage("2026-10-04", "04:00", 30),
                ]
            )
        ]
    )
    api = YvwApi(client, portal_tz)

    readings = await api.async_get_hourly_usage(
        "1234567890", "YAW0000001", date(2026, 10, 4), date(2026, 10, 4)
    )

    assert [r.litres for r in readings] == [10, 20, 30]
    # Compared as instants: a set of datetimes would collapse the two that show
    # the same clock face, which is the very thing being guarded against.
    starts = [r.start.timestamp() for r in readings]
    assert len(set(starts)) == 3, "two readings landed on the same hour"
    # Consecutive real hours. Measured on the timestamps on purpose: subtracting
    # two datetimes in one timezone compares clock faces, and across the change
    # that reads as two hours where only one passed.
    gaps = [(b - a) / 3600 for a, b in pairwise(starts)]
    assert gaps == [1.0, 1.0]


async def test_the_morning_clocks_go_back_keeps_both_repeats(portal_tz) -> None:
    """In April 02:00-03:00 happens twice, reported twice with the same label.

    The second is the later of the two. Without that distinction both parse to
    the same instant and one hour of consumption disappears.
    """
    client = StubClient(
        [
            response(
                [
                    usage("2027-04-04", "02:00", 11),
                    usage("2027-04-04", "02:00", 22),
                    usage("2027-04-04", "03:00", 33),
                ]
            )
        ]
    )
    api = YvwApi(client, portal_tz)

    readings = await api.async_get_hourly_usage(
        "1234567890", "YAW0000001", date(2027, 4, 4), date(2027, 4, 4)
    )

    assert [r.litres for r in readings] == [11, 22, 33]
    starts = [r.start.timestamp() for r in readings]
    assert len(set(starts)) == 3, "the repeated hour overwrote itself"
    gaps = [(b - a) / 3600 for a, b in pairwise(starts)]
    assert gaps == [1.0, 1.0]


async def test_an_ordinary_hour_is_unaffected(portal_tz) -> None:
    """The real-time stepping must not disturb the other three hundred odd days."""
    client = StubClient([response([usage("2026-08-27", "13:00", 43)])])
    api = YvwApi(client, portal_tz)

    readings = await api.async_get_hourly_usage(
        "1234567890", "YAW0000001", date(2026, 8, 27), date(2026, 8, 27)
    )

    assert readings[0].start == datetime(2026, 8, 27, 12, 0, tzinfo=portal_tz)
