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


async def list_events(client: ZohoClient, start: str, end: str) -> list[dict]:
    """List calendar events in a time range (max 31-day span).

    Args:
        client: injected Zoho client.
        start: ISO 8601 datetime string with UTC offset, range start.
        end: ISO 8601 datetime string with UTC offset, range end.

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
    )
