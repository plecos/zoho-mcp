"""Tests for the hosted (streamable HTTP) entry point and its gate.

This transport exists so the server can be reached from a phone, where no
local process can be spawned. That reachability is the whole risk: the stdio
server's protection was that it had no listening socket at all, and putting
one on a host removes it. Everything here is about what replaces it.

The gate's invariant is the strict one -- like `check_for_updates` and unlike
`send_email`, the refused path does **nothing at all**: the inner app is never
called, so an unauthorized request cannot reach a tool, cannot reach Zoho, and
cannot even observe whether the server is authenticated.

`main_http` refusing to start without a token is the same reasoning one level
up. A default token would be a published credential; a generated one printed
to the log would be missed. Neither is better than not starting.
"""

import httpx
import pytest

from zoho_mcp import server
from zoho_mcp.http_app import BearerTokenGate, build_http_app
from zoho_mcp.zoho.token_store import EnvTokenStore, KeyringTokenStore

TOKEN = "s3cret-token"


class _Inner:
    """A stand-in ASGI app that records the scopes it was handed."""

    def __init__(self) -> None:
        self.scopes: list[dict] = []

    async def __call__(self, scope, receive, send) -> None:
        self.scopes.append(scope)
        if scope["type"] != "http":
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"reached the tools"})


async def call(app, headers=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        return await client.get("/mcp", headers=headers or {})


# --- the gate ---------------------------------------------------------------


async def test_a_correct_token_reaches_the_inner_app():
    inner = _Inner()

    response = await call(
        BearerTokenGate(inner, auth_token=TOKEN),
        {"Authorization": f"Bearer {TOKEN}"},
    )

    assert response.status_code == 200
    assert response.text == "reached the tools"
    assert len(inner.scopes) == 1


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="absent"),
        pytest.param({"Authorization": "Bearer wrong-token"}, id="wrong"),
        pytest.param({"Authorization": ""}, id="empty"),
        pytest.param({"Authorization": TOKEN}, id="no-scheme"),
        pytest.param({"Authorization": f"Basic {TOKEN}"}, id="wrong-scheme"),
        pytest.param({"Authorization": "Bearer"}, id="scheme-only"),
        pytest.param({"Authorization": f"Bearer {TOKEN} extra"}, id="trailing-junk"),
        pytest.param({"Authorization": f"Bearer {TOKEN[:-1]}"}, id="prefix-of-token"),
        pytest.param({"Authorization": f"Bearer {TOKEN}x"}, id="token-plus-suffix"),
        pytest.param({"Authorization": f"Bearer {TOKEN.upper()}"}, id="wrong-case"),
    ],
)
async def test_anything_but_the_exact_token_is_refused(headers):
    # "prefix-of-token" and "token-plus-suffix" are here to catch a
    # `startswith`/`in` comparison, which would pass a happy-path test while
    # accepting tokens nobody issued.
    inner = _Inner()

    response = await call(BearerTokenGate(inner, auth_token=TOKEN), headers)

    assert response.status_code == 401
    assert inner.scopes == []


async def test_the_scheme_is_matched_case_insensitively():
    # RFC 7235: the scheme is case-insensitive even though the token is not.
    inner = _Inner()

    response = await call(
        BearerTokenGate(inner, auth_token=TOKEN), {"Authorization": f"bearer {TOKEN}"}
    )

    assert response.status_code == 200


async def test_a_refusal_says_how_to_authenticate():
    response = await call(BearerTokenGate(_Inner(), auth_token=TOKEN))

    assert response.headers["www-authenticate"].lower().startswith("bearer")


async def test_a_refusal_does_not_echo_the_expected_token():
    # The 401 body is the one thing an unauthorized caller always sees.
    response = await call(
        BearerTokenGate(_Inner(), auth_token=TOKEN), {"Authorization": "Bearer nope"}
    )

    assert TOKEN not in response.text
    assert TOKEN not in str(response.headers)


async def test_non_http_traffic_passes_through_untouched():
    # Lifespan is not a request and has no headers; 401-ing it would leave the
    # session manager unstarted and every real request failing afterwards.
    inner = _Inner()
    gate = BearerTokenGate(inner, auth_token=TOKEN)
    sent: list[dict] = []

    async def receive():
        return {"type": "lifespan.startup"}

    await gate({"type": "lifespan"}, receive, sent.append)

    assert [scope["type"] for scope in inner.scopes] == ["lifespan"]


# --- building the app -------------------------------------------------------


@pytest.mark.parametrize("token", ["", "   ", "\n\t"])
def test_the_app_refuses_to_be_built_without_a_token(token):
    with pytest.raises(ValueError, match="ZOHO_HTTP_AUTH_TOKEN"):
        build_http_app(object(), auth_token=token)


# --- picking a token store --------------------------------------------------


def test_the_default_store_is_the_os_credential_store(monkeypatch):
    monkeypatch.delenv("ZOHO_TOKEN_STORE", raising=False)

    assert isinstance(server._build_token_store(), KeyringTokenStore)


def test_the_store_can_be_switched_to_the_environment(monkeypatch):
    monkeypatch.setenv("ZOHO_TOKEN_STORE", "env")

    assert isinstance(server._build_token_store(), EnvTokenStore)


@pytest.mark.parametrize("value", ["ENV", " env ", "Env"])
def test_the_store_setting_is_case_and_whitespace_insensitive(monkeypatch, value):
    monkeypatch.setenv("ZOHO_TOKEN_STORE", value)

    assert isinstance(server._build_token_store(), EnvTokenStore)


def test_an_unrecognised_store_fails_fast(monkeypatch):
    # Not a silent fallback to keyring: a hosted deployment that typed
    # "environment" would start, find no token, and report itself
    # unauthenticated -- naming the wrong problem.
    monkeypatch.setenv("ZOHO_TOKEN_STORE", "vault")

    with pytest.raises(ValueError, match="ZOHO_TOKEN_STORE"):
        server._build_token_store()


# --- the entry point --------------------------------------------------------


@pytest.fixture
def hosted(monkeypatch):
    """A viable hosted environment, with nothing actually served."""
    served: list[dict] = []
    monkeypatch.setattr(server, "load_env", lambda: None)
    monkeypatch.setattr(
        server,
        "_serve",
        lambda app, host, port: served.append({"app": app, "host": host, "port": port}),
    )
    monkeypatch.setenv("ZOHO_CLIENT_ID", "id")
    monkeypatch.setenv("ZOHO_CLIENT_SECRET", "secret")
    monkeypatch.setenv("ZOHO_HTTP_AUTH_TOKEN", TOKEN)
    for name in ("ZOHO_TOKEN_STORE", "ZOHO_HTTP_HOST", "ZOHO_HTTP_PORT"):
        monkeypatch.delenv(name, raising=False)
    return served


def test_main_http_refuses_to_start_without_an_auth_token(hosted, monkeypatch):
    monkeypatch.delenv("ZOHO_HTTP_AUTH_TOKEN")

    with pytest.raises(ValueError, match="ZOHO_HTTP_AUTH_TOKEN"):
        server.main_http()

    assert hosted == []


@pytest.mark.parametrize("token", ["", "   "])
def test_main_http_refuses_a_blank_auth_token(hosted, monkeypatch, token):
    monkeypatch.setenv("ZOHO_HTTP_AUTH_TOKEN", token)

    with pytest.raises(ValueError, match="ZOHO_HTTP_AUTH_TOKEN"):
        server.main_http()

    assert hosted == []


def test_main_http_binds_loopback_by_default(hosted):
    # Not 0.0.0.0. Reaching this from a phone means a tunnel or a reverse
    # proxy in front, both of which connect over loopback; defaulting to every
    # interface would publish the mailbox to the host's whole network the
    # moment someone forgot a firewall rule.
    server.main_http()

    assert hosted[0]["host"] == "127.0.0.1"
    assert hosted[0]["port"] == 8000


def test_main_http_honours_an_explicit_host_and_port(hosted, monkeypatch):
    monkeypatch.setenv("ZOHO_HTTP_HOST", "0.0.0.0")
    monkeypatch.setenv("ZOHO_HTTP_PORT", "9443")

    server.main_http()

    assert hosted[0]["host"] == "0.0.0.0"
    assert hosted[0]["port"] == 9443


def test_main_http_serves_the_gate_not_the_bare_app(hosted):
    # The gate has to wrap the app that actually gets served. Building one and
    # serving the other would pass every gate unit test above and still ship
    # an open server.
    server.main_http()

    assert isinstance(hosted[0]["app"], BearerTokenGate)
