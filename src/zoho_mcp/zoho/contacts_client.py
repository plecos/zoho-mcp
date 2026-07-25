"""Zoho Contacts REST client, plus raw-response normalization.

Zoho Contacts is a distinct Zoho product from Mail/Calendar -- its own base
URL (``contacts.zoho.com``, not ``mail.zoho.com``) and its own OAuth scope
family (``zohocontacts.contactapi.*``). Kept in its own module rather than
growing ``zoho/client.py`` into a third service.

A single account can have both a Personal ("self") and an Organization
("org") contacts pool -- confirmed live, two genuinely separate resources
with their own Archived/Inactive folders, not a view over one dataset.
Critically, ``contact_id`` is not globally unique across the two: fetching
an org contact's id through the personal endpoint returns a 200 with a
different, partial record for that same id rather than a 404, so scope
must always travel with a contact_id rather than being guessed/retried.
"""

import httpx

from zoho_mcp.zoho.auth import ZohoTokenManager
from zoho_mcp.zoho.client import (
    MALFORMED_DATA_ERRORS,
    ZohoAPIError,
    zoho_authenticated_get,
)

ZOHO_CONTACTS_SELF_URL = "https://contacts.zoho.com/api/v1/accounts/self/contacts"
ZOHO_CONTACTS_ORG_URL = "https://contacts.zoho.com/api/v1/accounts/org/contacts"
_SCOPE_URLS = {
    "personal": ZOHO_CONTACTS_SELF_URL,
    "organization": ZOHO_CONTACTS_ORG_URL,
}
MIN_SEARCH_LIMIT = 1
# "filter_type" is undocumented -- not in Zoho's published parameter list
# (https://www.zoho.com/contacts/api/parameters.html), found by inspecting
# the real network requests the Zoho Contacts web client makes when
# opening its Archived/Inactive folders, then confirmed live against a
# contact actually archived on this account.
_STATUS_FILTER_TYPES = {"archived", "inactive"}
VALID_STATUSES = {"active"} | _STATUS_FILTER_TYPES


def _scope_url(scope: str) -> str:
    try:
        return _SCOPE_URLS[scope]
    except KeyError:
        raise ZohoAPIError(
            f"Unknown contacts scope {scope!r}, must be one of {sorted(_SCOPE_URLS)}"
        ) from None


def _format_birthday(raw: dict) -> str:
    """Format Zoho's separate birth_year/birth_month/birth_day fields.

    Many people record only month/day (no year, often omitted for privacy)
    -- a real, valid combination confirmed against the live API. Returns
    "" if month or day is missing (a birthday needs at least those two).
    """
    month = raw.get("birth_month")
    day = raw.get("birth_day")
    if not (month and day):
        return ""
    year = raw.get("birth_year")
    if year:
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return f"{int(month):02d}-{int(day):02d}"


def normalize_contact(raw: dict, scope: str) -> dict:
    """Normalize one Zoho Contacts contact object.

    Args:
        raw: the raw contact object from Zoho.
        scope: which contacts pool ``raw`` came from -- ``"personal"`` or
            ``"organization"``. Stamped onto the result because contact_id
            is not unique across scopes, so any later ``get_contact`` call
            needs to know which endpoint to use.

    Most fields default to "" / [] rather than raising when absent -- real
    contacts frequently omit last_name, company, phones, notes, etc.
    entirely, confirmed against the live API. Zoho also sometimes sends an
    explicit ``null`` (not an absent key) for empty list fields, which
    ``raw.get(key, [])`` would not catch -- ``raw.get(key) or []`` handles
    both cases.

    Deliberately excludes Zoho-internal fields with no meaning to an LLM
    (contact_type, contact_zuid, zid, status, is_active, is_writable,
    own_org, created_time/updated_time, primary_email_id -- redundant with
    emails, photo_url) and fields whose real shape was never confirmed
    (categories -- never seen populated in any real response).

    Raises:
        ZohoAPIError: if ``raw`` is missing ``contact_id``, an entry in
            ``emails`` is missing ``email_id``, or an entry in ``phones``
            is missing ``number``.
    """
    try:
        return {
            "id": raw["contact_id"],
            "scope": scope,
            "first_name": raw.get("first_name", ""),
            "last_name": raw.get("last_name", ""),
            "nickname": raw.get("nick_name", ""),
            "company": raw.get("company", ""),
            "emails": [e["email_id"] for e in raw.get("emails") or []],
            "phones": [
                {"number": p["number"], "type": p.get("type", "")}
                for p in raw.get("phones") or []
            ],
            "notes": raw.get("notes", ""),
            "birthday": _format_birthday(raw),
        }
    except MALFORMED_DATA_ERRORS as e:
        raise ZohoAPIError(f"Malformed contact from Zoho: {e}") from e


class ZohoContactsClient:
    """Thin async REST wrapper over the Zoho Contacts API."""

    def __init__(
        self, token_manager: ZohoTokenManager, http_client: httpx.AsyncClient
    ) -> None:
        self._token_manager = token_manager
        self._http_client = http_client

    async def _get(self, url: str, params: dict | None = None) -> dict:
        token = await self._token_manager.get_access_token()
        return await zoho_authenticated_get(self._http_client, url, token, params)

    async def search_contacts(
        self, query: str = "", limit: int = 20, status: str = "active"
    ) -> tuple[list[dict], bool]:
        """Search the user's Zoho Contacts, across both Personal and
        Organization scopes.

        Args:
            query: free-text search (name, email, phone, etc. -- Zoho's
                backend matches across all of these). May be empty to list
                contacts without filtering.
            limit: maximum number of combined results to return, across
                both scopes.
            status: which folder to search -- ``"active"`` (default),
                ``"archived"``, or ``"inactive"``. Archived/inactive
                contacts are excluded unless explicitly requested via this
                argument.

        Returns:
            ``(contacts, has_more)``. Each contact is tagged with its
            ``scope``. ``has_more`` is True if either scope's own signal
            said so, or if the combined results before truncation exceeded
            ``limit`` -- callers never have to guess from a
            suspiciously-round result count.

        Raises:
            ZohoAPIError: if ``limit`` is less than 1, ``status`` is not
                recognized, or the Contacts API rejects or fails the
                request.
        """
        if limit < MIN_SEARCH_LIMIT:
            raise ZohoAPIError(f"limit must be >= {MIN_SEARCH_LIMIT} (got {limit})")
        if status not in VALID_STATUSES:
            raise ZohoAPIError(
                f"Unknown contacts status {status!r}, must be one of {sorted(VALID_STATUSES)}"
            )
        params: dict = {"per_page": limit, "include": "emails,phones"}
        if query:
            params["q"] = query
        if status in _STATUS_FILTER_TYPES:
            params["filter_type"] = status

        self_payload = await self._get(ZOHO_CONTACTS_SELF_URL, params=params)
        org_payload = await self._get(ZOHO_CONTACTS_ORG_URL, params=params)

        contacts = [
            normalize_contact(c, "personal") for c in self_payload.get("contacts", [])
        ] + [
            normalize_contact(c, "organization")
            for c in org_payload.get("contacts", [])
        ]
        has_more = bool(self_payload.get("has_more", False)) or bool(
            org_payload.get("has_more", False)
        )
        if len(contacts) > limit:
            has_more = True
            contacts = contacts[:limit]
        return contacts, has_more

    async def get_contact(self, contact_id: str, scope: str) -> dict:
        """Fetch one contact by id from a specific scope.

        Args:
            contact_id: a contact's ``id`` from a prior ``search_contacts``
                result.
            scope: that same result's ``scope`` (``"personal"`` or
                ``"organization"``) -- required because contact_id is not
                unique across scopes, and querying the wrong scope can
                return a 200 with a different, partial record rather than
                a clean error.

        Raises:
            ZohoAPIError: if ``scope`` is not recognized, or the Contacts
                API rejects or fails the request.
        """
        url = _scope_url(scope)
        payload = await self._get(
            f"{url}/{contact_id}", params={"include": "emails,phones"}
        )
        try:
            contact = payload["contacts"]
        except (KeyError, TypeError) as e:
            raise ZohoAPIError(f"Malformed contact response from Zoho: {e}") from e
        return normalize_contact(contact, scope)

    async def count_contacts(self) -> dict:
        """Return the user's contact counts directly, via Zoho's own
        dedicated count endpoint -- no pagination, no guessing from a
        result list that might have been capped by ``limit``.

        Returns:
            ``{"personal": {"contacts": int, "archived": int, "inactive":
            int}, "organization": {...same shape...}, "total": int}``.
            Archived/inactive are surfaced as their own properties rather
            than silently dropped, so the caller is aware of the
            distinction even though ``total`` only sums the active
            ``contacts`` count from each scope.

        Raises:
            ZohoAPIError: if either scope's response is malformed or the
                request fails.
        """
        self_payload = await self._get(
            ZOHO_CONTACTS_SELF_URL, params={"fields": "count"}
        )
        org_payload = await self._get(ZOHO_CONTACTS_ORG_URL, params={"fields": "count"})
        try:
            personal = {
                "contacts": self_payload["contacts"]["contacts"],
                "archived": self_payload["contacts"]["archived"],
                "inactive": self_payload["contacts"]["inactive"],
            }
            organization = {
                "contacts": org_payload["contacts"]["contacts"],
                "archived": org_payload["contacts"]["archived"],
                "inactive": org_payload["contacts"]["inactive"],
            }
        except MALFORMED_DATA_ERRORS as e:
            raise ZohoAPIError(
                f"Malformed contacts count response from Zoho: {e}"
            ) from e
        return {
            "personal": personal,
            "organization": organization,
            "total": personal["contacts"] + organization["contacts"],
        }
