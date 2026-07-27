"""MCP tool wrappers for Zoho Calendar's Resource Booking feature.

Shapes LLM-facing input/output only. No HTTP calls, no token logic -- the
Zoho client is injected by the caller (``server.py``), never constructed
here.
"""

from zoho_mcp.tools.envelope import counted
from zoho_mcp.zoho.client import ZohoClient


async def list_branches(client: ZohoClient) -> dict:
    """List the office branches configured for Resource Booking.

    Args:
        client: injected Zoho client.

    Returns:
        ``{"branches": [...], "count": int}``. Each branch carries nested
        buildings and floors (id, name, and for floors, has_resource).
        ``count`` counts branches, not the nested rows. ``count: 0`` is
        normal -- Resource Booking is an office-facility feature most
        accounts never set up.

    Raises:
        ZohoAPIError: if the Calendar API rejects or fails the request.
    """
    return counted("branches", await client.list_branches())


async def list_resources(
    client: ZohoClient, branch_id: str, building_id: str, floor_id: str
) -> dict:
    """List the bookable resources (rooms, equipment) on one floor.

    Args:
        client: injected Zoho client.
        branch_id: a branch's ``id`` from a prior ``list_branches`` result.
        building_id: a building's ``id`` from that branch.
        floor_id: a floor's ``id`` from that building.

    Returns:
        ``{"resources": [...], "count": int}``. Each resource has id,
        name, category, email (invite this address to a calendar event to
        book it), capacity, location, branch_id, building_id, floor_id.

    Raises:
        ZohoAPIError: if the Calendar API rejects or fails the request.
    """
    return counted(
        "resources",
        await client.list_resources(
            branch_id=branch_id, building_id=building_id, floor_id=floor_id
        ),
    )
