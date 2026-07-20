"""MCP tool wrappers for Zoho Calendar.

Shapes LLM-facing input/output only. No HTTP calls, no token logic -- the
Zoho client is injected by the caller (``server.py``), never constructed here.
"""

from datetime import datetime, timezone

from zoho_mcp.zoho.client import ZohoClient


def _parse_iso8601_utc(value: str, *, field_name: str) -> datetime:
    """Parse an ISO 8601 datetime string and convert it to UTC.

    Raises:
        ValueError: if ``value`` isn't a valid ISO 8601 string, or omits a
            UTC offset (Zoho's Calendar API is UTC-only).
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(
            f"{field_name} must be an ISO 8601 datetime string, got: {value!r}"
        ) from e
    if parsed.tzinfo is None:
        raise ValueError(
            f"{field_name} must include a UTC offset "
            f"(e.g. '2024-10-29T16:00:00+00:00'), got: {value!r}"
        )
    return parsed.astimezone(timezone.utc)


async def list_events(
    client: ZohoClient, start: str, end: str, calendar_id: str | None = None
) -> list[dict]:
    """List calendar events in a time range (max 31-day span).

    Args:
        client: injected Zoho client.
        start: ISO 8601 datetime string with UTC offset, range start.
        end: ISO 8601 datetime string with UTC offset, range end.
        calendar_id: which calendar to query -- defaults to the user's
            configured default calendar if omitted. Use ``list_calendars``
            to see what else is available.

    Returns:
        Normalized events: id, title, start, end, attendees. ``start``/
        ``end`` are in the mailbox's own local timezone, not UTC -- see
        ``ZohoClient._get_mailbox_timezone``.

    Raises:
        ValueError: if start/end aren't valid ISO 8601 strings with a UTC offset.
        ZohoAPIError: if the range exceeds 31 days or the Calendar API fails.
    """
    return await client.list_events(
        start=_parse_iso8601_utc(start, field_name="start"),
        end=_parse_iso8601_utc(end, field_name="end"),
        calendar_id=calendar_id,
    )


async def get_event(
    client: ZohoClient, uid: str, calendar_id: str | None = None
) -> dict:
    """Fetch full details for one event found via list_events.

    Args:
        client: injected Zoho client.
        uid: an event's ``id`` from a prior ``list_events`` result.
        calendar_id: which calendar the event belongs to -- defaults to
            the user's configured default calendar if omitted.

    Returns:
        id, title, organizer, full attendee list (list_events can report
        only the caller's own attendee entry for an occurrence, not every
        invitee), location, description, and recurrence (an iCal RRULE
        string, or "" if the event doesn't recur). Deliberately excludes
        start/end -- use the occurrence's own start/end from list_events,
        not this call: Zoho's single-event endpoint can return the wrong
        occurrence's dates for a recurring event.

    Raises:
        ZohoAPIError: if no event is found for uid, or the Calendar API
            rejects or fails the request.
    """
    return await client.get_event(uid, calendar_id=calendar_id)


async def list_calendars(client: ZohoClient) -> list[dict]:
    """List all calendars the user has access to.

    Args:
        client: injected Zoho client.

    Returns:
        Each calendar has id, name, is_default, timezone, privilege. Pass
        a calendar's id to list_events/get_event's calendar_id argument
        to target it instead of the default.

    Raises:
        ZohoAPIError: if the Calendar API rejects or fails the request.
    """
    return await client.list_calendars()


async def get_freebusy(
    client: ZohoClient, email: str, start: str, end: str
) -> list[dict]:
    """Get busy time slots for a user's calendar in a time range.

    Args:
        client: injected Zoho client.
        email: the calendar owner's email address.
        start: ISO 8601 datetime string with UTC offset, range start.
        end: ISO 8601 datetime string with UTC offset, range end.

    Returns:
        Busy slots: start, end (mailbox's own local timezone, not UTC),
        status. Only available for calendars that person has explicitly
        enabled "include in my Free/Busy sharing" for.

    Raises:
        ValueError: if start/end aren't valid ISO 8601 strings with a UTC offset.
        ZohoAPIError: if free/busy sharing isn't enabled for email, or
            the Calendar API rejects or fails the request.
    """
    return await client.get_freebusy(
        email=email,
        start=_parse_iso8601_utc(start, field_name="start"),
        end=_parse_iso8601_utc(end, field_name="end"),
    )
