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
    """List every shared Zoho Mail group the user belongs to.

    Args:
        client: injected Zoho client.

    Returns:
        ``[{"id", "name", "owner", "member_count"}, ...]``, one row per
        distinct group. A group is shared across Tasks, Notes, and
        Bookmarks rather than belonging to one of them, so the same id
        works for all three. ``owner``/``member_count`` may be
        ``""``/``None`` for a group Zoho's Tasks listing didn't report.
        An empty list is a normal, common result -- groups are a
        shared-mailbox feature most personal accounts never set up.

    Raises:
        ZohoAPIError: if any of the three underlying requests fails.
    """
    return await client.list_groups()
