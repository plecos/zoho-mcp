"""Tests for the HTTP binding of the authorization server.

Drives the whole flow the way a client would -- register, authorize, exchange,
refresh -- over ASGI, plus the access-token gate that guards the MCP path and
nothing else. The FastMCP app isn't involved: a stub inner app stands in for
``/mcp``, so these exercise the routing, the consent step and the gate without
needing a real Zoho server.
"""

from urllib.parse import parse_qs, urlparse

import httpx
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from zoho_mcp.oauth.asgi import (
    JwtAuthGate,
    authorization_server_metadata,
    build_oauth_app,
    build_oauth_routes,
    protected_resource_metadata,
)
from zoho_mcp.oauth.server import SUPPORTED_SCOPES, create_authorization_server
from zoho_mcp.oauth.store import (
    AuthorizationCodeStore,
    ClientStore,
    RefreshTokenStore,
)
from zoho_mcp.oauth.tokens import ACCESS_TOKEN_USE, TokenSigner

ISSUER = "https://mail.example.com"
RESOURCE = ISSUER + "/mcp"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
PASSWORD = "correct-horse-battery-staple"
RMU = ISSUER + "/.well-known/oauth-protected-resource"


def a_signer() -> TokenSigner:
    return TokenSigner(signing_key="k" * 43, issuer=ISSUER, audience=RESOURCE)


def build_server(tmp_path):
    signer = a_signer()
    server = create_authorization_server(
        signer=signer,
        client_store=ClientStore(tmp_path / "clients.json"),
        code_store=AuthorizationCodeStore(),
        refresh_store=RefreshTokenStore(tmp_path / "refresh.json"),
    )
    return server, signer


def routes_app(tmp_path):
    server, signer = build_server(tmp_path)
    app = Starlette(
        routes=build_oauth_routes(
            server=server,
            operator_password=PASSWORD,
            issuer=ISSUER,
            resource=RESOURCE,
        )
    )
    return app, signer


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ISSUER)


# --- discovery metadata -----------------------------------------------------


def test_protected_resource_metadata_names_this_server_as_the_authorizer():
    doc = protected_resource_metadata(ISSUER, RESOURCE)

    assert doc["resource"] == RESOURCE
    assert doc["authorization_servers"] == [ISSUER]


def test_authorization_server_metadata_advertises_the_endpoints_and_pkce():
    doc = authorization_server_metadata(ISSUER)

    assert doc["issuer"] == ISSUER
    assert doc["authorization_endpoint"] == ISSUER + "/authorize"
    assert doc["token_endpoint"] == ISSUER + "/token"
    assert doc["registration_endpoint"] == ISSUER + "/register"
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert "refresh_token" in doc["grant_types_supported"]


async def test_the_metadata_documents_are_served(tmp_path):
    app, _ = routes_app(tmp_path)

    async with client(app) as c:
        prm = await c.get("/.well-known/oauth-protected-resource")
        asm = await c.get("/.well-known/oauth-authorization-server")

    assert prm.json()["authorization_servers"] == [ISSUER]
    assert asm.json()["token_endpoint"] == ISSUER + "/token"


# --- the full flow over HTTP ------------------------------------------------


async def register(c, auth_method="none"):
    response = await c.post(
        "/register",
        json={
            "redirect_uris": [REDIRECT],
            "token_endpoint_auth_method": auth_method,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    return response


def authorize_params(client_id, verifier):
    return {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "scope": SUPPORTED_SCOPES[0],
        "state": "state-xyz",
        "code_challenge": create_s256_code_challenge(verifier),
        "code_challenge_method": "S256",
    }


async def test_register_authorize_exchange_yields_a_working_token(tmp_path):
    app, signer = routes_app(tmp_path)
    verifier = "verifier-" + "a" * 40

    async with client(app) as c:
        client_id = (await register(c)).json()["client_id"]
        params = authorize_params(client_id, verifier)

        page = await c.get("/authorize", params=params)
        assert page.status_code == 200
        assert "operator_password" in page.text

        approved = await c.post(
            "/authorize", data={**params, "operator_password": PASSWORD}
        )
        assert approved.status_code == 302
        code = parse_qs(urlparse(approved.headers["location"]).query)["code"][0]

        tokens = await c.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
            },
        )

    assert tokens.status_code == 200
    access = tokens.json()["access_token"]
    assert signer.verify(access, expect_use=ACCESS_TOKEN_USE).subject == "owner"


async def test_the_wrong_passphrase_does_not_issue_a_code(tmp_path):
    app, _ = routes_app(tmp_path)
    verifier = "verifier-" + "b" * 40

    async with client(app) as c:
        client_id = (await register(c)).json()["client_id"]
        params = authorize_params(client_id, verifier)

        refused = await c.post(
            "/authorize", data={**params, "operator_password": "wrong"}
        )

    assert refused.status_code == 403
    assert "location" not in refused.headers


async def test_authorizing_an_unknown_client_is_refused(tmp_path):
    app, _ = routes_app(tmp_path)

    async with client(app) as c:
        page = await c.get(
            "/authorize", params=authorize_params("ghost-client", "v" * 43)
        )

    assert page.status_code >= 400


# --- the access-token gate --------------------------------------------------


class _Inner:
    """Stands in for the MCP app; answers any path so the gate is what's tested."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    async def __call__(self, scope, receive, send) -> None:
        self.paths.append(scope.get("path", ""))
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"inner"})


def gate(inner):
    return JwtAuthGate(inner, signer=a_signer(), resource_metadata_url=RMU)


async def test_the_mcp_path_refuses_a_request_with_no_token():
    inner = _Inner()

    async with client(gate(inner)) as c:
        response = await c.get("/mcp")

    assert response.status_code == 401
    assert "resource_metadata" in response.headers["www-authenticate"]
    assert inner.paths == []


async def test_the_mcp_path_passes_a_valid_access_token_through():
    inner = _Inner()
    token = a_signer().mint_access_token("owner", [SUPPORTED_SCOPES[0]])

    async with client(gate(inner)) as c:
        response = await c.get("/mcp", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.text == "inner"


async def test_a_refresh_token_does_not_open_the_mcp_path():
    # The gate demands an access token specifically; a refresh token verifies
    # but is the wrong kind, and must not reach the tools.
    inner = _Inner()
    refresh = a_signer().mint_refresh_token("owner", [SUPPORTED_SCOPES[0]])

    async with client(gate(inner)) as c:
        response = await c.get("/mcp", headers={"Authorization": f"Bearer {refresh}"})

    assert response.status_code == 401
    assert inner.paths == []


async def test_the_oauth_paths_are_not_gated():
    inner = _Inner()

    async with client(gate(inner)) as c:
        wellknown = await c.get("/.well-known/oauth-authorization-server")
        authorize = await c.get("/authorize")

    assert wellknown.status_code == 200
    assert authorize.status_code == 200
    assert inner.paths == [
        "/.well-known/oauth-authorization-server",
        "/authorize",
    ]


# --- build_oauth_app assembly -----------------------------------------------


class _FakeMcp:
    def streamable_http_app(self):
        async def tools(request):
            return PlainTextResponse("mcp-tools")

        return Starlette(routes=[Route("/mcp", tools)])


async def test_build_oauth_app_opens_discovery_and_gates_the_tools(tmp_path):
    server, signer = build_server(tmp_path)
    app = build_oauth_app(
        _FakeMcp(),
        server=server,
        signer=signer,
        issuer=ISSUER,
        operator_password=PASSWORD,
    )

    async with client(app) as c:
        discovery = await c.get("/.well-known/oauth-authorization-server")
        unauthed = await c.get("/mcp")
        token = signer.mint_access_token("owner", [SUPPORTED_SCOPES[0]])
        authed = await c.get("/mcp", headers={"Authorization": f"Bearer {token}"})

    assert discovery.status_code == 200
    assert unauthed.status_code == 401
    assert authed.status_code == 200
    assert authed.text == "mcp-tools"
