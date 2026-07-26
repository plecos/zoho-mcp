"""Tests for starting the server with no stored refresh token.

An MCPB bundle has one entry point, so `zoho-mcp-setup` isn't reachable from
an installed extension. The server therefore has to start unauthenticated and
gain its token later, via the `authenticate` tool.

`get_access_token` is the seam that makes that cheap: all 39 tools reach Zoho
through it, so one clear error there covers every one of them without a
per-tool change. Same reasoning as the send gate living in the client rather
than in a wrapper -- put the check where the traffic actually passes.
"""

import httpx
import pytest

from zoho_mcp.zoho.auth import ZohoAuthError, ZohoTokenManager

TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


def manager(http_client, refresh_token=None):
    return ZohoTokenManager(
        client_id="id",
        client_secret="secret",
        refresh_token=refresh_token,
        http_client=http_client,
    )


def mock_token_endpoint(respx_mock, access_token="access-1"):
    return respx_mock.post(TOKEN_URL).mock(
        return_value=httpx.Response(
            200, json={"access_token": access_token, "expires_in": 3600}
        )
    )


async def test_a_manager_can_be_built_with_no_refresh_token(http_client):
    assert manager(http_client).is_authenticated is False


async def test_a_manager_built_with_a_token_reports_authenticated(http_client):
    assert manager(http_client, "refresh-1").is_authenticated is True


async def test_asking_for_a_token_unauthenticated_names_the_way_out(
    respx_mock, http_client
):
    route = mock_token_endpoint(respx_mock)

    with pytest.raises(ZohoAuthError, match="authenticate"):
        await manager(http_client).get_access_token()

    # No network call: there is nothing to refresh, and a request here would
    # surface as a confusing Zoho error instead of an actionable one.
    assert not route.called


async def test_setting_a_refresh_token_makes_calls_work(respx_mock, http_client):
    mock_token_endpoint(respx_mock)
    token_manager = manager(http_client)

    token_manager.set_refresh_token("refresh-1")

    assert token_manager.is_authenticated is True
    assert await token_manager.get_access_token() == "access-1"


async def test_setting_a_refresh_token_discards_the_cached_access_token(
    respx_mock, http_client
):
    # Re-authenticating is how new scopes are granted. If the old access
    # token survived, every call would keep using the narrower grant until
    # it expired -- a scope error the user already fixed.
    route = mock_token_endpoint(respx_mock, access_token="access-old")
    token_manager = manager(http_client, "refresh-old")
    assert await token_manager.get_access_token() == "access-old"

    route.mock(
        return_value=httpx.Response(
            200, json={"access_token": "access-new", "expires_in": 3600}
        )
    )
    token_manager.set_refresh_token("refresh-new")

    assert await token_manager.get_access_token() == "access-new"


@pytest.mark.parametrize("blank", ["", "   "])
async def test_a_blank_refresh_token_is_not_authentication(http_client, blank):
    token_manager = manager(http_client, blank)

    assert token_manager.is_authenticated is False
    with pytest.raises(ZohoAuthError, match="authenticate"):
        await token_manager.get_access_token()


@pytest.mark.parametrize("blank", ["", "   "])
async def test_setting_a_blank_refresh_token_is_rejected(http_client, blank):
    token_manager = manager(http_client)

    with pytest.raises(ZohoAuthError, match="refresh token"):
        token_manager.set_refresh_token(blank)


# Found by driving the packed bundle: with a token already in the credential
# store but no client id/secret configured, Zoho answers a refresh with
# `invalid_client`, which tells the user nothing about what to fix. A bundle
# reinstall lands exactly there -- keyring survives, the settings form starts
# empty.
async def test_missing_credentials_are_reported_before_asking_zoho(
    respx_mock, http_client
):
    route = mock_token_endpoint(respx_mock)
    token_manager = ZohoTokenManager(
        client_id="",
        client_secret="secret",
        refresh_token="refresh-1",
        http_client=http_client,
    )

    with pytest.raises(ZohoAuthError, match="ZOHO_CLIENT_ID"):
        await token_manager.get_access_token()

    assert not route.called


async def test_missing_secret_is_reported_before_asking_zoho(respx_mock, http_client):
    route = mock_token_endpoint(respx_mock)
    token_manager = ZohoTokenManager(
        client_id="id",
        client_secret="   ",
        refresh_token="refresh-1",
        http_client=http_client,
    )

    with pytest.raises(ZohoAuthError, match="ZOHO_CLIENT_SECRET"):
        await token_manager.get_access_token()

    assert not route.called


async def test_credentials_are_checked_before_the_missing_token(http_client):
    # Both missing: naming the credentials is more useful, because
    # `authenticate` can't run without them either.
    token_manager = manager(http_client)
    token_manager._client_id = ""

    with pytest.raises(ZohoAuthError, match="ZOHO_CLIENT_ID"):
        await token_manager.get_access_token()


# Found in a real host: two clients were connected, so two server processes
# were running. `authenticate` in one wrote the token to the credential store
# and updated *its own* in-memory manager; the sibling process, started
# unauthenticated moments earlier, went on refusing every call until it was
# restarted. Re-reading the store before giving up costs one keyring lookup
# on a path that was about to fail anyway.
async def test_a_token_stored_by_another_process_is_picked_up(
    respx_mock, http_client, monkeypatch
):
    mock_token_endpoint(respx_mock)
    monkeypatch.setattr(
        "zoho_mcp.zoho.auth.load_refresh_token", lambda: "refresh-from-sibling"
    )
    token_manager = manager(http_client)

    assert await token_manager.get_access_token() == "access-1"
    assert token_manager.is_authenticated is True


async def test_the_store_is_only_consulted_when_there_is_no_token(
    respx_mock, http_client, monkeypatch
):
    # The stored token must not override one already in hand -- that would
    # undo `set_refresh_token` after a re-authentication granting new scopes.
    mock_token_endpoint(respx_mock)
    calls: list[int] = []
    monkeypatch.setattr(
        "zoho_mcp.zoho.auth.load_refresh_token",
        lambda: calls.append(1) or "refresh-stale",
    )
    token_manager = manager(http_client, "refresh-current")

    await token_manager.get_access_token()

    assert calls == []


async def test_an_empty_store_still_names_the_authenticate_tool(
    http_client, monkeypatch
):
    monkeypatch.setattr("zoho_mcp.zoho.auth.load_refresh_token", lambda: None)

    with pytest.raises(ZohoAuthError, match="authenticate"):
        await manager(http_client).get_access_token()


async def test_a_broken_credential_store_does_not_mask_the_real_advice(
    http_client, monkeypatch
):
    # keyring raises on some locked/unavailable backends. That's still "not
    # authenticated", and the actionable message beats a keyring traceback.
    def boom():
        raise RuntimeError("no usable keyring backend")

    monkeypatch.setattr("zoho_mcp.zoho.auth.load_refresh_token", boom)

    with pytest.raises(ZohoAuthError, match="authenticate"):
        await manager(http_client).get_access_token()
