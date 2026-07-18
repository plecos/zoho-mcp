from zoho_mcp.server import create_server


class FakeZohoClient:
    async def search_emails(self, query, limit=20):
        return []

    async def get_email(self, message_id, folder_id):
        return {}

    async def list_events(self, start, end):
        return []


async def test_create_server_registers_all_three_tools():
    server = create_server(FakeZohoClient())

    tools = await server.list_tools()
    tool_names = {tool.name for tool in tools}

    assert tool_names == {"search_emails", "get_email", "list_events"}


async def test_registered_tools_are_annotated_read_only():
    server = create_server(FakeZohoClient())

    tools = await server.list_tools()

    assert all(tool.annotations.readOnlyHint is True for tool in tools)
