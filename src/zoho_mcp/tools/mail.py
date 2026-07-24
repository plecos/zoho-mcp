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


async def list_emails(
    client: ZohoClient,
    status: str = "all",
    folder_id: str | None = None,
    limit: int = 20,
    start: int = 1,
) -> list[dict]:
    """List emails by read/unread status, with real pagination.

    Use this instead of search_emails when you need to reliably
    enumerate *every* unread (or read) email -- e.g. "mark all my
    unread email as read" -- rather than a keyword/recency search that
    can miss messages sitting past the first page of results.

    Args:
        client: injected Zoho client.
        status: "unread", "read", or "all" (default).
        folder_id: restrict to one folder's id, from list_folders. If
            omitted, searches the whole mailbox and excludes Sent/
            Drafts/Templates by default (same as search_emails).
        limit: maximum results per page (1-200).
        start: 1-based starting sequence number -- call again with
            start += limit to fetch the next page, repeating until a
            page comes back with fewer than limit results.

    Returns:
        Compact email summaries: id, from, subject, date, snippet,
        folder_id, read (bool). Same shape as search_emails.

    Raises:
        ZohoAPIError: if status isn't one of "read"/"unread"/"all",
            limit/start are out of range, or the Zoho Mail API rejects
            or fails the request.
    """
    return await client.list_emails(
        status=status, folder_id=folder_id, limit=limit, start=start
    )


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


async def create_draft(
    client: ZohoClient,
    to: list[str],
    subject: str,
    content: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> dict:
    """Save an email as a draft. Never sends.

    Args:
        client: injected Zoho client.
        to: recipient addresses (at least one required).
        subject: the subject line.
        content: the message body.
        cc/bcc: optional additional recipients.

    Returns:
        ``{"id": ...}`` -- the new draft's message id.

    Raises:
        ZohoAPIError: if no recipient is given, or the Zoho Mail API
            rejects or fails the request.
    """
    return await client.create_draft(
        to=to, subject=subject, content=content, cc=cc, bcc=bcc
    )


async def reply_draft(
    client: ZohoClient, message_id: str, content: str, reply_all: bool = False
) -> dict:
    """Save a reply to an existing email as a draft. Never sends.

    Args:
        client: injected Zoho client.
        message_id: the email being replied to.
        content: the reply body.
        reply_all: reply to all recipients rather than just the sender.

    Returns:
        ``{"id": ...}`` -- the new draft's message id.

    Raises:
        ZohoAPIError: if content is blank, or the Zoho Mail API rejects
            or fails the request.
    """
    return await client.reply_draft(
        message_id=message_id, content=content, reply_all=reply_all
    )


async def send_email(
    client: ZohoClient,
    to: list[str],
    subject: str,
    content: str,
    cc: list[str] | None = None,
    bcc: list[str] | None = None,
) -> dict:
    """Send an email immediately. Disabled unless the server is configured
    to allow it (``ZOHO_ALLOW_AUTO_SEND=true``).

    Args/Returns: same as ``create_draft``.

    Raises:
        ZohoAPIError: if auto-send isn't enabled, no recipient is given,
            or the Zoho Mail API rejects or fails the request.
    """
    return await client.send_email(
        to=to, subject=subject, content=content, cc=cc, bcc=bcc
    )


async def mark_as_read(client: ZohoClient, message_ids: list[str]) -> None:
    """Mark one or more emails as read in a single request.

    Args:
        client: injected Zoho client.
        message_ids: email ids from a prior search_emails result. Pass
            every id that needs marking in one call rather than calling
            this once per email -- Zoho's API handles the whole batch
            in a single request.

    Raises:
        ZohoAPIError: if message_ids is empty, or the Zoho Mail API
            rejects or fails the request.
    """
    await client.mark_as_read(message_ids=message_ids)


async def mark_as_unread(client: ZohoClient, message_ids: list[str]) -> None:
    """Mark one or more emails as unread in a single request.

    Args:
        client: injected Zoho client.
        message_ids: email ids from a prior search_emails result. Pass
            every id that needs marking in one call rather than calling
            this once per email -- Zoho's API handles the whole batch
            in a single request.

    Raises:
        ZohoAPIError: if message_ids is empty, or the Zoho Mail API
            rejects or fails the request.
    """
    await client.mark_as_unread(message_ids=message_ids)


async def move_email(
    client: ZohoClient, message_ids: list[str], folder_id: str
) -> None:
    """Move one or more emails to a different folder in a single request.

    Args:
        client: injected Zoho client.
        message_ids: email ids from a prior search_emails result. Pass
            every id that needs moving in one call rather than calling
            this once per email -- Zoho's API handles the whole batch
            in a single request.
        folder_id: the destination folder's id, from list_folders.

    Raises:
        ZohoAPIError: if message_ids is empty, or the Zoho Mail API
            rejects or fails the request.
    """
    await client.move_email(message_ids=message_ids, folder_id=folder_id)


async def add_label(client: ZohoClient, message_ids: list[str], label_id: str) -> None:
    """Apply one label to one or more emails in a single request.

    Args:
        client: injected Zoho client.
        message_ids: email ids from a prior search_emails result. Pass
            every id that needs labeling in one call rather than calling
            this once per email -- Zoho's API handles the whole batch
            in a single request.
        label_id: the label's id, from list_labels.

    Raises:
        ZohoAPIError: if message_ids is empty, or the Zoho Mail API
            rejects or fails the request.
    """
    await client.add_label(message_ids=message_ids, label_id=label_id)


async def remove_label(
    client: ZohoClient, message_ids: list[str], label_id: str
) -> None:
    """Remove one label from one or more emails in a single request.

    Args:
        client: injected Zoho client.
        message_ids: email ids from a prior search_emails result. Pass
            every id that needs unlabeling in one call rather than
            calling this once per email -- Zoho's API handles the whole
            batch in a single request.
        label_id: the label's id, from list_labels.

    Raises:
        ZohoAPIError: if message_ids is empty, or the Zoho Mail API
            rejects or fails the request.
    """
    await client.remove_label(message_ids=message_ids, label_id=label_id)


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
