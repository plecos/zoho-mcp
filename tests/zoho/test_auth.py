from datetime import datetime, timedelta, timezone

import httpx
import pytest
import time_machine

from zoho_mcp.zoho.auth import (
    ZohoAuthError,
    ZohoTokenManager,
    load_refresh_token,
    store_refresh_token,
)

TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"


def _success_response(access_token: str = "new-access-token", expires_in: int = 3600):
    return httpx.Response(
        200,
        json={
            "access_token": access_token,
            "expires_in": expires_in,
            "token_type": "Bearer",
            "api_domain": "https://www.zohoapis.com",
        },
    )


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


async def test_get_access_token_fetches_and_caches_token(respx_mock, http_client):
    route = respx_mock.post(TOKEN_URL).mock(return_value=_success_response())
    manager = ZohoTokenManager(
        client_id="id",
        client_secret="secret",
        refresh_token="refresh",
        http_client=http_client,
    )

    token = await manager.get_access_token()
    token_again = await manager.get_access_token()

    assert token == "new-access-token"
    assert token_again == "new-access-token"
    assert route.call_count == 1


async def test_get_access_token_refreshes_when_expired(respx_mock, http_client):
    route = respx_mock.post(TOKEN_URL).mock(
        side_effect=[
            _success_response("first-token", expires_in=3600),
            _success_response("second-token", expires_in=3600),
        ]
    )
    manager = ZohoTokenManager(
        client_id="id",
        client_secret="secret",
        refresh_token="refresh",
        http_client=http_client,
    )

    with time_machine.travel(
        datetime(2026, 1, 1, tzinfo=timezone.utc), tick=False
    ) as traveler:
        first = await manager.get_access_token()
        traveler.shift(timedelta(hours=2))
        second = await manager.get_access_token()

    assert first == "first-token"
    assert second == "second-token"
    assert route.call_count == 2


async def test_refresh_raises_zoho_auth_error_on_failure(respx_mock, http_client):
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(400, json={"error": "invalid_code"})
    )
    manager = ZohoTokenManager(
        client_id="id",
        client_secret="secret",
        refresh_token="bad-refresh",
        http_client=http_client,
    )

    with pytest.raises(ZohoAuthError):
        await manager.get_access_token()


async def test_refresh_wraps_network_error_as_zoho_auth_error(respx_mock, http_client):
    respx_mock.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    manager = ZohoTokenManager(
        client_id="id",
        client_secret="secret",
        refresh_token="refresh",
        http_client=http_client,
    )

    with pytest.raises(ZohoAuthError):
        await manager.get_access_token()


async def test_refresh_raises_zoho_auth_error_on_non_json_response(
    respx_mock, http_client
):
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, content=b"<html>not json</html>")
    )
    manager = ZohoTokenManager(
        client_id="id",
        client_secret="secret",
        refresh_token="refresh",
        http_client=http_client,
    )

    with pytest.raises(ZohoAuthError):
        await manager.get_access_token()


async def test_refresh_raises_zoho_auth_error_when_200_missing_access_token(
    respx_mock, http_client
):
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"token_type": "Bearer"})
    )
    manager = ZohoTokenManager(
        client_id="id",
        client_secret="secret",
        refresh_token="refresh",
        http_client=http_client,
    )

    with pytest.raises(ZohoAuthError):
        await manager.get_access_token()


def test_store_refresh_token_writes_via_keyring(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        "zoho_mcp.zoho.auth.keyring.set_password",
        lambda service, key, value: calls.update(
            service=service, key=key, value=value
        ),
    )

    store_refresh_token("abc123")

    assert calls == {
        "service": "zoho-mcp",
        "key": "zoho_refresh_token",
        "value": "abc123",
    }


def test_load_refresh_token_reads_via_keyring(monkeypatch):
    monkeypatch.setattr(
        "zoho_mcp.zoho.auth.keyring.get_password",
        lambda service, key: (
            "abc123" if (service, key) == ("zoho-mcp", "zoho_refresh_token") else None
        ),
    )

    assert load_refresh_token() == "abc123"
