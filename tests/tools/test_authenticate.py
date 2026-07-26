"""Tests for the `authenticate` tool.

The browser launch and the socket that catches the OAuth redirect are real
I/O and aren't unit tested, the same way `mcp.run()` isn't. What *is* tested
is everything around them: that the consent URL carries the right scopes,
that the code is exchanged and the resulting refresh token both stored and
adopted by the live token manager, and that a failure anywhere in there
leaves the server no more authenticated than it started.

That last one matters most. A half-succeeded authenticate that stored a token
without adopting it -- or adopted one without storing it -- would work until
the next restart and then stop, which is the worst kind of bug to debug.
"""

import httpx
import pytest

from zoho_mcp.tools.auth import authenticate
from zoho_mcp.zoho.auth import SCOPES, ZohoAuthError, ZohoTokenManager

TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def token_manager(http_client):
    return ZohoTokenManager(
        client_id="client-id",
        client_secret="client-secret",
        refresh_token=None,
        http_client=http_client,
    )


@pytest.fixture
def stored(monkeypatch):
    """Capture what would have gone into the OS credential store."""
    captured: list[str] = []
    monkeypatch.setattr(
        "zoho_mcp.tools.auth.store_refresh_token", lambda token: captured.append(token)
    )
    return captured


def mock_exchange(respx_mock, refresh_token="refresh-new"):
    return respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "access-new",
                "refresh_token": refresh_token,
                "expires_in": 3600,
            },
        )
    )


def code_from(seen: list[str], code="auth-code-1"):
    """A stand-in for the browser round trip; records the URL it was given."""

    def obtain(auth_url: str, port: int) -> str:
        seen.append(auth_url)
        return code

    return obtain


async def run(token_manager, http_client, seen=None, **kwargs):
    return await authenticate(
        token_manager,
        http_client,
        client_id="client-id",
        client_secret="client-secret",
        obtain_authorization_code=code_from(seen if seen is not None else []),
        **kwargs,
    )


async def test_a_successful_flow_reports_authenticated(
    respx_mock, token_manager, http_client, stored
):
    mock_exchange(respx_mock)

    result = await run(token_manager, http_client)

    assert result["authenticated"] is True
    assert result["was_already_authenticated"] is False


async def test_the_token_is_both_stored_and_adopted(
    respx_mock, token_manager, http_client, stored
):
    # Storing without adopting works until restart; adopting without storing
    # works only until restart. Both have to happen.
    mock_exchange(respx_mock, refresh_token="refresh-abc")

    await run(token_manager, http_client)

    assert stored == ["refresh-abc"]
    assert token_manager.is_authenticated is True


async def test_the_consent_url_requests_every_scope_the_tools_need(
    respx_mock, token_manager, http_client, stored
):
    mock_exchange(respx_mock)
    seen: list[str] = []

    await run(token_manager, http_client, seen=seen)

    assert len(seen) == 1
    for scope in SCOPES:
        assert scope in seen[0]


async def test_the_redirect_uri_follows_the_callback_port(
    respx_mock, token_manager, http_client, stored
):
    mock_exchange(respx_mock)
    seen: list[str] = []

    await run(token_manager, http_client, seen=seen, callback_port=9999)

    assert "localhost%3A9999%2Fcallback" in seen[0]


async def test_the_reported_scopes_are_the_ones_requested(
    respx_mock, token_manager, http_client, stored
):
    mock_exchange(respx_mock)

    result = await run(token_manager, http_client)

    assert result["scopes"] == SCOPES


async def test_re_authenticating_an_authenticated_server_says_so(
    respx_mock, http_client, stored
):
    # Re-running this is how new scopes get granted, so it must be allowed --
    # but the caller should know it wasn't a first-time setup.
    mock_exchange(respx_mock)
    token_manager = ZohoTokenManager(
        client_id="client-id",
        client_secret="client-secret",
        refresh_token="refresh-old",
        http_client=http_client,
    )

    result = await run(token_manager, http_client)

    assert result["was_already_authenticated"] is True
    assert result["authenticated"] is True


async def test_a_rejected_code_leaves_the_server_unauthenticated(
    respx_mock, token_manager, http_client, stored
):
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_code"})
    )

    with pytest.raises(ZohoAuthError, match="invalid_code"):
        await run(token_manager, http_client)

    assert token_manager.is_authenticated is False
    assert stored == []


async def test_a_response_without_a_refresh_token_is_rejected(
    respx_mock, token_manager, http_client, stored
):
    # Zoho only returns a refresh token when consent was actually re-granted;
    # a response without one must not be mistaken for success.
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "access-only", "expires_in": 3600}
        )
    )

    with pytest.raises(ZohoAuthError):
        await run(token_manager, http_client)

    assert token_manager.is_authenticated is False
    assert stored == []


async def test_a_browser_flow_failure_leaves_nothing_half_done(
    respx_mock, token_manager, http_client, stored
):
    def obtain(auth_url: str, port: int) -> str:
        raise OSError("port already in use")

    with pytest.raises(ZohoAuthError, match="port already in use"):
        await authenticate(
            token_manager,
            http_client,
            client_id="client-id",
            client_secret="client-secret",
            obtain_authorization_code=obtain,
        )

    assert token_manager.is_authenticated is False
    assert stored == []


@pytest.mark.parametrize("missing", ["client_id", "client_secret"])
async def test_missing_credentials_are_reported_before_a_browser_opens(
    token_manager, http_client, stored, missing
):
    seen: list[str] = []
    credentials = {"client_id": "client-id", "client_secret": "client-secret"}
    credentials[missing] = ""

    with pytest.raises(ZohoAuthError, match=missing.upper()):
        await authenticate(
            token_manager,
            http_client,
            obtain_authorization_code=code_from(seen),
            **credentials,
        )

    assert seen == []
    assert stored == []
