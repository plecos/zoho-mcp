"""MCP tool wrapper for Zoho Mail shared groups.

Shapes LLM-facing input/output only. No HTTP calls, no token logic -- the
Zoho client is injected by the caller (``server.py``), never constructed
here.

Lives in its own module rather than under ``tools/tasks.py`` etc. because
groups span all three of Tasks/Notes/Bookmarks -- it isn't any one
service's concern.
"""

from zoho_mcp.zoho.client import ZohoClient


async def list_groups(client: ZohoClient) -> list[dict]:
    """List every shared group the user belongs to, across Tasks, Notes,
    and Bookmarks.

    Args:
        client: injected Zoho client.

    Returns:
        ``[{"id", "name", "service"}, ...]`` where ``service`` is
        "tasks", "notes", or "bookmarks". An empty list is a normal,
        common result -- groups are a shared-mailbox feature most
        personal accounts never set up. Pass an id to the matching
        service's ``group_id`` argument (list_tasks/list_notes/
        list_bookmarks) to read that group's items.

    Raises:
        ZohoAPIError: if any of the three underlying requests fails.
    """
    return await client.list_groups()
