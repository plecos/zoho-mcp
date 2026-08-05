"""The hosted transport: a streamable-HTTP app behind a bearer-token gate.

Exists so the server can be reached from a client that cannot spawn a local
process -- a phone, principally. Not under ``zoho/``: nothing here knows what
Zoho is. It is transport and access control, and it would look the same
wrapping any FastMCP server.

**Why the gate is here and not in a tool.** The stdio server's protection was
structural: no listening socket, so the only way to reach a tool was to be the
process that spawned it. A hosted server gives that up, and nothing else in
the codebase replaces it -- every tool wrapper, and ``ZohoClient`` under them,
assumes the caller is already entitled to this mailbox. ASGI middleware is the
one line every HTTP request executes before any of that, which makes it the
same choice as ``get_access_token`` for the unauthenticated-server error: put
the check where the traffic passes, and no tool can route around it.

The refused path issues **no call to the wrapped app at all** -- the strict
invariant, matching ``check_for_updates`` rather than ``send_email``. An
unauthorized request cannot reach a tool, cannot reach Zoho, and cannot
observe whether this server is even authenticated.

What this is not: it is not the OAuth flow an MCP client performs against a
remote server. A static shared secret is the smallest thing that makes the
endpoint safe to expose at all, and it suits a single-user server whose
operator and user are the same person. Clients that require OAuth need
FastMCP's ``token_verifier``/``AuthSettings`` seam instead, which this
deliberately leaves alone.
"""

import hmac
from collections.abc import Awaitable, Callable
from typing import Any

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

AUTH_TOKEN_VAR = "ZOHO_HTTP_AUTH_TOKEN"


class BearerTokenGate:
    """ASGI middleware that refuses any request without the shared secret.

    Pure ASGI rather than Starlette middleware so it can be reasoned about and
    tested without a framework in the way -- and so it keeps working if the
    app underneath is ever built by something other than FastMCP.
    """

    def __init__(self, app: Any, *, auth_token: str) -> None:
        self._app = app
        self._auth_token = auth_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Lifespan (and anything else that isn't a request) passes straight
        # through: it carries no headers, and refusing it would leave the
        # session manager unstarted, breaking every authorized request too.
        if scope["type"] != "http" or self._is_authorized(scope):
            await self._app(scope, receive, send)
            return
        await self._refuse(send)

    def _is_authorized(self, scope: Scope) -> bool:
        for name, value in scope.get("headers", []):
            if name.lower() != b"authorization":
                continue
            scheme, _, token = value.decode("latin-1").partition(" ")
            # compare_digest rather than `==`: the comparison is against a
            # secret an attacker can submit guesses for, and it costs nothing
            # to not leak how far a guess got.
            return scheme.lower() == "bearer" and hmac.compare_digest(
                token, self._auth_token
            )
        return False

    async def _refuse(self, send: Send) -> None:
        body = b"Unauthorized"
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    # Names the scheme without hinting at the secret. The body
                    # stays constant for the same reason.
                    (b"www-authenticate", b'Bearer realm="zoho-mcp"'),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def require_auth_token(auth_token: str) -> str:
    """Return the shared secret, refusing a blank one.

    Separate from ``build_http_app`` so the entry point can fail on a missing
    secret before it builds anything -- there is nothing to be gained by
    opening an http client and registering 42 tools first.

    Raises:
        ValueError: if ``auth_token`` is blank. There is no safe default --
            a built-in token would be a published credential, and generating
            one silently would produce a server whose secret is whatever
            scrolled past in the log.
    """
    if not auth_token.strip():
        raise ValueError(
            f"{AUTH_TOKEN_VAR} is not set. The HTTP transport puts this "
            f"server on a socket, so it will not start without a shared "
            f"secret for callers to present. Generate one with "
            f"`python -c 'import secrets; print(secrets.token_urlsafe(32))'` "
            f"and set {AUTH_TOKEN_VAR} to it."
        )
    return auth_token.strip()


def build_http_app(mcp: Any, *, auth_token: str) -> BearerTokenGate:
    """Wrap a FastMCP server's streamable-HTTP app in the bearer gate.

    Args:
        mcp: the FastMCP instance to serve.
        auth_token: the shared secret every request must present.

    Returns:
        An ASGI app: the gate, with the MCP app behind it.

    Raises:
        ValueError: if ``auth_token`` is blank.
    """
    token = require_auth_token(auth_token)
    return BearerTokenGate(mcp.streamable_http_app(), auth_token=token)
