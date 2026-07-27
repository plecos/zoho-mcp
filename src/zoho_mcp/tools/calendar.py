"""MCP tool wrappers for Zoho Calendar.

Shapes LLM-facing input/output only. No HTTP calls, no token logic -- the
Zoho client is injected by the caller (``server.py``), never constructed here.
"""

from datetime import datetime, timezone

from zoho_mcp.tools.envelope import counted
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
) -> dict:
    """List calendar events in a time range (max 31-day span).

    Args:
        client: injected Zoho client.
        start: ISO 8601 datetime string with UTC offset, range start.
        end: ISO 8601 datetime string with UTC offset, range end.
        calendar_id: which calendar to query -- defaults to the user's
            configured default calendar if omitted. Use ``list_calendars``
            to see what else is available.

    Returns:
        ``{"events": [...], "count": int}``. Each event has id, title,
        start, end, attendees. ``start``/``end`` are in the mailbox's own
        local timezone, not UTC -- see
        ``ZohoClient._get_mailbox_timezone``. ``count`` counts
        occurrences in the range, so a recurring event contributes one
        per occurrence.

    Raises:
        ValueError: if start/end aren't valid ISO 8601 strings with a UTC offset.
        ZohoAPIError: if the range exceeds 31 days or the Calendar API fails.
    """
    return counted(
        "events",
        await client.list_events(
            start=_parse_iso8601_utc(start, field_name="start"),
            end=_parse_iso8601_utc(end, field_name="end"),
            calendar_id=calendar_id,
        ),
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


async def list_calendars(client: ZohoClient) -> dict:
    """List all calendars the user has access to.

    Args:
        client: injected Zoho client.

    Returns:
        ``{"calendars": [...], "count": int}``. Each calendar has id,
        name, is_default, timezone, privilege. Pass a calendar's id to
        list_events/get_event's calendar_id argument to target it instead
        of the default.

    Raises:
        ZohoAPIError: if the Calendar API rejects or fails the request.
    """
    return counted("calendars", await client.list_calendars())


async def get_freebusy(client: ZohoClient, email: str, start: str, end: str) -> dict:
    """Get busy time slots for a user's calendar in a time range.

    Args:
        client: injected Zoho client.
        email: the calendar owner's email address.
        start: ISO 8601 datetime string with UTC offset, range start.
        end: ISO 8601 datetime string with UTC offset, range end.

    Returns:
        ``{"busy_slots": [...], "count": int}``. Each slot has start, end
        (mailbox's own local timezone, not UTC), status. Only available
        for calendars that person has explicitly enabled "include in my
        Free/Busy sharing" for; an unshared calendar raises rather than
        returning ``count: 0``, so ``count: 0`` does mean genuinely free
        across the range.

    Raises:
        ValueError: if start/end aren't valid ISO 8601 strings with a UTC offset.
        ZohoAPIError: if free/busy sharing isn't enabled for email, or
            the Calendar API rejects or fails the request.
    """
    return counted(
        "busy_slots",
        await client.get_freebusy(
            email=email,
            start=_parse_iso8601_utc(start, field_name="start"),
            end=_parse_iso8601_utc(end, field_name="end"),
        ),
    )


async def create_event(
    client: ZohoClient,
    title: str,
    start: str,
    end: str,
    description: str = "",
    location: str = "",
    attendees: list[str] | None = None,
    calendar_id: str | None = None,
) -> dict:
    """Create a new calendar event.

    Args:
        client: injected Zoho client.
        title: event title.
        start: ISO 8601 datetime string with UTC offset, event start.
        end: ISO 8601 datetime string with UTC offset, event end.
        description: optional event description.
        location: optional event location.
        attendees: optional list of attendee email addresses. To book a
            Resource Booking resource, include its email (from
            list_resources) here.
        calendar_id: which calendar to create the event in -- defaults to
            the user's configured default calendar if omitted.

    Returns:
        The created event, normalized the same way as get_event (id,
        title, organizer, attendees, location, description, recurrence --
        no start/end).

    Raises:
        ValueError: if start/end aren't valid ISO 8601 strings with a UTC offset.
        ZohoAPIError: if end isn't after start, or the Calendar API
            rejects or fails the request.
    """
    return await client.create_event(
        title=title,
        start=_parse_iso8601_utc(start, field_name="start"),
        end=_parse_iso8601_utc(end, field_name="end"),
        description=description,
        location=location,
        attendees=attendees,
        calendar_id=calendar_id,
    )


async def update_event(
    client: ZohoClient,
    uid: str,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    calendar_id: str | None = None,
) -> dict:
    """Update an existing calendar event. Only given fields are changed.

    Args:
        client: injected Zoho client.
        uid: the event's id, from a prior list_events/get_event/create_event result.
        title/start/end/description/location/attendees: only the fields
            being changed need to be given. start/end (ISO 8601 datetime
            strings with a UTC offset) must be given together.
        calendar_id: which calendar the event belongs to -- defaults to
            the user's configured default calendar if omitted.

    Returns:
        The updated event, normalized the same way as get_event.

    Raises:
        ValueError: if start/end are given and aren't valid ISO 8601
            strings with a UTC offset.
        ZohoAPIError: if start/end are given inconsistently, end isn't
            after start, no event is found for uid, or the Calendar API
            rejects or fails the request.
    """
    return await client.update_event(
        uid=uid,
        title=title,
        start=_parse_iso8601_utc(start, field_name="start")
        if start is not None
        else None,
        end=_parse_iso8601_utc(end, field_name="end") if end is not None else None,
        description=description,
        location=location,
        attendees=attendees,
        calendar_id=calendar_id,
    )


async def delete_event(
    client: ZohoClient, uid: str, calendar_id: str | None = None
) -> None:
    """Delete an existing calendar event.

    Args:
        client: injected Zoho client.
        uid: the event's id, from a prior list_events/get_event/create_event result.
        calendar_id: which calendar the event belongs to -- defaults to
            the user's configured default calendar if omitted.

    Raises:
        ZohoAPIError: if no event is found for uid, or the Calendar API
            rejects or fails the request.
    """
    await client.delete_event(uid, calendar_id=calendar_id)
