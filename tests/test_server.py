import httpx

from zoho_mcp.server import create_server
from zoho_mcp.zoho.auth import ZohoTokenManager


class FakeZohoClient:
    async def search_emails(self, query, limit=20, days_back=None):
        return []

    async def list_emails(self, status="all", folder_id=None, limit=20, start=1):
        return []

    async def get_email(self, message_id, folder_id):
        return {}

    async def list_events(self, start, end, calendar_id=None):
        return []

    async def get_event(self, uid, calendar_id=None):
        return {}

    async def list_calendars(self):
        return []

    async def get_freebusy(self, email, start, end):
        return []

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
        return {}

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
        return {}

    async def delete_event(self, uid, calendar_id=None):
        return None

    async def list_attachments(self, message_id, folder_id):
        return []

    async def get_attachment(self, message_id, folder_id, attachment_id):
        return {}

    async def get_email_source(self, message_id, include_raw=False):
        return {}

    async def list_folders(self):
        return []

    async def list_labels(self):
        return []

    async def list_signatures(self):
        return []

    async def create_draft(self, to, subject, content, cc=None, bcc=None):
        return {}

    async def reply_draft(self, message_id, content, reply_all=False):
        return {}

    async def forward_draft(
        self, message_id, folder_id, to, content="", cc=None, bcc=None
    ):
        return {}

    async def send_email(self, to, subject, content, cc=None, bcc=None):
        return {}

    async def mark_as_read(self, message_ids):
        return None

    async def mark_as_unread(self, message_ids):
        return None

    async def move_email(self, message_ids, folder_id):
        return None

    async def add_label(self, message_ids, label_id):
        return None

    async def remove_label(self, message_ids, label_id):
        return None

    async def list_branches(self):
        return []

    async def list_resources(self, branch_id, building_id, floor_id):
        return []

    async def list_tasks(self, limit=20, offset=0, group_id=None, view=None):
        return [], False

    async def create_task(self, title, description="", priority="", group_id=None):
        return {}

    async def get_task(self, task_id):
        return {}

    async def list_notes(self, limit=20, after=0, group_id=None, oldest_first=False):
        return []

    async def create_note(self, content, title="", group_id=None):
        return {}

    async def get_note(self, note_id):
        return {}

    async def list_bookmarks(
        self, limit=20, after=0, group_id=None, oldest_first=False
    ):
        return []

    async def create_bookmark(self, url, title, summary="", group_id=None):
        return {}

    async def get_bookmark(self, bookmark_id):
        return {}

    async def list_groups(self):
        return []


class FakeContactsClient:
    async def search_contacts(self, query="", limit=20, status="active"):
        return [], False

    async def get_contact(self, contact_id, scope):
        return {}

    async def count_contacts(self):
        return {"personal": 0, "organization": 0, "total": 0}


def build_server():
    """A server wired to fakes -- registration and annotations only, no I/O."""
    http_client = httpx.AsyncClient()
    token_manager = ZohoTokenManager(
        client_id="id",
        client_secret="secret",
        refresh_token="refresh",
        http_client=http_client,
    )
    return create_server(
        FakeZohoClient(), FakeContactsClient(), token_manager, http_client
    )


async def test_create_server_registers_every_expected_tool():
    server = build_server()

    tools = await server.list_tools()
    tool_names = {tool.name for tool in tools}

    assert tool_names == {
        "authenticate",
        "search_emails",
        "list_emails",
        "get_email",
        "list_attachments",
        "get_attachment",
        "get_email_source",
        "list_folders",
        "list_labels",
        "list_signatures",
        "create_draft",
        "reply_draft",
        "forward_draft",
        "send_email",
        "mark_as_read",
        "mark_as_unread",
        "move_email",
        "add_label",
        "remove_label",
        "list_events",
        "get_event",
        "list_calendars",
        "get_freebusy",
        "create_event",
        "update_event",
        "delete_event",
        "list_tasks",
        "create_task",
        "get_task",
        "list_notes",
        "create_note",
        "get_note",
        "list_bookmarks",
        "list_groups",
        "create_bookmark",
        "get_bookmark",
        "list_branches",
        "list_resources",
        "search_contacts",
        "get_contact",
        "count_contacts",
    }


async def test_every_tool_has_a_human_readable_title():
    # MCP clients show `title` in permission prompts and tool pickers; a
    # missing one falls back to the snake_case function name.
    server = build_server()

    tools = await server.list_tools()
    untitled = [t.name for t in tools if not (t.title or "").strip()]

    assert untitled == []


async def test_tool_titles_are_prose_not_repeated_function_names():
    server = build_server()

    tools = await server.list_tools()
    titles = [t.title for t in tools]

    assert [t.name for t in tools if "_" in (t.title or "")] == []
    assert len(set(titles)) == len(titles)


_WRITE_TOOL_NAMES = {
    # Not a Zoho write, but definitely not read-only: it opens a browser and
    # obtains a credential.
    "authenticate",
    "create_draft",
    "reply_draft",
    "forward_draft",
    "send_email",
    "create_task",
    "create_note",
    "create_bookmark",
    "create_event",
    "update_event",
    "delete_event",
    "mark_as_read",
    "mark_as_unread",
    "move_email",
    "add_label",
    "remove_label",
}


async def test_read_only_tools_are_annotated_correctly():
    server = build_server()

    tools = await server.list_tools()
    read_only_tools = [t for t in tools if t.name not in _WRITE_TOOL_NAMES]

    assert all(tool.annotations.readOnlyHint is True for tool in read_only_tools)


async def test_write_tools_are_not_annotated_read_only():
    server = build_server()

    tools = await server.list_tools()
    write_tools = [t for t in tools if t.name in _WRITE_TOOL_NAMES]

    assert len(write_tools) == len(_WRITE_TOOL_NAMES)
    assert all(tool.annotations.readOnlyHint is False for tool in write_tools)


async def test_send_email_is_annotated_as_irreversible_and_outward_facing():
    # send_email is the only tool that reaches a third party and cannot be
    # undone; its annotations must say so, so clients can gate it.
    server = build_server()

    tools = await server.list_tools()
    send = next(t for t in tools if t.name == "send_email")

    assert send.annotations.readOnlyHint is False
    assert send.annotations.destructiveHint is True
    assert send.annotations.idempotentHint is False
    assert send.annotations.openWorldHint is True
