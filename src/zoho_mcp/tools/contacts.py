"""MCP tool wrappers for Zoho Contacts.

Shapes LLM-facing input/output only. No HTTP calls, no token logic -- the
Zoho Contacts client is injected by the caller (``server.py``), never
constructed here.
"""

from zoho_mcp.tools.envelope import counted
from zoho_mcp.zoho.contacts_client import ZohoContactsClient


async def search_contacts(
    client: ZohoContactsClient,
    query: str = "",
    limit: int = 20,
    status: str = "active",
) -> dict:
    """Search the user's Zoho Contacts.

    Args:
        client: injected Zoho Contacts client.
        query: free-text search -- Zoho's backend matches across name,
            email, and phone number. May be empty to list contacts
            without filtering.
        limit: maximum number of results to return.
        status: which folder to search -- "active" (default), "archived",
            or "inactive". Archived/inactive contacts are excluded unless
            explicitly requested via this argument.

    Returns:
        ``{"contacts": [...], "count": int, "has_more": bool}``. Each
        contact has id, scope ("personal" or "organization"), first_name,
        last_name, nickname, company, emails, phones, notes, birthday.
        Searches both the Personal and Organization contact pools and
        merges the results -- pass the result's ``scope`` back into
        ``get_contact``, since the same id can mean a different record in
        each pool. ``count`` is how many came back here; ``has_more`` is
        True if more results exist beyond ``limit`` -- don't infer that
        from ``count``, raise the limit or narrow the query instead. Use
        ``count_contacts`` for a reliable total rather than paginating
        and summing.

    Raises:
        ZohoAPIError: if limit is less than 1, status is not recognized,
            or the Contacts API rejects or fails the request.
    """
    contacts, has_more = await client.search_contacts(
        query=query, limit=limit, status=status
    )
    return counted("contacts", contacts, has_more=has_more)


async def get_contact(client: ZohoContactsClient, contact_id: str, scope: str) -> dict:
    """Fetch one contact's full details by id.

    Args:
        client: injected Zoho Contacts client.
        contact_id: a contact's ``id`` from a prior ``search_contacts`` result.
        scope: that same result's ``scope`` field ("personal" or
            "organization") -- required because the same contact_id can
            refer to a different, partial record in the other scope.

    Returns:
        A contact summary: id, scope, first_name, last_name, nickname,
        company, emails, phones, notes, birthday.

    Raises:
        ZohoAPIError: if scope is not recognized, or the Contacts API
            rejects or fails the request.
    """
    return await client.get_contact(contact_id, scope=scope)


async def count_contacts(client: ZohoContactsClient) -> dict:
    """Return the user's total contact count directly, via Zoho's own
    dedicated count endpoint -- no pagination, no risk of an LLM
    miscounting by summing across multiple searches.

    Returns:
        ``{"personal": {"contacts": int, "archived": int, "inactive":
        int}, "organization": {...same shape...}, "total": int}``.
        Archived/inactive are surfaced as their own properties, not
        silently dropped -- ``total`` only sums the active ``contacts``
        count from each scope.

    Raises:
        ZohoAPIError: if the Contacts API rejects or fails the request.
    """
    return await client.count_contacts()
