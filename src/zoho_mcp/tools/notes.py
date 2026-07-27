"""MCP tool wrappers for Zoho Mail Notes.

Shapes LLM-facing input/output only. No HTTP calls, no token logic -- the
Zoho client is injected by the caller (``server.py``), never constructed
here.
"""

from zoho_mcp.tools.envelope import counted
from zoho_mcp.zoho.client import ZohoClient


async def list_notes(
    client: ZohoClient,
    limit: int = 20,
    after: int = 0,
    group_id: str | None = None,
    oldest_first: bool = False,
) -> dict:
    """List Zoho Mail notes -- the user's personal ones, or a group's.

    Args:
        client: injected Zoho client.
        limit: maximum number of notes to return (1-399).
        after: how many notes to skip before returning results.
        group_id: list a shared group's notes instead of personal ones.
            Ids come from list_groups.
        oldest_first: return oldest-created notes first instead of the
            default newest-first.

    Returns:
        ``{"notes": [...], "count": int}``. Each note has id, title,
        content, book, owner, is_favorite, color, created_at,
        modified_at. ``count`` is this page's size, not the account
        total: there is no has_more signal for this endpoint, so a
        ``count`` below ``limit`` is the only reliable sign you've
        reached the end.

    Raises:
        ZohoAPIError: if limit/after are out of range, or the Notes API
            rejects or fails the request.
    """
    return counted(
        "notes",
        await client.list_notes(
            limit=limit, after=after, group_id=group_id, oldest_first=oldest_first
        ),
    )


async def create_note(
    client: ZohoClient,
    content: str,
    title: str = "",
    group_id: str | None = None,
) -> dict:
    """Create a personal or group note.

    Args:
        client: injected Zoho client.
        content: the note body -- the only field Zoho requires.
        title: optional note title.
        group_id: create in a shared group instead of personal notes.
            Ids come from list_groups.

    Returns:
        ``{"id": ...}``. Zoho's create response carries only the new id,
        not the stored note -- call get_note with it for full details.

    Raises:
        ZohoAPIError: if content is blank, or the Notes API rejects or
            fails the request.
    """
    return await client.create_note(content=content, title=title, group_id=group_id)


async def get_note(client: ZohoClient, note_id: str) -> dict:
    """Fetch one note's full details by id.

    Args:
        client: injected Zoho client.
        note_id: a note's ``id`` from a prior ``list_notes`` result.

    Raises:
        ZohoAPIError: if the Notes API rejects or fails the request.
    """
    return await client.get_note(note_id)
