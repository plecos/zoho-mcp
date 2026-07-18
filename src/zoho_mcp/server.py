"""FastMCP app instantiation and tool registration.

No business logic lives here -- ``create_server`` wires the already-tested
tool wrappers (``tools/mail.py``, ``tools/calendar.py``) to a FastMCP
instance, and ``main`` builds the real Zoho client from environment/keyring
config and runs the server over stdio.
"""

import os

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from zoho_mcp.config import load_env
from zoho_mcp.tools import calendar as calendar_tools
from zoho_mcp.tools import mail as mail_tools
from zoho_mcp.zoho.auth import ZohoTokenManager, load_refresh_token
from zoho_mcp.zoho.client import ZohoClient

_READ_ONLY = ToolAnnotations(readOnlyHint=True)


def create_server(client: ZohoClient) -> FastMCP:
    """Build the FastMCP app and register all tools against the given client."""
    mcp = FastMCP("zoho-mcp")

    @mcp.tool(annotations=_READ_ONLY)
    async def search_emails(
        query: str = "", limit: int = 20, days_back: int | None = None
    ) -> list[dict]:
        """Search the user's Zoho Mail mailbox for emails matching a query
        and/or a recency window.

        query (optional): must use Zoho Mail search syntax -- bare words
        are rejected. Use qualifiers like subject:, sender:, entire:
        (anywhere in the email), joined with :: for AND or :or: for OR,
        e.g. "subject:roadmap::sender:jamie". May be left empty if
        days_back is given.

        days_back (optional): only return emails from the last N days --
        0 for today only, 1 for today and yesterday, etc. This is resolved
        using the mailbox's real timezone; do not try to compute or pass an
        explicit date yourself, since you don't know the mailbox's
        timezone and getting it wrong silently returns the wrong day.
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
    async def list_events(start: str, end: str) -> list[dict]:
        """List Zoho Calendar events between two ISO 8601 UTC timestamps (max 31 days)."""
        return await calendar_tools.list_events(client, start=start, end=end)

    return mcp


def _build_zoho_client_from_env() -> ZohoClient:
    """Construct a ZohoClient from environment variables and the stored refresh token.

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
    return ZohoClient(
        token_manager=token_manager,
        http_client=http_client,
        account_id=os.environ["ZOHO_ACCOUNT_ID"],
        calendar_uid=os.environ["ZOHO_CALENDAR_UID"],
        strip_invisible_chars=os.environ.get("ZOHO_STRIP_INVISIBLE_CHARS", "false")
        .strip()
        .lower()
        == "true",
    )


def main() -> None:
    client = _build_zoho_client_from_env()
    server = create_server(client)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
