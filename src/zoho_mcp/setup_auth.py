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

from zoho_mcp.zoho.auth import (
    build_authorization_url,
    exchange_code_for_tokens,
    extract_authorization_code,
    store_refresh_token,
)

SCOPES = ["ZohoMail.messages.READ", "ZohoCalendar.event.READ"]
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
        self.wfile.write(b"<html><body>Authorized. You can close this tab.</body></html>")

    def log_message(self, format: str, *args: object) -> None:
        pass  # suppress http.server's default request logging to stderr


def _wait_for_callback(port: int) -> dict[str, list[str]]:
    """Block until exactly one request hits the local callback, then return its query."""
    server = http.server.HTTPServer(("localhost", port), _CallbackHandler)
    server.handle_request()
    return server.callback_query  # type: ignore[attr-defined]


async def _exchange_and_store(
    *, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> None:
    async with httpx.AsyncClient() as http_client:
        tokens = await exchange_code_for_tokens(
            http_client,
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
    store_refresh_token(tokens["refresh_token"])


def main() -> None:
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

    asyncio.run(
        _exchange_and_store(
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
    )
    print("Zoho refresh token stored. You can now run `uv run zoho-mcp`.")


if __name__ == "__main__":
    main()
