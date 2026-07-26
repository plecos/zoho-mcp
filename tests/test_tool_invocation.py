"""Invoke every registered MCP tool and assert it calls the right client method.

`test_server.py` covers registration -- names and annotations -- but never
*calls* a tool, which left `create_server`'s 37 hand-written closures with no
behavioral coverage at all. Each closure forwards arguments to a client method,
and nothing verified that any of them forwards to the *right* method with the
*right* arguments.

The bug class this exists to catch: `create_draft` and `send_email` are
adjacent 20-line blocks with identical signatures, so a copy-paste leaving
`create_draft` calling `mail_tools.send_email` would still register 37
correctly-annotated tools and pass every other test in the suite -- while the
safe tool mails a stranger. The same risk applies to any of the other 36.

Two things make this stronger than a hand-written fake:

- `create_autospec` enforces the real client signatures, so a tool passing an
  argument the client doesn't accept fails here. A hand-written fake drifts
  instead: `test_server.py`'s fake was missing `search_emails`' `days_back`
  parameter entirely, which no test noticed precisely because no test ever
  invoked the tool.
- Every case asserts no *other* client method was called, which is what
  actually catches a mis-wired closure.
"""

import inspect
from unittest.mock import AsyncMock, create_autospec

import httpx
import pytest

from zoho_mcp.server import create_server
from zoho_mcp.zoho.auth import ZohoTokenManager
from zoho_mcp.zoho.client import ZohoClient
from zoho_mcp.zoho.contacts_client import ZohoContactsClient

# `authenticate` is the one registered tool that doesn't forward to a client:
# it mutates the token manager and runs a browser flow, covered by
# tests/tools/test_authenticate.py instead.
UNFORWARDED_TOOLS = {"authenticate"}

# (tool name, arguments in, expected client method, expected forwarded kwargs).
#
# Arguments are deliberately non-default so a closure that drops one, hardcodes
# one, or swaps two same-typed parameters shows up as a mismatch. Where a tool
# has optional parameters they're passed explicitly for the same reason.
MAIL_CASES = [
    (
        "search_emails",
        {"query": "subject:x", "limit": 7, "days_back": 3},
        "search_emails",
        {"query": "subject:x", "limit": 7, "days_back": 3},
    ),
    (
        "list_emails",
        {"status": "unread", "folder_id": "f-1", "limit": 50, "start": 21},
        "list_emails",
        {"status": "unread", "folder_id": "f-1", "limit": 50, "start": 21},
    ),
    (
        "get_email",
        {"message_id": "m-1", "folder_id": "f-1"},
        "get_email",
        {"message_id": "m-1", "folder_id": "f-1"},
    ),
    (
        "list_attachments",
        {"message_id": "m-1", "folder_id": "f-1"},
        "list_attachments",
        {"message_id": "m-1", "folder_id": "f-1"},
    ),
    (
        "get_email_source",
        {"message_id": "m-1", "include_raw": True},
        "get_email_source",
        {"message_id": "m-1", "include_raw": True},
    ),
    (
        "get_attachment",
        {"message_id": "m-1", "folder_id": "f-1", "attachment_id": "a-1"},
        "get_attachment",
        {"message_id": "m-1", "folder_id": "f-1", "attachment_id": "a-1"},
    ),
    ("list_folders", {}, "list_folders", {}),
    ("list_labels", {}, "list_labels", {}),
    ("list_signatures", {}, "list_signatures", {}),
    (
        "create_draft",
        {
            "to": ["a@example.com"],
            "subject": "S",
            "content": "B",
            "cc": ["c@example.com"],
            "bcc": ["d@example.com"],
        },
        "create_draft",
        {
            "to": ["a@example.com"],
            "subject": "S",
            "content": "B",
            "cc": ["c@example.com"],
            "bcc": ["d@example.com"],
        },
    ),
    (
        "reply_draft",
        {"message_id": "m-1", "content": "B", "reply_all": True},
        "reply_draft",
        {"message_id": "m-1", "content": "B", "reply_all": True},
    ),
    (
        "send_email",
        {"to": ["a@example.com"], "subject": "S", "content": "B"},
        "send_email",
        {
            "to": ["a@example.com"],
            "subject": "S",
            "content": "B",
            "cc": None,
            "bcc": None,
        },
    ),
    (
        "mark_as_read",
        {"message_ids": ["m-1", "m-2"]},
        "mark_as_read",
        {"message_ids": ["m-1", "m-2"]},
    ),
    (
        "mark_as_unread",
        {"message_ids": ["m-1", "m-2"]},
        "mark_as_unread",
        {"message_ids": ["m-1", "m-2"]},
    ),
    (
        "move_email",
        {"message_ids": ["m-1"], "folder_id": "f-2"},
        "move_email",
        {"message_ids": ["m-1"], "folder_id": "f-2"},
    ),
    (
        "add_label",
        {"message_ids": ["m-1"], "label_id": "l-1"},
        "add_label",
        {"message_ids": ["m-1"], "label_id": "l-1"},
    ),
    (
        "remove_label",
        {"message_ids": ["m-1"], "label_id": "l-1"},
        "remove_label",
        {"message_ids": ["m-1"], "label_id": "l-1"},
    ),
]

# Calendar tools parse ISO strings into datetimes in the tool layer, so the
# forwarded values aren't the inputs. Those conversions are already covered in
# tests/tools/test_calendar_tools.py; here we only assert the right method was
# reached, via `expected_kwargs=None`.
CALENDAR_CASES = [
    (
        "list_events",
        {
            "start": "2026-07-01T00:00:00+00:00",
            "end": "2026-07-10T00:00:00+00:00",
            "calendar_id": "c-1",
        },
        "list_events",
        None,
    ),
    (
        "get_event",
        {"uid": "e-1", "calendar_id": "c-1"},
        "get_event",
        {"uid": "e-1", "calendar_id": "c-1"},
    ),
    ("list_calendars", {}, "list_calendars", {}),
    (
        "get_freebusy",
        {
            "email": "a@example.com",
            "start": "2026-07-01T00:00:00+00:00",
            "end": "2026-07-02T00:00:00+00:00",
        },
        "get_freebusy",
        None,
    ),
    (
        "create_event",
        {
            "title": "T",
            "start": "2026-07-01T10:00:00+00:00",
            "end": "2026-07-01T11:00:00+00:00",
            "description": "D",
            "location": "L",
            "attendees": ["a@example.com"],
            "calendar_id": "c-1",
        },
        "create_event",
        None,
    ),
    (
        "update_event",
        {"uid": "e-1", "title": "New", "calendar_id": "c-1"},
        "update_event",
        None,
    ),
    (
        "delete_event",
        {"uid": "e-1", "calendar_id": "c-1"},
        "delete_event",
        {"uid": "e-1", "calendar_id": "c-1"},
    ),
]

OTHER_CASES = [
    ("list_branches", {}, "list_branches", {}),
    (
        "list_resources",
        {"branch_id": "b-1", "building_id": "bu-1", "floor_id": "fl-1"},
        "list_resources",
        {"branch_id": "b-1", "building_id": "bu-1", "floor_id": "fl-1"},
    ),
    (
        "list_tasks",
        {"limit": 5, "offset": 2, "group_id": "g-1"},
        "list_tasks",
        {"limit": 5, "offset": 2, "group_id": "g-1", "view": None},
    ),
    (
        "create_task",
        {"title": "T", "description": "D", "priority": "high", "group_id": "g-1"},
        "create_task",
        {"title": "T", "description": "D", "priority": "high", "group_id": "g-1"},
    ),
    ("get_task", {"task_id": "t-1"}, "get_task", {"task_id": "t-1"}),
    (
        "list_notes",
        {"limit": 5, "after": 2, "group_id": "g-1", "oldest_first": True},
        "list_notes",
        {"limit": 5, "after": 2, "group_id": "g-1", "oldest_first": True},
    ),
    (
        "create_note",
        {"content": "C", "title": "T", "group_id": "g-1"},
        "create_note",
        {"content": "C", "title": "T", "group_id": "g-1"},
    ),
    ("get_note", {"note_id": "n-1"}, "get_note", {"note_id": "n-1"}),
    (
        "list_bookmarks",
        {"limit": 5, "after": 2, "group_id": "g-1", "oldest_first": True},
        "list_bookmarks",
        {"limit": 5, "after": 2, "group_id": "g-1", "oldest_first": True},
    ),
    (
        "create_bookmark",
        {"url": "https://x.com", "title": "T", "summary": "S", "group_id": "g-1"},
        "create_bookmark",
        {"url": "https://x.com", "title": "T", "summary": "S", "group_id": "g-1"},
    ),
    ("get_bookmark", {"bookmark_id": "b-1"}, "get_bookmark", {"bookmark_id": "b-1"}),
    ("list_groups", {}, "list_groups", {}),
]

CONTACTS_CASES = [
    (
        "search_contacts",
        {"query": "q", "limit": 5, "status": "archived"},
        "search_contacts",
        {"query": "q", "limit": 5, "status": "archived"},
    ),
    (
        "get_contact",
        {"contact_id": "c-1", "scope": "organization"},
        "get_contact",
        {"contact_id": "c-1", "scope": "organization"},
    ),
    ("count_contacts", {}, "count_contacts", {}),
]

ZOHO_CASES = MAIL_CASES + CALENDAR_CASES + OTHER_CASES


def _make_clients():
    zoho = create_autospec(ZohoClient, instance=True, spec_set=True)
    contacts = create_autospec(ZohoContactsClient, instance=True, spec_set=True)
    # These two results get unpacked by their tool wrappers.
    zoho.list_tasks.return_value = ([], False)
    contacts.search_contacts.return_value = ([], False)
    return zoho, contacts


@pytest.fixture
def clients():
    return _make_clients()


@pytest.fixture
def server(clients):
    http_client = httpx.AsyncClient()
    token_manager = ZohoTokenManager(
        client_id="id",
        client_secret="secret",
        refresh_token="refresh",
        http_client=http_client,
    )
    return create_server(*clients, token_manager, http_client)


def _awaited(mock_client) -> list[str]:
    """Names of async methods on `mock_client` that were actually awaited.

    Filtered to `AsyncMock` attributes on purpose: an autospec'd class also
    exposes non-async members whose `await_count` is itself a Mock, not an int.
    """
    names = []
    for name in dir(mock_client):
        if name.startswith("_"):
            continue
        attr = getattr(mock_client, name)
        if isinstance(attr, AsyncMock) and attr.await_count:
            names.append(name)
    return sorted(names)


def _forwarded(mock_client, method: str) -> dict:
    """What actually reached each parameter of `method`, by name.

    Bound through the real signature (autospec preserves it) so the assertion
    doesn't care whether a wrapper passes an argument positionally or by
    keyword -- only that the right value landed on the right parameter.
    """
    call = getattr(mock_client, method).await_args
    bound = inspect.signature(getattr(mock_client, method)).bind(
        *call.args, **call.kwargs
    )
    bound.apply_defaults()
    return dict(bound.arguments)


def _assert_only(mock_client, method: str):
    """Assert `method` was awaited and no sibling method was touched.

    This is the part that catches a mis-wired closure: forwarding to the wrong
    method still satisfies "something was called", but not "only this was".
    """
    called = _awaited(mock_client)
    assert called == [method], f"expected only {method!r} to be awaited, got {called}"


@pytest.mark.parametrize(
    ("tool", "args", "method", "expected_kwargs"),
    ZOHO_CASES,
    ids=[case[0] for case in ZOHO_CASES],
)
async def test_tool_forwards_to_the_right_zoho_client_method(
    server, clients, tool, args, method, expected_kwargs
):
    zoho, contacts = clients

    await server.call_tool(tool, args)

    _assert_only(zoho, method)
    if expected_kwargs is not None:
        assert _forwarded(zoho, method) == expected_kwargs
    # A Zoho tool must never touch the Contacts client, or vice versa.
    assert _awaited(contacts) == []


@pytest.mark.parametrize(
    ("tool", "args", "method", "expected_kwargs"),
    CONTACTS_CASES,
    ids=[case[0] for case in CONTACTS_CASES],
)
async def test_tool_forwards_to_the_right_contacts_client_method(
    server, clients, tool, args, method, expected_kwargs
):
    zoho, contacts = clients

    await server.call_tool(tool, args)

    _assert_only(contacts, method)
    assert _forwarded(contacts, method) == expected_kwargs
    assert _awaited(zoho) == []


async def test_every_registered_tool_is_covered_by_a_case(server):
    """Guard against a new tool being added with no invocation test.

    Without this, the next tool added silently reverts this file to partial
    coverage -- the same way `create_server` went 37 closures deep untested.
    """
    registered = {t.name for t in await server.list_tools()} - UNFORWARDED_TOOLS
    covered = {case[0] for case in ZOHO_CASES + CONTACTS_CASES}

    assert registered == covered, (
        f"untested tools: {sorted(registered - covered)}; "
        f"cases for nonexistent tools: {sorted(covered - registered)}"
    )


async def test_drafting_tools_never_reach_send_email(server, clients):
    """The specific catastrophe this file exists for.

    `create_draft`/`reply_draft` and `send_email` are adjacent closures with
    near-identical shapes. If either drafting tool were wired to the sending
    path, the tool list would look identical and every other test would pass.
    """
    zoho, _ = clients

    await server.call_tool(
        "create_draft", {"to": ["a@example.com"], "subject": "S", "content": "B"}
    )
    await server.call_tool("reply_draft", {"message_id": "m-1", "content": "B"})

    assert zoho.send_email.await_count == 0
    assert zoho.create_draft.await_count == 1
    assert zoho.reply_draft.await_count == 1
