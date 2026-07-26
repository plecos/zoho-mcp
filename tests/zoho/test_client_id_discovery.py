"""Tests for discovering the account id and default calendar uid at runtime.

Both are stable identifiers, so config is a defensible place for them -- but
requiring them there means setup has to print two values for the user to
hand-copy into `.env` before anything works. They're derivable from the same
endpoints `zoho-mcp-setup` already calls, so `ZohoClient` looks them up
itself when they aren't supplied, cached for the life of the process.

An explicitly configured value still wins and skips the lookup entirely --
that's what keeps this from costing an extra round trip for anyone who
already has the ids in `.env`.
"""

import httpx
import pytest

from zoho_mcp.zoho.client import ZohoAPIError, ZohoClient

DISCOVERED_ACCOUNT_ID = "acct-discovered-555"
DISCOVERED_CALENDAR_UID = "cal-discovered-555"
CONFIGURED_ACCOUNT_ID = "acct-configured-555"
CONFIGURED_CALENDAR_UID = "cal-configured-555"


class FakeTokenManager:
    async def get_access_token(self) -> str:
        return "fake-access-token"


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


def undiscovered_client(http_client):
    """A client built the way an operator with no ids in `.env` gets one."""
    return ZohoClient(token_manager=FakeTokenManager(), http_client=http_client)


def mock_accounts_endpoint(respx_mock):
    return respx_mock.get("https://mail.zoho.com/api/accounts").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "accountId": DISCOVERED_ACCOUNT_ID,
                        "isDefaultAccount": True,
                        "timeZone": "America/Los_Angeles",
                        "primaryEmailAddress": "me@example.com",
                    }
                ]
            },
        )
    )


def mock_calendars_endpoint(respx_mock):
    return respx_mock.get("https://calendar.zoho.com/api/v1/calendars").mock(
        return_value=httpx.Response(
            200,
            json={
                "calendars": [
                    {"uid": "cal-other-555", "isdefault": False, "name": "Other"},
                    {"uid": DISCOVERED_CALENDAR_UID, "isdefault": True, "name": "Mine"},
                ]
            },
        )
    )


def mock_folders_endpoint(respx_mock, account_id):
    return respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{account_id}/folders"
    ).mock(return_value=httpx.Response(200, json={"data": []}))


def mock_event_endpoint(respx_mock, calendar_uid, uid="evt-1"):
    return respx_mock.get(
        f"https://calendar.zoho.com/api/v1/calendars/{calendar_uid}/events/{uid}"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "events": [
                    {
                        "uid": uid,
                        "title": "Standup",
                        "organizer": "me@example.com",
                        "etag": "etag-1",
                    }
                ]
            },
        )
    )


async def test_account_id_is_discovered_when_not_configured(respx_mock, http_client):
    accounts = mock_accounts_endpoint(respx_mock)
    folders = mock_folders_endpoint(respx_mock, DISCOVERED_ACCOUNT_ID)

    await undiscovered_client(http_client).list_folders()

    assert accounts.called
    assert folders.called


async def test_discovered_account_id_is_cached_for_the_process(respx_mock, http_client):
    accounts = mock_accounts_endpoint(respx_mock)
    mock_folders_endpoint(respx_mock, DISCOVERED_ACCOUNT_ID)
    respx_mock.get(
        f"https://mail.zoho.com/api/accounts/{DISCOVERED_ACCOUNT_ID}/labels"
    ).mock(return_value=httpx.Response(200, json={"data": []}))
    client = undiscovered_client(http_client)

    await client.list_folders()
    await client.list_labels()

    assert accounts.call_count == 1


async def test_configured_account_id_skips_the_lookup(respx_mock, http_client):
    accounts = mock_accounts_endpoint(respx_mock)
    folders = mock_folders_endpoint(respx_mock, CONFIGURED_ACCOUNT_ID)
    client = ZohoClient(
        token_manager=FakeTokenManager(),
        http_client=http_client,
        account_id=CONFIGURED_ACCOUNT_ID,
    )

    await client.list_folders()

    assert folders.called
    assert not accounts.called


async def test_account_lookup_failure_surfaces_as_a_zoho_error(respx_mock, http_client):
    respx_mock.get("https://mail.zoho.com/api/accounts").mock(
        return_value=httpx.Response(200, json={"data": []})
    )

    with pytest.raises(ZohoAPIError, match="No default Zoho Mail account"):
        await undiscovered_client(http_client).list_folders()


async def test_calendar_uid_is_discovered_when_not_configured(respx_mock, http_client):
    calendars = mock_calendars_endpoint(respx_mock)
    event = mock_event_endpoint(respx_mock, DISCOVERED_CALENDAR_UID)

    await undiscovered_client(http_client).get_event("evt-1")

    assert calendars.called
    assert event.called


async def test_discovered_calendar_uid_is_cached_for_the_process(
    respx_mock, http_client
):
    calendars = mock_calendars_endpoint(respx_mock)
    mock_event_endpoint(respx_mock, DISCOVERED_CALENDAR_UID, uid="evt-1")
    mock_event_endpoint(respx_mock, DISCOVERED_CALENDAR_UID, uid="evt-2")
    client = undiscovered_client(http_client)

    await client.get_event("evt-1")
    await client.get_event("evt-2")

    assert calendars.call_count == 1


async def test_configured_calendar_uid_skips_the_lookup(respx_mock, http_client):
    calendars = mock_calendars_endpoint(respx_mock)
    event = mock_event_endpoint(respx_mock, CONFIGURED_CALENDAR_UID)
    client = ZohoClient(
        token_manager=FakeTokenManager(),
        http_client=http_client,
        calendar_uid=CONFIGURED_CALENDAR_UID,
    )

    await client.get_event("evt-1")

    assert event.called
    assert not calendars.called


async def test_explicit_calendar_id_argument_skips_the_lookup(respx_mock, http_client):
    # The `or` here has to short-circuit: an explicit calendar_id means the
    # default is never needed, so paying for the lookup would be waste.
    calendars = mock_calendars_endpoint(respx_mock)
    event = mock_event_endpoint(respx_mock, "cal-explicit-555")

    await undiscovered_client(http_client).get_event(
        "evt-1", calendar_id="cal-explicit-555"
    )

    assert event.called
    assert not calendars.called


async def test_calendar_lookup_failure_surfaces_as_a_zoho_error(
    respx_mock, http_client
):
    respx_mock.get("https://calendar.zoho.com/api/v1/calendars").mock(
        return_value=httpx.Response(200, json={"calendars": []})
    )

    with pytest.raises(ZohoAPIError, match="No default Zoho Calendar"):
        await undiscovered_client(http_client).get_event("evt-1")
