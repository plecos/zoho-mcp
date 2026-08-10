"""Tests for the Authlib authorization-server wiring.

Built up in pieces, matching how the module is: first the client wrapper that
adapts a stored ``RegisteredClient`` to the interface Authlib's grants call,
then the grants and token generation on top of it.

The client wrapper is where several security checks live -- exact redirect-URI
matching, which grant and response types are allowed, and public-vs-confidential
auth -- so its rejections carry the tests.
"""

from zoho_mcp.oauth.server import (
    SUPPORTED_SCOPES,
    AuthlibClient,
    build_token_generator,
    record_issued_refresh_token,
)
from zoho_mcp.oauth.store import RefreshTokenStore, RegisteredClient
from zoho_mcp.oauth.tokens import ACCESS_TOKEN_USE, REFRESH_TOKEN_USE, TokenSigner

REDIRECT = "https://claude.ai/api/mcp/auth_callback"
ISSUER = "https://mail.example.com"
AUDIENCE = "https://mail.example.com/mcp"


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
