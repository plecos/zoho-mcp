import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import time_machine

from zoho_mcp.zoho.client import (
    MAX_FORWARD_ATTACHMENT_BYTES,
    ZohoAPIError,
    ZohoClient,
)

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


@pytest.fixture
def sending_client(http_client):
    """A client with auto-send explicitly enabled, as ZOHO_ALLOW_AUTO_SEND does."""
    return ZohoClient(
        token_manager=FakeTokenManager(),
        http_client=http_client,
        account_id=ACCOUNT_ID,
        calendar_uid=CALENDAR_UID,
        allow_auto_send=True,
    )


def mock_compose_endpoints(respx_mock):
    """Accounts lookup (for the mandatory fromAddress) plus the compose POST."""
    respx_mock.get("https://mail.zoho.com/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "accountId": ACCOUNT_ID,
                        "isDefaultAccount": True,
                        "timeZone": "America/Los_Angeles",
                        "primaryEmailAddress": "me@example.com",
                    }
                ]
            },
        )
    )
    return respx_mock.post(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages"
    ).mock(
        return_value=httpx.Response(
            200, json={"status": {"code": 200}, "data": {"messageId": "msg-new-1"}}
        )
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
        "spam-folder-id": "Spam",
        "trash-folder-id": "Trash",
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
            "from_name": "",
            "subject": "Q3 Roadmap Sync",
            # Mailbox's own offset, not UTC -- see _epoch_ms_to_iso8601.
            "date": "2024-10-29T09:00:00-07:00",
            "snippet": "Let's sync on the Q3 roadmap tomorrow morning.",
            "folder_id": "1122334455",
            "read": False,
            # This fixture omits every optional key, which is realistic:
            # Zoho's key set varies per message. They default rather than
            # raise -- see tests/zoho/test_email_summary_fields.py.
            "to": [],
            "cc": [],
            "has_attachment": False,
            "size_bytes": None,
            "label_ids": [],
            "flag": "",
            "priority": "normal",
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


async def test_search_emails_does_not_itself_filter_spam_or_trash(
    respx_mock, zoho_client
):
    """Spam/Trash are missing from unscoped results because *Zoho* drops
    them, not because this layer does -- see "Unscoped queries silently
    omit Spam and Trash" in docs/zoho-api-notes.md.

    Pinned because the distinction is invisible in production (both
    mechanisms look identical from the outside: no Spam in the results)
    and because the docstrings now assert whose behavior it is. If
    someone "fixes" the exclusion by adding Spam/Trash to
    EXCLUDED_FOLDER_TYPES, the docs become wrong and an explicitly
    folder-scoped fetch starts dropping the rows it was asked for.
    """
    mock_pacific_accounts_endpoint(respx_mock)
    mock_folder_types_endpoint(respx_mock)
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _raw_email(message_id="1", folder_id="1122334455"),
                    _raw_email(message_id="6", folder_id="spam-folder-id"),
                    _raw_email(message_id="7", folder_id="trash-folder-id"),
                ]
            },
        )
    )

    results = await zoho_client.search_emails(query="roadmap")

    assert {r["id"] for r in results} == {"1", "6", "7"}


async def test_list_emails_does_not_itself_filter_spam_or_trash(
    respx_mock, zoho_client
):
    """Same invariant on the List API's whole-mailbox path.

    ``list_emails`` runs the folder filter whenever ``folder_id`` is
    None, so this is the call that would silently start dropping rows if
    Spam/Trash were added to the excluded types.
    """
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
                    _raw_email(message_id="6", folder_id="spam-folder-id"),
                    _raw_email(message_id="7", folder_id="trash-folder-id"),
                ]
            },
        )
    )

    emails, _ = await zoho_client.list_emails(status="unread")

    assert {e["id"] for e in emails} == {"1", "6", "7"}


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

    results, _ = await zoho_client.list_emails()

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
            "from_name": "",
            "subject": "Subject",
            "date": "2024-10-29T09:00:00-07:00",
            "snippet": "Snippet",
            "folder_id": "1122334455",
            "read": True,
            "to": [],
            "cc": [],
            "has_attachment": False,
            "size_bytes": None,
            "label_ids": [],
            "flag": "",
            "priority": "normal",
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
    results, _ = await zoho_client.list_emails(folder_id="folder-9")

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

    results, _ = await zoho_client.list_emails()

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

    results, _ = await zoho_client.list_emails()

    assert {r["id"] for r in results} == {"1"}


async def test_list_emails_reports_more_pages_when_the_raw_page_came_back_full(
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
                    _raw_email(message_id="2", folder_id="1122334455"),
                ]
            },
        )
    )

    results, has_more = await zoho_client.list_emails(limit=2)

    assert len(results) == 2
    assert has_more is True


async def test_list_emails_still_reports_more_pages_when_filtering_shrinks_the_page(
    respx_mock, zoho_client
):
    """The exclusion filter runs *after* the page is fetched.

    So a full page of `limit` raw messages can return fewer than `limit`
    results once Sent/Drafts/Templates are dropped. Deriving "last page" from
    the returned length -- which is what callers were told to do -- then stops
    the sweep early and silently leaves later mail unenumerated. `has_more`
    has to be measured against the raw page, before any filtering.
    """
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

    results, has_more = await zoho_client.list_emails(limit=2)

    assert {r["id"] for r in results} == {"1"}
    assert has_more is True


async def test_list_emails_reports_no_more_pages_when_the_raw_page_was_short(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    mock_folder_types_endpoint(respx_mock)
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/view"
    ).mock(
        return_value=httpx.Response(
            200,
            json={"data": [_raw_email(message_id="1", folder_id="1122334455")]},
        )
    )

    _, has_more = await zoho_client.list_emails(limit=20)

    assert has_more is False


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


# Confirmed live against three real notes: isPrev=true returns oldest ->
# newest, while absent and isPrev=false are both newest -> oldest and
# byte-identical. It is a sort-order flag, not the paging-direction flag
# its name suggests. `after` composes with it, offsetting within whichever
# order is selected.
async def test_list_notes_requests_ascending_order_when_oldest_first(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    route = respx_mock.get("https://mail.zoho.com/api/notes/me").mock(
        return_value=httpx.Response(200, json={"data": {"list": []}})
    )

    await zoho_client.list_notes(oldest_first=True)

    assert route.calls.last.request.url.params["isPrev"] == "true"


async def test_list_notes_omits_isprev_when_defaulting_to_newest_first(
    respx_mock, zoho_client
):
    # Absent and "false" are confirmed equivalent live, so the default
    # request stays byte-identical to what shipped before this flag.
    mock_pacific_accounts_endpoint(respx_mock)
    route = respx_mock.get("https://mail.zoho.com/api/notes/me").mock(
        return_value=httpx.Response(200, json={"data": {"list": []}})
    )

    await zoho_client.list_notes()

    assert "isPrev" not in route.calls.last.request.url.params


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


# Verified independently against real bookmarks rather than inferred from
# Notes: these two sibling endpoints already disagree on isFavorite's type
# (bool on Notes, string here), so identical docs prove nothing. Bookmarks
# carry no createdTime at all, so ordering was checked via their
# epoch-ms-prefixed entityIds -- absent == "false" (newest first) and
# "true" is the exact reverse, matching Notes.
async def test_list_bookmarks_requests_ascending_order_when_oldest_first(
    respx_mock, zoho_client
):
    route = respx_mock.get("https://mail.zoho.com/api/links/me").mock(
        return_value=httpx.Response(200, json={"data": {"list": []}})
    )

    await zoho_client.list_bookmarks(oldest_first=True)

    assert route.calls.last.request.url.params["isPrev"] == "true"


async def test_list_bookmarks_omits_isprev_when_defaulting_to_newest_first(
    respx_mock, zoho_client
):
    route = respx_mock.get("https://mail.zoho.com/api/links/me").mock(
        return_value=httpx.Response(200, json={"data": {"list": []}})
    )

    await zoho_client.list_bookmarks()

    assert "isPrev" not in route.calls.last.request.url.params


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


# Zoho documents 1-399 for both, but confirmed live it silently ACCEPTS an
# over-max limit (HTTP 200 for limit=10000) rather than rejecting it -- so a
# caller asking for 1000 can't tell whether they got everything or were
# quietly capped. Validate explicitly so that ambiguity never reaches them.
@pytest.mark.parametrize("bad_limit", [400, 500, 10_000])
async def test_list_notes_rejects_limit_above_max_without_a_request(
    respx_mock, zoho_client, bad_limit
):
    route = respx_mock.get("https://mail.zoho.com/api/notes/me")

    with pytest.raises(ZohoAPIError, match="limit"):
        await zoho_client.list_notes(limit=bad_limit)

    assert not route.called


@pytest.mark.parametrize("bad_limit", [400, 500, 10_000])
async def test_list_bookmarks_rejects_limit_above_max_without_a_request(
    respx_mock, zoho_client, bad_limit
):
    route = respx_mock.get("https://mail.zoho.com/api/links/me")

    with pytest.raises(ZohoAPIError, match="limit"):
        await zoho_client.list_bookmarks(limit=bad_limit)

    assert not route.called


@pytest.mark.parametrize("edge_limit", [1, 399])
async def test_list_notes_accepts_boundary_limit_values(
    respx_mock, zoho_client, edge_limit
):
    mock_pacific_accounts_endpoint(respx_mock)
    route = respx_mock.get("https://mail.zoho.com/api/notes/me").mock(
        return_value=httpx.Response(200, json={"data": {"list": []}})
    )

    await zoho_client.list_notes(limit=edge_limit)

    assert route.called


@pytest.mark.parametrize("edge_limit", [1, 399])
async def test_list_bookmarks_accepts_boundary_limit_values(
    respx_mock, zoho_client, edge_limit
):
    route = respx_mock.get("https://mail.zoho.com/api/links/me").mock(
        return_value=httpx.Response(200, json={"data": {"list": []}})
    )

    await zoho_client.list_bookmarks(limit=edge_limit)

    assert route.called


async def test_list_tasks_targets_group_endpoint_when_group_id_given(
    respx_mock, zoho_client
):
    route = respx_mock.get("https://mail.zoho.com/api/tasks/groups/zg-1").mock(
        return_value=httpx.Response(200, json={"data": {"tasks": []}})
    )

    await zoho_client.list_tasks(group_id="zg-1")

    assert route.called


async def test_list_notes_targets_group_endpoint_when_group_id_given(
    respx_mock, zoho_client
):
    mock_pacific_accounts_endpoint(respx_mock)
    route = respx_mock.get("https://mail.zoho.com/api/notes/groups/g-1").mock(
        return_value=httpx.Response(200, json={"data": {"list": []}})
    )

    await zoho_client.list_notes(group_id="g-1")

    assert route.called


async def test_list_bookmarks_targets_group_endpoint_when_group_id_given(
    respx_mock, zoho_client
):
    route = respx_mock.get("https://mail.zoho.com/api/links/groups/g-1").mock(
        return_value=httpx.Response(200, json={"data": {"list": []}})
    )

    await zoho_client.list_bookmarks(group_id="g-1")

    assert route.called


# Confirmed live: these two views are real (assignedbyme is not -- it 400s
# with PATTERN_NOT_MATCHED), and both require action=view alongside them.
@pytest.mark.parametrize(
    ("view", "zoho_view"),
    [("assigned_to_me", "assignedtome"), ("created_by_me", "createdbyme")],
)
async def test_list_tasks_uses_view_endpoint_when_view_given(
    respx_mock, zoho_client, view, zoho_view
):
    route = respx_mock.get("https://mail.zoho.com/api/tasks/").mock(
        return_value=httpx.Response(200, json={"data": {"tasks": []}})
    )

    await zoho_client.list_tasks(view=view)

    assert route.called
    request = route.calls.last.request
    assert request.url.params["view"] == zoho_view
    assert request.url.params["action"] == "view"


async def test_list_tasks_rejects_unknown_view_without_a_request(
    respx_mock, zoho_client
):
    route = respx_mock.get("https://mail.zoho.com/api/tasks/")

    with pytest.raises(ZohoAPIError, match="view"):
        await zoho_client.list_tasks(view="assigned_by_me")

    assert not route.called


async def test_list_tasks_rejects_group_id_and_view_together_without_a_request(
    respx_mock, zoho_client
):
    route = respx_mock.get("https://mail.zoho.com/api/tasks/")

    with pytest.raises(ZohoAPIError, match="together"):
        await zoho_client.list_tasks(group_id="zg-1", view="assigned_to_me")

    assert not route.called


def mock_group_endpoints(respx_mock, *, tasks, notes, bookmarks):
    respx_mock.get("https://mail.zoho.com/api/tasks/groups").mock(
        return_value=httpx.Response(200, json={"data": {"groups": tasks}})
    )
    respx_mock.get("https://mail.zoho.com/api/notes/groups").mock(
        return_value=httpx.Response(200, json={"data": notes})
    )
    respx_mock.get("https://mail.zoho.com/api/links/groups").mock(
        return_value=httpx.Response(200, json={"data": bookmarks})
    )


# Confirmed live: a Zoho Mail group is ONE entity that every service
# lists, not a per-service thing. The real "test" group came back from
# all three endpoints -- including tasks and bookmarks, where it holds
# zero items -- keyed as an int "id" by Tasks and a string "groupId" by
# Notes/Bookmarks. So the same group must collapse to a single row, or
# a caller would report three groups where the user has one.
async def test_list_groups_deduplicates_one_group_reported_by_every_service(
    respx_mock, zoho_client
):
    mock_group_endpoints(
        respx_mock,
        tasks=[
            {
                "id": 555000123,
                "name": "test",
                "owner": "Sam Rivera",
                "numberOfMembers": 1,
            }
        ],
        notes=[{"groupId": "555000123", "name": "test"}],
        bookmarks=[{"groupId": "555000123", "name": "test"}],
    )

    results = await zoho_client.list_groups()

    assert results == [
        {"id": "555000123", "name": "test", "owner": "Sam Rivera", "member_count": 1}
    ]


async def test_list_groups_includes_a_group_only_one_service_reports(
    respx_mock, zoho_client
):
    # Don't assume the three listings always agree just because they did
    # for the one real group available -- a group missing from Tasks
    # still has to surface, just without the Tasks-only metadata.
    mock_group_endpoints(
        respx_mock,
        tasks=[],
        notes=[{"groupId": "84626808", "name": "note-only"}],
        bookmarks=[],
    )

    results = await zoho_client.list_groups()

    assert results == [
        {"id": "84626808", "name": "note-only", "owner": "", "member_count": None}
    ]


async def test_list_groups_returns_empty_list_when_no_groups_exist(
    respx_mock, zoho_client
):
    # The real shape on an account with no groups -- confirmed live.
    respx_mock.get("https://mail.zoho.com/api/tasks/groups").mock(
        return_value=httpx.Response(200, json={"data": {"groups": []}})
    )
    respx_mock.get("https://mail.zoho.com/api/notes/groups").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx_mock.get("https://mail.zoho.com/api/links/groups").mock(
        return_value=httpx.Response(200, json={"data": []})
    )

    assert await zoho_client.list_groups() == []


async def test_list_groups_wraps_http_errors_as_zoho_api_error(respx_mock, zoho_client):
    respx_mock.get("https://mail.zoho.com/api/tasks/groups").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )
    respx_mock.get("https://mail.zoho.com/api/notes/groups").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx_mock.get("https://mail.zoho.com/api/links/groups").mock(
        return_value=httpx.Response(200, json={"data": []})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.list_groups()


async def test_list_groups_raises_clear_error_on_malformed_group(
    respx_mock, zoho_client
):
    respx_mock.get("https://mail.zoho.com/api/tasks/groups").mock(
        return_value=httpx.Response(200, json={"data": {"groups": [{"name": "no-id"}]}})
    )
    respx_mock.get("https://mail.zoho.com/api/notes/groups").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    respx_mock.get("https://mail.zoho.com/api/links/groups").mock(
        return_value=httpx.Response(200, json={"data": []})
    )

    with pytest.raises(ZohoAPIError, match="group"):
        await zoho_client.list_groups()


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
        {
            "id": "folder-1",
            "name": "Inbox",
            "path": "/Inbox",
            "type": "Inbox",
            "parent_id": "",
        }
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


def _created_task_payload() -> dict:
    """Zoho's real create-task response: unlike Notes/Bookmarks, it returns
    the whole task object, so it can be normalized like a listed one."""
    return {
        "status": {"code": 200, "description": "success"},
        "data": {
            "id": "7000000089009",
            "title": "Blog Updates",
            "description": "Announcement blog for recent revamp",
            "status": "In Progress",
            "priority": "low",
            "owner": {"name": "Ken", "id": 4650081},
            "assignee": {"name": "Ken", "id": 4650081},
            "tags": [],
            "subtasks": [],
            "createdAt": "2017-07-07T01:20:39+05:30",
            "modifiedTime": "2017-07-07T01:20:39+05:30",
        },
    }


async def test_create_task_posts_title_and_returns_normalized_task(
    respx_mock, zoho_client
):
    route = respx_mock.post("https://mail.zoho.com/api/tasks/me").mock(
        return_value=httpx.Response(200, json=_created_task_payload())
    )

    result = await zoho_client.create_task(title="Blog Updates")

    assert route.called
    assert json.loads(route.calls.last.request.content) == {"title": "Blog Updates"}
    assert result["id"] == "7000000089009"
    assert result["title"] == "Blog Updates"
    assert result["status"] == "In Progress"


async def test_create_task_sends_only_optional_fields_that_were_given(
    respx_mock, zoho_client
):
    route = respx_mock.post("https://mail.zoho.com/api/tasks/me").mock(
        return_value=httpx.Response(200, json=_created_task_payload())
    )

    await zoho_client.create_task(
        title="Blog Updates", description="desc", priority="high"
    )

    assert json.loads(route.calls.last.request.content) == {
        "title": "Blog Updates",
        "description": "desc",
        "priority": "high",
    }


async def test_create_task_targets_group_endpoint_when_group_id_given(
    respx_mock, zoho_client
):
    route = respx_mock.post("https://mail.zoho.com/api/tasks/groups/zg-1").mock(
        return_value=httpx.Response(200, json=_created_task_payload())
    )

    await zoho_client.create_task(title="Blog Updates", group_id="zg-1")

    assert route.called


@pytest.mark.parametrize("bad_title", ["", "   "])
async def test_create_task_rejects_blank_title_without_a_request(
    respx_mock, zoho_client, bad_title
):
    route = respx_mock.post("https://mail.zoho.com/api/tasks/me")

    with pytest.raises(ZohoAPIError, match="title"):
        await zoho_client.create_task(title=bad_title)

    assert not route.called


async def test_create_task_raises_when_response_has_no_task(respx_mock, zoho_client):
    respx_mock.post("https://mail.zoho.com/api/tasks/me").mock(
        return_value=httpx.Response(200, json={"status": {"code": 200}})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.create_task(title="Blog Updates")


async def test_create_task_wraps_http_errors_as_zoho_api_error(respx_mock, zoho_client):
    respx_mock.post("https://mail.zoho.com/api/tasks/me").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.create_task(title="Blog Updates")


# Notes and Bookmarks, unlike Tasks, return only the new id and a URI --
# no created object -- so these return the id rather than inventing a
# full record the API never sent back.
async def test_create_note_posts_content_and_returns_new_id(respx_mock, zoho_client):
    route = respx_mock.post("https://mail.zoho.com/api/notes/me").mock(
        return_value=httpx.Response(
            201,
            json={
                "status": {"code": 201, "description": "Created"},
                "data": {
                    "entityId": "1711974988431110001",
                    "URI": "https://mail.zoho.com/api/notes/me/1711974988431110001",
                },
            },
        )
    )

    result = await zoho_client.create_note(content="note body")

    assert route.called
    # color is required despite being documented optional -- confirmed live
    # that content alone 404s. Int, though the read side returns a string.
    assert json.loads(route.calls.last.request.content) == {
        "content": "note body",
        "color": -1,
    }
    assert result == {"id": "1711974988431110001"}


async def test_create_note_sends_title_when_given(respx_mock, zoho_client):
    route = respx_mock.post("https://mail.zoho.com/api/notes/me").mock(
        return_value=httpx.Response(201, json={"data": {"entityId": "1"}})
    )

    await zoho_client.create_note(content="body", title="My note")

    assert json.loads(route.calls.last.request.content) == {
        "content": "body",
        "color": -1,
        "title": "My note",
    }


async def test_create_note_targets_group_endpoint_when_group_id_given(
    respx_mock, zoho_client
):
    route = respx_mock.post("https://mail.zoho.com/api/notes/groups/g-1").mock(
        return_value=httpx.Response(201, json={"data": {"entityId": "1"}})
    )

    await zoho_client.create_note(content="body", group_id="g-1")

    assert route.called


@pytest.mark.parametrize("bad_content", ["", "   "])
async def test_create_note_rejects_blank_content_without_a_request(
    respx_mock, zoho_client, bad_content
):
    route = respx_mock.post("https://mail.zoho.com/api/notes/me")

    with pytest.raises(ZohoAPIError, match="content"):
        await zoho_client.create_note(content=bad_content)

    assert not route.called


async def test_create_note_raises_when_entity_id_absent(respx_mock, zoho_client):
    respx_mock.post("https://mail.zoho.com/api/notes/me").mock(
        return_value=httpx.Response(201, json={"data": {}})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.create_note(content="body")


async def test_create_note_wraps_http_errors_as_zoho_api_error(respx_mock, zoho_client):
    respx_mock.post("https://mail.zoho.com/api/notes/me").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.create_note(content="body")


async def test_create_bookmark_posts_link_and_title_and_returns_new_id(
    respx_mock, zoho_client
):
    route = respx_mock.post("https://mail.zoho.com/api/links/me").mock(
        return_value=httpx.Response(
            200, json={"data": {"entityId": "1712055358708110001"}}
        )
    )

    result = await zoho_client.create_bookmark(
        url="https://www.zoho.com", title="zoho link"
    )

    assert route.called
    # Zoho's field is "link"; the read side normalizes it to "url", so the
    # write side takes "url" too and translates at the boundary.
    assert json.loads(route.calls.last.request.content) == {
        "link": "https://www.zoho.com",
        "title": "zoho link",
    }
    assert result == {"id": "1712055358708110001"}


async def test_create_bookmark_sends_summary_when_given(respx_mock, zoho_client):
    route = respx_mock.post("https://mail.zoho.com/api/links/me").mock(
        return_value=httpx.Response(200, json={"data": {"entityId": "1"}})
    )

    await zoho_client.create_bookmark(
        url="https://www.zoho.com", title="zoho link", summary="desc"
    )

    assert json.loads(route.calls.last.request.content) == {
        "link": "https://www.zoho.com",
        "title": "zoho link",
        "summary": "desc",
    }


async def test_create_bookmark_targets_group_endpoint_when_group_id_given(
    respx_mock, zoho_client
):
    route = respx_mock.post("https://mail.zoho.com/api/links/groups/g-1").mock(
        return_value=httpx.Response(200, json={"data": {"entityId": "1"}})
    )

    await zoho_client.create_bookmark(
        url="https://www.zoho.com", title="zoho link", group_id="g-1"
    )

    assert route.called


@pytest.mark.parametrize(
    ("url", "title", "expected"),
    [("", "t", "url"), ("   ", "t", "url"), ("https://x.com", "", "title")],
)
async def test_create_bookmark_rejects_blank_required_fields_without_a_request(
    respx_mock, zoho_client, url, title, expected
):
    route = respx_mock.post("https://mail.zoho.com/api/links/me")

    with pytest.raises(ZohoAPIError, match=expected):
        await zoho_client.create_bookmark(url=url, title=title)

    assert not route.called


async def test_create_bookmark_raises_when_entity_id_absent(respx_mock, zoho_client):
    respx_mock.post("https://mail.zoho.com/api/links/me").mock(
        return_value=httpx.Response(200, json={"data": {}})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.create_bookmark(url="https://x.com", title="t")


async def test_create_bookmark_wraps_http_errors_as_zoho_api_error(
    respx_mock, zoho_client
):
    respx_mock.post("https://mail.zoho.com/api/links/me").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.create_bookmark(url="https://x.com", title="t")


# ---------------------------------------------------------------------------
# Mail composition. Zoho sends and saves-a-draft through the SAME endpoint,
# distinguished only by "mode": "draft" -- omitting that one field mails a
# real person. The tests below pin that field deliberately: they are the
# regression guard against a refactor silently turning drafts into sends.
# ---------------------------------------------------------------------------


async def test_create_draft_always_sets_mode_draft(respx_mock, zoho_client):
    route = mock_compose_endpoints(respx_mock)

    await zoho_client.create_draft(to=["a@example.com"], subject="Hi", content="Body")

    sent = json.loads(route.calls.last.request.content)
    assert sent["mode"] == "draft"  # never remove: without it Zoho SENDS


async def test_create_draft_builds_the_expected_payload(respx_mock, zoho_client):
    route = mock_compose_endpoints(respx_mock)

    result = await zoho_client.create_draft(
        to=["a@example.com", "b@example.com"],
        subject="Hi",
        content="Body",
        cc=["c@example.com"],
        bcc=["d@example.com"],
    )

    assert json.loads(route.calls.last.request.content) == {
        "mode": "draft",
        "fromAddress": "me@example.com",
        "toAddress": "a@example.com,b@example.com",
        "subject": "Hi",
        "content": "Body",
        "ccAddress": "c@example.com",
        "bccAddress": "d@example.com",
    }
    assert result == {"id": "msg-new-1"}


async def test_create_draft_omits_cc_and_bcc_when_not_given(respx_mock, zoho_client):
    route = mock_compose_endpoints(respx_mock)

    await zoho_client.create_draft(to=["a@example.com"], subject="Hi", content="B")

    sent = json.loads(route.calls.last.request.content)
    assert "ccAddress" not in sent
    assert "bccAddress" not in sent


@pytest.mark.parametrize("bad_to", [[], [""], ["   "]])
async def test_create_draft_rejects_missing_recipients_without_a_request(
    respx_mock, zoho_client, bad_to
):
    route = respx_mock.post(f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages")

    with pytest.raises(ZohoAPIError, match="recipient"):
        await zoho_client.create_draft(to=bad_to, subject="Hi", content="B")

    assert not route.called


async def test_create_draft_works_without_auto_send_enabled(respx_mock, zoho_client):
    # Drafting must never be gated -- only sending is.
    route = mock_compose_endpoints(respx_mock)

    await zoho_client.create_draft(to=["a@example.com"], subject="Hi", content="B")

    assert route.called


async def test_send_email_sets_draft_mode_when_not_enabled(respx_mock, zoho_client):
    # The critical safety test. A disabled client still posts -- it saves the
    # message to Drafts rather than erroring -- so "made no request" is no
    # longer the property that keeps mail in the account. This is: the one
    # field that decides send-vs-draft must be "draft" on every gated call.
    route = mock_compose_endpoints(respx_mock)

    await zoho_client.send_email(to=["a@example.com"], subject="Hi", content="B")

    assert json.loads(route.calls.last.request.content)["mode"] == "draft"


async def test_send_email_reports_that_it_did_not_send_when_not_enabled(
    respx_mock, zoho_client
):
    # Without an explicit flag the caller sees only an id, which is exactly
    # what a real send returns -- an LLM would report "sent" either way.
    mock_compose_endpoints(respx_mock)

    result = await zoho_client.send_email(
        to=["a@example.com"], subject="Hi", content="B"
    )

    assert result["sent"] is False
    assert result["id"] == "msg-new-1"
    assert "draft" in result["detail"].lower()


async def test_send_email_keeps_the_whole_message_in_the_gated_draft(
    respx_mock, zoho_client
):
    # A draft that silently dropped cc/bcc would look like a successful save
    # and then go out incomplete when the user sends it by hand.
    route = mock_compose_endpoints(respx_mock)

    await zoho_client.send_email(
        to=["a@example.com", "b@example.com"],
        subject="Hi",
        content="Body",
        cc=["c@example.com"],
        bcc=["d@example.com"],
    )

    sent = json.loads(route.calls.last.request.content)
    assert sent["toAddress"] == "a@example.com,b@example.com"
    assert sent["ccAddress"] == "c@example.com"
    assert sent["bccAddress"] == "d@example.com"
    assert sent["subject"] == "Hi"
    assert sent["content"] == "Body"


async def test_send_email_still_rejects_missing_recipients_when_not_enabled(
    respx_mock, zoho_client
):
    # Falling back to a draft must not turn a malformed call into a silent
    # half-success -- a draft with no recipient helps nobody.
    route = respx_mock.post(f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages")

    with pytest.raises(ZohoAPIError, match="recipient"):
        await zoho_client.send_email(to=[], subject="Hi", content="B")

    assert not route.called


async def test_send_email_omits_mode_so_zoho_actually_sends(respx_mock, sending_client):
    route = mock_compose_endpoints(respx_mock)

    result = await sending_client.send_email(
        to=["a@example.com"], subject="Hi", content="Body"
    )

    sent = json.loads(route.calls.last.request.content)
    assert "mode" not in sent
    assert sent["toAddress"] == "a@example.com"
    assert result == {"id": "msg-new-1", "sent": True}


async def test_send_email_rejects_missing_recipients_without_a_request(
    respx_mock, sending_client
):
    route = respx_mock.post(f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages")

    with pytest.raises(ZohoAPIError, match="recipient"):
        await sending_client.send_email(to=[], subject="Hi", content="B")

    assert not route.called


async def test_compose_wraps_http_errors_as_zoho_api_error(respx_mock, zoho_client):
    respx_mock.get("https://mail.zoho.com/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "accountId": ACCOUNT_ID,
                        "isDefaultAccount": True,
                        "timeZone": "America/Los_Angeles",
                        "primaryEmailAddress": "me@example.com",
                    }
                ]
            },
        )
    )
    respx_mock.post(f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages").mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )

    with pytest.raises(ZohoAPIError):
        await zoho_client.create_draft(to=["a@example.com"], subject="Hi", content="B")


async def test_reply_draft_sets_both_action_reply_and_mode_draft(
    respx_mock, zoho_client
):
    respx_mock.get("https://mail.zoho.com/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "accountId": ACCOUNT_ID,
                        "isDefaultAccount": True,
                        "timeZone": "America/Los_Angeles",
                        "primaryEmailAddress": "me@example.com",
                    }
                ]
            },
        )
    )
    route = respx_mock.post(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/m-1"
    ).mock(
        return_value=httpx.Response(200, json={"data": {"messageId": "msg-reply-1"}})
    )

    result = await zoho_client.reply_draft(message_id="m-1", content="Sure thing")

    sent = json.loads(route.calls.last.request.content)
    assert sent["action"] == "reply"
    assert sent["mode"] == "draft"  # never remove: without it Zoho SENDS
    assert sent["content"] == "Sure thing"
    assert result == {"id": "msg-reply-1"}


async def test_reply_draft_uses_reply_all_action_when_asked(respx_mock, zoho_client):
    respx_mock.get("https://mail.zoho.com/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "accountId": ACCOUNT_ID,
                        "isDefaultAccount": True,
                        "timeZone": "America/Los_Angeles",
                        "primaryEmailAddress": "me@example.com",
                    }
                ]
            },
        )
    )
    route = respx_mock.post(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/m-1"
    ).mock(return_value=httpx.Response(200, json={"data": {"messageId": "r-1"}}))

    await zoho_client.reply_draft(message_id="m-1", content="Sure", reply_all=True)

    assert json.loads(route.calls.last.request.content)["action"] == "replyall"


@pytest.mark.parametrize("bad_content", ["", "   "])
async def test_reply_draft_rejects_blank_content_without_a_request(
    respx_mock, zoho_client, bad_content
):
    route = respx_mock.post(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/m-1"
    )

    with pytest.raises(ZohoAPIError, match="content"):
        await zoho_client.reply_draft(message_id="m-1", content=bad_content)

    assert not route.called


ORIGINAL_SOURCE = (
    "From: Jamie Rivera <jamie@example.com>\r\n"
    "To: <ken@example.com>\r\n"
    "Date: Mon, 27 Jul 2026 08:24:04 -0700\r\n"
    "Subject: Quarterly numbers\r\n"
    "\r\n"
    "body ignored -- the HTML comes from the content endpoint\r\n"
)
ORIGINAL_HTML = (
    "<div><b>Quarterly numbers</b></div>"
    '<img src="/mail/ImageDisplay?na=123&amp;f=1.png&amp;mode=inline">'
)


def mock_forward_endpoints(respx_mock, *, original_html=ORIGINAL_HTML):
    """Accounts lookup, the original's source and HTML, plus the compose POST.

    Forwarding reads the original through two endpoints -- headers from
    `originalmessage` (account-scoped) and the body from the folder-scoped
    content endpoint -- then posts an ordinary draft, because Zoho's
    action=forward is broken. See docs/zoho-api-notes.md.
    """
    respx_mock.get("https://mail.zoho.com/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "accountId": ACCOUNT_ID,
                        "isDefaultAccount": True,
                        "timeZone": "America/Los_Angeles",
                        "primaryEmailAddress": "me@example.com",
                    }
                ]
            },
        )
    )
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/m-1/originalmessage"
    ).mock(
        return_value=httpx.Response(200, json={"data": {"content": ORIGINAL_SOURCE}})
    )
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}"
        f"/folders/f-1/messages/m-1/content"
    ).mock(
        return_value=httpx.Response(
            200, json={"data": {"messageId": "m-1", "content": original_html}}
        )
    )
    # Named so mock_attachment_copy can replace it: respx matches routes in
    # insertion order, so a second registration of the same URL never wins.
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}"
        f"/folders/f-1/messages/m-1/attachmentinfo",
        name="attachmentinfo",
    ).mock(return_value=httpx.Response(200, json={"data": {"attachments": []}}))
    return respx_mock.post(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages"
    ).mock(return_value=httpx.Response(200, json={"data": {"messageId": "msg-fwd-1"}}))


def mock_attachment_copy(respx_mock, attachments=(("a-1", "invoice.pdf", 11),)):
    """The three calls that carry one original's attachments to a new draft.

    Zoho has no server-side "forward with attachments": the bytes come down
    from the original and go back up through the upload endpoint, which mints
    the storeName/attachmentPath descriptors the compose body wants.
    """
    respx_mock["attachmentinfo"].mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "attachments": [
                        {
                            "attachmentId": att_id,
                            "attachmentName": name,
                            "attachmentSize": size,
                        }
                        for att_id, name, size in attachments
                    ]
                }
            },
        )
    )
    for att_id, name, size in attachments:
        respx_mock.get(
            f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}"
            f"/folders/f-1/messages/m-1/attachments/{att_id}"
        ).mock(
            return_value=httpx.Response(
                200,
                content=b"x" * size,
                headers={"content-disposition": f"attachment; filename = {name}"},
            )
        )
    return respx_mock.post(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/attachments"
    ).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "storeName": "709548548",
                            "attachmentName": name,
                            "attachmentPath": f"/Mail/abc-{name}",
                        }
                    ]
                },
            )
            for _, name, _ in attachments
        ]
    )


async def test_forward_draft_carries_the_originals_attachments(respx_mock, zoho_client):
    # The reason this exists at all: a forward that silently drops the PDF
    # everyone cares about is worse than an error.
    route = mock_forward_endpoints(respx_mock)
    upload = mock_attachment_copy(
        respx_mock,
        attachments=(("a-1", "invoice.pdf", 11), ("a-2", "receipt.pdf", 22)),
    )

    await zoho_client.forward_draft(
        message_id="m-1", folder_id="f-1", to=["fwd@example.com"]
    )

    assert upload.call_count == 2
    sent = json.loads(route.calls.last.request.content)
    assert [a["attachmentName"] for a in sent["attachments"]] == [
        "invoice.pdf",
        "receipt.pdf",
    ]
    assert sent["attachments"][0]["storeName"] == "709548548"
    assert sent["attachments"][0]["attachmentPath"] == "/Mail/abc-invoice.pdf"


async def test_forward_draft_uploads_the_bytes_it_downloaded(respx_mock, zoho_client):
    mock_forward_endpoints(respx_mock)
    upload = mock_attachment_copy(respx_mock)

    await zoho_client.forward_draft(
        message_id="m-1", folder_id="f-1", to=["fwd@example.com"]
    )

    request = upload.calls.last.request
    assert request.url.params["uploadType"] == "multipart"  # 0 bytes without it
    assert request.url.params["fileName"] == "invoice.pdf"
    assert b"x" * 11 in request.content


async def test_forward_draft_omits_attachments_when_the_original_has_none(
    respx_mock, zoho_client
):
    route = mock_forward_endpoints(respx_mock)
    upload = mock_attachment_copy(respx_mock, attachments=())

    await zoho_client.forward_draft(
        message_id="m-1", folder_id="f-1", to=["fwd@example.com"]
    )

    assert not upload.called
    assert "attachments" not in json.loads(route.calls.last.request.content)


async def test_forward_draft_fails_loudly_when_an_attachment_is_too_large(
    respx_mock, zoho_client
):
    # Dropping it and composing anyway would produce a draft that looks
    # complete and isn't -- the failure mode this tool was built to end.
    mock_forward_endpoints(respx_mock)
    respx_mock["attachmentinfo"].mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "attachments": [
                        {
                            "attachmentId": "a-1",
                            "attachmentName": "huge.zip",
                            "attachmentSize": 99,
                        }
                    ]
                }
            },
        )
    )
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}"
        f"/folders/f-1/messages/m-1/attachments/a-1"
    ).mock(
        return_value=httpx.Response(
            200,
            content=b"",
            headers={
                "content-length": str(MAX_FORWARD_ATTACHMENT_BYTES + 1),
                "content-disposition": "attachment; filename = huge.zip",
            },
        )
    )
    post = respx_mock.post(f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages")

    with pytest.raises(ZohoAPIError, match="huge.zip"):
        await zoho_client.forward_draft(
            message_id="m-1", folder_id="f-1", to=["fwd@example.com"]
        )

    assert not post.called


async def test_forward_draft_reports_an_upload_failure_without_composing(
    respx_mock, zoho_client
):
    mock_forward_endpoints(respx_mock)
    mock_attachment_copy(respx_mock)
    respx_mock.post(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/attachments"
    ).mock(return_value=httpx.Response(413, json={"data": {"errorCode": "TOO_BIG"}}))
    post = respx_mock.post(f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages")

    with pytest.raises(ZohoAPIError):
        await zoho_client.forward_draft(
            message_id="m-1", folder_id="f-1", to=["fwd@example.com"]
        )

    assert not post.called


async def test_forward_draft_rejects_an_upload_response_missing_its_descriptor(
    respx_mock, zoho_client
):
    # Zoho is a third party; a 200 whose shape we didn't expect must not
    # become a draft with a malformed attachments array.
    mock_forward_endpoints(respx_mock)
    mock_attachment_copy(respx_mock)
    respx_mock.post(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/attachments"
    ).mock(return_value=httpx.Response(200, json={"status": {"code": 200}}))

    with pytest.raises(ZohoAPIError, match="invoice.pdf"):
        await zoho_client.forward_draft(
            message_id="m-1", folder_id="f-1", to=["fwd@example.com"]
        )


async def test_forward_draft_posts_a_draft_carrying_the_originals_html(
    respx_mock, zoho_client
):
    route = mock_forward_endpoints(respx_mock)

    result = await zoho_client.forward_draft(
        message_id="m-1", folder_id="f-1", to=["fwd@example.com"], content="FYI"
    )

    sent = json.loads(route.calls.last.request.content)
    assert sent["mode"] == "draft"  # never remove: without it Zoho SENDS
    assert sent["toAddress"] == "fwd@example.com"
    assert sent["subject"] == "Fwd: Quarterly numbers"
    assert sent["mailFormat"] == "html"
    assert "FYI" in sent["content"]
    assert "<b>Quarterly numbers</b>" in sent["content"]
    assert "Forwarded message" in sent["content"]
    assert "From: Jamie Rivera &lt;jamie@example.com&gt;" in sent["content"]
    assert result == {"id": "msg-fwd-1"}


# The regression this whole feature exists for: reading a message through
# get_email flattens its HTML, so a forward rebuilt from that text arrives as
# plain prose. The body must come from the raw content endpoint instead.
async def test_forward_draft_does_not_flatten_the_original_to_plain_text(
    respx_mock, zoho_client
):
    route = mock_forward_endpoints(
        respx_mock,
        original_html="<table><tr><td><b>Q1</b></td></tr></table>",
    )

    await zoho_client.forward_draft(
        message_id="m-1", folder_id="f-1", to=["fwd@example.com"]
    )

    content = json.loads(route.calls.last.request.content)["content"]
    assert "<table>" in content
    assert "<b>Q1</b>" in content


async def test_forward_draft_passes_inline_image_references_through_untouched(
    respx_mock, zoho_client
):
    # Verified end-to-end on a real send: Zoho turns these relative references
    # into real base64 MIME image parts, carrying the original's Content-IDs.
    # Rewriting them is work Zoho undoes on store, so we leave them alone.
    route = mock_forward_endpoints(respx_mock)

    await zoho_client.forward_draft(
        message_id="m-1", folder_id="f-1", to=["fwd@example.com"]
    )

    content = json.loads(route.calls.last.request.content)["content"]
    assert 'src="/mail/ImageDisplay?na=123&amp;f=1.png&amp;mode=inline"' in content


async def test_forward_draft_allows_an_empty_added_note(respx_mock, zoho_client):
    route = mock_forward_endpoints(respx_mock)

    await zoho_client.forward_draft(
        message_id="m-1", folder_id="f-1", to=["fwd@example.com"]
    )

    content = json.loads(route.calls.last.request.content)["content"]
    assert "Forwarded message" in content


async def test_forward_draft_carries_cc_and_bcc(respx_mock, zoho_client):
    route = mock_forward_endpoints(respx_mock)

    await zoho_client.forward_draft(
        message_id="m-1",
        folder_id="f-1",
        to=["fwd@example.com"],
        cc=[" c@example.com "],
        bcc=["  "],
    )

    sent = json.loads(route.calls.last.request.content)
    assert sent["ccAddress"] == "c@example.com"
    assert "bccAddress" not in sent


@pytest.mark.parametrize("bad_to", [[], [""], ["   "]])
async def test_forward_draft_rejects_blank_recipients_without_any_request(
    respx_mock, zoho_client, bad_to
):
    # Validated before the two reads, not just before the post -- otherwise a
    # bad recipient list costs two round trips before failing.
    with pytest.raises(ZohoAPIError, match="recipient"):
        await zoho_client.forward_draft(
            message_id="m-1", folder_id="f-1", to=bad_to, content="FYI"
        )

    assert not respx_mock.calls


@pytest.mark.parametrize("bad_id", ["", "   "])
async def test_forward_draft_rejects_a_blank_message_id_without_any_request(
    respx_mock, zoho_client, bad_id
):
    with pytest.raises(ZohoAPIError, match="message_id"):
        await zoho_client.forward_draft(
            message_id=bad_id, folder_id="f-1", to=["fwd@example.com"]
        )

    assert not respx_mock.calls


@pytest.mark.parametrize("bad_id", ["", "   "])
async def test_forward_draft_rejects_a_blank_folder_id_without_any_request(
    respx_mock, zoho_client, bad_id
):
    with pytest.raises(ZohoAPIError, match="folder_id"):
        await zoho_client.forward_draft(
            message_id="m-1", folder_id=bad_id, to=["fwd@example.com"]
        )

    assert not respx_mock.calls


async def test_forward_draft_wraps_a_failure_reading_the_original(
    respx_mock, zoho_client
):
    respx_mock.get("https://mail.zoho.com/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "accountId": ACCOUNT_ID,
                        "isDefaultAccount": True,
                        "timeZone": "America/Los_Angeles",
                        "primaryEmailAddress": "me@example.com",
                    }
                ]
            },
        )
    )
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/m-1/originalmessage"
    ).mock(return_value=httpx.Response(404, json={"data": {"errorCode": "INVALID_ID"}}))
    post = respx_mock.post(f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages")

    with pytest.raises(ZohoAPIError):
        await zoho_client.forward_draft(
            message_id="m-1", folder_id="f-1", to=["fwd@example.com"]
        )

    # Nothing half-composed: a failed read must not produce a draft.
    assert not post.called


async def test_forward_draft_handles_an_original_with_no_html_body(
    respx_mock, zoho_client
):
    route = mock_forward_endpoints(respx_mock, original_html="")

    await zoho_client.forward_draft(
        message_id="m-1", folder_id="f-1", to=["fwd@example.com"], content="FYI"
    )

    content = json.loads(route.calls.last.request.content)["content"]
    assert "FYI" in content
    assert "Forwarded message" in content


async def test_get_email_raises_clear_error_when_data_key_absent(
    respx_mock, zoho_client
):
    # Zoho is documented to return 200 with an error-shaped body (fb_not_enabled,
    # and 404-with-Invalid-Input on note create), so a 200 missing "data" is a
    # realistic response, not a hypothetical one.
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}"
        f"/folders/1122334455/messages/1730217600123456789/content"
    ).mock(return_value=httpx.Response(200, json={"status": {"code": 200}}))

    with pytest.raises(ZohoAPIError):
        await zoho_client.get_email(
            message_id="1730217600123456789", folder_id="1122334455"
        )


# etag is mandatory for both update and delete. A shared or subscribed calendar
# could plausibly omit it, and a bare KeyError as the tool result is exactly the
# raw-exception leak the error-handling rules forbid.
async def test_update_event_raises_clear_error_when_etag_absent(
    respx_mock, zoho_client
):
    raw = _raw_event_for_update()
    del raw["etag"]
    respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    ).mock(return_value=httpx.Response(200, json={"events": [raw]}))
    put_route = respx_mock.put(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    )

    with pytest.raises(ZohoAPIError, match="etag"):
        await zoho_client.update_event(uid="evt-1", title="New")

    assert not put_route.called


async def test_delete_event_raises_clear_error_when_etag_absent(
    respx_mock, zoho_client
):
    raw = _raw_event_for_update()
    del raw["etag"]
    respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    ).mock(return_value=httpx.Response(200, json={"events": [raw]}))
    delete_route = respx_mock.delete(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events/evt-1"
    )

    with pytest.raises(ZohoAPIError, match="etag"):
        await zoho_client.delete_event(uid="evt-1")

    assert not delete_route.called


# The draft-mode assertions elsewhere all run on a client with sending
# DISABLED, which left the guard untested in the one configuration where
# mailing a stranger is possible. Coupling as_draft to _allow_auto_send would
# pass every other test in this file.
async def test_create_draft_still_sets_mode_draft_when_auto_send_enabled(
    respx_mock, sending_client
):
    route = mock_compose_endpoints(respx_mock)

    await sending_client.create_draft(
        to=["a@example.com"], subject="Hi", content="Body"
    )

    sent = json.loads(route.calls.last.request.content)
    assert sent["mode"] == "draft"  # never remove: without it Zoho SENDS


async def test_reply_draft_still_sets_mode_draft_when_auto_send_enabled(
    respx_mock, sending_client
):
    respx_mock.get("https://mail.zoho.com/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "accountId": ACCOUNT_ID,
                        "isDefaultAccount": True,
                        "timeZone": "America/Los_Angeles",
                        "primaryEmailAddress": "me@example.com",
                    }
                ]
            },
        )
    )
    route = respx_mock.post(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/m-1"
    ).mock(return_value=httpx.Response(200, json={"data": {"messageId": "r-1"}}))

    await sending_client.reply_draft(message_id="m-1", content="Sure")

    sent = json.loads(route.calls.last.request.content)
    assert sent["mode"] == "draft"  # never remove: without it Zoho SENDS


async def test_forward_draft_still_sets_mode_draft_when_auto_send_enabled(
    respx_mock, sending_client
):
    route = mock_forward_endpoints(respx_mock)

    await sending_client.forward_draft(
        message_id="m-1", folder_id="f-1", to=["fwd@example.com"], content="FYI"
    )

    sent = json.loads(route.calls.last.request.content)
    assert sent["mode"] == "draft"  # never remove: without it Zoho SENDS


# _compose already strips and rejects blank recipients; _update_message did
# not, so a blank id assembled by an LLM from a partial parse went straight to
# Zoho. Given this vendor's documented habit of accepting bad input silently,
# that risks the operation applying to a subset while returning 200.
async def test_batch_write_strips_blank_message_ids(respx_mock, zoho_client):
    route = respx_mock.put(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/updatemessage"
    ).mock(return_value=httpx.Response(200, json={"status": {"code": 200}}))

    await zoho_client.mark_as_read(message_ids=["111", "", "  ", "222"])

    sent = json.loads(route.calls.last.request.content)
    assert sent["messageId"] == ["111", "222"]


# Zoho's real success body, verified live 2026-08-04 (docs/zoho-api-notes.md).
# It is byte-identical for one message and for fifty, and identical again for a
# messageId that doesn't exist -- so the ids we submitted are the only record of
# what the request covered, and they have to come back out.
_UPDATEMESSAGE_SUCCESS = {"status": {"code": 200, "description": "success"}}


async def test_batch_write_returns_the_ids_it_submitted(respx_mock, zoho_client):
    respx_mock.put(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/updatemessage"
    ).mock(return_value=httpx.Response(200, json=_UPDATEMESSAGE_SUCCESS))

    assert await zoho_client.mark_as_read(message_ids=["111", "222"]) == ["111", "222"]


async def test_batch_write_returns_the_stripped_ids_not_the_ones_passed_in(
    respx_mock, zoho_client
):
    """What comes back describes the request Zoho got, not the caller's input.

    The blank-stripping above means those can differ. If the return value were
    the caller's list, a count derived from it would claim four messages were
    covered by a request that named two.
    """
    respx_mock.put(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/updatemessage"
    ).mock(return_value=httpx.Response(200, json=_UPDATEMESSAGE_SUCCESS))

    submitted = await zoho_client.mark_as_read(message_ids=["111", "", "  ", "222"])

    assert submitted == ["111", "222"]


@pytest.mark.parametrize(
    "write",
    [
        lambda c: c.mark_as_read(message_ids=["111"]),
        lambda c: c.mark_as_unread(message_ids=["111"]),
        lambda c: c.move_email(message_ids=["111"], folder_id="f-1"),
        lambda c: c.add_label(message_ids=["111"], label_id="l-1"),
        lambda c: c.remove_label(message_ids=["111"], label_id="l-1"),
    ],
    ids=["mark_as_read", "mark_as_unread", "move_email", "add_label", "remove_label"],
)
async def test_every_batch_write_returns_its_submitted_ids(
    respx_mock, zoho_client, write
):
    respx_mock.put(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/updatemessage"
    ).mock(return_value=httpx.Response(200, json=_UPDATEMESSAGE_SUCCESS))

    assert await write(zoho_client) == ["111"]


@pytest.mark.parametrize("blank_ids", [[""], ["   "], ["", "  ", "\t"]])
async def test_batch_write_rejects_all_blank_message_ids_without_a_request(
    respx_mock, zoho_client, blank_ids
):
    route = respx_mock.put(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/updatemessage"
    )

    with pytest.raises(ZohoAPIError, match="message_ids"):
        await zoho_client.mark_as_read(message_ids=blank_ids)

    assert not route.called


# The 31-day cap only had reject-side coverage, so flipping > to >= would have
# broken nothing. search_emails already has this counterpart for its limit.
async def test_list_events_accepts_a_range_of_exactly_31_days(respx_mock, zoho_client):
    mock_pacific_accounts_endpoint(respx_mock)
    route = respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{CALENDAR_UID}/events"
    ).mock(return_value=httpx.Response(200, json={"events": []}))
    start = datetime(2026, 7, 1, tzinfo=timezone.utc)

    await zoho_client.list_events(start=start, end=start + timedelta(days=31))

    assert route.called


@pytest.mark.parametrize(("limit", "start"), [(1, 1), (200, 1), (20, 999)])
async def test_list_emails_accepts_boundary_limit_and_start(
    respx_mock, zoho_client, limit, start
):
    mock_pacific_accounts_endpoint(respx_mock)
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/view"
    ).mock(return_value=httpx.Response(200, json={"data": []}))

    await zoho_client.list_emails(limit=limit, start=start)

    assert route.called


# One folder missing folderType used to fail the whole call, and the folder map
# feeds both search_emails and list_emails -- so a single odd folder made the
# entire mailbox unreadable. The map only drives a convenience filter, so an
# unclassifiable folder should be skipped, not fatal.
async def test_search_emails_survives_one_malformed_folder(respx_mock, zoho_client):
    mock_pacific_accounts_endpoint(respx_mock)
    respx_mock.get(f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/folders").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"folderId": "1122334455", "folderType": "Inbox"},
                    {"folderId": "broken"},  # no folderType
                    {"folderId": "sent-folder-id", "folderType": "Sent"},
                ]
            },
        )
    )
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
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

    results = await zoho_client.search_emails(query="x")

    # Inbox kept, Sent still excluded -- the unclassifiable folder didn't
    # prevent either decision.
    assert {r["id"] for r in results} == {"1"}


# An LLM passing group_id="" instead of omitting the argument is realistic
# client behavior, and it used to build ".../groups/" -- a wrong URL rather
# than personal scope or a clear error.
@pytest.mark.parametrize("blank", ["", "   "])
async def test_blank_group_id_is_treated_as_personal_scope(
    respx_mock, zoho_client, blank
):
    route = respx_mock.get("https://mail.zoho.com/api/tasks/me").mock(
        return_value=httpx.Response(200, json={"data": {"tasks": []}})
    )

    await zoho_client.list_tasks(group_id=blank)

    assert route.called


# timedelta rejects magnitudes over 999999999 days, so a huge days_back leaked
# a raw OverflowError instead of a usable message.
@pytest.mark.parametrize("huge", [10**9, 10**12])
async def test_search_emails_rejects_absurd_days_back_without_a_request(
    respx_mock, zoho_client, huge
):
    mock_pacific_accounts_endpoint(respx_mock)
    route = respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/search"
    )

    with pytest.raises(ZohoAPIError, match="days_back"):
        await zoho_client.search_emails(query="", days_back=huge)

    assert not route.called


# cc=[""] is truthy, so it produced an empty ccAddress header rather than
# being omitted. Recipients in `to` are already stripped; cc/bcc weren't.
async def test_blank_cc_and_bcc_entries_are_dropped(respx_mock, zoho_client):
    route = mock_compose_endpoints(respx_mock)

    await zoho_client.create_draft(
        to=["a@example.com", "  ", ""],
        subject="S",
        content="B",
        cc=[""],
        bcc=["  ", "d@example.com"],
    )

    sent = json.loads(route.calls.last.request.content)
    assert sent["toAddress"] == "a@example.com"
    assert "ccAddress" not in sent
    assert sent["bccAddress"] == "d@example.com"


# The flag reaching the normalizer is the part that was actually missing:
# `normalize_email_summary` grew the parameter, but a listing tool that
# forgot to pass it would look correct in every unit test of the normalizer
# and still hand the model padded snippets.
# search_emails refuses an empty query by design, so each tool gets the
# minimum arguments it considers valid.
_LISTING_CALLS = {"search_emails": {"query": "entire:deal"}, "list_emails": {}}


def _listing_emails(result):
    """The emails out of either listing tool's return.

    `search_emails` returns a bare list; `list_emails` returns
    `(emails, has_more)` because its page-level filtering makes the returned
    length unusable as an end-of-results signal.
    """
    return result[0] if isinstance(result, tuple) else result


@pytest.mark.parametrize("tool", list(_LISTING_CALLS))
async def test_listing_tools_pass_strip_invisible_chars_through(
    respx_mock, http_client, tool
):
    combining_grapheme_joiner = chr(0x034F)
    mock_pacific_accounts_endpoint(respx_mock)
    mock_folder_types_endpoint(respx_mock)
    raw = _raw_email(message_id="1", folder_id="1122334455")
    raw["summary"] = f"Deal{combining_grapheme_joiner} inside"
    for endpoint in ("search", "view"):
        respx_mock.get(
            f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/{endpoint}"
        ).mock(return_value=httpx.Response(200, json={"data": [raw]}))
    stripping_client = ZohoClient(
        token_manager=FakeTokenManager(),
        http_client=http_client,
        account_id=ACCOUNT_ID,
        calendar_uid=CALENDAR_UID,
        strip_invisible_chars=True,
    )

    results = _listing_emails(
        await getattr(stripping_client, tool)(**_LISTING_CALLS[tool])
    )

    assert results[0]["snippet"] == "Deal inside"


@pytest.mark.parametrize("tool", list(_LISTING_CALLS))
async def test_listing_tools_keep_padding_when_the_flag_is_off(
    respx_mock, http_client, zoho_client, tool
):
    combining_grapheme_joiner = chr(0x034F)
    mock_pacific_accounts_endpoint(respx_mock)
    mock_folder_types_endpoint(respx_mock)
    raw = _raw_email(message_id="1", folder_id="1122334455")
    padded = f"Deal{combining_grapheme_joiner} inside"
    raw["summary"] = padded
    for endpoint in ("search", "view"):
        respx_mock.get(
            f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}/messages/{endpoint}"
        ).mock(return_value=httpx.Response(200, json={"data": [raw]}))

    results = _listing_emails(await getattr(zoho_client, tool)(**_LISTING_CALLS[tool]))

    assert results[0]["snippet"] == padded
