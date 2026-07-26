"""One-time interactive CLI: obtain and store a Zoho refresh token.

This is thin wiring only, composing already-tested pieces from
``zoho/auth.py`` (URL building, code exchange, callback parsing). The
socket-level callback listener itself isn't unit tested, the same way
``server.py``'s ``mcp.run()`` isn't -- there's no meaningful behavior left
to assert once the tested pieces are wired together, only real network I/O.
"""

import asyncio
import json
import os
import sys
import webbrowser
from pathlib import Path

import httpx

from zoho_mcp.config import load_env
from zoho_mcp.zoho.auth import (
    DEFAULT_CALLBACK_PORT,
    SCOPES,
    ZohoTokenManager,
    build_authorization_url,
    exchange_code_for_tokens,
    extract_authorization_code,
    store_refresh_token,
    wait_for_callback,
)
from zoho_mcp.zoho.client import get_default_calendar_uid, get_primary_account_id


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


def zoho_mcp_executable() -> Path:
    """Absolute path to this install's ``zoho-mcp`` entry point.

    Derived from the running interpreter rather than ``PATH``: an MCP
    client launches the server with its own environment, so a bare
    ``zoho-mcp`` that happens to resolve in the developer's shell won't
    resolve there.
    """
    name = "zoho-mcp.exe" if os.name == "nt" else "zoho-mcp"
    return Path(sys.executable).parent / name


def build_client_config_snippet(executable: Path) -> str:
    """Render the MCP-client config entry for this install, ready to paste.

    ``json.dumps`` rather than an f-string because Windows paths are full
    of backslashes -- hand-formatting one produces a snippet the client
    can't parse.
    """
    return json.dumps(
        {"mcpServers": {"zoho-mcp": {"command": str(executable), "args": []}}},
        indent=2,
    )


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
    query = wait_for_callback(port)
    code = extract_authorization_code(query)

    account_id, calendar_uid = asyncio.run(
        _exchange_store_and_lookup(
            client_id=client_id,
            client_secret=client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
    )
    print("Zoho refresh token stored. Setup is complete.\n")
    print("Point your MCP client at this server by adding:\n")
    print(build_client_config_snippet(zoho_mcp_executable()))
    print("\nOptional -- the server looks both of these up on startup if")
    print("they're absent, so adding them to .env only saves two API calls:")
    print(f"ZOHO_ACCOUNT_ID={account_id}")
    print(f"ZOHO_CALENDAR_UID={calendar_uid}")


if __name__ == "__main__":
    main()
