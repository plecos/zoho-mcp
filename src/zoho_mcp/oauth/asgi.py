"""The HTTP binding for the self-contained authorization server.

This is the one module in the ``oauth`` package that knows about a web
framework. ``server.py`` stayed framework-neutral on purpose -- requests reach
it already built and responses leave as :class:`OAuthResponse` -- so all the
Starlette translation lives here: the discovery metadata, the ``/authorize``,
``/token`` and ``/register`` routes, the operator consent page, and the gate
that verifies an access token on the way to the tools.

The gate guards only the MCP path. The OAuth and ``.well-known`` routes must be
reachable without a token, or a client could never obtain one; ``/mcp`` is the
only thing that needs a valid access token in front of it.
"""

import hmac
from html import escape
from typing import Any

from authlib.oauth2 import OAuth2Error
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from zoho_mcp.oauth.server import (
    DEFAULT_SUBJECT,
    SUPPORTED_SCOPES,
    OAuthResponse,
    ZohoAuthorizationServer,
    build_json_request,
    build_oauth2_request,
)
from zoho_mcp.oauth.tokens import ACCESS_TOKEN_USE, TokenError, TokenSigner

Scope = dict[str, Any]

MCP_PATH = "/mcp"
AUTHORIZE_PATH = "/authorize"
TOKEN_PATH = "/token"
REGISTER_PATH = "/register"
PROTECTED_RESOURCE_PATH = "/.well-known/oauth-protected-resource"
AUTHORIZATION_SERVER_PATH = "/.well-known/oauth-authorization-server"

_TOKEN_ENDPOINT_AUTH_METHODS = ["none", "client_secret_post", "client_secret_basic"]


# --- discovery metadata -----------------------------------------------------


def protected_resource_metadata(issuer: str, resource: str) -> dict:
    """RFC 9728 metadata: which authorization server protects this resource.

    Claude fetches this after a 401 to learn where to authorize.
    """
    return {"resource": resource, "authorization_servers": [issuer]}


def authorization_server_metadata(issuer: str) -> dict:
    """RFC 8414 metadata: where the authorization server's endpoints live.

    Advertises S256-only PKCE and the two grants this server implements, so a
    client discovers the exact shape it must use rather than guessing.
    """
    return {
        "issuer": issuer,
        "authorization_endpoint": issuer + AUTHORIZE_PATH,
        "token_endpoint": issuer + TOKEN_PATH,
        "registration_endpoint": issuer + REGISTER_PATH,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": _TOKEN_ENDPOINT_AUTH_METHODS,
        "scopes_supported": list(SUPPORTED_SCOPES),
    }


# --- the access-token gate --------------------------------------------------


class JwtAuthGate:
    """ASGI middleware that requires a valid access token on the MCP path.

    Only the MCP path is guarded. Everything else -- the OAuth endpoints and
    the discovery documents -- must stay open, since a client has to reach them
    to obtain the very token this gate demands. A refused request reaches the
    wrapped app not at all, and the 401 points at the resource metadata so the
    client knows where to authorize (RFC 9728).
    """

    def __init__(
        self,
        app: Any,
        *,
        signer: TokenSigner,
        resource_metadata_url: str,
        protected_path: str = MCP_PATH,
    ) -> None:
        self._app = app
        self._signer = signer
        self._resource_metadata_url = resource_metadata_url
        self._protected_path = protected_path

    async def __call__(self, scope: Scope, receive: Any, send: Any) -> None:
        if (
            scope["type"] != "http"
            or not self._is_protected(scope["path"])
            or self._is_authorized(scope)
        ):
            await self._app(scope, receive, send)
            return
        await self._refuse(send)

    def _is_protected(self, path: str) -> bool:
        return path == self._protected_path or path.startswith(
            self._protected_path + "/"
        )

    def _is_authorized(self, scope: Scope) -> bool:
        token = self._bearer_token(scope)
        if token is None:
            return False
        try:
            self._signer.verify(token, expect_use=ACCESS_TOKEN_USE)
        except TokenError:
            return False
        return True

    def _bearer_token(self, scope: Scope) -> str | None:
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                scheme, _, token = value.decode("latin-1").partition(" ")
                if scheme.lower() == "bearer" and token:
                    return token
        return None

    async def _refuse(self, send: Any) -> None:
        body = b"Unauthorized"
        challenge = f'Bearer resource_metadata="{self._resource_metadata_url}"'
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"text/plain; charset=utf-8"),
                    (b"content-length", str(len(body)).encode()),
                    (b"www-authenticate", challenge.encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


# --- rendering Authlib's neutral response into Starlette --------------------


def _render(response: OAuthResponse) -> Response:
    headers = dict(response.headers)
    if isinstance(response.body, dict):
        # JSONResponse sets its own content-type; drop Authlib's to avoid a
        # duplicate, keeping cache-control and the like.
        headers.pop("Content-Type", None)
        headers.pop("content-type", None)
        return JSONResponse(response.body, status_code=response.status, headers=headers)
    return Response(
        content=response.body or "", status_code=response.status, headers=headers
    )


def _consent_page(params: dict, *, error: str | None = None) -> str:
    """The operator's approval form.

    The original authorization parameters ride along as hidden fields, so the
    server stays stateless between showing the form and the operator submitting
    it -- the POST carries everything the redirect needs.
    """
    hidden = "".join(
        f'<input type="hidden" name="{escape(k)}" value="{escape(str(v))}">'
        for k, v in params.items()
    )
    banner = f'<p class="error">{escape(error)}</p>' if error else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Authorize access</title></head><body>"
        "<h1>Authorize access to your Zoho account</h1>"
        "<p>An application is requesting access. Enter your passphrase to "
        "approve, or close this tab to deny.</p>"
        f"{banner}"
        f'<form method="post" action="{AUTHORIZE_PATH}">{hidden}'
        '<label>Passphrase <input type="password" name="operator_password" '
        "autofocus></label> "
        '<button type="submit">Approve</button></form>'
        "</body></html>"
    )


def build_oauth_routes(
    *,
    server: ZohoAuthorizationServer,
    operator_password: str,
    issuer: str,
    resource: str,
) -> list[Route]:
    """The Starlette routes for discovery, registration, authorize and token.

    The signer isn't needed here -- the routes reach it through ``server``;
    only the access-token gate verifies tokens directly.
    """

    async def protected_resource(request: Request) -> Response:
        return JSONResponse(protected_resource_metadata(issuer, resource))

    async def as_metadata(request: Request) -> Response:
        return JSONResponse(authorization_server_metadata(issuer))

    async def register(request: Request) -> Response:
        body = await request.json()
        oauth_request = build_json_request(
            "POST", issuer + REGISTER_PATH, body, dict(request.headers)
        )
        return _render(
            server.create_endpoint_response("client_registration", oauth_request)
        )

    async def token(request: Request) -> Response:
        form = dict(await request.form())
        oauth_request = build_oauth2_request(
            "POST", issuer + TOKEN_PATH, form, dict(request.headers)
        )
        return _render(server.create_token_response(oauth_request))

    async def authorize(request: Request) -> Response:
        if request.method == "GET":
            params = dict(request.query_params)
            oauth_request = build_oauth2_request(
                "GET", issuer + AUTHORIZE_PATH, params, dict(request.headers)
            )
            try:
                server.get_consent_grant(oauth_request, end_user=DEFAULT_SUBJECT)
            except OAuth2Error as error:
                return PlainTextResponse(
                    f"{error.error}: {error.description or ''}",
                    status_code=error.status_code or 400,
                )
            return HTMLResponse(_consent_page(params))

        form = dict(await request.form())
        password = str(form.pop("operator_password", ""))
        if not _password_ok(password, operator_password):
            return HTMLResponse(
                _consent_page(form, error="Incorrect passphrase."), status_code=403
            )
        oauth_request = build_oauth2_request(
            "GET", issuer + AUTHORIZE_PATH, form, dict(request.headers)
        )
        try:
            server.get_consent_grant(oauth_request, end_user=DEFAULT_SUBJECT)
        except OAuth2Error as error:
            return PlainTextResponse(
                f"{error.error}: {error.description or ''}",
                status_code=error.status_code or 400,
            )
        return _render(
            server.create_authorization_response(
                oauth_request, grant_user=DEFAULT_SUBJECT
            )
        )

    return [
        Route(PROTECTED_RESOURCE_PATH, protected_resource, methods=["GET"]),
        Route(AUTHORIZATION_SERVER_PATH, as_metadata, methods=["GET"]),
        Route(REGISTER_PATH, register, methods=["POST"]),
        Route(TOKEN_PATH, token, methods=["POST"]),
        Route(AUTHORIZE_PATH, authorize, methods=["GET", "POST"]),
    ]


def _password_ok(supplied: str, expected: str) -> bool:
    # Constant-time so a wrong passphrase leaks nothing about how close it was.
    return bool(expected) and hmac.compare_digest(supplied, expected)


def build_oauth_app(
    mcp: Any,
    *,
    server: ZohoAuthorizationServer,
    signer: TokenSigner,
    issuer: str,
    operator_password: str,
) -> JwtAuthGate:
    """Assemble the OAuth-mode ASGI app around a FastMCP server.

    The OAuth routes are added to the MCP app's own Starlette router, so the
    MCP app's lifespan (which starts the session manager) is preserved exactly
    as the bearer mode has it. The whole thing is then wrapped in the access-
    token gate, which enforces only on the MCP path.
    """
    resource = issuer + MCP_PATH
    app = mcp.streamable_http_app()
    app.router.routes.extend(
        build_oauth_routes(
            server=server,
            operator_password=operator_password,
            issuer=issuer,
            resource=resource,
        )
    )
    return JwtAuthGate(
        app,
        signer=signer,
        resource_metadata_url=issuer + PROTECTED_RESOURCE_PATH,
    )
