from datetime import datetime, timezone

import pytest

from zoho_mcp.tools.calendar import list_events


class FakeZohoClient:
    def __init__(self):
        self.list_events_calls = []
        self.list_events_result = [{"id": "evt-1", "title": "Sync"}]

    async def list_events(self, start, end):
        self.list_events_calls.append({"start": start, "end": end})
        return self.list_events_result


async def test_list_events_parses_iso8601_utc_strings_and_delegates():
    client = FakeZohoClient()

    result = await list_events(
        client, start="2024-10-29T16:00:00+00:00", end="2024-10-29T17:00:00+00:00"
    )

    assert client.list_events_calls == [
        {
            "start": datetime(2024, 10, 29, 16, 0, 0, tzinfo=timezone.utc),
            "end": datetime(2024, 10, 29, 17, 0, 0, tzinfo=timezone.utc),
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
        }
    ]


async def test_list_events_rejects_malformed_date_string():
    client = FakeZohoClient()

    with pytest.raises(ValueError, match="ISO 8601"):
        await list_events(client, start="tomorrow morning", end="2024-10-29T17:00:00+00:00")

    assert client.list_events_calls == []


async def test_list_events_rejects_naive_datetime_without_utc_offset():
    client = FakeZohoClient()

    with pytest.raises(ValueError, match="UTC offset"):
        await list_events(
            client, start="2024-10-29T16:00:00", end="2024-10-29T17:00:00+00:00"
        )

    assert client.list_events_calls == []
