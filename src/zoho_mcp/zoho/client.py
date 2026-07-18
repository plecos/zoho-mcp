"""Zoho Mail and Calendar REST client, plus raw-response normalization.

All conversion from Zoho's wire format (epoch-millisecond strings, HTML email
bodies, event timestamps) into the compact, LLM-facing shapes used by the
MCP tools happens here and only here.
"""

import json
from datetime import datetime, timedelta, timezone

import httpx
from bs4 import BeautifulSoup

from zoho_mcp.zoho.auth import ZohoTokenManager

ZOHO_EVENT_RANGE_REQUEST_FORMAT = "%Y%m%dT%H%M%SZ"
ZOHO_MAIL_BASE_URL = "https://mail.zoho.com/api"
ZOHO_CALENDAR_BASE_URL = "https://calendar.zoho.com/api/v1"
MAX_EVENT_RANGE_DAYS = 31
MIN_SEARCH_LIMIT = 1
MAX_SEARCH_LIMIT = 200

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


def _epoch_ms_to_iso8601(epoch_ms: str) -> str:
    """Convert a Zoho epoch-millisecond timestamp string to ISO 8601 UTC."""
    return datetime.fromtimestamp(int(epoch_ms) / 1000, tz=timezone.utc).isoformat()


def _zoho_event_time_to_iso8601(value: str) -> str:
    """Convert a Zoho Calendar event timestamp to ISO 8601.

    Zoho returns two real shapes here (not the single documented one):
    a date-only ``yyyyMMdd`` for all-day events, or a full timestamp with
    either a ``Z`` or a numeric UTC offset (``yyyyMMdd'T'HHmmss(Z|+/-HHMM)``).
    """
    if "T" not in value:
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    return (
        datetime.strptime(value, "%Y%m%dT%H%M%S%z")
        .astimezone(timezone.utc)
        .isoformat()
    )


def normalize_email_summary(raw: dict) -> dict:
    """Normalize one entry from Zoho Mail's List Emails ``data`` array.

    Returns the compact shape the LLM sees: id, from, subject, date, snippet.

    Raises:
        ZohoAPIError: if ``raw`` is missing an expected field or a field has
            an unexpected type/value (e.g. a non-numeric date).
    """
    try:
        return {
            "id": raw["messageId"],
            "from": raw["fromAddress"],
            "subject": raw["subject"],
            "date": _epoch_ms_to_iso8601(raw["sentDateInGMT"]),
            "snippet": raw["summary"],
            "folder_id": raw["folderId"],
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


def normalize_event(raw: dict) -> dict:
    """Normalize one entry from Zoho Calendar's Events List ``events`` array.

    Raises:
        ZohoAPIError: if ``raw`` is missing an expected field or a field has
            an unexpected type/value (e.g. an unparseable timestamp).
    """
    try:
        dateandtime = raw["dateandtime"]
        return {
            "id": raw["uid"],
            "title": raw["title"],
            "start": _zoho_event_time_to_iso8601(dateandtime["start"]),
            "end": _zoho_event_time_to_iso8601(dateandtime["end"]),
            "attendees": [
                {"email": a["email"], "status": a["status"]}
                for a in raw.get("attendees", [])
            ],
        }
    except (KeyError, TypeError, ValueError) as e:
        raise ZohoAPIError(f"Malformed event from Zoho: {e}") from e


async def _zoho_authenticated_get(
    http_client: httpx.AsyncClient,
    url: str,
    access_token: str,
    params: dict | None = None,
) -> dict:
    """Shared GET-with-Zoho-auth-header-and-error-wrapping used by every Zoho call.

    Raises:
        ZohoAPIError: if the request fails or Zoho returns a non-2xx response.
    """
    headers = {
        "Authorization": f"Zoho-oauthtoken {access_token}",
        "Accept": "application/json",
    }
    try:
        response = await http_client.get(url, params=params, headers=headers)
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise ZohoAPIError(
            f"Zoho API request to {url} failed with "
            f"{e.response.status_code}: {e.response.text}"
        ) from e
    except httpx.HTTPError as e:
        raise ZohoAPIError(f"Zoho API request to {url} failed: {e}") from e
    return response.json()


async def get_primary_account_id(
    token_manager: ZohoTokenManager, http_client: httpx.AsyncClient
) -> str:
    """Look up the user's default Zoho Mail account id (for the ``ZOHO_ACCOUNT_ID`` setting).

    Raises:
        ZohoAPIError: if the request fails, the response is malformed, or no
            account is flagged as the default.
    """
    token = await token_manager.get_access_token()
    payload = await _zoho_authenticated_get(
        http_client, f"{ZOHO_MAIL_BASE_URL}/accounts", token
    )
    try:
        for account in payload["data"]:
            if account.get("isDefaultAccount"):
                return account["accountId"]
    except (KeyError, TypeError) as e:
        raise ZohoAPIError(f"Malformed accounts response from Zoho: {e}") from e
    raise ZohoAPIError("No default Zoho Mail account found in the accounts response")


async def get_default_calendar_uid(
    token_manager: ZohoTokenManager, http_client: httpx.AsyncClient
) -> str:
    """Look up the user's default calendar uid (for the ``ZOHO_CALENDAR_UID`` setting).

    Raises:
        ZohoAPIError: if the request fails, the response is malformed, or no
            calendar is flagged as the default.
    """
    token = await token_manager.get_access_token()
    payload = await _zoho_authenticated_get(
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

    async def _get(self, url: str, params: dict | None = None) -> dict:
        token = await self._token_manager.get_access_token()
        return await _zoho_authenticated_get(self._http_client, url, token, params)

    async def search_emails(self, query: str, limit: int = 20) -> list[dict]:
        """Search the user's mailbox and return compact, normalized results.

        Raises:
            ZohoAPIError: if ``limit`` is outside Zoho's documented 1-200
                range, or the Zoho Mail API rejects or fails the request.
        """
        if not (MIN_SEARCH_LIMIT <= limit <= MAX_SEARCH_LIMIT):
            raise ZohoAPIError(
                f"limit must be between {MIN_SEARCH_LIMIT} and "
                f"{MAX_SEARCH_LIMIT} (got {limit})"
            )
        payload = await self._get(
            f"{ZOHO_MAIL_BASE_URL}/accounts/{self._account_id}/messages/search",
            params={"searchKey": query, "limit": limit},
        )
        return [normalize_email_summary(item) for item in payload.get("data", [])]

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

    async def list_events(self, start: datetime, end: datetime) -> list[dict]:
        """List calendar events in ``[start, end]``.

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
        payload = await self._get(
            f"{ZOHO_CALENDAR_BASE_URL}/calendars/{self._calendar_uid}/events",
            params={"range": range_param},
        )
        return [normalize_event(item) for item in payload.get("events", [])]
