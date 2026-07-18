"""MCP tool wrappers for Zoho Mail.

Shapes LLM-facing input/output only. No HTTP calls, no token logic -- the
Zoho client is injected by the caller (``server.py``), never constructed here.
"""

from zoho_mcp.zoho.client import ZohoClient


async def search_emails(client: ZohoClient, query: str, limit: int = 20) -> list[dict]:
    """Search the user's mailbox for emails matching a query.

    Args:
        client: injected Zoho client.
        query: Zoho Mail search syntax (e.g. ``subject:roadmap from:jamie``).
        limit: maximum number of results to return (1-200).

    Returns:
        Compact email summaries: id, from, subject, date, snippet, folder_id.

    Raises:
        ZohoAPIError: if the Zoho Mail API rejects or fails the request.
    """
    return await client.search_emails(query=query, limit=limit)


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
