"""FastMCP app instantiation and tool registration.

No business logic lives here -- ``create_server`` wires the already-tested
tool wrappers (``tools/mail.py``, ``tools/calendar.py``, ``tools/tasks.py``,
``tools/notes.py``, ``tools/bookmarks.py``, ``tools/resources.py``,
``tools/contacts.py``)
to a FastMCP instance, and ``main`` builds the real Zoho clients from
environment/keyring config and runs the server over stdio.
"""

import os

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from zoho_mcp.config import load_env
from zoho_mcp.tools import bookmarks as bookmarks_tools
from zoho_mcp.tools import calendar as calendar_tools
from zoho_mcp.tools import contacts as contacts_tools
from zoho_mcp.tools import mail as mail_tools
from zoho_mcp.tools import notes as notes_tools
from zoho_mcp.tools import resources as resources_tools
from zoho_mcp.tools import tasks as tasks_tools
from zoho_mcp.zoho.auth import ZohoTokenManager, load_refresh_token
from zoho_mcp.zoho.client import ZohoClient
from zoho_mcp.zoho.contacts_client import ZohoContactsClient

_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_CREATE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False
)


def create_server(client: ZohoClient, contacts_client: ZohoContactsClient) -> FastMCP:
    """Build the FastMCP app and register all tools against the given clients."""
    mcp = FastMCP("zoho-mcp")

    @mcp.tool(annotations=_READ_ONLY)
    async def search_emails(
        query: str = "", limit: int = 20, days_back: int | None = None
    ) -> list[dict]:
        """Search the user's Zoho Mail mailbox for emails matching a query
        and/or a recency window.

        query (optional): must use Zoho Mail search syntax -- bare words
        are rejected. Use qualifiers like subject:, sender:, entire:
        (anywhere in the email), in:<folder name> (search a specific
        folder), label:<label name> (search by tag/label), joined with ::
        for AND or :or: for OR, e.g. "subject:roadmap::sender:jamie". May
        be left empty if days_back is given.

        days_back (optional): only return emails from the last N days --
        0 for today only, 1 for today and yesterday, etc. This is resolved
        using the mailbox's real timezone; do not try to compute or pass an
        explicit date yourself, since you don't know the mailbox's
        timezone and getting it wrong silently returns the wrong day.

        Each result includes a read (bool) field. There is no separate
        "unread" query filter -- fetch results and filter on read=false
        yourself if asked for unread emails.

        Results exclude Sent, Drafts, and Templates by default (mail
        moved into other folders by your own rules is still included --
        only those three are dropped). Use an explicit in:Sent (or
        in:Drafts / in:Templates) qualifier in query to search one of
        those specifically.
        """
        return await mail_tools.search_emails(
            client, query=query, limit=limit, days_back=days_back
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def get_email(message_id: str, folder_id: str) -> dict:
        """Fetch the full plain-text body of one email found via search_emails."""
        return await mail_tools.get_email(
            client, message_id=message_id, folder_id=folder_id
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def list_attachments(message_id: str, folder_id: str) -> list[dict]:
        """List attachment metadata for one email found via search_emails.

        Returns [{"id", "name", "size_bytes"}, ...] -- metadata only.
        Reading the actual file content of an attachment isn't supported.
        """
        return await mail_tools.list_attachments(
            client, message_id=message_id, folder_id=folder_id
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def list_folders() -> list[dict]:
        """List all folders in the mailbox, including custom subfolders.

        Each folder has id, name, path (e.g. "/Inbox/Work" -- the
        hierarchy signal, not any id field), and type. Pass a folder's
        name to search_emails' in: qualifier to search it.
        """
        return await mail_tools.list_folders(client)

    @mcp.tool(annotations=_READ_ONLY)
    async def list_labels() -> list[dict]:
        """List all labels/tags configured in the mailbox.

        Each label has id, name, color. Pass a label's name to
        search_emails' label: qualifier to search it.
        """
        return await mail_tools.list_labels(client)

    @mcp.tool(annotations=_READ_ONLY)
    async def list_signatures() -> list[dict]:
        """List all configured email signatures.

        Each has id, name, content (plain text).
        """
        return await mail_tools.list_signatures(client)

    @mcp.tool(annotations=_READ_ONLY)
    async def list_events(
        start: str, end: str, calendar_id: str | None = None
    ) -> list[dict]:
        """List Zoho Calendar events in a time range (max 31 days).

        start/end: ISO 8601 datetime strings with an explicit UTC offset
        (any offset works, e.g. "+00:00" or "-07:00").

        calendar_id (optional): which calendar to query -- defaults to
        the user's configured default calendar. Use list_calendars to see
        what else is available if the user has more than one.

        Returned event times are already in the mailbox's own local
        timezone, not UTC -- do not convert them yourself.
        """
        return await calendar_tools.list_events(
            client, start=start, end=end, calendar_id=calendar_id
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def get_event(uid: str, calendar_id: str | None = None) -> dict:
        """Fetch full details for one event, given an id from list_events.

        calendar_id (optional): which calendar the event belongs to --
        defaults to the user's configured default calendar.

        Use this when you need an event's organizer, full attendee list
        (list_events can show only your own attendee entry, not every
        invitee), location, description, or recurrence rule (an iCal
        RRULE string, e.g. "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO", or "" if the
        event doesn't recur).

        Does NOT return start/end -- keep using the occurrence's own
        start/end from list_events for timing; this call's own date
        fields for a recurring event are not reliable.
        """
        return await calendar_tools.get_event(client, uid=uid, calendar_id=calendar_id)

    @mcp.tool(annotations=_READ_ONLY)
    async def list_calendars() -> list[dict]:
        """List all calendars the user has access to.

        Each calendar has id, name, is_default, timezone, privilege. Pass
        a calendar's id to list_events/get_event's calendar_id argument
        to target it instead of the default.
        """
        return await calendar_tools.list_calendars(client)

    @mcp.tool(annotations=_READ_ONLY)
    async def get_freebusy(email: str, start: str, end: str) -> list[dict]:
        """Get busy time slots for a user's calendar in a time range.

        email: the calendar owner's email address.
        start/end: ISO 8601 datetime strings with an explicit UTC offset.

        Only returns data for calendars that person has explicitly
        enabled "include in my Free/Busy sharing" for (a per-calendar
        Zoho Calendar setting) -- raises a clear error rather than
        silently returning an empty (misreadable as "fully free") list
        when that isn't the case.

        Returned times are already in the mailbox's own local timezone,
        not UTC -- do not convert them yourself.
        """
        return await calendar_tools.get_freebusy(
            client, email=email, start=start, end=end
        )

    @mcp.tool(annotations=_CREATE)
    async def create_event(
        title: str,
        start: str,
        end: str,
        description: str = "",
        location: str = "",
        attendees: list[str] | None = None,
        calendar_id: str | None = None,
    ) -> dict:
        """Create a new Zoho Calendar event.

        title: event title.
        start/end: ISO 8601 datetime strings with an explicit UTC offset.
        description/location (optional): plain text.
        attendees (optional): list of email addresses to invite. To book
        a Resource Booking resource (from list_resources), include its
        email address here.
        calendar_id (optional): which calendar to create the event in --
        defaults to the user's configured default calendar.

        Returns the created event, normalized the same way as get_event
        (id, title, organizer, attendees, location, description,
        recurrence -- no start/end in the response).
        """
        return await calendar_tools.create_event(
            client,
            title=title,
            start=start,
            end=end,
            description=description,
            location=location,
            attendees=attendees,
            calendar_id=calendar_id,
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def list_branches() -> list[dict]:
        """List the office branches configured for Zoho Calendar's
        Resource Booking feature (meeting rooms, equipment), each with
        nested buildings and floors.

        An empty list is normal -- Resource Booking is an office-facility
        feature most personal/small accounts never set up, not an error.
        Use a floor's id (only floors with has_resource true have any)
        with list_resources to see what's actually bookable there.
        """
        return await resources_tools.list_branches(client)

    @mcp.tool(annotations=_READ_ONLY)
    async def list_resources(
        branch_id: str, building_id: str, floor_id: str
    ) -> list[dict]:
        """List the bookable resources (rooms, equipment) on one floor.

        branch_id/building_id/floor_id: from a prior list_branches result
        -- all three are required by Zoho's own API.

        Each resource has an email address -- invite it to a calendar
        event (as an attendee) to book it.
        """
        return await resources_tools.list_resources(
            client, branch_id=branch_id, building_id=building_id, floor_id=floor_id
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def list_tasks(limit: int = 20, offset: int = 0) -> dict:
        """List the user's personal Zoho Mail tasks (Zoho Mail's Tasks
        feature, not a project-management tool).

        limit (optional): maximum number of tasks to return (1-499).
        offset (optional): how many tasks to skip -- use for pagination
        together with has_more.

        Returns {"tasks": [...], "has_more": bool}. Each task has id,
        title, description, status, priority, due_date, project,
        assignee, tags, subtask_count, recurring, created_at, modified_at.
        due_date's format is unverified against this account (never seen
        populated) -- treat it as an opaque string, don't assume a
        format. If has_more is true, raise limit or increase offset,
        don't assume the count you got back is the full total.
        """
        return await tasks_tools.list_tasks(client, limit=limit, offset=offset)

    @mcp.tool(annotations=_READ_ONLY)
    async def get_task(task_id: str) -> dict:
        """Fetch one task's full details, given an id from list_tasks."""
        return await tasks_tools.get_task(client, task_id=task_id)

    @mcp.tool(annotations=_READ_ONLY)
    async def list_notes(limit: int = 20, after: int = 0) -> list[dict]:
        """List the user's personal Zoho Mail notes.

        limit (optional): maximum number of notes to return.
        after (optional): how many notes to skip -- use for pagination.

        Each note has id, title, content, book, owner, is_favorite,
        color, created_at, modified_at. There is no has_more signal for
        this endpoint -- getting back fewer than limit results is the
        only reliable sign you've reached the end.
        """
        return await notes_tools.list_notes(client, limit=limit, after=after)

    @mcp.tool(annotations=_READ_ONLY)
    async def get_note(note_id: str) -> dict:
        """Fetch one note's full details, given an id from list_notes."""
        return await notes_tools.get_note(client, note_id=note_id)

    @mcp.tool(annotations=_READ_ONLY)
    async def list_bookmarks(limit: int = 20, after: int = 0) -> list[dict]:
        """List the user's personal Zoho Mail bookmarks.

        limit (optional): maximum number of bookmarks to return.
        after (optional): how many bookmarks to skip -- use for pagination.

        Each bookmark has id, title, url, summary, collection, owner,
        is_favorite, tags. There is no has_more signal for this endpoint
        -- getting back fewer than limit results is the only reliable
        sign you've reached the end.
        """
        return await bookmarks_tools.list_bookmarks(client, limit=limit, after=after)

    @mcp.tool(annotations=_READ_ONLY)
    async def get_bookmark(bookmark_id: str) -> dict:
        """Fetch one bookmark's full details, given an id from list_bookmarks."""
        return await bookmarks_tools.get_bookmark(client, bookmark_id=bookmark_id)

    @mcp.tool(annotations=_READ_ONLY)
    async def search_contacts(
        query: str = "", limit: int = 20, status: str = "active"
    ) -> dict:
        """Search the user's Zoho Contacts.

        query (optional): free-text search -- matches name, email, AND
        phone number (Zoho's backend searches across all of these, even
        though only some are shown by default). Leave empty to list
        contacts without filtering.

        status (optional): "active" (default), "archived", or "inactive"
        -- which folder to search. Archived and inactive contacts are
        excluded by default; pass status explicitly only when asked to
        find an archived or inactive contact specifically.

        Searches both the user's Personal and Organization contacts and
        merges the results -- these are two separate pools in Zoho, each
        with their own Archived/Inactive folders.

        Returns {"contacts": [...], "has_more": bool}. Each contact
        includes a scope field ("personal" or "organization") along with
        phones, notes, nickname, and birthday when set. Pass scope back
        into get_contact -- the same id can mean a different, unrelated
        record depending on scope. If has_more is true, there are more
        results than limit returned -- raise limit or narrow query, don't
        assume the count you got back is the full total. For "how many
        contacts do I have" style questions, use count_contacts instead of
        paginating and summing.
        """
        return await contacts_tools.search_contacts(
            contacts_client, query=query, limit=limit, status=status
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def get_contact(contact_id: str, scope: str) -> dict:
        """Fetch one contact's full details, given an id and scope from
        search_contacts.

        scope must be "personal" or "organization", taken from that same
        contact's search_contacts result -- the same contact_id can refer
        to a different record depending on scope, so it can't be guessed.
        """
        return await contacts_tools.get_contact(
            contacts_client, contact_id=contact_id, scope=scope
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def count_contacts() -> dict:
        """Return the user's Zoho Contacts counts directly and reliably.

        Returns {"personal": {"contacts": int, "archived": int,
        "inactive": int}, "organization": {...same shape...}, "total":
        int}. archived/inactive are broken out per scope rather than
        hidden -- total only sums the active "contacts" count from each
        scope. Prefer this over calling search_contacts repeatedly and
        summing/deduplicating results yourself.
        """
        return await contacts_tools.count_contacts(contacts_client)

    return mcp


def _build_zoho_clients_from_env() -> tuple[ZohoClient, ZohoContactsClient]:
    """Construct the Zoho Mail/Calendar and Contacts clients from env + keyring.

    Both share one token manager and http client -- Zoho's OAuth tokens
    carry scopes for every product at once, so there's only ever one
    access/refresh token pair regardless of how many Zoho services we call.

    Raises:
        RuntimeError: if no refresh token has been stored yet.
        KeyError: if a required environment variable is missing.
    """
    load_env()
    refresh_token = load_refresh_token()
    if refresh_token is None:
        raise RuntimeError(
            "No Zoho refresh token found in the OS credential store. "
            "Run the auth setup flow before starting the server."
        )
    http_client = httpx.AsyncClient()
    token_manager = ZohoTokenManager(
        client_id=os.environ["ZOHO_CLIENT_ID"],
        client_secret=os.environ["ZOHO_CLIENT_SECRET"],
        refresh_token=refresh_token,
        http_client=http_client,
    )
    client = ZohoClient(
        token_manager=token_manager,
        http_client=http_client,
        account_id=os.environ["ZOHO_ACCOUNT_ID"],
        calendar_uid=os.environ["ZOHO_CALENDAR_UID"],
        strip_invisible_chars=os.environ.get("ZOHO_STRIP_INVISIBLE_CHARS", "false")
        .strip()
        .lower()
        == "true",
    )
    contacts_client = ZohoContactsClient(
        token_manager=token_manager, http_client=http_client
    )
    return client, contacts_client


def main() -> None:
    client, contacts_client = _build_zoho_clients_from_env()
    server = create_server(client, contacts_client)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
