import json
from pathlib import Path

import httpx
import pytest

from zoho_mcp.zoho.client import (
    ZohoAPIError,
    get_default_calendar_uid,
    get_primary_account_id,
)

MAIL_ACCOUNTS_URL = "https://mail.zoho.com/api/accounts"
CALENDAR_LIST_URL = "https://calendar.zoho.com/api/v1/calendars"

FIXTURES = Path(__file__).parent.parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeTokenManager:
    async def get_access_token(self) -> str:
        return "fake-access-token"


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


async def test_get_primary_account_id_returns_the_default_account(
    respx_mock, http_client
):
    route = respx_mock.get(MAIL_ACCOUNTS_URL).mock(
        return_value=httpx.Response(
            200, json=load_fixture("mail_accounts_response.json")
        )
    )

    account_id = await get_primary_account_id(FakeTokenManager(), http_client)

    assert account_id == "3870383000000008002"
    assert (
        route.calls.last.request.headers["Authorization"]
        == "Zoho-oauthtoken fake-access-token"
    )


async def test_get_default_calendar_uid_returns_the_default_calendar(
    respx_mock, http_client
):
    respx_mock.get(CALENDAR_LIST_URL).mock(
        return_value=httpx.Response(
            200, json=load_fixture("calendar_list_response.json")
        )
    )

    calendar_uid = await get_default_calendar_uid(FakeTokenManager(), http_client)

    assert calendar_uid == "a809d42a99e34f258c5d5ebd043e5e23"


async def test_get_primary_account_id_raises_clearly_when_no_default_flagged(
    respx_mock, http_client
):
    respx_mock.get(MAIL_ACCOUNTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"accountId": "1", "isDefaultAccount": False},
                    {"accountId": "2", "isDefaultAccount": False},
                ]
            },
        )
    )

    with pytest.raises(ZohoAPIError, match="default"):
        await get_primary_account_id(FakeTokenManager(), http_client)


async def test_get_default_calendar_uid_raises_clearly_when_no_default_flagged(
    respx_mock, http_client
):
    respx_mock.get(CALENDAR_LIST_URL).mock(
        return_value=httpx.Response(
            200, json={"calendars": [{"uid": "a", "isdefault": False}]}
        )
    )

    with pytest.raises(ZohoAPIError, match="default"):
        await get_default_calendar_uid(FakeTokenManager(), http_client)


async def test_get_primary_account_id_raises_clearly_on_malformed_response(
    respx_mock, http_client
):
    respx_mock.get(MAIL_ACCOUNTS_URL).mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )

    with pytest.raises(ZohoAPIError):
        await get_primary_account_id(FakeTokenManager(), http_client)


async def test_get_default_calendar_uid_raises_clearly_on_malformed_response(
    respx_mock, http_client
):
    respx_mock.get(CALENDAR_LIST_URL).mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )

    with pytest.raises(ZohoAPIError):
        await get_default_calendar_uid(FakeTokenManager(), http_client)


async def test_get_primary_account_id_wraps_http_errors(respx_mock, http_client):
    respx_mock.get(MAIL_ACCOUNTS_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )

    with pytest.raises(ZohoAPIError):
        await get_primary_account_id(FakeTokenManager(), http_client)
