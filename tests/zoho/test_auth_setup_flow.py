from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from zoho_mcp.zoho.auth import (
    ZohoAuthError,
    build_authorization_url,
    exchange_code_for_tokens,
    extract_authorization_code,
)

TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


def test_build_authorization_url_includes_required_params_and_offline_access():
    url = build_authorization_url(
        client_id="my-client-id",
        redirect_uri="http://localhost:8765/callback",
        scopes=["ZohoMail.messages.READ", "ZohoCalendar.event.READ"],
    )

    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.zoho.com"
    assert parsed.path == "/oauth/v2/auth"
    assert params["client_id"] == ["my-client-id"]
    assert params["redirect_uri"] == ["http://localhost:8765/callback"]
    assert params["response_type"] == ["code"]
    assert params["scope"] == ["ZohoMail.messages.READ ZohoCalendar.event.READ"]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]


async def test_exchange_code_for_tokens_returns_payload_on_success(
    respx_mock, http_client
):
    route = respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "new-access-token",
                "refresh_token": "new-refresh-token",
                "expires_in": 3600,
                "token_type": "Bearer",
            },
        )
    )

    payload = await exchange_code_for_tokens(
        http_client,
        client_id="id",
        client_secret="secret",
        code="one-time-code",
        redirect_uri="http://localhost:8765/callback",
    )

    assert payload["refresh_token"] == "new-refresh-token"
    sent = route.calls.last.request.read().decode()
    assert "grant_type=authorization_code" in sent
    assert "code=one-time-code" in sent


async def test_exchange_code_for_tokens_raises_when_refresh_token_missing(
    respx_mock, http_client
):
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "token-only", "expires_in": 3600}
        )
    )

    with pytest.raises(ZohoAuthError):
        await exchange_code_for_tokens(
            http_client,
            client_id="id",
            client_secret="secret",
            code="one-time-code",
            redirect_uri="http://localhost:8765/callback",
        )


async def test_exchange_code_for_tokens_wraps_network_error(respx_mock, http_client):
    respx_mock.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(ZohoAuthError):
        await exchange_code_for_tokens(
            http_client,
            client_id="id",
            client_secret="secret",
            code="one-time-code",
            redirect_uri="http://localhost:8765/callback",
        )


def test_extract_authorization_code_returns_code():
    query = {"code": ["one-time-code"], "state": ["xyz"]}

    assert extract_authorization_code(query) == "one-time-code"


def test_extract_authorization_code_raises_on_error_param():
    query = {"error": ["access_denied"]}

    with pytest.raises(ZohoAuthError, match="access_denied"):
        extract_authorization_code(query)


def test_extract_authorization_code_raises_when_code_missing():
    query = {"state": ["xyz"]}

    with pytest.raises(ZohoAuthError, match="authorization code"):
        extract_authorization_code(query)
