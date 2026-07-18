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


def normalize_email_content(raw: dict) -> dict:
    """Normalize Zoho Mail's Get Email Content response into plain text.

    Raises:
        ZohoAPIError: if ``raw`` is missing an expected field. Malformed HTML
            itself never raises -- BeautifulSoup degrades gracefully.
    """
    try:
        text = BeautifulSoup(raw["content"], "html.parser").get_text(
            separator="\n", strip=True
        )
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
    ) -> None:
        self._token_manager = token_manager
        self._http_client = http_client
        self._account_id = account_id
        self._calendar_uid = calendar_uid

    async def _auth_headers(self) -> dict:
        token = await self._token_manager.get_access_token()
        return {
            "Authorization": f"Zoho-oauthtoken {token}",
            "Accept": "application/json",
        }

    async def _get(self, url: str, params: dict | None = None) -> dict:
        headers = await self._auth_headers()
        try:
            response = await self._http_client.get(url, params=params, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise ZohoAPIError(
                f"Zoho API request to {url} failed with "
                f"{e.response.status_code}: {e.response.text}"
            ) from e
        except httpx.HTTPError as e:
            raise ZohoAPIError(f"Zoho API request to {url} failed: {e}") from e
        return response.json()

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
        return normalize_email_content(payload["data"])

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
