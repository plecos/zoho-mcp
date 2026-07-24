from datetime import datetime, timezone

import pytest

from zoho_mcp.tools.calendar import get_event, get_freebusy, list_calendars, list_events


class FakeZohoClient:
    def __init__(self):
        self.list_events_calls = []
        self.list_events_result = [{"id": "evt-1", "title": "Sync"}]
        self.get_event_calls = []
        self.get_event_result = {"id": "evt-1", "title": "Sync", "attendees": []}
        self.list_calendars_calls = 0
        self.list_calendars_result = [{"id": "cal-1", "name": "ken"}]
        self.get_freebusy_calls = []
        self.get_freebusy_result = [{"start": "...", "end": "...", "status": "busy"}]

    async def list_events(self, start, end, calendar_id=None):
        self.list_events_calls.append(
            {"start": start, "end": end, "calendar_id": calendar_id}
        )
        return self.list_events_result

    async def get_event(self, uid, calendar_id=None):
        self.get_event_calls.append({"uid": uid, "calendar_id": calendar_id})
        return self.get_event_result

    async def list_calendars(self):
        self.list_calendars_calls += 1
        return self.list_calendars_result

    async def get_freebusy(self, email, start, end):
        self.get_freebusy_calls.append({"email": email, "start": start, "end": end})
        return self.get_freebusy_result


async def test_list_events_parses_iso8601_utc_strings_and_delegates():
    client = FakeZohoClient()

    result = await list_events(
        client, start="2024-10-29T16:00:00+00:00", end="2024-10-29T17:00:00+00:00"
    )

    assert client.list_events_calls == [
        {
            "start": datetime(2024, 10, 29, 16, 0, 0, tzinfo=timezone.utc),
            "end": datetime(2024, 10, 29, 17, 0, 0, tzinfo=timezone.utc),
            "calendar_id": None,
        }
    ]
    assert result == client.list_events_result


async def test_list_events_converts_non_utc_offset_to_utc():
    client = FakeZohoClient()

    await list_events(
        client, start="2024-10-29T11:00:00-05:00", end="2024-10-29T12:00:00-05:00"
    )

    assert client.list_events_calls == [
        {
            "start": datetime(2024, 10, 29, 16, 0, 0, tzinfo=timezone.utc),
            "end": datetime(2024, 10, 29, 17, 0, 0, tzinfo=timezone.utc),
            "calendar_id": None,
        }
    ]


async def test_list_events_rejects_malformed_date_string():
    client = FakeZohoClient()

    with pytest.raises(ValueError, match="ISO 8601"):
        await list_events(
            client, start="tomorrow morning", end="2024-10-29T17:00:00+00:00"
        )

    assert client.list_events_calls == []


async def test_list_events_rejects_naive_datetime_without_utc_offset():
    client = FakeZohoClient()

    with pytest.raises(ValueError, match="UTC offset"):
        await list_events(
            client, start="2024-10-29T16:00:00", end="2024-10-29T17:00:00+00:00"
        )

    assert client.list_events_calls == []


async def test_list_events_forwards_explicit_calendar_id():
    client = FakeZohoClient()

    await list_events(
        client,
        start="2024-10-29T16:00:00+00:00",
        end="2024-10-29T17:00:00+00:00",
        calendar_id="other-cal",
    )

    assert client.list_events_calls[0]["calendar_id"] == "other-cal"


async def test_get_event_delegates_to_client_with_uid():
    client = FakeZohoClient()

    result = await get_event(client, uid="evt-1")

    assert client.get_event_calls == [{"uid": "evt-1", "calendar_id": None}]
    assert result == client.get_event_result


async def test_get_event_forwards_explicit_calendar_id():
    client = FakeZohoClient()

    await get_event(client, uid="evt-1", calendar_id="other-cal")

    assert client.get_event_calls == [{"uid": "evt-1", "calendar_id": "other-cal"}]


async def test_list_calendars_delegates_to_client():
    client = FakeZohoClient()

    result = await list_calendars(client)

    assert client.list_calendars_calls == 1
    assert result == client.list_calendars_result


async def test_get_freebusy_parses_iso8601_and_delegates():
    client = FakeZohoClient()

    result = await get_freebusy(
        client,
        email="jamie@example.com",
        start="2026-07-21T00:00:00+00:00",
        end="2026-07-22T00:00:00+00:00",
    )

    assert client.get_freebusy_calls == [
        {
            "email": "jamie@example.com",
            "start": datetime(2026, 7, 21, tzinfo=timezone.utc),
            "end": datetime(2026, 7, 22, tzinfo=timezone.utc),
        }
    ]
    assert result == client.get_freebusy_result


async def test_get_freebusy_rejects_malformed_date_string():
    client = FakeZohoClient()

    with pytest.raises(ValueError, match="ISO 8601"):
        await get_freebusy(
            client,
            email="jamie@example.com",
            start="not-a-date",
            end="2026-07-22T00:00:00+00:00",
        )

    assert client.get_freebusy_calls == []
