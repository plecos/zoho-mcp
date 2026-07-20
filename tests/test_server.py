from zoho_mcp.server import create_server


class FakeZohoClient:
    async def search_emails(self, query, limit=20):
        return []

    async def get_email(self, message_id, folder_id):
        return {}

    async def list_events(self, start, end, calendar_id=None):
        return []

    async def get_event(self, uid, calendar_id=None):
        return {}

    async def list_calendars(self):
        return []

    async def list_attachments(self, message_id, folder_id):
        return []

    async def list_folders(self):
        return []

    async def list_labels(self):
        return []

    async def list_branches(self):
        return []

    async def list_resources(self, branch_id, building_id, floor_id):
        return []

    async def list_tasks(self, limit=20, offset=0):
        return [], False

    async def get_task(self, task_id):
        return {}

    async def list_notes(self, limit=20, after=0):
        return []

    async def get_note(self, note_id):
        return {}

    async def list_bookmarks(self, limit=20, after=0):
        return []

    async def get_bookmark(self, bookmark_id):
        return {}


class FakeContactsClient:
    async def search_contacts(self, query="", limit=20, status="active"):
        return [], False

    async def get_contact(self, contact_id, scope):
        return {}

    async def count_contacts(self):
        return {"personal": 0, "organization": 0, "total": 0}


async def test_create_server_registers_all_nineteen_tools():
    server = create_server(FakeZohoClient(), FakeContactsClient())

    tools = await server.list_tools()
    tool_names = {tool.name for tool in tools}

    assert tool_names == {
        "search_emails",
        "get_email",
        "list_attachments",
        "list_folders",
        "list_labels",
        "list_events",
        "get_event",
        "list_calendars",
        "list_tasks",
        "get_task",
        "list_notes",
        "get_note",
        "list_bookmarks",
        "get_bookmark",
        "list_branches",
        "list_resources",
        "search_contacts",
        "get_contact",
        "count_contacts",
    }


async def test_registered_tools_are_annotated_read_only():
    server = create_server(FakeZohoClient(), FakeContactsClient())

    tools = await server.list_tools()

    assert all(tool.annotations.readOnlyHint is True for tool in tools)
