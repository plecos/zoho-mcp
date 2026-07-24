"""MCP tool wrappers for Zoho Mail Bookmarks.

Shapes LLM-facing input/output only. No HTTP calls, no token logic -- the
Zoho client is injected by the caller (``server.py``), never constructed
here.
"""

from zoho_mcp.zoho.client import ZohoClient


async def list_bookmarks(
    client: ZohoClient,
    limit: int = 20,
    after: int = 0,
    group_id: str | None = None,
    oldest_first: bool = False,
) -> list[dict]:
    """List Zoho Mail bookmarks -- the user's personal ones, or a group's.

    Args:
        client: injected Zoho client.
        limit: maximum number of bookmarks to return (1-399).
        after: how many bookmarks to skip before returning results.
        group_id: list a shared group's bookmarks instead of personal
            ones. Ids come from list_groups.
        oldest_first: return oldest-created bookmarks first instead of
            the default newest-first.

    Returns:
        Normalized bookmarks: id, title, url, summary, collection, owner,
        is_favorite, tags. There is no has_more signal for this endpoint
        -- getting back fewer than limit results is the only reliable
        sign you've reached the end.

    Raises:
        ZohoAPIError: if limit/after are out of range, or the Bookmarks
            API rejects or fails the request.
    """
    return await client.list_bookmarks(
        limit=limit, after=after, group_id=group_id, oldest_first=oldest_first
    )


async def get_bookmark(client: ZohoClient, bookmark_id: str) -> dict:
    """Fetch one bookmark's full details by id.

    Args:
        client: injected Zoho client.
        bookmark_id: a bookmark's ``id`` from a prior ``list_bookmarks``
            result.

    Raises:
        ZohoAPIError: if the Bookmarks API rejects or fails the request.
    """
    return await client.get_bookmark(bookmark_id)
