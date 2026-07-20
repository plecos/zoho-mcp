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
