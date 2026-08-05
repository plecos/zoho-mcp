"""The `authenticate` tool: grant this server access to a Zoho account.

Exists because an MCPB bundle has a single entry point, so ``zoho-mcp-setup``
isn't reachable from an installed extension. The server starts
unauthenticated instead, every other tool fails with a message naming this
one, and this one runs the same OAuth consent flow the CLI does.

No HTTP or token logic of its own -- the flow lives in ``zoho/auth.py``, and
this composes it.
"""

import webbrowser
from collections.abc import Callable

import httpx

from zoho_mcp.zoho.auth import (
    DEFAULT_CALLBACK_PORT,
    SCOPES,
    ZohoAuthError,
    ZohoTokenManager,
    build_authorization_url,
    exchange_code_for_tokens,
    extract_authorization_code,
    wait_for_callback,
)
from zoho_mcp.zoho.token_store import KeyringTokenStore, TokenStore, TokenStoreError


def _open_browser_and_wait(authorization_url: str, port: int) -> str:
    """Open the consent page and block until Zoho redirects back with a code.

    Injectable via ``authenticate``'s ``obtain_authorization_code`` so the
    surrounding logic is testable without real I/O.
    """
    webbrowser.open(authorization_url)
    return extract_authorization_code(wait_for_callback(port))


async def authenticate(
    token_manager: ZohoTokenManager,
    http_client: httpx.AsyncClient,
    *,
    client_id: str,
    client_secret: str,
    callback_port: int = DEFAULT_CALLBACK_PORT,
    obtain_authorization_code: Callable[[str, int], str] = _open_browser_and_wait,
    token_store: TokenStore | None = None,
) -> dict:
    """Run the Zoho OAuth consent flow and adopt the resulting refresh token.

    Ordering is deliberate: nothing is stored or adopted until Zoho has
    actually returned a refresh token. A half-completed run that stored a
    token without adopting it would work until the next restart, and one that
    adopted without storing would work only until then -- both fail later and
    far from the cause.

    That ordering is also why a read-only store is rejected up front rather
    than at the write. Everything between here and the write is a human
    reading a consent screen and clicking Approve; failing afterwards spends
    that consent for nothing. On a hosted run it is worse than useless -- the
    browser would open on the server, where nobody is sitting.

    Args:
        token_manager: the live manager to adopt the new token.
        http_client: shared client, used for the code exchange.
        client_id: the Zoho API Console application's client id.
        client_secret: its client secret.
        callback_port: local port the redirect lands on. Must match the
            redirect URI registered in the console.
        obtain_authorization_code: the browser round trip, injectable for
            tests.
        token_store: where to persist the resulting token. Defaults to the OS
            credential store.

    Returns:
        ``{"authenticated": True, "was_already_authenticated": bool,
        "scopes": [...]}``.

    Raises:
        ZohoAuthError: if credentials are missing, the store cannot be written
            to, the browser flow fails, or Zoho rejects the exchange or
            returns no refresh token.
    """
    store = token_store or KeyringTokenStore()
    if not store.is_writable:
        # Reported through this module's own error type: to the caller this is
        # "authenticating cannot work here", not a storage-layer detail.
        raise ZohoAuthError(
            "This server cannot save a Zoho token, so authorizing it from a "
            "conversation would achieve nothing. It reads its refresh token "
            "from ZOHO_REFRESH_TOKEN. Run `zoho-mcp-setup` on a machine with a "
            "browser, then set ZOHO_REFRESH_TOKEN here and restart."
        )
    if not client_id.strip():
        raise ZohoAuthError(
            "ZOHO_CLIENT_ID is not set. Register a Server-based Application in "
            "the Zoho API Console and supply its client id before authenticating."
        )
    if not client_secret.strip():
        raise ZohoAuthError(
            "ZOHO_CLIENT_SECRET is not set. It comes from the same Zoho API "
            "Console application as the client id."
        )

    was_already_authenticated = token_manager.is_authenticated
    redirect_uri = f"http://localhost:{callback_port}/callback"
    authorization_url = build_authorization_url(
        client_id=client_id, redirect_uri=redirect_uri, scopes=SCOPES
    )

    try:
        code = obtain_authorization_code(authorization_url, callback_port)
    except ZohoAuthError:
        raise
    except OSError as e:
        # Almost always the callback port already being in use, which is
        # worth saying plainly rather than surfacing a bare errno.
        raise ZohoAuthError(
            f"Could not complete the browser authorization on port {callback_port}: {e}"
        ) from e

    tokens = await exchange_code_for_tokens(
        http_client,
        client_id=client_id,
        client_secret=client_secret,
        code=code,
        redirect_uri=redirect_uri,
    )
    refresh_token = tokens["refresh_token"]
    try:
        store.store(refresh_token)
    except TokenStoreError as e:
        raise ZohoAuthError(str(e)) from e
    token_manager.set_refresh_token(refresh_token)

    return {
        "authenticated": True,
        "was_already_authenticated": was_already_authenticated,
        "scopes": SCOPES,
    }
