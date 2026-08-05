from datetime import datetime, timedelta, timezone

import httpx
import pytest
import time_machine

from zoho_mcp.zoho.auth import ZohoAuthError, ZohoTokenManager

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
    respx_mock.post(TOKEN_URL).mock(
        side_effect=httpx.ConnectError("connection refused")
    )
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


def _manager(http_client):
    return ZohoTokenManager(
        client_id="id",
        client_secret="secret",
        refresh_token="refresh",
        http_client=http_client,
    )


# expires_in was trusted to be an int. Zoho is documented to send strings where
# numbers are expected elsewhere (status "1", isFavorite "false", color "-1"),
# and a string here leaked a bare TypeError out of every tool call rather than
# a ZohoAuthError the server can report.
@pytest.mark.parametrize("value", ["3600", 3600.0, " 3600 "])
async def test_numeric_expires_in_is_accepted_whatever_its_type(
    respx_mock, http_client, value
):
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "t", "expires_in": value}
        )
    )

    assert await _manager(http_client).get_access_token() == "t"


@pytest.mark.parametrize("value", [None, "soon", "", [], {}])
async def test_unparseable_expires_in_raises_auth_error(respx_mock, http_client, value):
    respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "t", "expires_in": value}
        )
    )

    with pytest.raises(ZohoAuthError, match="expires_in"):
        await _manager(http_client).get_access_token()


# A lifetime shorter than the safety margin used to push expires_at into the
# past, so every single call re-refreshed -- one token POST per API call, which
# is a straight path to being rate-limited into an outage.
@pytest.mark.parametrize("short_lifetime", [0, 1, 30, 60])
async def test_short_lifetime_does_not_cause_a_refresh_on_every_call(
    respx_mock, http_client, short_lifetime
):
    route = respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": "t", "expires_in": short_lifetime}
        )
    )
    manager = _manager(http_client)

    for _ in range(3):
        await manager.get_access_token()

    assert route.call_count == 1


# The safety margin itself was never exercised: the existing expiry test jumps
# two hours, so deleting REFRESH_SAFETY_MARGIN_SECONDS entirely broke nothing.
async def test_token_is_refreshed_inside_the_safety_margin(respx_mock, http_client):
    route = respx_mock.post(TOKEN_URL).mock(return_value=_success_response())
    manager = _manager(http_client)
    await manager.get_access_token()

    # 3570s in: past 3600-60, so the token is treated as expiring imminently.
    with time_machine.travel(
        datetime.now(timezone.utc) + timedelta(seconds=3570), tick=False
    ):
        await manager.get_access_token()

    assert route.call_count == 2


async def test_token_is_not_refreshed_before_the_safety_margin(respx_mock, http_client):
    route = respx_mock.post(TOKEN_URL).mock(return_value=_success_response())
    manager = _manager(http_client)
    await manager.get_access_token()

    with time_machine.travel(
        datetime.now(timezone.utc) + timedelta(seconds=3000), tick=False
    ):
        await manager.get_access_token()

    assert route.call_count == 1
