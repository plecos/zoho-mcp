"""Tests for the Authlib authorization-server wiring.

Built up in pieces, matching how the module is: first the client wrapper that
adapts a stored ``RegisteredClient`` to the interface Authlib's grants call,
then the grants and token generation on top of it.

The client wrapper is where several security checks live -- exact redirect-URI
matching, which grant and response types are allowed, and public-vs-confidential
auth -- so its rejections carry the tests.
"""

from urllib.parse import parse_qs, urlencode, urlparse

from authlib.oauth2.rfc7636 import create_s256_code_challenge

from zoho_mcp.oauth.server import (
    SUPPORTED_SCOPES,
    AuthlibClient,
    build_oauth2_request,
    build_token_generator,
    create_authorization_server,
    record_issued_refresh_token,
)
from zoho_mcp.oauth.store import (
    AuthorizationCodeStore,
    ClientStore,
    RefreshTokenStore,
    RegisteredClient,
)
from zoho_mcp.oauth.tokens import ACCESS_TOKEN_USE, REFRESH_TOKEN_USE, TokenSigner

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
ISSUER = "https://mail.example.com"
AUDIENCE = "https://mail.example.com/mcp"
# PKCE verifier: RFC 7636 requires 43-128 chars.
VERIFIER = "test-verifier-" + "a" * 40


def a_signer(**overrides) -> TokenSigner:
    kwargs = dict(signing_key="k" * 43, issuer=ISSUER, audience=AUDIENCE)
    kwargs.update(overrides)
    return TokenSigner(**kwargs)


def a_client(secret=None, redirect_uris=(REDIRECT,)) -> RegisteredClient:
    return RegisteredClient(
        client_id="client-1",
        client_secret=secret,
        redirect_uris=redirect_uris,
        metadata={},
        issued_at=1_750_000_000,
    )


def wrap(**kwargs) -> AuthlibClient:
    return AuthlibClient(a_client(**kwargs))


# --- identity ---------------------------------------------------------------


def test_it_exposes_the_client_id():
    assert wrap().get_client_id() == "client-1"


def test_the_default_redirect_uri_is_the_first_registered():
    assert wrap().get_default_redirect_uri() == REDIRECT


def test_a_client_with_no_redirect_uris_has_no_default():
    assert wrap(redirect_uris=()).get_default_redirect_uri() is None


# --- redirect URI matching (exact only) -------------------------------------


def test_a_registered_redirect_uri_matches():
    assert wrap().check_redirect_uri(REDIRECT) is True


def test_an_unregistered_redirect_uri_does_not_match():
    assert wrap().check_redirect_uri("https://evil.example.com/callback") is False


def test_redirect_uri_matching_is_exact_not_prefix():
    # A prefix or extended path must not pass -- open-redirect territory.
    assert wrap().check_redirect_uri(REDIRECT + "/../evil") is False
    assert wrap().check_redirect_uri(REDIRECT + ".evil.com") is False


# --- public vs confidential auth --------------------------------------------


def test_a_public_client_authenticates_with_none_and_no_secret():
    client = wrap(secret=None)

    assert client.check_endpoint_auth_method("none", "token") is True
    assert client.check_endpoint_auth_method("client_secret_post", "token") is False
    # A public client has no secret, so no secret can match.
    assert client.check_client_secret("anything") is False


def test_a_confidential_client_authenticates_with_its_secret():
    client = wrap(secret="s3cr3t")

    assert client.check_endpoint_auth_method("client_secret_post", "token") is True
    assert client.check_endpoint_auth_method("none", "token") is False
    assert client.check_client_secret("s3cr3t") is True
    assert client.check_client_secret("wrong") is False


# --- grant and response types -----------------------------------------------


def test_only_the_code_response_type_is_allowed():
    assert wrap().check_response_type("code") is True
    assert wrap().check_response_type("token") is False


def test_only_authorization_code_and_refresh_token_grants_are_allowed():
    client = wrap()

    assert client.check_grant_type("authorization_code") is True
    assert client.check_grant_type("refresh_token") is True
    assert client.check_grant_type("password") is False
    assert client.check_grant_type("client_credentials") is False


# --- scope ------------------------------------------------------------------


def test_a_supported_scope_is_allowed():
    scope = SUPPORTED_SCOPES[0]

    assert wrap().get_allowed_scope(scope) == scope


def test_unsupported_scopes_are_dropped_from_the_grant():
    scope = SUPPORTED_SCOPES[0]

    assert wrap().get_allowed_scope(f"{scope} admin root") == scope


def test_an_entirely_unsupported_scope_grants_nothing():
    assert wrap().get_allowed_scope("admin") == ""


# --- token generation -------------------------------------------------------


def test_the_generator_issues_a_verifiable_access_and_refresh_pair():
    signer = a_signer()
    generate = build_token_generator(signer, access_ttl_seconds=3600)

    token = generate(
        "authorization_code", wrap(), user="owner", scope=SUPPORTED_SCOPES[0]
    )

    assert token["token_type"] == "Bearer"
    assert token["expires_in"] == 3600
    access = signer.verify(token["access_token"], expect_use=ACCESS_TOKEN_USE)
    refresh = signer.verify(token["refresh_token"], expect_use=REFRESH_TOKEN_USE)
    assert access.subject == "owner"
    assert access.scopes == (SUPPORTED_SCOPES[0],)
    assert refresh.subject == "owner"


def test_the_access_token_can_be_issued_without_a_refresh_token():
    # Authlib omits the refresh token for grants that shouldn't rotate; the
    # generator must honour that rather than always minting one.
    signer = a_signer()
    generate = build_token_generator(signer, access_ttl_seconds=3600)

    token = generate(
        "authorization_code",
        wrap(),
        user="owner",
        scope=SUPPORTED_SCOPES[0],
        include_refresh_token=False,
    )

    assert "refresh_token" not in token


def test_recording_an_issued_refresh_token_makes_it_current(tmp_path):
    # save_token is the single chokepoint every issued token passes through, so
    # it is where the rotation ledger is updated -- for both the initial code
    # exchange and every later refresh.
    signer = a_signer()
    store = RefreshTokenStore(tmp_path / "refresh.json")
    token = build_token_generator(signer, access_ttl_seconds=3600)(
        "authorization_code", wrap(), user="owner", scope=SUPPORTED_SCOPES[0]
    )

    record_issued_refresh_token(store, signer, token, client_id="client-1")

    jti = signer.verify(token["refresh_token"], expect_use=REFRESH_TOKEN_USE).jti
    assert store.is_current("client-1", jti) is True


def test_recording_a_token_without_a_refresh_token_is_a_no_op(tmp_path):
    signer = a_signer()
    store = RefreshTokenStore(tmp_path / "refresh.json")

    record_issued_refresh_token(
        store, signer, {"access_token": "x", "token_type": "Bearer"}, "client-1"
    )

    assert store.is_current("client-1", "anything") is False


# --- full authorization-code + refresh flow ---------------------------------


def build_full_server(tmp_path, client_secret=None):
    signer = a_signer()
    clients = ClientStore(tmp_path / "clients.json")
    clients.add(
        RegisteredClient(
            client_id="client-1",
            client_secret=client_secret,
            redirect_uris=(REDIRECT,),
            metadata={},
            issued_at=1_750_000_000,
        )
    )
    server = create_authorization_server(
        signer=signer,
        client_store=clients,
        code_store=AuthorizationCodeStore(),
        refresh_store=RefreshTokenStore(tmp_path / "refresh.json"),
    )
    return server, signer


def authorize(server, verifier=VERIFIER, client_id="client-1", redirect=REDIRECT):
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect,
        "scope": SUPPORTED_SCOPES[0],
        "state": "state-xyz",
        "code_challenge": create_s256_code_challenge(verifier),
        "code_challenge_method": "S256",
    }
    request = build_oauth2_request(
        "GET", f"https://mail.example.com/authorize?{urlencode(params)}", params
    )
    return server.create_authorization_response(request, grant_user="owner")


def code_from(response):
    location = dict(response.headers)["Location"]
    return parse_qs(urlparse(location).query)["code"][0]


def exchange_code(
    server, code, verifier=VERIFIER, client_id="client-1", redirect=REDIRECT
):
    params = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    return server.create_token_response(
        build_oauth2_request("POST", "https://mail.example.com/token", params)
    )


def refresh(server, refresh_token, client_id="client-1"):
    params = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    return server.create_token_response(
        build_oauth2_request("POST", "https://mail.example.com/token", params)
    )


def test_authorization_redirects_back_with_a_code(tmp_path):
    server, _ = build_full_server(tmp_path)

    response = authorize(server)

    assert response.status == 302
    location = dict(response.headers)["Location"]
    query = parse_qs(urlparse(location).query)
    assert location.startswith(REDIRECT)
    assert query["state"] == ["state-xyz"]
    assert "code" in query


def test_a_code_exchanges_for_working_access_and_refresh_tokens(tmp_path):
    server, signer = build_full_server(tmp_path)

    token = exchange_code(server, code_from(authorize(server))).body

    assert token["token_type"] == "Bearer"
    access = signer.verify(token["access_token"], expect_use=ACCESS_TOKEN_USE)
    assert access.subject == "owner"
    assert access.scopes == (SUPPORTED_SCOPES[0],)
    signer.verify(token["refresh_token"], expect_use=REFRESH_TOKEN_USE)


def test_the_wrong_pkce_verifier_is_rejected(tmp_path):
    server, _ = build_full_server(tmp_path)
    code = code_from(authorize(server, verifier=VERIFIER))

    response = exchange_code(server, code, verifier="a-different-verifier-" + "b" * 30)

    assert response.status == 400
    assert "error" in response.body


def test_a_code_cannot_be_exchanged_twice(tmp_path):
    server, _ = build_full_server(tmp_path)
    code = code_from(authorize(server))

    assert exchange_code(server, code).status == 200
    assert exchange_code(server, code).status == 400


def test_a_refresh_rotates_the_tokens(tmp_path):
    server, signer = build_full_server(tmp_path)
    first = exchange_code(server, code_from(authorize(server))).body

    second = refresh(server, first["refresh_token"]).body

    assert second["refresh_token"] != first["refresh_token"]
    assert (
        signer.verify(second["access_token"], expect_use=ACCESS_TOKEN_USE).subject
        == "owner"
    )


def test_a_replayed_old_refresh_token_is_refused(tmp_path):
    # Strict rotation with reuse detection: the previous refresh token stops
    # working the instant a new one is issued.
    server, _ = build_full_server(tmp_path)
    first = exchange_code(server, code_from(authorize(server))).body
    refresh(server, first["refresh_token"])  # rotates first -> second

    replay = refresh(server, first["refresh_token"])

    assert replay.status == 400


def test_a_forged_refresh_token_is_refused(tmp_path):
    server, _ = build_full_server(tmp_path)
    other = TokenSigner(signing_key="d" * 43, issuer=ISSUER, audience=AUDIENCE)

    response = refresh(server, other.mint_refresh_token("owner", [SUPPORTED_SCOPES[0]]))

    assert response.status == 400
