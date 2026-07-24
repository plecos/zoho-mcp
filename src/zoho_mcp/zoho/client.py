"""Zoho Mail and Calendar REST client, plus raw-response normalization.

All conversion from Zoho's wire format (epoch-millisecond strings, HTML email
bodies, event timestamps) into the compact, LLM-facing shapes used by the
MCP tools happens here and only here.
"""

import html
import json
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup

from zoho_mcp.zoho.auth import ZohoTokenManager

ZOHO_EVENT_RANGE_REQUEST_FORMAT = "%Y%m%dT%H%M%SZ"
ZOHO_MAIL_BASE_URL = "https://mail.zoho.com/api"
ZOHO_CALENDAR_BASE_URL = "https://calendar.zoho.com/api/v1"
# Each of these three services scopes its data the same way: "/me" for the
# user's own personal items, "/groups/{id}" for a shared group's. The roots
# exist so both forms are built in one place (see _scoped_url).
ZOHO_TASKS_ROOT_URL = "https://mail.zoho.com/api/tasks"
ZOHO_NOTES_ROOT_URL = "https://mail.zoho.com/api/notes"
ZOHO_BOOKMARKS_ROOT_URL = "https://mail.zoho.com/api/links"
ZOHO_TASKS_BASE_URL = f"{ZOHO_TASKS_ROOT_URL}/me"
ZOHO_NOTES_BASE_URL = f"{ZOHO_NOTES_ROOT_URL}/me"
ZOHO_BOOKMARKS_BASE_URL = f"{ZOHO_BOOKMARKS_ROOT_URL}/me"
ZOHO_BRANCHES_URL = "https://calendar.zoho.com/api/v1/branches"
ZOHO_RESOURCES_URL = "https://calendar.zoho.com/api/v1/resources"
MAX_EVENT_RANGE_DAYS = 31

# Zoho's own documented eventdata fields accepted by create/update -- see
# https://www.zoho.com/calendar/help/api/post-create-event.html. Confirmed
# live that blindly echoing an event's *entire* raw GET response back into
# an update's eventdata fails: the GET response's "notifyType" (an int,
# response-only) isn't valid input and gets rejected with a 400
# "PATTERN_NOT_MATCHED" -- it is NOT the same field as the write-side
# "notify_attendee". update_event carries forward only fields in this set
# from the current event, rather than the full raw object, specifically
# to avoid resending response-only fields Zoho's write endpoint rejects.
_EVENT_WRITABLE_FIELDS = frozenset(
    {
        "title",
        "isallday",
        "isprivate",
        "url",
        "location",
        "description",
        "richtext_description",
        "color",
        "attendees",
        "group_attendees",
        "reminders",
        "calendar_alarm",
        "notify_attendee",
        "attach",
        "transparency",
        "conference",
        "allowForwarding",
        "rrule",
        "repeat",
        "dateandtime",
    }
)
MIN_SEARCH_LIMIT = 1
MAX_SEARCH_LIMIT = 200
_VALID_EMAIL_STATUSES = frozenset({"read", "unread", "all"})
MIN_TASKS_LIMIT = 1
MAX_TASKS_LIMIT = 499  # per Zoho's own documented range for this endpoint
MIN_NOTES_LIMIT = 1
# Zoho documents 1-399 for both Notes and Bookmarks, but confirmed live it
# silently *accepts* an over-max limit (HTTP 200 for limit=10000) instead of
# rejecting it. Silent capping is the worse failure mode -- a caller asking
# for 1000 can't distinguish "that's everything" from "you were truncated"
# -- so both bounds are enforced here rather than deferred to Zoho.
MAX_NOTES_LIMIT = 399
MIN_BOOKMARKS_LIMIT = 1
MAX_BOOKMARKS_LIMIT = 399

# Zoho's own view names for the two cross-group task queries, mapped from
# readable argument values. Confirmed live that only these two exist --
# "assignedbyme" 400s with PATTERN_NOT_MATCHED.
_TASK_VIEWS = {"assigned_to_me": "assignedtome", "created_by_me": "createdbyme"}

# search_emails excludes these by default -- not "received" mail by nature.
# Every user-created/rule-filed folder reports folderType "Inbox" (confirmed
# against the real API), so this can never accidentally catch a user's own
# folder, only Zoho's built-in non-received ones.
EXCLUDED_FOLDER_TYPES = frozenset({"Sent", "Drafts", "Templates"})

# Characters with no legitimate visible meaning, used by some marketing
# emails purely to pad preview text. Deliberately excludes ZWJ (U+200D) and
# ZWNJ (U+200C), which are load-bearing for emoji sequences and some scripts.
_INVISIBLE_PADDING_CHARS = frozenset(
    chr(codepoint)
    for codepoint in (
        0x034F,  # COMBINING GRAPHEME JOINER
        0x200B,  # ZERO WIDTH SPACE
        0xFEFF,  # ZERO WIDTH NO-BREAK SPACE / BOM
        0x2060,  # WORD JOINER
    )
)


class ZohoAPIError(Exception):
    """Raised when a Zoho Mail/Calendar API call fails or is rejected."""


def _epoch_ms_to_iso8601(epoch_ms: str, tz_name: str) -> str:
    """Convert a Zoho epoch-millisecond timestamp string to ISO 8601 in ``tz_name``.

    Returned in the mailbox's own local offset, not UTC -- deliberately, so
    an LLM client never has to convert (or forget to convert) a timezone it
    doesn't know, which is exactly what produced a wrong displayed time
    despite the underlying UTC value always having been correct.
    """
    return datetime.fromtimestamp(
        int(epoch_ms) / 1000, tz=ZoneInfo(tz_name)
    ).isoformat()


def _zoho_event_time_to_iso8601(value: str, tz_name: str) -> str:
    """Convert a Zoho Calendar event timestamp to ISO 8601 in ``tz_name``.

    Zoho returns two real shapes here (not the single documented one):
    a date-only ``yyyyMMdd`` for all-day events (no time/timezone to
    convert), or a full timestamp with either a ``Z`` or a numeric UTC
    offset (``yyyyMMdd'T'HHmmss(Z|+/-HHMM)``), converted to the mailbox's
    local offset for the same reason as ``_epoch_ms_to_iso8601``.
    """
    if "T" not in value:
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    return (
        datetime.strptime(value, "%Y%m%dT%H%M%S%z")
        .astimezone(ZoneInfo(tz_name))
        .isoformat()
    )


def _today_in_timezone(tz_name: str) -> date:
    """Return the current calendar date in the given IANA timezone.

    Never use ``datetime.now(timezone.utc).date()`` as a stand-in for "the
    user's today" -- UTC's calendar day and the mailbox's calendar day
    disagree for several hours every day (e.g. it can already be tomorrow in
    UTC while it's still this evening in ``America/Los_Angeles``), which is
    exactly what caused ``search_emails(days_back=0)`` to return the wrong
    day when this was first computed naively.
    """
    return datetime.now(ZoneInfo(tz_name)).date()


def normalize_email_summary(raw: dict, mailbox_timezone: str) -> dict:
    """Normalize one entry from Zoho Mail's List Emails ``data`` array.

    Returns the compact shape the LLM sees: id, from, subject, date, snippet.
    ``date`` is in ``mailbox_timezone``, not UTC -- see ``_epoch_ms_to_iso8601``.
    ``subject``/``snippet`` are HTML-entity-decoded -- confirmed live that
    Zoho's search API frequently returns these with literal undecoded
    entities (e.g. "&#39;" instead of an apostrophe, seen in ~10% of
    subjects and ~36% of snippets in a real sample), not human-readable text.

    Raises:
        ZohoAPIError: if ``raw`` is missing an expected field or a field has
            an unexpected type/value (e.g. a non-numeric date).
    """
    try:
        return {
            "id": raw["messageId"],
            "from": raw["fromAddress"],
            "subject": html.unescape(raw["subject"]),
            # receivedTime, not sentDateInGMT: despite its name, sentDateInGMT
            # is not reliably GMT -- observed consistently off by exactly the
            # account's own UTC offset across unrelated senders. receivedTime
            # is Zoho's own authoritative server-side receipt timestamp.
            "date": _epoch_ms_to_iso8601(raw["receivedTime"], mailbox_timezone),
            "snippet": html.unescape(raw["summary"]),
            "folder_id": raw["folderId"],
            # Confirmed empirically (a freshly-sent, unopened email showed
            # status="0"; already-read mail showed "1"); any other/unknown
            # value defaults to unread, the safer failure mode.
            "read": raw["status"] == "1",
        }
    except (KeyError, TypeError, ValueError) as e:
        raise ZohoAPIError(f"Malformed email summary from Zoho: {e}") from e


def normalize_email_content(raw: dict, *, strip_invisible_chars: bool = False) -> dict:
    """Normalize Zoho Mail's Get Email Content response into plain text.

    Args:
        raw: the ``data`` object from Zoho's Get Email Content response.
        strip_invisible_chars: if True, remove characters some marketing
            emails use purely to pad preview text (combining grapheme
            joiner, zero-width space, BOM, word joiner). Deliberately does
            *not* touch zero-width joiner/non-joiner, since those carry real
            meaning in emoji sequences and some scripts (Persian, Indic) --
            stripping them would silently corrupt content, not just tidy it.

    Raises:
        ZohoAPIError: if ``raw`` is missing an expected field. Malformed HTML
            itself never raises -- BeautifulSoup degrades gracefully.
    """
    try:
        text = BeautifulSoup(raw["content"], "html.parser").get_text(
            separator="\n", strip=True
        )
        if strip_invisible_chars:
            text = "".join(c for c in text if c not in _INVISIBLE_PADDING_CHARS)
        return {
            "id": str(raw["messageId"]),
            "text": text,
        }
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed email content from Zoho: {e}") from e


def normalize_signature(raw: dict) -> dict:
    """Normalize one signature from Zoho Mail's Signature API.

    ``content`` is stripped from HTML to plain text, same as
    ``normalize_email_content``.

    Raises:
        ZohoAPIError: if ``raw`` is missing ``id``/``name``/``content``.
    """
    try:
        text = BeautifulSoup(raw["content"], "html.parser").get_text(
            separator="\n", strip=True
        )
        return {
            "id": raw["id"],
            "name": raw["name"],
            "content": text,
        }
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed signature from Zoho: {e}") from e


def normalize_folder(raw: dict) -> dict:
    """Normalize one folder from Zoho Mail's Folders API.

    ``path`` (e.g. "/Inbox/Work") is the hierarchy signal -- ``folderId``/
    ``previousFolderId`` is NOT a parent reference despite the name; it's
    a display-order "previous sibling" pointer (confirmed live: Drafts'
    ``previousFolderId`` is Inbox's own folderId, Templates' is Drafts',
    and so on -- a linked list, not a tree), so it's deliberately excluded
    here rather than mislabeled as a parent id.

    Raises:
        ZohoAPIError: if ``raw`` is missing ``folderId``/``folderName``/
            ``path``/``folderType``.
    """
    try:
        return {
            "id": raw["folderId"],
            "name": raw["folderName"],
            "path": raw["path"],
            "type": raw["folderType"],
        }
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed folder from Zoho: {e}") from e


def normalize_label(raw: dict) -> dict:
    """Normalize one label from Zoho Mail's Labels API.

    Raises:
        ZohoAPIError: if ``raw`` is missing ``labelId``/``displayName``.
    """
    try:
        return {
            "id": raw["labelId"],
            "name": raw["displayName"],
            "color": raw.get("color", ""),
        }
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed label from Zoho: {e}") from e


def normalize_attachment(raw: dict) -> dict:
    """Normalize one attachment entry from Zoho Mail's attachment info API.

    Metadata only -- fetching/parsing actual attachment content (PDFs,
    images, etc.) is out of scope; there's no document-parsing
    infrastructure here to make binary content usefully consumable.

    Raises:
        ZohoAPIError: if ``raw`` is missing ``attachmentId``/
            ``attachmentName``/``attachmentSize``.
    """
    try:
        return {
            "id": raw["attachmentId"],
            "name": raw["attachmentName"],
            "size_bytes": raw["attachmentSize"],
        }
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed attachment from Zoho: {e}") from e


def normalize_calendar(raw: dict) -> dict:
    """Normalize one calendar from Zoho Calendar's Calendars API.

    Raises:
        ZohoAPIError: if ``raw`` is missing ``uid``/``name``.
    """
    try:
        return {
            "id": raw["uid"],
            "name": raw["name"],
            "is_default": raw.get("isdefault", False),
            "timezone": raw.get("timezone", ""),
            "privilege": raw.get("privilege", ""),
        }
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed calendar from Zoho: {e}") from e


def normalize_freebusy_slot(raw: dict, mailbox_timezone: str) -> dict:
    """Normalize one busy-time slot from Zoho Calendar's Free/Busy API.

    ``start``/``end`` use the same wire format as event times, converted
    the same way -- see ``_zoho_event_time_to_iso8601``.

    Raises:
        ZohoAPIError: if ``raw`` is missing ``startTime``/``endTime``/
            ``fbtype``, or the times aren't parseable.
    """
    try:
        return {
            "start": _zoho_event_time_to_iso8601(raw["startTime"], mailbox_timezone),
            "end": _zoho_event_time_to_iso8601(raw["endTime"], mailbox_timezone),
            "status": raw["fbtype"],
        }
    except (KeyError, TypeError, ValueError) as e:
        raise ZohoAPIError(f"Malformed free/busy slot from Zoho: {e}") from e


def normalize_event(raw: dict, mailbox_timezone: str) -> dict:
    """Normalize one entry from Zoho Calendar's Events List ``events`` array.

    ``start``/``end`` are in ``mailbox_timezone``, not UTC -- see
    ``_zoho_event_time_to_iso8601``.

    Raises:
        ZohoAPIError: if ``raw`` is missing an expected field or a field has
            an unexpected type/value (e.g. an unparseable timestamp).
    """
    try:
        dateandtime = raw["dateandtime"]
        return {
            "id": raw["uid"],
            "title": raw["title"],
            "start": _zoho_event_time_to_iso8601(
                dateandtime["start"], mailbox_timezone
            ),
            "end": _zoho_event_time_to_iso8601(dateandtime["end"], mailbox_timezone),
            "attendees": [
                {"email": a["email"], "status": a["status"]}
                for a in raw.get("attendees", [])
            ],
        }
    except (KeyError, TypeError, ValueError) as e:
        raise ZohoAPIError(f"Malformed event from Zoho: {e}") from e


def normalize_event_detail(raw: dict) -> dict:
    """Normalize one event from Zoho Calendar's single-event ``events[0]``.

    Deliberately excludes ``start``/``end`` -- confirmed live that Zoho's
    single-event endpoint can return the wrong occurrence's dates for a
    recurring event (for one all-day yearly event, requesting a specific
    ``recurrenceid`` returned a start date one day later, and end date
    padded to an extra day of duration versus that same occurrence in
    Events List). Get the correct start/end for a specific occurrence from
    ``list_events`` instead; use this only for detail it doesn't include
    -- full attendee list (Events List can report only the caller's own
    attendee entry for an occurrence, not every invitee), organizer,
    location, description, and recurrence rule.

    Raises:
        ZohoAPIError: if ``raw`` is missing ``uid``/``title``/``organizer``,
            or an entry in ``attendees`` is missing ``email``/``status``.
    """
    try:
        return {
            "id": raw["uid"],
            "title": raw["title"],
            "organizer": raw["organizer"],
            "location": raw.get("location") or "",
            "description": raw.get("description") or "",
            "recurrence": raw.get("rrule") or "",
            "attendees": [
                {"email": a["email"], "status": a["status"]}
                for a in raw.get("attendees") or []
            ],
        }
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed event detail from Zoho: {e}") from e


def _format_recurring(raw: dict | None) -> dict | None:
    if not raw:
        return None
    return {"type": raw.get("type", ""), "frequency": raw.get("frequency", 1)}


def normalize_task(raw: dict) -> dict:
    """Normalize one task from Zoho Mail's Tasks API ``data.tasks`` array.

    ``created_at``/``modified_at`` are passed through unchanged -- unlike
    Mail's epoch-millisecond strings or Calendar's custom
    ``yyyyMMdd'T'HHmmss(Z|+/-HHMM)`` format, Zoho's Tasks API already
    returns proper ISO 8601 timestamps with a real UTC offset, confirmed
    live, so no conversion is needed here.

    ``due_date``'s real format is unverified -- no task in the account
    this was built against has ever had one set, so it's passed through
    as an opaque string (defaulting to "") rather than parsed under an
    unconfirmed format assumption.

    Raises:
        ZohoAPIError: if ``raw`` is missing ``id``, ``title``, or ``status``.
    """
    try:
        return {
            "id": raw["id"],
            "title": raw["title"],
            "description": raw.get("description") or "",
            "status": raw["status"],
            "priority": raw.get("priority") or "",
            "due_date": raw.get("dueDate") or "",
            "project": (raw.get("project") or {}).get("name", ""),
            "assignee": (raw.get("assignee") or {}).get("name", ""),
            "tags": raw.get("tags") or [],
            "subtask_count": len(raw.get("subtasks") or []),
            "recurring": _format_recurring(raw.get("recurring")),
            "created_at": raw.get("createdAt") or "",
            "modified_at": raw.get("modifiedTime") or "",
        }
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed task from Zoho: {e}") from e


def normalize_note(raw: dict, mailbox_timezone: str) -> dict:
    """Normalize one note from Zoho Mail's Notes API.

    ``created_at``/``modified_at`` are converted from Zoho's epoch-
    millisecond strings to ISO 8601 in ``mailbox_timezone`` -- see
    ``_epoch_ms_to_iso8601``. Unlike Tasks' timestamps, Notes' are epoch
    strings like Mail's, not already-formatted ISO 8601 -- confirmed live,
    not assumed from Tasks' behavior.

    Excludes ``summary`` (redundant with ``content``, just a preview of
    it), the numeric ``color`` index (``colorHex`` is the actual usable
    value), and ``namespaceId``/``ownerZuid`` (Zoho-internal, redundant
    with ``ownerDisplayName``).

    Raises:
        ZohoAPIError: if ``raw`` is missing ``entityId``/``title``, or
            ``createdTime``/``modifiedTime`` aren't parseable.
    """
    try:
        return {
            "id": raw["entityId"],
            "title": raw["title"],
            "content": raw.get("content") or "",
            "book": raw.get("bookName") or "",
            "owner": raw.get("ownerDisplayName") or "",
            "is_favorite": raw.get("isFavorite", False),
            "color": raw.get("colorHex") or "",
            "created_at": _epoch_ms_to_iso8601(raw["createdTime"], mailbox_timezone),
            "modified_at": _epoch_ms_to_iso8601(raw["modifiedTime"], mailbox_timezone),
        }
    except (KeyError, TypeError, ValueError) as e:
        raise ZohoAPIError(f"Malformed note from Zoho: {e}") from e


def normalize_bookmark(raw: dict) -> dict:
    """Normalize one bookmark from Zoho Mail's Bookmarks API.

    Excludes ``linkMetaInfo`` (its ``linkTitle``/``linkDescription`` are
    redundant with the top-level ``title``/``summary``, confirmed
    identical live) and ``namespaceId``/``ownerZuid`` (Zoho-internal,
    redundant with ``ownerDisplayName``). No timestamps -- confirmed live
    that bookmarks, unlike notes, don't have created/modified fields at
    all.

    ``is_favorite`` is compared as a string: confirmed live that
    Bookmarks' ``isFavorite`` is the string ``"true"``/``"false"``, not
    the real boolean the same-named field is on Notes -- a sibling Zoho
    Mail feature with the same field name but a different type. Don't
    assume type consistency across endpoints just because a field name
    matches.

    Raises:
        ZohoAPIError: if ``raw`` is missing ``entityId``, ``title``, or
            ``link``.
    """
    try:
        return {
            "id": raw["entityId"],
            "title": raw["title"],
            "url": raw["link"],
            "summary": raw.get("summary") or "",
            "collection": raw.get("collectionName") or "",
            "owner": raw.get("ownerDisplayName") or "",
            "is_favorite": str(raw.get("isFavorite", False)).lower() == "true",
            "tags": raw.get("tags") or [],
        }
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed bookmark from Zoho: {e}") from e


def normalize_group(raw: dict) -> dict:
    """Normalize one group from a Zoho Mail Tasks/Notes/Bookmarks group list.

    The three services disagree on shape, confirmed live against a real
    group: Tasks nests its array under ``data.groups`` and keys the id
    as an **int** ``id``, while Notes and Bookmarks return ``data`` as
    the array directly and key the id as a **string** ``groupId`` --
    same group, different key *and* different type. Ids are coerced to
    ``str`` so callers get one consistent type either way.

    ``owner``/``member_count`` come from Tasks' richer payload
    (``owner``/``numberOfMembers``); Notes and Bookmarks report neither,
    so they fall back to ``""``/``None``. In practice Tasks lists every
    group the user belongs to -- see ``list_groups`` -- so these are
    normally populated, but the fallbacks keep a group that only one
    service reports from being dropped or faked.

    Raises:
        ZohoAPIError: if ``raw`` is missing its id or ``name``.
    """
    try:
        return {
            "id": str(raw["id"] if "id" in raw else raw["groupId"]),
            "name": raw["name"],
            "owner": raw.get("owner") or "",
            "member_count": raw.get("numberOfMembers"),
        }
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed group from Zoho: {e}") from e


def _scoped_url(root: str, group_id: str | None) -> str:
    """Build a Tasks/Notes/Bookmarks URL for either personal or group scope.

    Zoho splits these three services' data by scope in the path rather
    than by a query param: ``/me`` for the caller's own items,
    ``/groups/{id}`` for a shared group's.
    """
    return f"{root}/groups/{group_id}" if group_id is not None else f"{root}/me"


def normalize_floor(raw: dict) -> dict:
    """Normalize one floor from Zoho Calendar's Resource Booking API.

    Raises:
        ZohoAPIError: if ``raw`` is missing ``floor_id``/``floor_name``.
    """
    try:
        return {
            "id": raw["floor_id"],
            "name": raw["floor_name"],
            "has_resource": raw.get("has_resource", False),
        }
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed floor from Zoho: {e}") from e


def normalize_building(raw: dict) -> dict:
    """Normalize one building from Zoho Calendar's Resource Booking API.

    Raises:
        ZohoAPIError: if ``raw`` is missing ``building_id``/``building_name``,
            or an entry in ``floors`` is malformed.
    """
    try:
        return {
            "id": raw["building_id"],
            "name": raw["building_name"],
            "floors": [normalize_floor(f) for f in raw.get("floors") or []],
        }
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed building from Zoho: {e}") from e


def normalize_branch(raw: dict) -> dict:
    """Normalize one branch from Zoho Calendar's Resource Booking API.

    Raises:
        ZohoAPIError: if ``raw`` is missing ``branch_id``/``branch_name``,
            or an entry in ``buildings`` is malformed.
    """
    try:
        return {
            "id": raw["branch_id"],
            "name": raw["branch_name"],
            "timezone": raw.get("time_zone", ""),
            "buildings": [normalize_building(b) for b in raw.get("buildings") or []],
        }
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed branch from Zoho: {e}") from e


def normalize_resource(raw: dict) -> dict:
    """Normalize one resource from Zoho Calendar's Resource Booking API.

    ``email`` is the resource's own bookable calendar address (invite it
    to an event to book it). ``location`` is ``res_location``, a
    "Branch/Building/Floor" path.

    Raises:
        ZohoAPIError: if ``raw`` is missing ``resource_id``/``resource_name``.
    """
    try:
        return {
            "id": raw["resource_id"],
            "name": raw["resource_name"],
            "category": raw.get("category_name") or "",
            "email": raw.get("res_email_id") or "",
            "capacity": raw.get("capacity", 0),
            "location": raw.get("res_location") or "",
            "branch_id": raw.get("branch_id") or "",
            "building_id": raw.get("building_id") or "",
            "floor_id": raw.get("floor_id") or "",
        }
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed resource from Zoho: {e}") from e


async def _zoho_authenticated_request(
    method: str,
    http_client: httpx.AsyncClient,
    url: str,
    access_token: str,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict:
    """Shared Zoho-auth-header-and-error-wrapping used by every Zoho call,
    regardless of HTTP method.

    Raises:
        ZohoAPIError: if the request fails or Zoho returns a non-2xx response.
    """
    headers = {
        "Authorization": f"Zoho-oauthtoken {access_token}",
        "Accept": "application/json",
    }
    try:
        response = await http_client.request(
            method, url, params=params, json=json_body, headers=headers
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise ZohoAPIError(
            f"Zoho API request to {url} failed with "
            f"{e.response.status_code}: {e.response.text}"
        ) from e
    except httpx.HTTPError as e:
        raise ZohoAPIError(f"Zoho API request to {url} failed: {e}") from e
    return response.json()


async def zoho_authenticated_get(
    http_client: httpx.AsyncClient,
    url: str,
    access_token: str,
    params: dict | None = None,
) -> dict:
    """Shared GET-with-Zoho-auth-header-and-error-wrapping used by every Zoho call.

    Raises:
        ZohoAPIError: if the request fails or Zoho returns a non-2xx response.
    """
    return await _zoho_authenticated_request(
        "GET", http_client, url, access_token, params
    )


async def zoho_authenticated_post(
    http_client: httpx.AsyncClient,
    url: str,
    access_token: str,
    params: dict | None = None,
) -> dict:
    """Shared POST-with-Zoho-auth-header-and-error-wrapping used by every Zoho write call.

    Raises:
        ZohoAPIError: if the request fails or Zoho returns a non-2xx response.
    """
    return await _zoho_authenticated_request(
        "POST", http_client, url, access_token, params
    )


async def zoho_authenticated_put(
    http_client: httpx.AsyncClient,
    url: str,
    access_token: str,
    params: dict | None = None,
    json_body: dict | None = None,
) -> dict:
    """Shared PUT-with-Zoho-auth-header-and-error-wrapping used by every Zoho write call.

    Raises:
        ZohoAPIError: if the request fails or Zoho returns a non-2xx response.
    """
    return await _zoho_authenticated_request(
        "PUT", http_client, url, access_token, params, json_body
    )


async def zoho_authenticated_delete(
    http_client: httpx.AsyncClient,
    url: str,
    access_token: str,
    params: dict | None = None,
) -> dict:
    """Shared DELETE-with-Zoho-auth-header-and-error-wrapping used by every Zoho write call.

    Raises:
        ZohoAPIError: if the request fails or Zoho returns a non-2xx response.
    """
    return await _zoho_authenticated_request(
        "DELETE", http_client, url, access_token, params
    )


async def _get_default_mail_account(
    token_manager: ZohoTokenManager, http_client: httpx.AsyncClient
) -> dict:
    """Fetch the user's default Zoho Mail account object (raw, unnormalized).

    Shared by ``get_primary_account_id`` and ``get_mailbox_timezone`` so the
    fetch-and-find-default logic lives in exactly one place.

    Raises:
        ZohoAPIError: if the request fails, the response is malformed, or no
            account is flagged as the default.
    """
    token = await token_manager.get_access_token()
    payload = await zoho_authenticated_get(
        http_client, f"{ZOHO_MAIL_BASE_URL}/accounts", token
    )
    try:
        for account in payload["data"]:
            if account.get("isDefaultAccount"):
                return account
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed accounts response from Zoho: {e}") from e
    raise ZohoAPIError("No default Zoho Mail account found in the accounts response")


async def get_primary_account_id(
    token_manager: ZohoTokenManager, http_client: httpx.AsyncClient
) -> str:
    """Look up the user's default Zoho Mail account id (for the ``ZOHO_ACCOUNT_ID`` setting).

    Raises:
        ZohoAPIError: if the request fails, the response is malformed, or no
            account is flagged as the default.
    """
    account = await _get_default_mail_account(token_manager, http_client)
    try:
        return account["accountId"]
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed accounts response from Zoho: {e}") from e


async def get_mailbox_timezone(
    token_manager: ZohoTokenManager, http_client: httpx.AsyncClient
) -> str:
    """Look up the user's mailbox timezone.

    Used by ``ZohoClient`` for two things: resolving "today" for
    ``search_emails(days_back=...)``, and returning every normalized
    date/time already converted to this timezone instead of UTC, so an LLM
    client is never responsible for timezone conversion it might skip or
    get wrong.

    Raises:
        ZohoAPIError: if the request fails, the response is malformed, or no
            account is flagged as the default.
    """
    account = await _get_default_mail_account(token_manager, http_client)
    try:
        return account["timeZone"]
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed accounts response from Zoho: {e}") from e


async def get_folder_types(
    token_manager: ZohoTokenManager, http_client: httpx.AsyncClient, account_id: str
) -> dict[str, str]:
    """Map every one of the account's folder ids to its ``folderType``.

    Used to filter Sent/Drafts/Templates out of ``search_emails`` results.
    Confirmed against the real API: every user-created folder (including
    subfolders and mail-rule destinations) reports ``folderType: "Inbox"``,
    not a distinct "custom" type -- so excluding by type never catches a
    user's own folders, only Zoho's built-in non-received ones.

    Raises:
        ZohoAPIError: if the request fails or the response is malformed.
    """
    token = await token_manager.get_access_token()
    payload = await zoho_authenticated_get(
        http_client, f"{ZOHO_MAIL_BASE_URL}/accounts/{account_id}/folders", token
    )
    try:
        return {folder["folderId"]: folder["folderType"] for folder in payload["data"]}
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed folders response from Zoho: {e}") from e


async def get_default_calendar_uid(
    token_manager: ZohoTokenManager, http_client: httpx.AsyncClient
) -> str:
    """Look up the user's default calendar uid (for the ``ZOHO_CALENDAR_UID`` setting).

    Raises:
        ZohoAPIError: if the request fails, the response is malformed, or no
            calendar is flagged as the default.
    """
    token = await token_manager.get_access_token()
    payload = await zoho_authenticated_get(
        http_client, f"{ZOHO_CALENDAR_BASE_URL}/calendars", token
    )
    try:
        for calendar in payload["calendars"]:
            if calendar.get("isdefault"):
                return calendar["uid"]
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed calendars response from Zoho: {e}") from e
    raise ZohoAPIError("No default Zoho Calendar found in the calendars response")


class ZohoClient:
    """Thin async REST wrapper over the Zoho Mail and Calendar APIs.

    Returns only normalized, LLM-facing shapes -- callers never see raw
    Zoho payloads or raw httpx exceptions.
    """

    def __init__(
        self,
        token_manager: ZohoTokenManager,
        http_client: httpx.AsyncClient,
        account_id: str,
        calendar_uid: str,
        strip_invisible_chars: bool = False,
    ) -> None:
        self._token_manager = token_manager
        self._http_client = http_client
        self._account_id = account_id
        self._calendar_uid = calendar_uid
        self._strip_invisible_chars = strip_invisible_chars
        self._mailbox_timezone_cache: str | None = None
        self._excluded_folder_ids_cache: frozenset[str] | None = None

    async def _get(self, url: str, params: dict | None = None) -> dict:
        token = await self._token_manager.get_access_token()
        return await zoho_authenticated_get(self._http_client, url, token, params)

    async def _post(self, url: str, params: dict | None = None) -> dict:
        token = await self._token_manager.get_access_token()
        return await zoho_authenticated_post(self._http_client, url, token, params)

    async def _put(
        self,
        url: str,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> dict:
        token = await self._token_manager.get_access_token()
        return await zoho_authenticated_put(
            self._http_client, url, token, params, json_body
        )

    async def _delete(self, url: str, params: dict | None = None) -> dict:
        token = await self._token_manager.get_access_token()
        return await zoho_authenticated_delete(self._http_client, url, token, params)

    async def _get_mailbox_timezone(self) -> str:
        """Return the mailbox's timezone, fetched once per client and cached.

        Deliberately not a static config value: the timezone is a real
        account setting a person can change (e.g. after moving), and a
        stale cached value would silently misresolve "today" again. Caching
        per-process (not persisted) bounds staleness to "since this server
        last started" rather than "since setup was last run", without
        paying a Zoho API call on every single search_emails call.
        """
        if self._mailbox_timezone_cache is None:
            self._mailbox_timezone_cache = await get_mailbox_timezone(
                self._token_manager, self._http_client
            )
        return self._mailbox_timezone_cache

    async def _get_excluded_folder_ids(self) -> frozenset[str]:
        """Folder ids to drop from search_emails results (Sent/Drafts/Templates).

        Fetched once per client and cached, same rationale as
        ``_get_mailbox_timezone``: bounded staleness (since this process
        started) rather than a static value that could drift if folders
        are added or restructured.
        """
        if self._excluded_folder_ids_cache is None:
            folder_types = await get_folder_types(
                self._token_manager, self._http_client, self._account_id
            )
            self._excluded_folder_ids_cache = frozenset(
                folder_id
                for folder_id, folder_type in folder_types.items()
                if folder_type in EXCLUDED_FOLDER_TYPES
            )
        return self._excluded_folder_ids_cache

    async def search_emails(
        self, query: str = "", limit: int = 20, days_back: int | None = None
    ) -> list[dict]:
        """Search the user's mailbox and return compact, normalized results.

        Args:
            query: Zoho Mail search syntax. May be empty if ``days_back`` is
                given (a bare ``fromDate`` filter is valid on its own).
            limit: maximum number of results (1-200).
            days_back: if given, only return emails from the last N days
                (0 = today only), computed from the mailbox's own timezone
                -- never from UTC, which disagrees with the mailbox's
                calendar day for several hours every day.

        Raises:
            ZohoAPIError: if ``limit`` is out of range, ``days_back`` is
                negative, both ``query`` and ``days_back`` are empty, or the
                Zoho Mail API rejects or fails the request.
        """
        if not (MIN_SEARCH_LIMIT <= limit <= MAX_SEARCH_LIMIT):
            raise ZohoAPIError(
                f"limit must be between {MIN_SEARCH_LIMIT} and "
                f"{MAX_SEARCH_LIMIT} (got {limit})"
            )
        if days_back is not None and days_back < 0:
            raise ZohoAPIError(f"days_back must be >= 0 (got {days_back})")
        if not query and days_back is None:
            raise ZohoAPIError(
                "search_emails requires a query, a days_back filter, or both"
            )

        # Always needed now, not just for days_back: normalized dates are
        # returned in the mailbox's own local offset, not UTC (see
        # _epoch_ms_to_iso8601), so the LLM never has to convert (or forget
        # to convert) a timezone it doesn't actually know.
        mailbox_timezone = await self._get_mailbox_timezone()

        search_key = query
        if days_back is not None:
            cutoff = _today_in_timezone(mailbox_timezone) - timedelta(days=days_back)
            date_filter = f"fromDate:{cutoff.strftime('%d-%b-%Y')}"
            search_key = f"{search_key}::{date_filter}" if search_key else date_filter

        payload = await self._get(
            f"{ZOHO_MAIL_BASE_URL}/accounts/{self._account_id}/messages/search",
            params={"searchKey": search_key, "limit": limit},
        )
        raw_items = payload.get("data", [])
        results = [
            normalize_email_summary(item, mailbox_timezone) for item in raw_items
        ]

        # Skip the folder-type fetch entirely when there's nothing to
        # filter, or when the caller explicitly scoped the search to a
        # folder themselves (e.g. "in:Sent") -- excluding by type would
        # otherwise silently strip out the exact folder they asked for.
        if raw_items and "in:" not in query.lower():
            excluded_folder_ids = await self._get_excluded_folder_ids()
            results = [r for r in results if r["folder_id"] not in excluded_folder_ids]

        return results

    async def list_emails(
        self,
        status: str = "all",
        folder_id: str | None = None,
        limit: int = 20,
        start: int = 1,
    ) -> list[dict]:
        """List emails by read/unread status, with real pagination.

        Unlike ``search_emails`` (Zoho's Search API, which has no status
        filter and -- until this was added -- no way to page past the
        first result window), this uses Zoho's separate List Emails API
        (``GET .../messages/view``), which supports a documented
        ``status`` filter and ``start``/``limit`` pagination. Use this
        when you need to reliably enumerate *every* unread (or read)
        email, not just the top N by recency.

        Args:
            status: "unread", "read", or "all" (default).
            folder_id: restrict to one folder's id, from ``list_folders``.
                If omitted, searches the whole mailbox and excludes
                Sent/Drafts/Templates by default (same as
                ``search_emails``) -- pass a folder_id explicitly to
                include one of those.
            limit: maximum number of results per page (1-200).
            start: 1-based starting sequence number, for paging past the
                first ``limit`` results (e.g. ``start=21`` with
                ``limit=20`` fetches the second page).

        Raises:
            ZohoAPIError: if ``status`` isn't one of "read"/"unread"/
                "all", ``limit``/``start`` are out of range, or the Zoho
                Mail API rejects or fails the request.
        """
        if status not in _VALID_EMAIL_STATUSES:
            raise ZohoAPIError(
                f"status must be one of {sorted(_VALID_EMAIL_STATUSES)} (got {status!r})"
            )
        if not (MIN_SEARCH_LIMIT <= limit <= MAX_SEARCH_LIMIT):
            raise ZohoAPIError(
                f"limit must be between {MIN_SEARCH_LIMIT} and "
                f"{MAX_SEARCH_LIMIT} (got {limit})"
            )
        if start < 1:
            raise ZohoAPIError(f"start must be >= 1 (got {start})")

        mailbox_timezone = await self._get_mailbox_timezone()

        params: dict = {"status": status, "start": start, "limit": limit}
        if folder_id is not None:
            params["folderId"] = folder_id

        payload = await self._get(
            f"{ZOHO_MAIL_BASE_URL}/accounts/{self._account_id}/messages/view",
            params=params,
        )
        raw_items = payload.get("data", [])
        results = [
            normalize_email_summary(item, mailbox_timezone) for item in raw_items
        ]

        if raw_items and folder_id is None:
            excluded_folder_ids = await self._get_excluded_folder_ids()
            results = [r for r in results if r["folder_id"] not in excluded_folder_ids]

        return results

    async def get_email(self, message_id: str, folder_id: str) -> dict:
        """Fetch the full plain-text content of one email.

        Raises:
            ZohoAPIError: if the Zoho Mail API rejects or fails the request.
        """
        payload = await self._get(
            f"{ZOHO_MAIL_BASE_URL}/accounts/{self._account_id}"
            f"/folders/{folder_id}/messages/{message_id}/content"
        )
        return normalize_email_content(
            payload["data"], strip_invisible_chars=self._strip_invisible_chars
        )

    async def list_attachments(self, message_id: str, folder_id: str) -> list[dict]:
        """List attachment metadata (name, size) for one email.

        Metadata only -- fetching/parsing actual attachment content is
        out of scope, see ``normalize_attachment``.

        Raises:
            ZohoAPIError: if the Zoho Mail API rejects or fails the request.
        """
        payload = await self._get(
            f"{ZOHO_MAIL_BASE_URL}/accounts/{self._account_id}"
            f"/folders/{folder_id}/messages/{message_id}/attachmentinfo"
        )
        attachments = payload.get("data", {}).get("attachments", [])
        return [normalize_attachment(a) for a in attachments]

    async def _update_message(
        self, mode: str, message_ids: list[str], **extra: object
    ) -> None:
        """Shared PUT to Zoho Mail's ``updatemessage`` endpoint underlying
        every message-state write (mark read/unread, move, label add/
        remove) -- they differ only in ``mode`` and mode-specific fields.

        Unlike Zoho Calendar's write APIs (payload as an ``eventdata``
        query param), Zoho Mail's write API takes a JSON request body.
        Zoho's own endpoint accepts a batch of ``messageId``s per call --
        this is not looped one-at-a-time, since a caller asking to mark
        35 emails read should cost one request, not 35.

        Raises:
            ZohoAPIError: if ``message_ids`` is empty, or the Zoho Mail
                API rejects or fails the request.
        """
        if not message_ids:
            raise ZohoAPIError("message_ids must contain at least one message id")
        await self._put(
            f"{ZOHO_MAIL_BASE_URL}/accounts/{self._account_id}/updatemessage",
            json_body={"mode": mode, "messageId": message_ids, **extra},
        )

    async def mark_as_read(self, message_ids: list[str]) -> None:
        """Mark one or more emails as read in a single request.

        Raises:
            ZohoAPIError: if ``message_ids`` is empty, or the Zoho Mail
                API rejects or fails the request.
        """
        await self._update_message("markAsRead", message_ids)

    async def mark_as_unread(self, message_ids: list[str]) -> None:
        """Mark one or more emails as unread in a single request.

        Raises:
            ZohoAPIError: if ``message_ids`` is empty, or the Zoho Mail
                API rejects or fails the request.
        """
        await self._update_message("markAsUnread", message_ids)

    async def move_email(self, message_ids: list[str], folder_id: str) -> None:
        """Move one or more emails to a different folder in a single request.

        Args:
            message_ids: email ``id``s from a prior ``search_emails`` result.
            folder_id: the destination folder's ``id``, from ``list_folders``.

        Raises:
            ZohoAPIError: if ``message_ids`` is empty, or the Zoho Mail
                API rejects or fails the request.
        """
        await self._update_message("moveMessage", message_ids, destfolderId=folder_id)

    async def add_label(self, message_ids: list[str], label_id: str) -> None:
        """Apply one label to one or more emails in a single request.

        Args:
            message_ids: email ``id``s from a prior ``search_emails`` result.
            label_id: the label's ``id``, from ``list_labels``.

        Raises:
            ZohoAPIError: if ``message_ids`` is empty, or the Zoho Mail
                API rejects or fails the request.
        """
        await self._update_message("applyLabel", message_ids, labelId=[label_id])

    async def remove_label(self, message_ids: list[str], label_id: str) -> None:
        """Remove one label from one or more emails in a single request.

        Args:
            message_ids: email ``id``s from a prior ``search_emails`` result.
            label_id: the label's ``id``, from ``list_labels``.

        Raises:
            ZohoAPIError: if ``message_ids`` is empty, or the Zoho Mail
                API rejects or fails the request.
        """
        await self._update_message("removeLabel", message_ids, labelId=[label_id])

    async def list_folders(self) -> list[dict]:
        """List all folders in the mailbox, including custom subfolders.

        Raises:
            ZohoAPIError: if the Zoho Mail API rejects or fails the request.
        """
        payload = await self._get(
            f"{ZOHO_MAIL_BASE_URL}/accounts/{self._account_id}/folders"
        )
        return [normalize_folder(f) for f in payload.get("data", [])]

    async def list_labels(self) -> list[dict]:
        """List all labels/tags configured in the mailbox.

        Raises:
            ZohoAPIError: if the Zoho Mail API rejects or fails the request.
        """
        payload = await self._get(
            f"{ZOHO_MAIL_BASE_URL}/accounts/{self._account_id}/labels"
        )
        return [normalize_label(item) for item in payload.get("data", [])]

    async def list_signatures(self) -> list[dict]:
        """List all configured email signatures.

        Raises:
            ZohoAPIError: if the Zoho Mail API rejects or fails the request.
        """
        payload = await self._get(f"{ZOHO_MAIL_BASE_URL}/accounts/signature")
        return [normalize_signature(item) for item in payload.get("data", [])]

    async def list_events(
        self, start: datetime, end: datetime, calendar_id: str | None = None
    ) -> list[dict]:
        """List calendar events in ``[start, end]``.

        Args:
            calendar_id: which calendar to query -- defaults to the
                configured ``ZOHO_CALENDAR_UID`` if omitted. Use
                ``list_calendars`` to see what else is available.

        Raises:
            ZohoAPIError: if the range exceeds Zoho's 31-day cap, or the
                Calendar API rejects or fails the request.
        """
        if end <= start:
            raise ZohoAPIError(
                f"end must be after start (got start={start.isoformat()}, "
                f"end={end.isoformat()})"
            )
        if (end - start) > timedelta(days=MAX_EVENT_RANGE_DAYS):
            raise ZohoAPIError(
                f"Date range cannot exceed {MAX_EVENT_RANGE_DAYS} days "
                f"(requested {(end - start).days} days). Narrow the range."
            )
        range_param = json.dumps(
            {
                "start": start.strftime(ZOHO_EVENT_RANGE_REQUEST_FORMAT),
                "end": end.strftime(ZOHO_EVENT_RANGE_REQUEST_FORMAT),
            }
        )
        # See search_emails: returned in the mailbox's own local offset, not
        # UTC, so the LLM never has to convert a timezone it doesn't know.
        mailbox_timezone = await self._get_mailbox_timezone()
        payload = await self._get(
            f"{ZOHO_CALENDAR_BASE_URL}/calendars/{calendar_id or self._calendar_uid}/events",
            params={"range": range_param},
        )
        return [
            normalize_event(item, mailbox_timezone)
            for item in payload.get("events", [])
        ]

    async def get_event(self, uid: str, calendar_id: str | None = None) -> dict:
        """Fetch full details for one event by uid.

        Args:
            calendar_id: which calendar the event belongs to -- defaults
                to the configured ``ZOHO_CALENDAR_UID`` if omitted.

        See ``normalize_event_detail`` for why this deliberately omits
        start/end -- get the occurrence's actual date/time from
        ``list_events`` instead.

        Raises:
            ZohoAPIError: if no event is found for ``uid``, or the
                Calendar API rejects or fails the request.
        """
        payload = await self._get(
            f"{ZOHO_CALENDAR_BASE_URL}/calendars/{calendar_id or self._calendar_uid}/events/{uid}"
        )
        events = payload.get("events", [])
        if not events:
            raise ZohoAPIError(f"No event found for uid={uid!r}")
        return normalize_event_detail(events[0])

    async def list_calendars(self) -> list[dict]:
        """List all calendars the user has access to.

        Raises:
            ZohoAPIError: if the Calendar API rejects or fails the request.
        """
        payload = await self._get(f"{ZOHO_CALENDAR_BASE_URL}/calendars")
        return [normalize_calendar(c) for c in payload.get("calendars", [])]

    async def get_freebusy(
        self, email: str, start: datetime, end: datetime
    ) -> list[dict]:
        """Get busy time slots for a user's calendar in ``[start, end]``.

        Only returns data for calendars that user has explicitly enabled
        "include in my Free/Busy sharing" for (a per-calendar Zoho
        Calendar setting) -- confirmed live, a calendar without it
        returns no usable data at all rather than an empty/all-free
        result, which is why this raises instead of silently returning
        ``[]`` in that case.

        Raises:
            ZohoAPIError: if ``end`` isn't after ``start``, free/busy
                sharing isn't enabled for ``email``, or the Calendar API
                rejects or fails the request.
        """
        if end <= start:
            raise ZohoAPIError(
                f"end must be after start (got start={start.isoformat()}, "
                f"end={end.isoformat()})"
            )
        mailbox_timezone = await self._get_mailbox_timezone()
        payload = await self._get(
            f"{ZOHO_CALENDAR_BASE_URL}/calendars/freebusy",
            params={
                "uemail": email,
                "sdate": start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S"),
                "edate": end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S"),
            },
        )
        if payload.get("fb_not_enabled"):
            raise ZohoAPIError(
                f"Free/busy sharing is not enabled for {email!r}'s calendar"
            )
        return [
            normalize_freebusy_slot(slot, mailbox_timezone)
            for slot in payload.get("freebusy", [])
        ]

    async def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        description: str = "",
        location: str = "",
        attendees: list[str] | None = None,
        calendar_id: str | None = None,
    ) -> dict:
        """Create a new calendar event.

        Args:
            title: event title.
            start/end: event start/end (any timezone-aware datetime).
            description: optional event description.
            location: optional event location.
            attendees: optional list of attendee email addresses. To book
                a Resource Booking resource, include its ``email`` (from
                ``list_resources``) here.
            calendar_id: which calendar to create the event in -- defaults
                to the configured default calendar if omitted.

        Returns:
            The created event, normalized the same way as ``get_event``
            (id, title, organizer, attendees, location, description,
            recurrence -- no start/end, see ``normalize_event_detail``).

        Raises:
            ZohoAPIError: if ``end`` isn't after ``start``, or the
                Calendar API rejects or fails the request.
        """
        if end <= start:
            raise ZohoAPIError(
                f"end must be after start (got start={start.isoformat()}, "
                f"end={end.isoformat()})"
            )
        eventdata: dict = {
            "title": title,
            "dateandtime": {
                "start": start.astimezone(timezone.utc).strftime(
                    ZOHO_EVENT_RANGE_REQUEST_FORMAT
                ),
                "end": end.astimezone(timezone.utc).strftime(
                    ZOHO_EVENT_RANGE_REQUEST_FORMAT
                ),
                "timezone": "UTC",
            },
        }
        if description:
            eventdata["description"] = description
        if location:
            eventdata["location"] = location
        if attendees:
            eventdata["attendees"] = [
                {"email": email, "status": "NEEDS-ACTION"} for email in attendees
            ]
        payload = await self._post(
            f"{ZOHO_CALENDAR_BASE_URL}/calendars/{calendar_id or self._calendar_uid}/events",
            params={"eventdata": json.dumps(eventdata)},
        )
        events = payload.get("events", [])
        if not events:
            raise ZohoAPIError("Zoho did not return the created event")
        return normalize_event_detail(events[0])

    async def _get_raw_event(self, uid: str, calendar_id: str | None = None) -> dict:
        """Fetch one event's raw, complete Zoho representation (unnormalized).

        Used internally by ``update_event``/``delete_event``, which need
        the current ``etag`` (mandatory for both) and, for update, the
        full current field set to merge against -- Zoho's update endpoint
        replaces the entire event, so any field not resent is deleted.

        Raises:
            ZohoAPIError: if no event is found for ``uid``, or the
                Calendar API rejects or fails the request.
        """
        payload = await self._get(
            f"{ZOHO_CALENDAR_BASE_URL}/calendars/{calendar_id or self._calendar_uid}/events/{uid}"
        )
        events = payload.get("events", [])
        if not events:
            raise ZohoAPIError(f"No event found for uid={uid!r}")
        return events[0]

    async def update_event(
        self,
        uid: str,
        title: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        description: str | None = None,
        location: str | None = None,
        attendees: list[str] | None = None,
        calendar_id: str | None = None,
    ) -> dict:
        """Update an existing calendar event.

        Only fields explicitly given are changed -- everything else is
        carried forward from the event's current state (fields in
        ``_EVENT_WRITABLE_FIELDS`` only, not the full raw GET response --
        confirmed live that resending an event's full raw representation
        verbatim fails, since some fields the GET response includes
        aren't valid write input; see ``_EVENT_WRITABLE_FIELDS``). This
        matters because Zoho's update endpoint is a full replace, not a
        partial patch: it "overwrites all existing fields with the values
        provided in the request" (confirmed in Zoho's own docs), so any
        writable field silently omitted -- including ones this method
        doesn't expose an argument for, like ``rrule``/``reminders`` --
        would be deleted from the event, not left alone.

        Args:
            uid: the event's id, from a prior ``list_events``/``get_event``/
                ``create_event`` result.
            title/start/end/description/location/attendees: only the
                fields being changed need to be given. ``start``/``end``
                must be given together (both or neither) -- there's no
                sensible default for "change only one".
            calendar_id: which calendar the event belongs to -- defaults
                to the configured default calendar if omitted.

        Returns:
            The updated event, normalized the same way as ``get_event``.

        Raises:
            ZohoAPIError: if ``start``/``end`` are given inconsistently,
                ``end`` isn't after ``start``, no event is found for
                ``uid``, or the Calendar API rejects or fails the request.
        """
        if (start is None) != (end is None):
            raise ZohoAPIError("start and end must be given together")
        if start is not None and end is not None and end <= start:
            raise ZohoAPIError(
                f"end must be after start (got start={start.isoformat()}, "
                f"end={end.isoformat()})"
            )

        raw = await self._get_raw_event(uid, calendar_id)
        eventdata = {k: v for k, v in raw.items() if k in _EVENT_WRITABLE_FIELDS}
        eventdata["etag"] = raw[
            "etag"
        ]  # mandatory for update, not itself writable data

        if title is not None:
            eventdata["title"] = title
        if start is not None and end is not None:
            eventdata["dateandtime"] = {
                "start": start.astimezone(timezone.utc).strftime(
                    ZOHO_EVENT_RANGE_REQUEST_FORMAT
                ),
                "end": end.astimezone(timezone.utc).strftime(
                    ZOHO_EVENT_RANGE_REQUEST_FORMAT
                ),
                "timezone": "UTC",
            }
        if description is not None:
            eventdata["description"] = description
        if location is not None:
            eventdata["location"] = location
        if attendees is not None:
            eventdata["attendees"] = [
                {"email": email, "status": "NEEDS-ACTION"} for email in attendees
            ]

        payload = await self._put(
            f"{ZOHO_CALENDAR_BASE_URL}/calendars/{calendar_id or self._calendar_uid}/events/{uid}",
            params={"eventdata": json.dumps(eventdata)},
        )
        events = payload.get("events", [])
        if not events:
            raise ZohoAPIError("Zoho did not return the updated event")
        return normalize_event_detail(events[0])

    async def delete_event(self, uid: str, calendar_id: str | None = None) -> None:
        """Delete an existing calendar event.

        Args:
            uid: the event's id, from a prior ``list_events``/``get_event``/
                ``create_event`` result.
            calendar_id: which calendar the event belongs to -- defaults
                to the configured default calendar if omitted.

        Raises:
            ZohoAPIError: if no event is found for ``uid``, or the
                Calendar API rejects or fails the request.
        """
        raw = await self._get_raw_event(uid, calendar_id)
        await self._delete(
            f"{ZOHO_CALENDAR_BASE_URL}/calendars/{calendar_id or self._calendar_uid}/events/{uid}",
            params={"eventdata": json.dumps({"etag": raw["etag"]})},
        )

    async def list_branches(self) -> list[dict]:
        """List the office branches configured for Resource Booking,
        each with its nested buildings and floors.

        An empty list is a normal, common result -- Resource Booking is
        an office-facility feature most personal/small accounts never
        set up, not an error condition.

        Raises:
            ZohoAPIError: if the response isn't a JSON list, or the
                Calendar API rejects or fails the request.
        """
        payload = await self._get(ZOHO_BRANCHES_URL)
        if not isinstance(payload, list):
            raise ZohoAPIError(
                f"Malformed branches response from Zoho: expected a list, "
                f"got {type(payload).__name__}"
            )
        return [normalize_branch(b) for b in payload]

    async def list_resources(
        self, branch_id: str, building_id: str, floor_id: str
    ) -> list[dict]:
        """List the bookable resources (rooms, equipment) on one floor.

        All three ids are required by Zoho's own API -- get them from
        ``list_branches``.

        Raises:
            ZohoAPIError: if the response isn't a JSON list, or the
                Calendar API rejects or fails the request.
        """
        payload = await self._get(
            ZOHO_RESOURCES_URL,
            params={
                "branchId": branch_id,
                "buildingId": building_id,
                "floorId": floor_id,
            },
        )
        if not isinstance(payload, list):
            raise ZohoAPIError(
                f"Malformed resources response from Zoho: expected a list, "
                f"got {type(payload).__name__}"
            )
        return [normalize_resource(r) for r in payload]

    async def list_groups(self) -> list[dict]:
        """List every shared Zoho Mail group the user belongs to.

        A group is a single entity shared by Tasks, Notes, and Bookmarks
        -- not a per-service thing. Confirmed live: one real group came
        back from all three endpoints, keyed by the same id, *including*
        the services where it holds zero items. So these endpoints list
        "groups you belong to", not "groups with items in this service",
        and the same group must collapse to one row here rather than
        appearing once per service (which would read as three groups).

        Zoho has no single "my groups" endpoint, so all three are still
        queried and merged by id, rather than trusting one to be
        complete on the strength of a single observed group. Tasks is
        merged first because only its payload carries ``owner`` and
        ``numberOfMembers``. All three work under the scopes already
        requested for reading those services, and each returns a 200
        with an empty collection (not an error) when there are no groups.

        Returns:
            ``[{"id", "name", "owner", "member_count"}, ...]``, one row
            per distinct group. ``owner``/``member_count`` fall back to
            ``""``/``None`` for a group Tasks didn't report. An empty
            list is a normal result -- groups are a shared-mailbox
            feature most personal accounts never set up. Pass an ``id``
            to any of ``list_tasks``/``list_notes``/``list_bookmarks``'s
            ``group_id`` argument; the same id works for all three.

        Raises:
            ZohoAPIError: if any of the three requests fails, or a group
                in the response is malformed.
        """
        tasks_payload = await self._get(f"{ZOHO_TASKS_ROOT_URL}/groups")
        notes_payload = await self._get(f"{ZOHO_NOTES_ROOT_URL}/groups")
        bookmarks_payload = await self._get(f"{ZOHO_BOOKMARKS_ROOT_URL}/groups")

        # Tasks nests its array under data.groups; Notes and Bookmarks
        # return data as the array itself. Confirmed live.
        raw_groups = [
            *tasks_payload.get("data", {}).get("groups", []),
            *notes_payload.get("data", []),
            *bookmarks_payload.get("data", []),
        ]

        # dict preserves insertion order, so Tasks' richer entry wins and
        # the later bare Notes/Bookmarks duplicates are dropped.
        merged: dict[str, dict] = {}
        for raw in raw_groups:
            group = normalize_group(raw)
            merged.setdefault(group["id"], group)
        return list(merged.values())

    async def list_tasks(
        self,
        limit: int = 20,
        offset: int = 0,
        group_id: str | None = None,
        view: str | None = None,
    ) -> tuple[list[dict], bool]:
        """List Zoho Mail tasks -- personal, a group's, or a cross-group view.

        Args:
            limit: maximum number of tasks to return (1-499).
            offset: how many tasks to skip before returning results (Zoho's
                own ``from`` param -- renamed here since ``from`` is a
                Python keyword).
            group_id: list a shared group's tasks instead of the caller's
                personal ones. Ids come from ``list_groups``.
            view: ``"assigned_to_me"`` or ``"created_by_me"`` -- Zoho's
                two cross-group task views, which span every group the
                user belongs to rather than one scope. Confirmed live
                that on an account with no groups these return exactly
                the same tasks as the personal list, so they're only
                meaningfully different once group tasks exist. Mutually
                exclusive with ``group_id``, since the two select scope
                in incompatible ways (a path segment vs. a query param).

        Returns:
            ``(tasks, has_more)`` -- ``has_more`` reflects whether Zoho's
            response included a ``paging.nextPage``, not a guess from the
            result count.

        Raises:
            ZohoAPIError: if ``limit``/``offset`` are out of range,
                ``view`` isn't a recognized value, both ``group_id`` and
                ``view`` are given, or the Tasks API rejects or fails the
                request.
        """
        if not (MIN_TASKS_LIMIT <= limit <= MAX_TASKS_LIMIT):
            raise ZohoAPIError(
                f"limit must be between {MIN_TASKS_LIMIT} and "
                f"{MAX_TASKS_LIMIT} (got {limit})"
            )
        if offset < 0:
            raise ZohoAPIError(f"offset must be >= 0 (got {offset})")
        if group_id is not None and view is not None:
            raise ZohoAPIError("group_id and view cannot be given together")
        if view is not None and view not in _TASK_VIEWS:
            raise ZohoAPIError(
                f"view must be one of {sorted(_TASK_VIEWS)} (got {view!r})"
            )

        params: dict = {"limit": limit, "from": offset}
        if view is not None:
            # The cross-group views live at the service root (trailing
            # slash required) and need action=view alongside the view name.
            url = f"{ZOHO_TASKS_ROOT_URL}/"
            params |= {"action": "view", "view": _TASK_VIEWS[view]}
        else:
            url = _scoped_url(ZOHO_TASKS_ROOT_URL, group_id)

        payload = await self._get(url, params=params)
        data = payload.get("data", {})
        tasks = [normalize_task(t) for t in data.get("tasks", [])]
        has_more = bool(data.get("paging", {}).get("nextPage"))
        return tasks, has_more

    async def get_task(self, task_id: str) -> dict:
        """Fetch one personal task by id.

        Raises:
            ZohoAPIError: if no task is found for ``task_id``, or the
                Tasks API rejects or fails the request.
        """
        payload = await self._get(f"{ZOHO_TASKS_BASE_URL}/{task_id}")
        tasks = payload.get("data", {}).get("tasks", [])
        if not tasks:
            raise ZohoAPIError(f"No task found for task_id={task_id!r}")
        return normalize_task(tasks[0])

    async def list_notes(
        self,
        limit: int = 20,
        after: int = 0,
        group_id: str | None = None,
        oldest_first: bool = False,
    ) -> list[dict]:
        """List Zoho Mail notes -- the caller's personal ones, or a group's.

        Args:
            limit: maximum number of notes to return (1-399).
            group_id: list a shared group's notes instead of the caller's
                personal ones. Ids come from ``list_groups``.
            oldest_first: return oldest-created first instead of the
                default newest-first. Sent as Zoho's ``isPrev`` param,
                whose docs describe only "ascending or descending order
                based on created time" without saying which value is
                which -- confirmed live against real notes that
                ``isPrev=true`` is ascending (oldest first), and that an
                absent ``isPrev`` is identical to ``isPrev=false``. It's
                a sort-order flag despite the paging-sounding name;
                ``after`` offsets within whichever order is selected.
            after: how many notes to skip before returning results. Zoho's
                own docs describe this vaguely as "specifies from which
                retrieval has to be done" -- confirmed live it behaves as
                a plain integer offset (0 works; passing a note's own id
                or timestamp as a cursor causes a 500), not the opaque
                cursor the name suggests.

        Returns:
            Normalized notes. Unlike ``search_contacts``/``list_tasks``,
            there is no ``has_more`` -- confirmed live, Zoho's response
            includes no paging/total signal at all for this endpoint.
            Getting back fewer than ``limit`` results is the only
            reliable sign you've reached the end.

        Raises:
            ZohoAPIError: if ``limit``/``after`` are out of range, or the
                Notes API rejects or fails the request.
        """
        if not (MIN_NOTES_LIMIT <= limit <= MAX_NOTES_LIMIT):
            raise ZohoAPIError(
                f"limit must be between {MIN_NOTES_LIMIT} and "
                f"{MAX_NOTES_LIMIT} (got {limit})"
            )
        if after < 0:
            raise ZohoAPIError(f"after must be >= 0 (got {after})")
        mailbox_timezone = await self._get_mailbox_timezone()
        params: dict = {"limit": limit, "after": after}
        if oldest_first:
            # Only sent when switching order -- absent and "false" are
            # confirmed equivalent, so the default request is unchanged.
            params["isPrev"] = "true"
        payload = await self._get(
            _scoped_url(ZOHO_NOTES_ROOT_URL, group_id), params=params
        )
        notes = payload.get("data", {}).get("list", [])
        return [normalize_note(n, mailbox_timezone) for n in notes]

    async def get_note(self, note_id: str) -> dict:
        """Fetch one personal note by id.

        Raises:
            ZohoAPIError: if the Notes API rejects or fails the request,
                or its response is missing the note data.
        """
        mailbox_timezone = await self._get_mailbox_timezone()
        payload = await self._get(f"{ZOHO_NOTES_BASE_URL}/{note_id}")
        try:
            note = payload["data"]
        except (KeyError, TypeError) as e:
            raise ZohoAPIError(f"Malformed note response from Zoho: {e}") from e
        return normalize_note(note, mailbox_timezone)

    async def list_bookmarks(
        self, limit: int = 20, after: int = 0, group_id: str | None = None
    ) -> list[dict]:
        """List Zoho Mail bookmarks -- the caller's personal ones, or a group's.

        Args:
            limit: maximum number of bookmarks to return (1-399).
            group_id: list a shared group's bookmarks instead of the
                caller's personal ones. Ids come from ``list_groups``.
            after: how many bookmarks to skip before returning results
                (behaves as a plain integer offset -- see ``list_notes``).

        Returns:
            Normalized bookmarks. No ``has_more`` -- same as
            ``list_notes``, Zoho's response includes no paging/total
            signal for this endpoint either.

        Raises:
            ZohoAPIError: if ``limit``/``after`` are out of range, or the
                Bookmarks API rejects or fails the request.
        """
        if not (MIN_BOOKMARKS_LIMIT <= limit <= MAX_BOOKMARKS_LIMIT):
            raise ZohoAPIError(
                f"limit must be between {MIN_BOOKMARKS_LIMIT} and "
                f"{MAX_BOOKMARKS_LIMIT} (got {limit})"
            )
        if after < 0:
            raise ZohoAPIError(f"after must be >= 0 (got {after})")
        payload = await self._get(
            _scoped_url(ZOHO_BOOKMARKS_ROOT_URL, group_id),
            params={"limit": limit, "after": after},
        )
        bookmarks = payload.get("data", {}).get("list", [])
        return [normalize_bookmark(b) for b in bookmarks]

    async def get_bookmark(self, bookmark_id: str) -> dict:
        """Fetch one personal bookmark by id.

        Raises:
            ZohoAPIError: if the Bookmarks API rejects or fails the
                request, or its response is missing the bookmark data.
        """
        payload = await self._get(f"{ZOHO_BOOKMARKS_BASE_URL}/{bookmark_id}")
        try:
            bookmark = payload["data"]
        except (KeyError, TypeError) as e:
            raise ZohoAPIError(f"Malformed bookmark response from Zoho: {e}") from e
        return normalize_bookmark(bookmark)
