from datetime import datetime, timezone

import pytest

from zoho_mcp.tools.calendar import (
    create_event,
    delete_event,
    get_event,
    get_freebusy,
    list_calendars,
    list_events,
    update_event,
)


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
        self.create_event_calls = []
        self.create_event_result = {"id": "evt-new-1", "title": "Sync"}
        self.update_event_calls = []
        self.update_event_result = {"id": "evt-1", "title": "New Title"}
        self.delete_event_calls = []

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

    async def create_event(
        self,
        title,
        start,
        end,
        description="",
        location="",
        attendees=None,
        calendar_id=None,
    ):
        self.create_event_calls.append(
            {
                "title": title,
                "start": start,
                "end": end,
                "description": description,
                "location": location,
                "attendees": attendees,
                "calendar_id": calendar_id,
            }
        )
        return self.create_event_result

    async def update_event(
        self,
        uid,
        title=None,
        start=None,
        end=None,
        description=None,
        location=None,
        attendees=None,
        calendar_id=None,
    ):
        self.update_event_calls.append(
            {
                "uid": uid,
                "title": title,
                "start": start,
                "end": end,
                "description": description,
                "location": location,
                "attendees": attendees,
                "calendar_id": calendar_id,
            }
        )
        return self.update_event_result

    async def delete_event(self, uid, calendar_id=None):
        self.delete_event_calls.append({"uid": uid, "calendar_id": calendar_id})


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


async def test_create_event_parses_iso8601_and_delegates():
    client = FakeZohoClient()

    result = await create_event(
        client,
        title="Sync",
        start="2026-07-21T16:00:00+00:00",
        end="2026-07-21T17:00:00+00:00",
    )

    assert client.create_event_calls == [
        {
            "title": "Sync",
            "start": datetime(2026, 7, 21, 16, 0, 0, tzinfo=timezone.utc),
            "end": datetime(2026, 7, 21, 17, 0, 0, tzinfo=timezone.utc),
            "description": "",
            "location": "",
            "attendees": None,
            "calendar_id": None,
        }
    ]
    assert result == client.create_event_result


async def test_create_event_forwards_optional_fields():
    client = FakeZohoClient()

    await create_event(
        client,
        title="Sync",
        start="2026-07-21T16:00:00+00:00",
        end="2026-07-21T17:00:00+00:00",
        description="Quarterly review",
        location="Room 1",
        attendees=["jamie@example.com"],
        calendar_id="other-cal",
    )

    call = client.create_event_calls[0]
    assert call["description"] == "Quarterly review"
    assert call["location"] == "Room 1"
    assert call["attendees"] == ["jamie@example.com"]
    assert call["calendar_id"] == "other-cal"


async def test_create_event_rejects_malformed_date_string():
    client = FakeZohoClient()

    with pytest.raises(ValueError, match="ISO 8601"):
        await create_event(
            client,
            title="Sync",
            start="not-a-date",
            end="2026-07-21T17:00:00+00:00",
        )

    assert client.create_event_calls == []


async def test_update_event_forwards_given_fields_only():
    client = FakeZohoClient()

    result = await update_event(client, uid="evt-1", title="New Title")

    assert client.update_event_calls == [
        {
            "uid": "evt-1",
            "title": "New Title",
            "start": None,
            "end": None,
            "description": None,
            "location": None,
            "attendees": None,
            "calendar_id": None,
        }
    ]
    assert result == client.update_event_result


async def test_update_event_parses_start_and_end_when_given():
    client = FakeZohoClient()

    await update_event(
        client,
        uid="evt-1",
        start="2026-07-22T16:00:00+00:00",
        end="2026-07-22T17:00:00+00:00",
    )

    call = client.update_event_calls[0]
    assert call["start"] == datetime(2026, 7, 22, 16, 0, 0, tzinfo=timezone.utc)
    assert call["end"] == datetime(2026, 7, 22, 17, 0, 0, tzinfo=timezone.utc)


async def test_update_event_rejects_malformed_start_string():
    client = FakeZohoClient()

    with pytest.raises(ValueError, match="ISO 8601"):
        await update_event(client, uid="evt-1", start="not-a-date")

    assert client.update_event_calls == []


async def test_delete_event_delegates_to_client_with_uid():
    client = FakeZohoClient()

    result = await delete_event(client, uid="evt-1")

    assert client.delete_event_calls == [{"uid": "evt-1", "calendar_id": None}]
    assert result is None


async def test_delete_event_forwards_explicit_calendar_id():
    client = FakeZohoClient()

    await delete_event(client, uid="evt-1", calendar_id="cal-2")

    assert client.delete_event_calls == [{"uid": "evt-1", "calendar_id": "cal-2"}]
