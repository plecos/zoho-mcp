"""One-time interactive CLI: obtain and store a Zoho refresh token.

This is thin wiring only, composing already-tested pieces from
``zoho/auth.py`` (URL building, code exchange, callback parsing). The
socket-level callback listener itself isn't unit tested, the same way
``server.py``'s ``mcp.run()`` isn't -- there's no meaningful behavior left
to assert once the tested pieces are wired together, only real network I/O.
"""

import asyncio
import http.server
import os
import urllib.parse
import webbrowser

import httpx

from zoho_mcp.config import load_env
from zoho_mcp.zoho.auth import (
    ZohoTokenManager,
    build_authorization_url,
    exchange_code_for_tokens,
    extract_authorization_code,
    store_refresh_token,
)
from zoho_mcp.zoho.client import get_default_calendar_uid, get_primary_account_id

SCOPES = [
    "ZohoMail.messages.READ",
    "ZohoMail.messages.ALL",  # write access: mark_as_read/unread, move_email, add/remove_label
    "ZohoMail.accounts.READ",  # needed once, to look up the mail account id
    "ZohoMail.folders.READ",  # used at runtime to filter out Sent/Drafts by folder type
    "ZohoCalendar.event.READ",
    "ZohoCalendar.event.ALL",  # write access: create_event/update_event/delete_event
    "ZohoCalendar.calendar.READ",  # needed once, to look up the calendar's uid
    "zohocontacts.contactapi.READ",
    "ZohoMail.tasks.READ",
    "ZohoMail.tasks.CREATE",  # write access: create_task
    "ZohoMail.notes.READ",
    "ZohoMail.notes.CREATE",  # write access: create_note
    "ZohoMail.links.READ",
    "ZohoMail.links.CREATE",  # write access: create_bookmark
    "ZohoCalendar.resources.READ",
    "ZohoCalendar.branches.READ",
    "ZohoMail.tags.READ",
    "ZohoCalendar.freebusy.READ",
]
DEFAULT_CALLBACK_PORT = 8765


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the OAuth redirect's query string, then serves a plain notice."""

    def do_GET(self) -> None:  # noqa: N802 (http.server's required method name)
        self.server.callback_query = urllib.parse.parse_qs(  # type: ignore[attr-defined]
            urllib.parse.urlparse(self.path).query
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<html><body>Authorized. You can close this tab.</body></html>"
        )

    def log_message(self, format: str, *args: object) -> None:
        pass  # suppress http.server's default request logging to stderr


def _wait_for_callback(port: int) -> dict[str, list[str]]:
    """Block until exactly one request hits the local callback, then return its query."""
    server = http.server.HTTPServer(("localhost", port), _CallbackHandler)
    server.handle_request()
    return server.callback_query  # type: ignore[attr-defined]


async def _exchange_store_and_lookup(
    *, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> tuple[str, str]:
    """Exchange the code, store the refresh token, then look up account/calendar ids.

    Returns:
        ``(account_id, calendar_uid)`` for the user to add to ``.env``.
    """
    async with httpx.AsyncClient() as http_client:
        tokens = await exchange_code_for_tokens(
            http_client,
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
        store_refresh_token(tokens["refresh_token"])

        token_manager = ZohoTokenManager(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=tokens["refresh_token"],
            http_client=http_client,
        )
        account_id = await get_primary_account_id(token_manager, http_client)
        calendar_uid = await get_default_calendar_uid(token_manager, http_client)
    return account_id, calendar_uid


def main() -> None:
    load_env()
    client_id = os.environ["ZOHO_CLIENT_ID"]
    client_secret = os.environ["ZOHO_CLIENT_SECRET"]
    port = int(os.environ.get("ZOHO_OAUTH_CALLBACK_PORT", DEFAULT_CALLBACK_PORT))
    redirect_uri = f"http://localhost:{port}/callback"

    auth_url = build_authorization_url(
        client_id=client_id, redirect_uri=redirect_uri, scopes=SCOPES
    )
    print(f"Opening your browser to authorize zoho-mcp:\n\n{auth_url}\n")
    webbrowser.open(auth_url)

    print(f"Waiting for the OAuth redirect on {redirect_uri} ...")
    query = _wait_for_callback(port)
    code = extract_authorization_code(query)

    account_id, calendar_uid = asyncio.run(
        _exchange_store_and_lookup(
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
    )
    print("Zoho refresh token stored.\n")
    print("Add these to your .env:")
    print(f"ZOHO_ACCOUNT_ID={account_id}")
    print(f"ZOHO_CALENDAR_UID={calendar_uid}\n")
    print("Then run `uv run zoho-mcp`.")


if __name__ == "__main__":
    main()
