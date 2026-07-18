import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from zoho_mcp.zoho.client import ZohoAPIError, ZohoClient

ACCOUNT_ID = "acct-123"
CALENDAR_UID = "cal-556677"


class FakeTokenManager:
    async def get_access_token(self) -> str:
        return "fake-access-token"


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def zoho_client(http_client):
    return ZohoClient(
        token_manager=FakeTokenManager(),
        http_client=http_client,
        account_id=ACCOUNT_ID,
        calendar_uid=CALENDAR_UID,
    )


async def test_search_emails_calls_search_endpoint_with_auth_header(
    respx_mock, zoho_client
):
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": {"code": 200, "description": "success"},
                "data": [
                    {
                        "summary": "Let's sync on the Q3 roadmap tomorrow morning.",
                        "sentDateInGMT": "1730217600000",
                        "subject": "Q3 Roadmap Sync",
                        "messageId": "1730217600123456789",
                        "folderId": "1122334455",
                        "fromAddress": "jamie.rivera@example.com",
                    }
                ],
            },
        )
    )

    results = await zoho_client.search_emails(query="roadmap", limit=5)

    assert route.called
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Zoho-oauthtoken fake-access-token"
    assert request.url.params["searchKey"] == "roadmap"
    assert request.url.params["limit"] == "5"
    assert results == [
        {
            "id": "1730217600123456789",
            "from": "jamie.rivera@example.com",
            "subject": "Q3 Roadmap Sync",
            "date": "2024-10-29T16:00:00+00:00",
            "snippet": "Let's sync on the Q3 roadmap tomorrow morning.",
            "folder_id": "1122334455",
        }
    ]


async def test_get_email_calls_content_endpoint_with_folder_and_message_id(
    respx_mock, zoho_client
):
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}"
        f"/folders/1122334455/messages/1730217600123456789/content"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": {"code": 200, "description": "success"},
                "data": {
                    "messageId": "1730217600123456789",
                    "content": "<p>Hi Ken, let's sync tomorrow.</p>",
                },
            },
        )
    )

    result = await zoho_client.get_email(
        message_id="1730217600123456789", folder_id="1122334455"
    )

    assert route.called
    assert (
        route.calls.last.request.headers["Authorization"]
        == "Zoho-oauthtoken fake-access-token"
    )
    assert result["id"] == "1730217600123456789"
    assert "Hi Ken" in result["text"]


async def test_list_events_sends_json_encoded_range_param(respx_mock, zoho_client):
    route = respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "events": [
                    {
                        "uid": "evt-998877",
                        "title": "Q3 Roadmap Sync",
                        "dateandtime": {
                            "start": "20241029T160000Z",
                            "end": "20241029T170000Z",
                        },
                        "attendees": [],
                    }
                ]
            },
        )
    )
    start = datetime(2024, 10, 29, 16, 0, 0, tzinfo=timezone.utc)
    end = datetime(2024, 10, 29, 17, 0, 0, tzinfo=timezone.utc)

    results = await zoho_client.list_events(start=start, end=end)

    assert route.called
    sent_range = json.loads(route.calls.last.request.url.params["range"])
    assert sent_range == {"start": "20241029T160000Z", "end": "20241029T170000Z"}
    assert results == [
        {
            "id": "evt-998877",
            "title": "Q3 Roadmap Sync",
            "start": "2024-10-29T16:00:00+00:00",
            "end": "2024-10-29T17:00:00+00:00",
            "attendees": [],
        }
    ]


async def test_list_events_rejects_range_over_31_days_without_a_request(
    respx_mock, zoho_client
):
    route = respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events"
    )
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=32)

    with pytest.raises(ZohoAPIError, match="31 days"):
        await zoho_client.list_events(start=start, end=end)

    assert not route.called


async def test_list_events_rejects_end_before_start_without_a_request(
    respx_mock, zoho_client
):
    route = respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events"
    )
    start = datetime(2024, 1, 2, tzinfo=timezone.utc)
    end = datetime(2024, 1, 1, tzinfo=timezone.utc)

    with pytest.raises(ZohoAPIError, match="end must be after start"):
        await zoho_client.list_events(start=start, end=end)

    assert not route.called


async def test_list_events_rejects_end_equal_to_start_without_a_request(
    respx_mock, zoho_client
):
    route = respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events"
    )
    same_instant = datetime(2024, 1, 1, tzinfo=timezone.utc)

    with pytest.raises(ZohoAPIError, match="end must be after start"):
        await zoho_client.list_events(start=same_instant, end=same_instant)

    assert not route.called


async def test_search_emails_wraps_http_errors_as_zoho_api_error(
    respx_mock, zoho_client
):
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    ).mock(return_value=httpx.Response(401, json={"error": "invalid token"}))

    with pytest.raises(ZohoAPIError):
        await zoho_client.search_emails(query="roadmap")


@pytest.mark.parametrize("bad_limit", [0, -5, 201, 10_000])
async def test_search_emails_rejects_out_of_range_limit_without_a_request(
    respx_mock, zoho_client, bad_limit
):
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    )

    with pytest.raises(ZohoAPIError, match="limit"):
        await zoho_client.search_emails(query="roadmap", limit=bad_limit)

    assert not route.called


@pytest.mark.parametrize("edge_limit", [1, 200])
async def test_search_emails_accepts_boundary_limit_values(
    respx_mock, zoho_client, edge_limit
):
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    ).mock(return_value=httpx.Response(200, json={"data": []}))

    await zoho_client.search_emails(query="roadmap", limit=edge_limit)

    assert route.called


async def test_search_emails_returns_empty_list_when_data_key_absent(
    respx_mock, zoho_client
):
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    ).mock(return_value=httpx.Response(200, json={"status": {"code": 200}}))

    results = await zoho_client.search_emails(query="roadmap")

    assert results == []


async def test_list_events_returns_empty_list_when_events_key_absent(
    respx_mock, zoho_client
):
    respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events"
    ).mock(return_value=httpx.Response(200, json={}))
    start = datetime(2024, 10, 29, 16, 0, 0, tzinfo=timezone.utc)
    end = datetime(2024, 10, 29, 17, 0, 0, tzinfo=timezone.utc)

    results = await zoho_client.list_events(start=start, end=end)

    assert results == []
