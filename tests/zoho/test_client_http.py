import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import time_machine

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


def mock_pacific_accounts_endpoint(respx_mock):
    return respx_mock.get("https://mail.zoho.com/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "accountId": ACCOUNT_ID,
                        "isDefaultAccount": True,
                        "timeZone": "America/Los_Angeles",
                    }
                ]
            },
        )
    )


def mock_folder_types_endpoint(respx_mock, folder_types=None):
    folder_types = folder_types or {
        "1122334455": "Inbox",
        "sent-folder-id": "Sent",
        "drafts-folder-id": "Drafts",
        "templates-folder-id": "Templates",
        "newsletter-folder-id": "Inbox",
    }
    return respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/folders"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"folderId": fid, "folderName": fid, "folderType": ftype}
                    for fid, ftype in folder_types.items()
                ]
            },
        )
    )


def _raw_email(message_id: str, folder_id: str) -> dict:
    return {
        "messageId": message_id,
        "fromAddress": "someone@example.com",
        "subject": "Subject",
        "receivedTime": "1730217600000",
        "summary": "Snippet",
        "status": "1",
        "folderId": folder_id,
    }


async def test_search_emails_calls_search_endpoint_with_auth_header(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    mock_folder_types_endpoint(respx_mock)
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
                        "sentDateInGMT": "1730242800000",
                        "receivedTime": "1730217600000",
                        "subject": "Q3 Roadmap Sync",
                        "messageId": "1730217600123456789",
                        "folderId": "1122334455",
                        "fromAddress": "jamie.rivera@example.com",
                        "status": "0",
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
            # Mailbox's own offset, not UTC -- see _epoch_ms_to_iso8601.
            "date": "2024-10-29T09:00:00-07:00",
            "snippet": "Let's sync on the Q3 roadmap tomorrow morning.",
            "folder_id": "1122334455",
            "read": False,
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


async def test_get_email_passes_strip_invisible_chars_flag_through(
    respx_mock, http_client
):
    combining_grapheme_joiner = chr(0x034F)
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}"
        f"/folders/1122334455/messages/1730217600123456789/content"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "messageId": "1730217600123456789",
                    "content": f"<p>Hi{combining_grapheme_joiner} Ken</p>",
                },
            },
        )
    )
    stripping_client = ZohoClient(
        token_manager=FakeTokenManager(),
        http_client=http_client,
        account_id=ACCOUNT_ID,
        calendar_uid=CALENDAR_UID,
        strip_invisible_chars=True,
    )

    result = await stripping_client.get_email(
        message_id="1730217600123456789", folder_id="1122334455"
    )

    assert combining_grapheme_joiner not in result["text"]


async def test_list_events_sends_json_encoded_range_param(respx_mock, zoho_client):
    mock_pacific_accounts_endpoint(respx_mock)
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
    # The outgoing request range still uses UTC 'Z' -- that's the wire
    # format Zoho's API expects, independent of how results are displayed.
    assert sent_range == {"start": "20241029T160000Z", "end": "20241029T170000Z"}
    assert results == [
        {
            "id": "evt-998877",
            "title": "Q3 Roadmap Sync",
            # Mailbox's own offset, not UTC -- see _zoho_event_time_to_iso8601.
            "start": "2024-10-29T09:00:00-07:00",
            "end": "2024-10-29T10:00:00-07:00",
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
    mock_pacific_accounts_endpoint(respx_mock)
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
    mock_pacific_accounts_endpoint(respx_mock)
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    ).mock(return_value=httpx.Response(200, json={"data": []}))

    await zoho_client.search_emails(query="roadmap", limit=edge_limit)

    assert route.called


async def test_search_emails_returns_empty_list_when_data_key_absent(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    ).mock(return_value=httpx.Response(200, json={"status": {"code": 200}}))

    results = await zoho_client.search_emails(query="roadmap")

    assert results == []


async def test_search_emails_appends_fromDate_filter_for_days_back(
    respx_mock, zoho_client
):
    # 2026-07-18T02:00:00 UTC is still 2026-07-17 evening in Los Angeles --
    # this is the exact boundary case that caused the original bug when
    # "today" was computed from UTC instead of the mailbox's timezone.
    mock_pacific_accounts_endpoint(respx_mock)
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    ).mock(return_value=httpx.Response(200, json={"data": []}))

    with time_machine.travel(
        datetime(2026, 7, 18, 2, 0, 0, tzinfo=timezone.utc), tick=False
    ):
        await zoho_client.search_emails(query="", days_back=0)

    assert route.calls.last.request.url.params["searchKey"] == "fromDate:17-Jul-2026"


async def test_search_emails_combines_query_and_days_back_with_double_colon(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    ).mock(return_value=httpx.Response(200, json={"data": []}))

    with time_machine.travel(
        datetime(2026, 7, 18, 15, 0, 0, tzinfo=timezone.utc), tick=False
    ):
        await zoho_client.search_emails(query="subject:roadmap", days_back=1)

    assert (
        route.calls.last.request.url.params["searchKey"]
        == "subject:roadmap::fromDate:17-Jul-2026"
    )


async def test_search_emails_caches_mailbox_timezone_across_calls(
    respx_mock, zoho_client
):
    accounts_route = mock_pacific_accounts_endpoint(respx_mock)
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    ).mock(return_value=httpx.Response(200, json={"data": []}))

    with time_machine.travel(
        datetime(2026, 7, 18, 15, 0, 0, tzinfo=timezone.utc), tick=False
    ):
        await zoho_client.search_emails(query="", days_back=0)
        await zoho_client.search_emails(query="", days_back=1)

    assert accounts_route.call_count == 1


async def test_search_emails_looks_up_timezone_even_without_days_back(
    respx_mock, zoho_client
):
    # Results are normalized to the mailbox's local offset unconditionally
    # now (not just for days_back), since that's what fixed the LLM
    # displaying a raw UTC hour mislabeled as local time.
    accounts_route = mock_pacific_accounts_endpoint(respx_mock)
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    ).mock(return_value=httpx.Response(200, json={"data": []}))

    await zoho_client.search_emails(query="roadmap")

    assert accounts_route.call_count == 1


async def test_search_emails_filters_out_sent_drafts_and_templates(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    mock_folder_types_endpoint(respx_mock)
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _raw_email(message_id="1", folder_id="1122334455"),  # Inbox
                    _raw_email(message_id="2", folder_id="sent-folder-id"),
                    _raw_email(message_id="3", folder_id="drafts-folder-id"),
                    _raw_email(message_id="4", folder_id="templates-folder-id"),
                    # User-created/rule-filed folder, reports as folderType
                    # "Inbox" -- confirmed against the real API -- so it
                    # must survive the filter, not just Sent/Drafts/Templates.
                    _raw_email(message_id="5", folder_id="newsletter-folder-id"),
                ]
            },
        )
    )

    results = await zoho_client.search_emails(query="roadmap")

    assert {r["id"] for r in results} == {"1", "5"}


async def test_search_emails_skips_folder_filter_when_query_scopes_a_folder(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    folders_route = mock_folder_types_endpoint(respx_mock)
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"data": [_raw_email(message_id="2", folder_id="sent-folder-id")]},
        )
    )

    # Caller explicitly asked for Sent -- the exclusion filter must not
    # silently strip out the very folder they asked for.
    results = await zoho_client.search_emails(query="in:Sent")

    assert {r["id"] for r in results} == {"2"}
    assert folders_route.call_count == 0


async def test_search_emails_caches_folder_types_across_calls(respx_mock, zoho_client):
    mock_pacific_accounts_endpoint(respx_mock)
    folders_route = mock_folder_types_endpoint(respx_mock)
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"data": [_raw_email(message_id="1", folder_id="1122334455")]},
        )
    )

    await zoho_client.search_emails(query="roadmap")
    await zoho_client.search_emails(query="another")

    assert folders_route.call_count == 1


async def test_search_emails_does_not_fetch_folder_types_for_empty_results(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    ).mock(return_value=httpx.Response(200, json={"data": []}))

    # No mock_folder_types_endpoint at all -- if search_emails tried to
    # fetch it, respx would raise for the unmocked route.
    results = await zoho_client.search_emails(query="roadmap")

    assert results == []


async def test_search_emails_rejects_negative_days_back_without_a_request(
    respx_mock, zoho_client
):
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    )

    with pytest.raises(ZohoAPIError, match="days_back"):
        await zoho_client.search_emails(query="roadmap", days_back=-1)

    assert not route.called


async def test_search_emails_rejects_empty_query_and_no_days_back_without_a_request(
    respx_mock, zoho_client
):
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.search_emails(query="")

    assert not route.called


async def test_list_emails_calls_view_endpoint_with_correct_params(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    mock_folder_types_endpoint(respx_mock)
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/view"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"data": [_raw_email(message_id="1", folder_id="1122334455")]},
        )
    )

    results = await zoho_client.list_emails()

    assert route.called
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Zoho-oauthtoken fake-access-token"
    assert request.url.params["status"] == "all"
    assert request.url.params["start"] == "1"
    assert request.url.params["limit"] == "20"
    assert "folderId" not in request.url.params
    assert results == [
        {
            "id": "1",
            "from": "someone@example.com",
            "subject": "Subject",
            "date": "2024-10-29T09:00:00-07:00",
            "snippet": "Snippet",
            "folder_id": "1122334455",
            "read": True,
        }
    ]


async def test_list_emails_passes_status_start_and_limit_through(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    mock_folder_types_endpoint(respx_mock)
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/view"
    ).mock(return_value=httpx.Response(200, json={"data": []}))

    await zoho_client.list_emails(status="unread", limit=50, start=21)

    assert route.calls.last.request.url.params["status"] == "unread"
    assert route.calls.last.request.url.params["limit"] == "50"
    assert route.calls.last.request.url.params["start"] == "21"


async def test_list_emails_passes_folder_id_when_given(respx_mock, zoho_client):
    mock_pacific_accounts_endpoint(respx_mock)
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/view"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"data": [_raw_email(message_id="1", folder_id="folder-9")]},
        )
    )

    # No mock_folder_types_endpoint -- an explicit folder_id must skip the
    # exclusion-filter fetch entirely, same as search_emails' "in:" case.
    results = await zoho_client.list_emails(folder_id="folder-9")

    assert route.calls.last.request.url.params["folderId"] == "folder-9"
    assert {r["id"] for r in results} == {"1"}


@pytest.mark.parametrize("bad_status", ["", "READ", "unseen", "all "])
async def test_list_emails_rejects_invalid_status_without_a_request(
    respx_mock, zoho_client, bad_status
):
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/view"
    )

    with pytest.raises(ZohoAPIError, match="status"):
        await zoho_client.list_emails(status=bad_status)

    assert not route.called


@pytest.mark.parametrize("bad_limit", [0, -5, 201, 10_000])
async def test_list_emails_rejects_out_of_range_limit_without_a_request(
    respx_mock, zoho_client, bad_limit
):
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/view"
    )

    with pytest.raises(ZohoAPIError, match="limit"):
        await zoho_client.list_emails(limit=bad_limit)

    assert not route.called


@pytest.mark.parametrize("bad_start", [0, -1, -100])
async def test_list_emails_rejects_non_positive_start_without_a_request(
    respx_mock, zoho_client, bad_start
):
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/view"
    )

    with pytest.raises(ZohoAPIError, match="start"):
        await zoho_client.list_emails(start=bad_start)

    assert not route.called


async def test_list_emails_returns_empty_list_when_data_key_absent(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/view"
    ).mock(return_value=httpx.Response(200, json={"status": {"code": 200}}))

    results = await zoho_client.list_emails()

    assert results == []


async def test_list_emails_filters_out_sent_drafts_and_templates_by_default(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    mock_folder_types_endpoint(respx_mock)
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/view"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _raw_email(message_id="1", folder_id="1122334455"),
                    _raw_email(message_id="2", folder_id="sent-folder-id"),
                ]
            },
        )
    )

    results = await zoho_client.list_emails()

    assert {r["id"] for r in results} == {"1"}


async def test_list_emails_wraps_http_errors_as_zoho_api_error(respx_mock, zoho_client):
    mock_pacific_accounts_endpoint(respx_mock)
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/view"
    ).mock(return_value=httpx.Response(401, json={"error": "invalid token"}))

    with pytest.raises(ZohoAPIError):
        await zoho_client.list_emails()


async def test_get_event_fetches_by_uid_and_normalizes(respx_mock, zoho_client):
    route = respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-recurring-1"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "events": [
                    {
                        "uid": "evt-recurring-1",
                        "title": "Team Sync",
                        "organizer": "user@example.com",
                        "rrule": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO",
                        "location": "https://meet.example.com/abc",
                        "attendees": [
                            {"email": "jamie.rivera@example.com", "status": "accepted"},
                            {
                                "email": "morgan.lee@example.com",
                                "status": "needsaction",
                            },
                        ],
                    }
                ]
            },
        )
    )

    result = await zoho_client.get_event("evt-recurring-1")

    assert route.called
    assert (
        route.calls.last.request.headers["Authorization"]
        == "Zoho-oauthtoken fake-access-token"
    )
    assert result["id"] == "evt-recurring-1"
    assert result["organizer"] == "user@example.com"
    assert len(result["attendees"]) == 2


async def test_get_event_raises_clear_error_when_not_found(respx_mock, zoho_client):
    respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/does-not-exist"
    ).mock(return_value=httpx.Response(200, json={"events": []}))

    with pytest.raises(ZohoAPIError, match="does-not-exist"):
        await zoho_client.get_event("does-not-exist")


async def test_get_event_raises_clear_error_when_events_key_absent(
    respx_mock, zoho_client
):
    respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    ).mock(return_value=httpx.Response(200, json={}))

    with pytest.raises(ZohoAPIError, match="evt-1"):
        await zoho_client.get_event("evt-1")


async def test_get_event_wraps_http_errors_as_zoho_api_error(respx_mock, zoho_client):
    respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    ).mock(return_value=httpx.Response(401, json={"error": "invalid token"}))

    with pytest.raises(ZohoAPIError):
        await zoho_client.get_event("evt-1")


async def test_list_tasks_sends_limit_and_from_params(respx_mock, zoho_client):
    route = respx_mock.get("https://mail.zoho.com/api/tasks/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "paging": {},
                    "tasks": [
                        {"id": "1", "title": "Renew passport", "status": "In Progress"}
                    ],
                }
            },
        )
    )

    results, has_more = await zoho_client.list_tasks(limit=10, offset=5)

    assert route.called
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Zoho-oauthtoken fake-access-token"
    assert request.url.params["limit"] == "10"
    assert request.url.params["from"] == "5"
    assert [t["id"] for t in results] == ["1"]
    assert has_more is False


async def test_list_tasks_reports_has_more_when_next_page_present(
    respx_mock, zoho_client
):
    respx_mock.get("https://mail.zoho.com/api/tasks/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "paging": {"nextPage": "tasks/me?from=1&limit=1"},
                    "tasks": [{"id": "1", "title": "Task", "status": "Open"}],
                }
            },
        )
    )

    _, has_more = await zoho_client.list_tasks(limit=1)

    assert has_more is True


async def test_list_tasks_returns_empty_list_when_tasks_key_absent(
    respx_mock, zoho_client
):
    respx_mock.get("https://mail.zoho.com/api/tasks/me").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )

    results, has_more = await zoho_client.list_tasks()

    assert results == []
    assert has_more is False


async def test_list_tasks_rejects_limit_below_one_without_a_request(
    respx_mock, zoho_client
):
    route = respx_mock.get("https://mail.zoho.com/api/tasks/me")

    with pytest.raises(ZohoAPIError, match="limit"):
        await zoho_client.list_tasks(limit=0)

    assert not route.called


async def test_list_tasks_rejects_negative_offset_without_a_request(
    respx_mock, zoho_client
):
    route = respx_mock.get("https://mail.zoho.com/api/tasks/me")

    with pytest.raises(ZohoAPIError, match="offset"):
        await zoho_client.list_tasks(offset=-1)

    assert not route.called


async def test_list_tasks_wraps_http_errors_as_zoho_api_error(respx_mock, zoho_client):
    respx_mock.get("https://mail.zoho.com/api/tasks/me").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.list_tasks()


async def test_get_task_fetches_by_id_and_normalizes(respx_mock, zoho_client):
    route = respx_mock.get("https://mail.zoho.com/api/tasks/me/1001").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "tasks": [
                        {
                            "id": "1001",
                            "title": "Renew passport",
                            "status": "In Progress",
                        }
                    ]
                }
            },
        )
    )

    result = await zoho_client.get_task("1001")

    assert route.called
    assert (
        route.calls.last.request.headers["Authorization"]
        == "Zoho-oauthtoken fake-access-token"
    )
    assert result["id"] == "1001"
    assert result["title"] == "Renew passport"


async def test_get_task_wraps_http_errors_as_zoho_api_error(respx_mock, zoho_client):
    respx_mock.get("https://mail.zoho.com/api/tasks/me/does-not-exist").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.get_task("does-not-exist")


async def test_get_task_raises_clear_error_when_tasks_key_absent(
    respx_mock, zoho_client
):
    respx_mock.get("https://mail.zoho.com/api/tasks/me/1001").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )

    with pytest.raises(ZohoAPIError, match="1001"):
        await zoho_client.get_task("1001")


async def test_list_notes_sends_limit_and_after_params(respx_mock, zoho_client):
    mock_pacific_accounts_endpoint(respx_mock)
    route = respx_mock.get("https://mail.zoho.com/api/notes/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "list": [
                        {
                            "entityId": "1",
                            "title": "Dinner party ideas",
                            "createdTime": "1730217600000",
                            "modifiedTime": "1730217600000",
                        }
                    ]
                }
            },
        )
    )

    results = await zoho_client.list_notes(limit=10, after=5)

    assert route.called
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Zoho-oauthtoken fake-access-token"
    assert request.url.params["limit"] == "10"
    assert request.url.params["after"] == "5"
    assert [n["id"] for n in results] == ["1"]


async def test_list_notes_returns_empty_list_when_list_key_absent(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    respx_mock.get("https://mail.zoho.com/api/notes/me").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )

    results = await zoho_client.list_notes()

    assert results == []


async def test_list_notes_rejects_limit_below_one_without_a_request(
    respx_mock, zoho_client
):
    route = respx_mock.get("https://mail.zoho.com/api/notes/me")

    with pytest.raises(ZohoAPIError, match="limit"):
        await zoho_client.list_notes(limit=0)

    assert not route.called


async def test_list_notes_rejects_negative_after_without_a_request(
    respx_mock, zoho_client
):
    route = respx_mock.get("https://mail.zoho.com/api/notes/me")

    with pytest.raises(ZohoAPIError, match="after"):
        await zoho_client.list_notes(after=-1)

    assert not route.called


async def test_list_notes_wraps_http_errors_as_zoho_api_error(respx_mock, zoho_client):
    mock_pacific_accounts_endpoint(respx_mock)
    respx_mock.get("https://mail.zoho.com/api/notes/me").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.list_notes()


async def test_get_note_fetches_by_id_and_normalizes(respx_mock, zoho_client):
    mock_pacific_accounts_endpoint(respx_mock)
    route = respx_mock.get("https://mail.zoho.com/api/notes/me/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "entityId": "1",
                    "title": "Dinner party ideas",
                    "createdTime": "1730217600000",
                    "modifiedTime": "1730217600000",
                }
            },
        )
    )

    result = await zoho_client.get_note("1")

    assert route.called
    assert (
        route.calls.last.request.headers["Authorization"]
        == "Zoho-oauthtoken fake-access-token"
    )
    assert result["id"] == "1"
    assert result["title"] == "Dinner party ideas"


async def test_get_note_raises_clear_error_when_data_key_absent(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    respx_mock.get("https://mail.zoho.com/api/notes/me/1").mock(
        return_value=httpx.Response(200, json={"status": {"code": 200}})
    )

    with pytest.raises(ZohoAPIError, match="note"):
        await zoho_client.get_note("1")


async def test_get_note_wraps_http_errors_as_zoho_api_error(respx_mock, zoho_client):
    mock_pacific_accounts_endpoint(respx_mock)
    respx_mock.get("https://mail.zoho.com/api/notes/me/does-not-exist").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.get_note("does-not-exist")


async def test_list_bookmarks_sends_limit_and_after_params(respx_mock, zoho_client):
    route = respx_mock.get("https://mail.zoho.com/api/links/me").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "list": [
                        {
                            "entityId": "1",
                            "title": "Roadmap Template",
                            "link": "https://example.com",
                        }
                    ]
                }
            },
        )
    )

    results = await zoho_client.list_bookmarks(limit=10, after=5)

    assert route.called
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Zoho-oauthtoken fake-access-token"
    assert request.url.params["limit"] == "10"
    assert request.url.params["after"] == "5"
    assert [b["id"] for b in results] == ["1"]


async def test_list_bookmarks_returns_empty_list_when_list_key_absent(
    respx_mock, zoho_client
):
    respx_mock.get("https://mail.zoho.com/api/links/me").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )

    results = await zoho_client.list_bookmarks()

    assert results == []


async def test_list_bookmarks_rejects_limit_below_one_without_a_request(
    respx_mock, zoho_client
):
    route = respx_mock.get("https://mail.zoho.com/api/links/me")

    with pytest.raises(ZohoAPIError, match="limit"):
        await zoho_client.list_bookmarks(limit=0)

    assert not route.called


async def test_list_bookmarks_rejects_negative_after_without_a_request(
    respx_mock, zoho_client
):
    route = respx_mock.get("https://mail.zoho.com/api/links/me")

    with pytest.raises(ZohoAPIError, match="after"):
        await zoho_client.list_bookmarks(after=-1)

    assert not route.called


async def test_list_bookmarks_wraps_http_errors_as_zoho_api_error(
    respx_mock, zoho_client
):
    respx_mock.get("https://mail.zoho.com/api/links/me").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.list_bookmarks()


async def test_get_bookmark_fetches_by_id_and_normalizes(respx_mock, zoho_client):
    route = respx_mock.get("https://mail.zoho.com/api/links/me/1").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "entityId": "1",
                    "title": "Roadmap Template",
                    "link": "https://example.com",
                }
            },
        )
    )

    result = await zoho_client.get_bookmark("1")

    assert route.called
    assert (
        route.calls.last.request.headers["Authorization"]
        == "Zoho-oauthtoken fake-access-token"
    )
    assert result["id"] == "1"
    assert result["title"] == "Roadmap Template"


async def test_get_bookmark_raises_clear_error_when_data_key_absent(
    respx_mock, zoho_client
):
    respx_mock.get("https://mail.zoho.com/api/links/me/1").mock(
        return_value=httpx.Response(200, json={"status": {"code": 200}})
    )

    with pytest.raises(ZohoAPIError, match="bookmark"):
        await zoho_client.get_bookmark("1")


async def test_get_bookmark_wraps_http_errors_as_zoho_api_error(
    respx_mock, zoho_client
):
    respx_mock.get("https://mail.zoho.com/api/links/me/does-not-exist").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.get_bookmark("does-not-exist")


async def test_list_branches_fetches_and_normalizes(respx_mock, zoho_client):
    route = respx_mock.get("https://calendar.zoho.com/api/v1/branches").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "branch_id": "branch-1",
                    "branch_name": "Example Branch",
                    "time_zone": "America/Los_Angeles",
                    "buildings": [],
                }
            ],
        )
    )

    results = await zoho_client.list_branches()

    assert route.called
    assert (
        route.calls.last.request.headers["Authorization"]
        == "Zoho-oauthtoken fake-access-token"
    )
    assert results == [
        {
            "id": "branch-1",
            "name": "Example Branch",
            "timezone": "America/Los_Angeles",
            "buildings": [],
        }
    ]


async def test_list_branches_raises_clear_error_on_non_list_response(
    respx_mock, zoho_client
):
    respx_mock.get("https://calendar.zoho.com/api/v1/branches").mock(
        return_value=httpx.Response(200, json={"error": "not a list"})
    )

    with pytest.raises(ZohoAPIError, match="branches"):
        await zoho_client.list_branches()


async def test_list_branches_wraps_http_errors_as_zoho_api_error(
    respx_mock, zoho_client
):
    respx_mock.get("https://calendar.zoho.com/api/v1/branches").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.list_branches()


async def test_list_resources_sends_branch_building_floor_ids(respx_mock, zoho_client):
    route = respx_mock.get("https://calendar.zoho.com/api/v1/resources").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "resource_id": "resource-1",
                    "resource_name": "Meeting Room",
                    "res_email_id": "resource-1@example.com",
                    "capacity": 10,
                    "res_location": "Example Branch/Example Building/Floor 1",
                }
            ],
        )
    )

    results = await zoho_client.list_resources(
        branch_id="branch-1", building_id="building-1", floor_id="floor-1"
    )

    assert route.called
    request = route.calls.last.request
    assert request.url.params["branchId"] == "branch-1"
    assert request.url.params["buildingId"] == "building-1"
    assert request.url.params["floorId"] == "floor-1"
    assert results[0]["name"] == "Meeting Room"


async def test_list_resources_raises_clear_error_on_non_list_response(
    respx_mock, zoho_client
):
    respx_mock.get("https://calendar.zoho.com/api/v1/resources").mock(
        return_value=httpx.Response(200, json={"error": "not a list"})
    )

    with pytest.raises(ZohoAPIError, match="resources"):
        await zoho_client.list_resources(
            branch_id="branch-1", building_id="building-1", floor_id="floor-1"
        )


async def test_list_resources_wraps_http_errors_as_zoho_api_error(
    respx_mock, zoho_client
):
    respx_mock.get("https://calendar.zoho.com/api/v1/resources").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.list_resources(
            branch_id="branch-1", building_id="building-1", floor_id="floor-1"
        )


async def test_list_events_returns_empty_list_when_events_key_absent(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events"
    ).mock(return_value=httpx.Response(200, json={}))
    start = datetime(2024, 10, 29, 16, 0, 0, tzinfo=timezone.utc)
    end = datetime(2024, 10, 29, 17, 0, 0, tzinfo=timezone.utc)

    results = await zoho_client.list_events(start=start, end=end)

    assert results == []


async def test_list_events_uses_given_calendar_id_instead_of_default(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    other_calendar_route = respx_mock.get(
        "https://calendar.zoho.com/api/v1/calendars/other-cal/events"
    ).mock(return_value=httpx.Response(200, json={"events": []}))
    default_calendar_route = respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events"
    )
    start = datetime(2024, 10, 29, 16, 0, 0, tzinfo=timezone.utc)
    end = datetime(2024, 10, 29, 17, 0, 0, tzinfo=timezone.utc)

    await zoho_client.list_events(start=start, end=end, calendar_id="other-cal")

    assert other_calendar_route.called
    assert not default_calendar_route.called


async def test_get_event_uses_given_calendar_id_instead_of_default(
    respx_mock, zoho_client
):
    route = respx_mock.get(
        "https://calendar.zoho.com/api/v1/calendars/other-cal/events/evt-1"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "events": [
                    {"uid": "evt-1", "title": "Sync", "organizer": "user@example.com"}
                ]
            },
        )
    )

    result = await zoho_client.get_event("evt-1", calendar_id="other-cal")

    assert route.called
    assert result["id"] == "evt-1"


async def test_list_folders_fetches_and_normalizes(respx_mock, zoho_client):
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/folders"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "folderId": "folder-1",
                        "folderName": "Inbox",
                        "path": "/Inbox",
                        "folderType": "Inbox",
                    }
                ]
            },
        )
    )

    results = await zoho_client.list_folders()

    assert route.called
    assert (
        route.calls.last.request.headers["Authorization"]
        == "Zoho-oauthtoken fake-access-token"
    )
    assert results == [
        {"id": "folder-1", "name": "Inbox", "path": "/Inbox", "type": "Inbox"}
    ]


async def test_list_folders_returns_empty_list_when_data_key_absent(
    respx_mock, zoho_client
):
    respx_mock.get(f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/folders").mock(
        return_value=httpx.Response(200, json={"status": {"code": 200}})
    )

    results = await zoho_client.list_folders()

    assert results == []


async def test_list_folders_wraps_http_errors_as_zoho_api_error(
    respx_mock, zoho_client
):
    respx_mock.get(f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/folders").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.list_folders()


async def test_list_labels_fetches_and_normalizes(respx_mock, zoho_client):
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/labels"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "labelId": "label-1",
                        "displayName": "Notification",
                        "color": "#FFD700",
                    }
                ]
            },
        )
    )

    results = await zoho_client.list_labels()

    assert route.called
    assert (
        route.calls.last.request.headers["Authorization"]
        == "Zoho-oauthtoken fake-access-token"
    )
    assert results == [{"id": "label-1", "name": "Notification", "color": "#FFD700"}]


async def test_list_labels_returns_empty_list_when_data_key_absent(
    respx_mock, zoho_client
):
    respx_mock.get(f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/labels").mock(
        return_value=httpx.Response(200, json={"status": {"code": 200}})
    )

    results = await zoho_client.list_labels()

    assert results == []


async def test_list_labels_wraps_http_errors_as_zoho_api_error(respx_mock, zoho_client):
    respx_mock.get(f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/labels").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.list_labels()


async def test_list_signatures_fetches_and_normalizes(respx_mock, zoho_client):
    route = respx_mock.get("https://mail.zoho.com/api/accounts/signature").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "sig-1",
                        "name": "default",
                        "content": "<div>Regards,<br/>Jamie Rivera</div>",
                    }
                ]
            },
        )
    )

    results = await zoho_client.list_signatures()

    assert route.called
    assert (
        route.calls.last.request.headers["Authorization"]
        == "Zoho-oauthtoken fake-access-token"
    )
    assert results[0]["id"] == "sig-1"
    assert results[0]["name"] == "default"
    assert "Jamie Rivera" in results[0]["content"]


async def test_list_signatures_returns_empty_list_when_data_key_absent(
    respx_mock, zoho_client
):
    respx_mock.get("https://mail.zoho.com/api/accounts/signature").mock(
        return_value=httpx.Response(200, json={"status": {"code": 200}})
    )

    results = await zoho_client.list_signatures()

    assert results == []


async def test_list_signatures_wraps_http_errors_as_zoho_api_error(
    respx_mock, zoho_client
):
    respx_mock.get("https://mail.zoho.com/api/accounts/signature").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.list_signatures()


async def test_list_attachments_fetches_and_normalizes(respx_mock, zoho_client):
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}"
        f"/folders/1122334455/messages/1730217600123456789/attachmentinfo"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "messageId": "1730217600123456789",
                    "attachments": [
                        {
                            "attachmentId": "attach-1",
                            "attachmentName": "roadmap.pdf",
                            "attachmentSize": 666755,
                        }
                    ],
                }
            },
        )
    )

    results = await zoho_client.list_attachments(
        message_id="1730217600123456789", folder_id="1122334455"
    )

    assert route.called
    assert (
        route.calls.last.request.headers["Authorization"]
        == "Zoho-oauthtoken fake-access-token"
    )
    assert results == [{"id": "attach-1", "name": "roadmap.pdf", "size_bytes": 666755}]


async def test_list_attachments_returns_empty_list_when_none_present(
    respx_mock, zoho_client
):
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}"
        f"/folders/1122334455/messages/1730217600123456789/attachmentinfo"
    ).mock(
        return_value=httpx.Response(
            200, json={"data": {"messageId": "1730217600123456789"}}
        )
    )

    results = await zoho_client.list_attachments(
        message_id="1730217600123456789", folder_id="1122334455"
    )

    assert results == []


async def test_list_attachments_wraps_http_errors_as_zoho_api_error(
    respx_mock, zoho_client
):
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}"
        f"/folders/1122334455/messages/1730217600123456789/attachmentinfo"
    ).mock(return_value=httpx.Response(401, json={"error": "invalid token"}))

    with pytest.raises(ZohoAPIError):
        await zoho_client.list_attachments(
            message_id="1730217600123456789", folder_id="1122334455"
        )


async def test_mark_as_read_sends_correct_mode_and_message_ids(respx_mock, zoho_client):
    route = respx_mock.put(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/updatemessage"
    ).mock(return_value=httpx.Response(200, json={"status": {"code": 200}}))

    await zoho_client.mark_as_read(message_ids=["111", "222", "333"])

    assert route.called
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == {"mode": "markAsRead", "messageId": ["111", "222", "333"]}


async def test_mark_as_read_rejects_empty_message_ids_without_a_request(
    respx_mock, zoho_client
):
    with pytest.raises(ZohoAPIError, match="message_ids"):
        await zoho_client.mark_as_read(message_ids=[])

    assert not respx_mock.calls


async def test_mark_as_read_wraps_http_errors_as_zoho_api_error(
    respx_mock, zoho_client
):
    respx_mock.put(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/updatemessage"
    ).mock(return_value=httpx.Response(401, json={"error": "invalid token"}))

    with pytest.raises(ZohoAPIError):
        await zoho_client.mark_as_read(message_ids=["111"])


async def test_mark_as_unread_sends_correct_mode_and_message_ids(
    respx_mock, zoho_client
):
    route = respx_mock.put(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/updatemessage"
    ).mock(return_value=httpx.Response(200, json={"status": {"code": 200}}))

    await zoho_client.mark_as_unread(message_ids=["111", "222"])

    assert route.called
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == {"mode": "markAsUnread", "messageId": ["111", "222"]}


async def test_mark_as_unread_rejects_empty_message_ids_without_a_request(
    respx_mock, zoho_client
):
    with pytest.raises(ZohoAPIError, match="message_ids"):
        await zoho_client.mark_as_unread(message_ids=[])

    assert not respx_mock.calls


async def test_move_email_sends_correct_mode_and_dest_folder(respx_mock, zoho_client):
    route = respx_mock.put(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/updatemessage"
    ).mock(return_value=httpx.Response(200, json={"status": {"code": 200}}))

    await zoho_client.move_email(message_ids=["111", "222"], folder_id="1122334455")

    assert route.called
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == {
        "mode": "moveMessage",
        "messageId": ["111", "222"],
        "destfolderId": "1122334455",
    }


async def test_move_email_rejects_empty_message_ids_without_a_request(
    respx_mock, zoho_client
):
    with pytest.raises(ZohoAPIError, match="message_ids"):
        await zoho_client.move_email(message_ids=[], folder_id="1122334455")

    assert not respx_mock.calls


async def test_move_email_wraps_http_errors_as_zoho_api_error(respx_mock, zoho_client):
    respx_mock.put(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/updatemessage"
    ).mock(return_value=httpx.Response(401, json={"error": "invalid token"}))

    with pytest.raises(ZohoAPIError):
        await zoho_client.move_email(message_ids=["111"], folder_id="1122334455")


async def test_add_label_sends_correct_mode_and_label_id(respx_mock, zoho_client):
    route = respx_mock.put(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/updatemessage"
    ).mock(return_value=httpx.Response(200, json={"status": {"code": 200}}))

    await zoho_client.add_label(message_ids=["111", "222"], label_id="lbl-1")

    assert route.called
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == {
        "mode": "applyLabel",
        "messageId": ["111", "222"],
        "labelId": ["lbl-1"],
    }


async def test_add_label_rejects_empty_message_ids_without_a_request(
    respx_mock, zoho_client
):
    with pytest.raises(ZohoAPIError, match="message_ids"):
        await zoho_client.add_label(message_ids=[], label_id="lbl-1")

    assert not respx_mock.calls


async def test_add_label_wraps_http_errors_as_zoho_api_error(respx_mock, zoho_client):
    respx_mock.put(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/updatemessage"
    ).mock(return_value=httpx.Response(401, json={"error": "invalid token"}))

    with pytest.raises(ZohoAPIError):
        await zoho_client.add_label(message_ids=["111"], label_id="lbl-1")


async def test_remove_label_sends_correct_mode_and_label_id(respx_mock, zoho_client):
    route = respx_mock.put(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/updatemessage"
    ).mock(return_value=httpx.Response(200, json={"status": {"code": 200}}))

    await zoho_client.remove_label(message_ids=["111", "222"], label_id="lbl-1")

    assert route.called
    sent_body = json.loads(route.calls.last.request.content)
    assert sent_body == {
        "mode": "removeLabel",
        "messageId": ["111", "222"],
        "labelId": ["lbl-1"],
    }


async def test_remove_label_rejects_empty_message_ids_without_a_request(
    respx_mock, zoho_client
):
    with pytest.raises(ZohoAPIError, match="message_ids"):
        await zoho_client.remove_label(message_ids=[], label_id="lbl-1")

    assert not respx_mock.calls


async def test_remove_label_wraps_http_errors_as_zoho_api_error(
    respx_mock, zoho_client
):
    respx_mock.put(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/updatemessage"
    ).mock(return_value=httpx.Response(401, json={"error": "invalid token"}))

    with pytest.raises(ZohoAPIError):
        await zoho_client.remove_label(message_ids=["111"], label_id="lbl-1")


async def test_list_calendars_fetches_and_normalizes(respx_mock, zoho_client):
    route = respx_mock.get("https://calendar.zoho.com/api/v1/calendars").mock(
        return_value=httpx.Response(
            200,
            json={
                "calendars": [
                    {
                        "uid": "cal-556677",
                        "name": "ken",
                        "isdefault": True,
                        "timezone": "America/Los_Angeles",
                        "privilege": "owner",
                    }
                ]
            },
        )
    )

    results = await zoho_client.list_calendars()

    assert route.called
    assert (
        route.calls.last.request.headers["Authorization"]
        == "Zoho-oauthtoken fake-access-token"
    )
    assert results == [
        {
            "id": "cal-556677",
            "name": "ken",
            "is_default": True,
            "timezone": "America/Los_Angeles",
            "privilege": "owner",
        }
    ]


async def test_list_calendars_returns_empty_list_when_calendars_key_absent(
    respx_mock, zoho_client
):
    respx_mock.get("https://calendar.zoho.com/api/v1/calendars").mock(
        return_value=httpx.Response(200, json={})
    )

    results = await zoho_client.list_calendars()

    assert results == []


async def test_list_calendars_wraps_http_errors_as_zoho_api_error(
    respx_mock, zoho_client
):
    respx_mock.get("https://calendar.zoho.com/api/v1/calendars").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.list_calendars()


async def test_get_freebusy_fetches_and_normalizes(respx_mock, zoho_client):
    mock_pacific_accounts_endpoint(respx_mock)
    route = respx_mock.get("https://calendar.zoho.com/api/v1/calendars/freebusy").mock(
        return_value=httpx.Response(
            200,
            json={
                "freebusy": [
                    {
                        "startTime": "20260721T180000Z",
                        "endTime": "20260721T190000Z",
                        "fbtype": "busy",
                    }
                ]
            },
        )
    )
    start = datetime(2026, 7, 21, tzinfo=timezone.utc)
    end = datetime(2026, 7, 22, tzinfo=timezone.utc)

    results = await zoho_client.get_freebusy(
        email="jamie@example.com", start=start, end=end
    )

    assert route.called
    request = route.calls.last.request
    assert request.url.params["uemail"] == "jamie@example.com"
    assert request.url.params["sdate"] == "20260721T000000"
    assert request.url.params["edate"] == "20260722T000000"
    assert results == [
        {
            "start": "2026-07-21T11:00:00-07:00",
            "end": "2026-07-21T12:00:00-07:00",
            "status": "busy",
        }
    ]


async def test_get_freebusy_returns_empty_list_when_freebusy_key_absent(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    respx_mock.get("https://calendar.zoho.com/api/v1/calendars/freebusy").mock(
        return_value=httpx.Response(200, json={})
    )
    start = datetime(2026, 7, 21, tzinfo=timezone.utc)
    end = datetime(2026, 7, 22, tzinfo=timezone.utc)

    results = await zoho_client.get_freebusy(
        email="jamie@example.com", start=start, end=end
    )

    assert results == []


async def test_get_freebusy_raises_clear_error_when_sharing_not_enabled(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    respx_mock.get("https://calendar.zoho.com/api/v1/calendars/freebusy").mock(
        return_value=httpx.Response(200, json={"fb_not_enabled": True})
    )
    start = datetime(2026, 7, 21, tzinfo=timezone.utc)
    end = datetime(2026, 7, 22, tzinfo=timezone.utc)

    with pytest.raises(ZohoAPIError, match="not enabled"):
        await zoho_client.get_freebusy(email="jamie@example.com", start=start, end=end)


async def test_get_freebusy_rejects_end_before_start_without_a_request(
    respx_mock, zoho_client
):
    route = respx_mock.get("https://calendar.zoho.com/api/v1/calendars/freebusy")
    start = datetime(2026, 7, 22, tzinfo=timezone.utc)
    end = datetime(2026, 7, 21, tzinfo=timezone.utc)

    with pytest.raises(ZohoAPIError, match="end must be after start"):
        await zoho_client.get_freebusy(email="jamie@example.com", start=start, end=end)

    assert not route.called


async def test_get_freebusy_wraps_http_errors_as_zoho_api_error(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    respx_mock.get("https://calendar.zoho.com/api/v1/calendars/freebusy").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )
    start = datetime(2026, 7, 21, tzinfo=timezone.utc)
    end = datetime(2026, 7, 22, tzinfo=timezone.utc)

    with pytest.raises(ZohoAPIError):
        await zoho_client.get_freebusy(email="jamie@example.com", start=start, end=end)


async def test_create_event_sends_eventdata_and_normalizes_response(
    respx_mock, zoho_client
):
    route = respx_mock.post(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "events": [
                    {
                        "uid": "evt-new-1",
                        "title": "Q3 Sync",
                        "organizer": "user@example.com",
                        "attendees": [],
                    }
                ]
            },
        )
    )
    start = datetime(2026, 7, 21, 16, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 21, 17, 0, 0, tzinfo=timezone.utc)

    result = await zoho_client.create_event(title="Q3 Sync", start=start, end=end)

    assert route.called
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Zoho-oauthtoken fake-access-token"
    sent_eventdata = json.loads(request.url.params["eventdata"])
    assert sent_eventdata["title"] == "Q3 Sync"
    assert sent_eventdata["dateandtime"] == {
        "start": "20260721T160000Z",
        "end": "20260721T170000Z",
        "timezone": "UTC",
    }
    assert result["id"] == "evt-new-1"
    assert result["title"] == "Q3 Sync"


async def test_create_event_includes_optional_fields_when_given(
    respx_mock, zoho_client
):
    route = respx_mock.post(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "events": [
                    {
                        "uid": "evt-new-1",
                        "title": "Q3 Sync",
                        "organizer": "u@example.com",
                    }
                ]
            },
        )
    )
    start = datetime(2026, 7, 21, 16, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 21, 17, 0, 0, tzinfo=timezone.utc)

    await zoho_client.create_event(
        title="Q3 Sync",
        start=start,
        end=end,
        description="Quarterly roadmap review",
        location="Room 1",
        attendees=["jamie@example.com"],
    )

    sent_eventdata = json.loads(route.calls.last.request.url.params["eventdata"])
    assert sent_eventdata["description"] == "Quarterly roadmap review"
    assert sent_eventdata["location"] == "Room 1"
    assert sent_eventdata["attendees"] == [
        {"email": "jamie@example.com", "status": "NEEDS-ACTION"}
    ]


async def test_create_event_uses_given_calendar_id_instead_of_default(
    respx_mock, zoho_client
):
    route = respx_mock.post(
        "https://calendar.zoho.com/api/v1/calendars/other-cal/events"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "events": [{"uid": "evt-1", "title": "Sync", "organizer": "u@e.com"}]
            },
        )
    )
    start = datetime(2026, 7, 21, 16, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 21, 17, 0, 0, tzinfo=timezone.utc)

    await zoho_client.create_event(
        title="Sync", start=start, end=end, calendar_id="other-cal"
    )

    assert route.called


async def test_create_event_rejects_end_before_start_without_a_request(
    respx_mock, zoho_client
):
    route = respx_mock.post(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events"
    )
    start = datetime(2026, 7, 21, 17, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 21, 16, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(ZohoAPIError, match="end must be after start"):
        await zoho_client.create_event(title="Sync", start=start, end=end)

    assert not route.called


async def test_create_event_raises_clear_error_when_events_key_absent(
    respx_mock, zoho_client
):
    respx_mock.post(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events"
    ).mock(return_value=httpx.Response(200, json={}))
    start = datetime(2026, 7, 21, 16, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 21, 17, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(ZohoAPIError):
        await zoho_client.create_event(title="Sync", start=start, end=end)


async def test_create_event_wraps_http_errors_as_zoho_api_error(
    respx_mock, zoho_client
):
    respx_mock.post(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events"
    ).mock(return_value=httpx.Response(401, json={"error": "invalid token"}))
    start = datetime(2026, 7, 21, 16, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 21, 17, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(ZohoAPIError):
        await zoho_client.create_event(title="Sync", start=start, end=end)


def _raw_event_for_update():
    return {
        "uid": "evt-1",
        "title": "Old Title",
        "organizer": "user@example.com",
        "createdby": "user@example.com",
        "modifiedby": "user@example.com",
        "viewEventURL": "https://calendar.zoho.com/zc/viewevent/evt-1",
        "role": "organizer",
        "calid": "cal-internal-id",
        "caluid": "cal-internal-uid",
        # Confirmed live: this response-only field is NOT valid write
        # input (a different, incompatible thing from the write-side
        # "notify_attendee") -- resending it verbatim causes a real 400
        # "PATTERN_NOT_MATCHED" error. Included here so the merge logic
        # is tested against the exact shape that broke live.
        "notifyType": 0,
        "etag": "111222333",
        "dateandtime": {
            "start": "20260721T160000Z",
            "end": "20260721T170000Z",
        },
        "description": "Old description",
        "location": "Old location",
        "attendees": [{"email": "old@example.com", "status": "ACCEPTED"}],
        "rrule": "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO",
        "reminders": [{"action": "popup", "minutes": -30}],
    }


async def test_update_event_fetches_current_event_then_puts_merged_result(
    respx_mock, zoho_client
):
    get_route = respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    ).mock(return_value=httpx.Response(200, json={"events": [_raw_event_for_update()]}))
    put_route = respx_mock.put(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "events": [
                    {
                        "uid": "evt-1",
                        "title": "New Title",
                        "organizer": "user@example.com",
                    }
                ]
            },
        )
    )

    result = await zoho_client.update_event(uid="evt-1", title="New Title")

    assert get_route.called
    assert put_route.called
    sent_eventdata = json.loads(put_route.calls.last.request.url.params["eventdata"])
    # Changed field applied...
    assert sent_eventdata["title"] == "New Title"
    # ...but everything untouched carried forward as-is, including etag
    # (mandatory for the update to be accepted) and fields with no
    # dedicated update_event argument at all (rrule, reminders) -- Zoho's
    # update is a full replace, so omitting these would silently delete them.
    assert sent_eventdata["etag"] == "111222333"
    assert sent_eventdata["description"] == "Old description"
    assert sent_eventdata["location"] == "Old location"
    assert sent_eventdata["rrule"] == "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO"
    assert sent_eventdata["reminders"] == [{"action": "popup", "minutes": -30}]
    assert "uid" not in sent_eventdata  # goes in the URL, not the body
    # Response-only fields, including the exact one that caused a real
    # live 400 "PATTERN_NOT_MATCHED" when echoed back verbatim.
    for field in (
        "notifyType",
        "organizer",
        "createdby",
        "modifiedby",
        "viewEventURL",
        "role",
        "calid",
        "caluid",
    ):
        assert field not in sent_eventdata
    assert result["id"] == "evt-1"
    assert result["title"] == "New Title"


async def test_update_event_overrides_only_given_fields(respx_mock, zoho_client):
    respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    ).mock(return_value=httpx.Response(200, json={"events": [_raw_event_for_update()]}))
    put_route = respx_mock.put(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "events": [
                    {"uid": "evt-1", "title": "Old Title", "organizer": "u@e.com"}
                ]
            },
        )
    )
    start = datetime(2026, 7, 22, 16, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 22, 17, 0, 0, tzinfo=timezone.utc)

    await zoho_client.update_event(
        uid="evt-1",
        start=start,
        end=end,
        description="New description",
        location="New location",
        attendees=["new@example.com"],
    )

    sent_eventdata = json.loads(put_route.calls.last.request.url.params["eventdata"])
    assert sent_eventdata["title"] == "Old Title"  # untouched
    assert sent_eventdata["dateandtime"] == {
        "start": "20260722T160000Z",
        "end": "20260722T170000Z",
        "timezone": "UTC",
    }
    assert sent_eventdata["description"] == "New description"
    assert sent_eventdata["location"] == "New location"
    assert sent_eventdata["attendees"] == [
        {"email": "new@example.com", "status": "NEEDS-ACTION"}
    ]


async def test_update_event_uses_given_calendar_id_instead_of_default(
    respx_mock, zoho_client
):
    get_route = respx_mock.get(
        "https://calendar.zoho.com/api/v1/calendars/other-cal/events/evt-1"
    ).mock(return_value=httpx.Response(200, json={"events": [_raw_event_for_update()]}))
    put_route = respx_mock.put(
        "https://calendar.zoho.com/api/v1/calendars/other-cal/events/evt-1"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"events": [{"uid": "evt-1", "title": "T", "organizer": "u@e.com"}]},
        )
    )

    await zoho_client.update_event(uid="evt-1", title="T", calendar_id="other-cal")

    assert get_route.called
    assert put_route.called


async def test_update_event_rejects_start_without_end(respx_mock, zoho_client):
    route = respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    )

    with pytest.raises(ZohoAPIError, match="start and end must be given together"):
        await zoho_client.update_event(
            uid="evt-1", start=datetime(2026, 7, 22, tzinfo=timezone.utc)
        )

    assert not route.called


async def test_update_event_rejects_end_before_start(respx_mock, zoho_client):
    respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    ).mock(return_value=httpx.Response(200, json={"events": [_raw_event_for_update()]}))
    start = datetime(2026, 7, 22, 17, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 22, 16, 0, 0, tzinfo=timezone.utc)

    with pytest.raises(ZohoAPIError, match="end must be after start"):
        await zoho_client.update_event(uid="evt-1", start=start, end=end)


async def test_update_event_raises_clear_error_when_event_not_found(
    respx_mock, zoho_client
):
    respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-missing"
    ).mock(return_value=httpx.Response(200, json={"events": []}))

    with pytest.raises(ZohoAPIError, match="evt-missing"):
        await zoho_client.update_event(uid="evt-missing", title="New Title")


async def test_update_event_wraps_http_errors_as_zoho_api_error(
    respx_mock, zoho_client
):
    respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    ).mock(return_value=httpx.Response(200, json={"events": [_raw_event_for_update()]}))
    respx_mock.put(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    ).mock(return_value=httpx.Response(401, json={"error": "invalid token"}))

    with pytest.raises(ZohoAPIError):
        await zoho_client.update_event(uid="evt-1", title="New Title")


async def test_delete_event_fetches_etag_then_deletes(respx_mock, zoho_client):
    get_route = respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    ).mock(return_value=httpx.Response(200, json={"events": [_raw_event_for_update()]}))
    delete_route = respx_mock.delete(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    ).mock(return_value=httpx.Response(200, json={"status": {"code": 200}}))

    await zoho_client.delete_event(uid="evt-1")

    assert get_route.called
    assert delete_route.called
    sent_eventdata = json.loads(delete_route.calls.last.request.url.params["eventdata"])
    assert sent_eventdata["etag"] == "111222333"


async def test_delete_event_uses_given_calendar_id_instead_of_default(
    respx_mock, zoho_client
):
    get_route = respx_mock.get(
        "https://calendar.zoho.com/api/v1/calendars/other-cal/events/evt-1"
    ).mock(return_value=httpx.Response(200, json={"events": [_raw_event_for_update()]}))
    delete_route = respx_mock.delete(
        "https://calendar.zoho.com/api/v1/calendars/other-cal/events/evt-1"
    ).mock(return_value=httpx.Response(200, json={"status": {"code": 200}}))

    await zoho_client.delete_event(uid="evt-1", calendar_id="other-cal")

    assert get_route.called
    assert delete_route.called


async def test_delete_event_raises_clear_error_when_event_not_found(
    respx_mock, zoho_client
):
    respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-missing"
    ).mock(return_value=httpx.Response(200, json={"events": []}))

    with pytest.raises(ZohoAPIError, match="evt-missing"):
        await zoho_client.delete_event(uid="evt-missing")


async def test_delete_event_wraps_http_errors_as_zoho_api_error(
    respx_mock, zoho_client
):
    respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    ).mock(return_value=httpx.Response(200, json={"events": [_raw_event_for_update()]}))
    respx_mock.delete(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    ).mock(return_value=httpx.Response(401, json={"error": "invalid token"}))

    with pytest.raises(ZohoAPIError):
        await zoho_client.delete_event(uid="evt-1")
