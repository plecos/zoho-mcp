"""Tests for the Authlib authorization-server wiring.

Built up in pieces, matching how the module is: first the client wrapper that
adapts a stored ``RegisteredClient`` to the interface Authlib's grants call,
then the grants and token generation on top of it.

The client wrapper is where several security checks live -- exact redirect-URI
matching, which grant and response types are allowed, and public-vs-confidential
auth -- so its rejections carry the tests.
"""

from zoho_mcp.oauth.server import SUPPORTED_SCOPES, AuthlibClient
from zoho_mcp.oauth.store import RegisteredClient

REDIRECT = "https://claude.ai/api/mcp/auth_callback"


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
