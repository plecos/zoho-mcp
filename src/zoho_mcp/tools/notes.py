"""MCP tool wrappers for Zoho Mail Notes.

Shapes LLM-facing input/output only. No HTTP calls, no token logic -- the
Zoho client is injected by the caller (``server.py``), never constructed
here.
"""

from zoho_mcp.zoho.client import ZohoClient


async def list_notes(client: ZohoClient, limit: int = 20, after: int = 0) -> list[dict]:
    """List the user's personal Zoho Mail notes.

    Args:
        client: injected Zoho client.
        limit: maximum number of notes to return.
        after: how many notes to skip before returning results.

    Returns:
        Normalized notes: id, title, content, book, owner, is_favorite,
        color, created_at, modified_at. There is no has_more signal for
        this endpoint -- getting back fewer than limit results is the
        only reliable sign you've reached the end.

    Raises:
        ZohoAPIError: if limit/after are out of range, or the Notes API
            rejects or fails the request.
    """
    return await client.list_notes(limit=limit, after=after)


async def get_note(client: ZohoClient, note_id: str) -> dict:
    """Fetch one note's full details by id.

    Args:
        client: injected Zoho client.
        note_id: a note's ``id`` from a prior ``list_notes`` result.

    Raises:
        ZohoAPIError: if the Notes API rejects or fails the request.
    """
    return await client.get_note(note_id)
