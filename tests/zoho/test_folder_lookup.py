import json
from pathlib import Path

import httpx
import pytest

from zoho_mcp.zoho.client import ZohoAPIError, get_folder_types

MAIL_FOLDERS_URL = "https://mail.zoho.com/api/accounts/acct-123/folders"
ACCOUNT_ID = "acct-123"

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


async def test_get_folder_types_maps_folder_id_to_folder_type(respx_mock, http_client):
    respx_mock.get(MAIL_FOLDERS_URL).mock(
        return_value=httpx.Response(
            200, json=load_fixture("mail_folders_response.json")
        )
    )

    folder_types = await get_folder_types(FakeTokenManager(), http_client, ACCOUNT_ID)

    assert folder_types["1000000000001"] == "Inbox"
    assert folder_types["1000000000002"] == "Drafts"
    assert folder_types["1000000000003"] == "Templates"
    assert folder_types["1000000000004"] == "Sent"
    # User-created/rule-filed folders report folderType "Inbox", not a
    # distinct "custom" type -- confirmed against the real API.
    assert folder_types["1000000000008"] == "Inbox"


async def test_get_folder_types_raises_clearly_on_malformed_response(
    respx_mock, http_client
):
    respx_mock.get(MAIL_FOLDERS_URL).mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )

    with pytest.raises(ZohoAPIError):
        await get_folder_types(FakeTokenManager(), http_client, ACCOUNT_ID)


async def test_get_folder_types_wraps_http_errors(respx_mock, http_client):
    respx_mock.get(MAIL_FOLDERS_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )

    with pytest.raises(ZohoAPIError):
        await get_folder_types(FakeTokenManager(), http_client, ACCOUNT_ID)
