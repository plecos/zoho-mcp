"""MCP tool wrappers for Zoho Mail.

Shapes LLM-facing input/output only. No HTTP calls, no token logic -- the
Zoho client is injected by the caller (``server.py``), never constructed here.
"""

from zoho_mcp.zoho.client import ZohoClient


async def search_emails(
    client: ZohoClient, query: str = "", limit: int = 20, days_back: int | None = None
) -> list[dict]:
    """Search the user's mailbox for emails matching a query and/or a recency window.

    Args:
        client: injected Zoho client.
        query: Zoho Mail search syntax -- bare words are invalid and Zoho
            will reject them. Use qualifiers like ``subject:``, ``sender:``,
            ``entire:`` (anywhere in the email), ``in:<folder name>``
            (search a specific folder), ``label:<label name>`` (search by
            tag/label), joined with ``::`` for AND or ``:or:`` for OR (e.g.
            ``subject:roadmap::sender:jamie``). May be empty if
            ``days_back`` is given.
        limit: maximum number of results to return (1-200).
        days_back: only return emails from the last N days (0 = today
            only), resolved using the mailbox's own timezone.

    Returns:
        Compact email summaries: id, from, subject, date, snippet,
        folder_id, read (bool). ``date`` is in the mailbox's own local
        timezone, not UTC -- see ``ZohoClient._get_mailbox_timezone``.
        Excludes Sent/Drafts/Templates by default (see
        ``ZohoClient._get_excluded_folder_ids``); use an explicit ``in:``
        qualifier in ``query`` to search one of those folders instead.

    Raises:
        ZohoAPIError: if query and days_back are both empty, days_back is
            negative, or the Zoho Mail API rejects or fails the request.
    """
    return await client.search_emails(query=query, limit=limit, days_back=days_back)


async def get_email(client: ZohoClient, message_id: str, folder_id: str) -> dict:
    """Fetch the full plain-text body of one email.

    Args:
        client: injected Zoho client.
        message_id: an email's ``id`` from a prior ``search_emails`` result.
        folder_id: that same email's ``folder_id`` from ``search_emails``.

    Returns:
        ``{"id": ..., "text": ...}`` with the body as plain text.

    Raises:
        ZohoAPIError: if the Zoho Mail API rejects or fails the request.
    """
    return await client.get_email(message_id=message_id, folder_id=folder_id)


async def list_attachments(
    client: ZohoClient, message_id: str, folder_id: str
) -> list[dict]:
    """List attachment metadata (name, size) for one email.

    Args:
        client: injected Zoho client.
        message_id: an email's ``id`` from a prior ``search_emails`` result.
        folder_id: that same email's ``folder_id`` from ``search_emails``.

    Returns:
        ``[{"id": ..., "name": ..., "size_bytes": ...}, ...]``. Metadata
        only -- reading the actual file content of an attachment isn't
        supported.

    Raises:
        ZohoAPIError: if the Zoho Mail API rejects or fails the request.
    """
    return await client.list_attachments(message_id=message_id, folder_id=folder_id)


async def list_folders(client: ZohoClient) -> list[dict]:
    """List all folders in the mailbox, including custom subfolders.

    Args:
        client: injected Zoho client.

    Returns:
        Each folder has id, name, path (e.g. "/Inbox/Work" -- the
        hierarchy signal), and type. Pass a folder's name to
        ``search_emails``'s ``in:`` qualifier to search it.

    Raises:
        ZohoAPIError: if the Zoho Mail API rejects or fails the request.
    """
    return await client.list_folders()


async def list_labels(client: ZohoClient) -> list[dict]:
    """List all labels/tags configured in the mailbox.

    Args:
        client: injected Zoho client.

    Returns:
        Each label has id, name, color. Pass a label's name to
        ``search_emails``'s ``label:`` qualifier to search it.

    Raises:
        ZohoAPIError: if the Zoho Mail API rejects or fails the request.
    """
    return await client.list_labels()


async def list_signatures(client: ZohoClient) -> list[dict]:
    """List all configured email signatures.

    Args:
        client: injected Zoho client.

    Returns:
        Each signature has id, name, content (plain text).

    Raises:
        ZohoAPIError: if the Zoho Mail API rejects or fails the request.
    """
    return await client.list_signatures()
