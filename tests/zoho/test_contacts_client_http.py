import json
from pathlib import Path

import httpx
import pytest

from zoho_mcp.zoho.client import ZohoAPIError
from zoho_mcp.zoho.contacts_client import (
    ZOHO_CONTACTS_ORG_URL,
    ZOHO_CONTACTS_SELF_URL,
    ZohoContactsClient,
)

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


@pytest.fixture
def contacts_client(http_client):
    return ZohoContactsClient(token_manager=FakeTokenManager(), http_client=http_client)


def _empty(has_more=False):
    return httpx.Response(200, json={"contacts": [], "has_more": has_more})


async def test_search_contacts_queries_both_personal_and_organization_scopes(
    respx_mock, contacts_client
):
    self_route = respx_mock.get(ZOHO_CONTACTS_SELF_URL).mock(
        return_value=httpx.Response(
            200, json=load_fixture("contacts_list_response.json")
        )
    )
    org_route = respx_mock.get(ZOHO_CONTACTS_ORG_URL).mock(return_value=_empty())

    results, has_more = await contacts_client.search_contacts(query="Jamie", limit=10)

    assert self_route.called
    assert org_route.called
    for route in (self_route, org_route):
        request = route.calls.last.request
        assert request.headers["Authorization"] == "Zoho-oauthtoken fake-access-token"
        assert request.url.params["q"] == "Jamie"
        assert request.url.params["per_page"] == "10"
        assert request.url.params["include"] == "emails,phones"
    assert [r["id"] for r in results] == ["44205000000003001", "44205000000003121"]
    assert all(r["scope"] == "personal" for r in results)
    assert has_more is False


async def test_search_contacts_merges_and_tags_results_from_both_scopes(
    respx_mock, contacts_client
):
    respx_mock.get(ZOHO_CONTACTS_SELF_URL).mock(
        return_value=httpx.Response(
            200, json={"contacts": [{"contact_id": "self-1", "first_name": "Sam"}]}
        )
    )
    respx_mock.get(ZOHO_CONTACTS_ORG_URL).mock(
        return_value=httpx.Response(
            200, json={"contacts": [{"contact_id": "org-1", "first_name": "Org"}]}
        )
    )

    results, has_more = await contacts_client.search_contacts()

    assert [(r["id"], r["scope"]) for r in results] == [
        ("self-1", "personal"),
        ("org-1", "organization"),
    ]
    assert has_more is False


async def test_search_contacts_surfaces_has_more_true_from_either_scope(
    respx_mock, contacts_client
):
    respx_mock.get(ZOHO_CONTACTS_SELF_URL).mock(return_value=_empty(has_more=True))
    respx_mock.get(ZOHO_CONTACTS_ORG_URL).mock(return_value=_empty())

    _, has_more = await contacts_client.search_contacts(query="a")

    assert has_more is True


async def test_search_contacts_truncates_merged_results_to_limit_and_flags_has_more(
    respx_mock, contacts_client
):
    self_contacts = [{"contact_id": f"self-{i}", "first_name": "Sam"} for i in range(3)]
    org_contacts = [{"contact_id": f"org-{i}", "first_name": "Org"} for i in range(3)]
    respx_mock.get(ZOHO_CONTACTS_SELF_URL).mock(
        return_value=httpx.Response(200, json={"contacts": self_contacts})
    )
    respx_mock.get(ZOHO_CONTACTS_ORG_URL).mock(
        return_value=httpx.Response(200, json={"contacts": org_contacts})
    )

    results, has_more = await contacts_client.search_contacts(limit=4)

    assert len(results) == 4
    assert has_more is True


async def test_search_contacts_defaults_to_active_and_omits_filter_type_param(
    respx_mock, contacts_client
):
    self_route = respx_mock.get(ZOHO_CONTACTS_SELF_URL).mock(return_value=_empty())
    org_route = respx_mock.get(ZOHO_CONTACTS_ORG_URL).mock(return_value=_empty())

    await contacts_client.search_contacts()

    for route in (self_route, org_route):
        assert "filter_type" not in route.calls.last.request.url.params


async def test_search_contacts_status_archived_sends_filter_type_to_both_scopes(
    respx_mock, contacts_client
):
    self_route = respx_mock.get(ZOHO_CONTACTS_SELF_URL).mock(
        return_value=httpx.Response(
            200,
            json={"contacts": [{"contact_id": "arch-1", "first_name": "Angela"}]},
        )
    )
    org_route = respx_mock.get(ZOHO_CONTACTS_ORG_URL).mock(return_value=_empty())

    results, _ = await contacts_client.search_contacts(status="archived")

    for route in (self_route, org_route):
        assert route.calls.last.request.url.params["filter_type"] == "archived"
    assert [r["id"] for r in results] == ["arch-1"]


async def test_search_contacts_status_inactive_sends_filter_type_to_both_scopes(
    respx_mock, contacts_client
):
    self_route = respx_mock.get(ZOHO_CONTACTS_SELF_URL).mock(return_value=_empty())
    org_route = respx_mock.get(ZOHO_CONTACTS_ORG_URL).mock(return_value=_empty())

    await contacts_client.search_contacts(status="inactive")

    for route in (self_route, org_route):
        assert route.calls.last.request.url.params["filter_type"] == "inactive"


async def test_search_contacts_rejects_unknown_status_without_a_request(
    respx_mock, contacts_client
):
    self_route = respx_mock.get(ZOHO_CONTACTS_SELF_URL)
    org_route = respx_mock.get(ZOHO_CONTACTS_ORG_URL)

    with pytest.raises(ZohoAPIError, match="status"):
        await contacts_client.search_contacts(status="bogus")

    assert not self_route.called
    assert not org_route.called


async def test_search_contacts_omits_q_param_when_query_empty(
    respx_mock, contacts_client
):
    self_route = respx_mock.get(ZOHO_CONTACTS_SELF_URL).mock(return_value=_empty())
    org_route = respx_mock.get(ZOHO_CONTACTS_ORG_URL).mock(return_value=_empty())

    await contacts_client.search_contacts()

    assert "q" not in self_route.calls.last.request.url.params
    assert "q" not in org_route.calls.last.request.url.params


async def test_search_contacts_rejects_limit_below_one_without_a_request(
    respx_mock, contacts_client
):
    self_route = respx_mock.get(ZOHO_CONTACTS_SELF_URL)
    org_route = respx_mock.get(ZOHO_CONTACTS_ORG_URL)

    with pytest.raises(ZohoAPIError, match="limit"):
        await contacts_client.search_contacts(query="Jamie", limit=0)

    assert not self_route.called
    assert not org_route.called


async def test_search_contacts_returns_empty_list_when_contacts_key_absent(
    respx_mock, contacts_client
):
    respx_mock.get(ZOHO_CONTACTS_SELF_URL).mock(
        return_value=httpx.Response(200, json={"status_code": 200})
    )
    respx_mock.get(ZOHO_CONTACTS_ORG_URL).mock(
        return_value=httpx.Response(200, json={"status_code": 200})
    )

    results, has_more = await contacts_client.search_contacts(query="nobody")

    assert results == []
    assert has_more is False


async def test_search_contacts_wraps_http_errors_as_zoho_api_error(
    respx_mock, contacts_client
):
    respx_mock.get(ZOHO_CONTACTS_SELF_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )
    respx_mock.get(ZOHO_CONTACTS_ORG_URL).mock(return_value=_empty())

    with pytest.raises(ZohoAPIError):
        await contacts_client.search_contacts(query="Jamie")


async def test_get_contact_fetches_by_id_from_personal_scope(
    respx_mock, contacts_client
):
    route = respx_mock.get(f"{ZOHO_CONTACTS_SELF_URL}/44205000000003001").mock(
        return_value=httpx.Response(
            200, json=load_fixture("contacts_single_response.json")
        )
    )

    result = await contacts_client.get_contact("44205000000003001", scope="personal")

    assert route.called
    assert (
        route.calls.last.request.headers["Authorization"]
        == "Zoho-oauthtoken fake-access-token"
    )
    assert result["id"] == "44205000000003001"
    assert result["first_name"] == "Jamie"
    assert result["scope"] == "personal"
    assert result["emails"] == ["jamie.rivera@example.com", "jamie.r.alt@example.com"]


async def test_get_contact_fetches_by_id_from_organization_scope(
    respx_mock, contacts_client
):
    route = respx_mock.get(f"{ZOHO_CONTACTS_ORG_URL}/44205000000003001").mock(
        return_value=httpx.Response(
            200, json=load_fixture("contacts_single_response.json")
        )
    )

    result = await contacts_client.get_contact(
        "44205000000003001", scope="organization"
    )

    assert route.called
    assert result["scope"] == "organization"


async def test_get_contact_rejects_unknown_scope_without_a_request(
    respx_mock, contacts_client
):
    route = respx_mock.get(f"{ZOHO_CONTACTS_SELF_URL}/1")

    with pytest.raises(ZohoAPIError, match="scope"):
        await contacts_client.get_contact("1", scope="bogus")

    assert not route.called


async def test_get_contact_wraps_http_errors_as_zoho_api_error(
    respx_mock, contacts_client
):
    respx_mock.get(f"{ZOHO_CONTACTS_SELF_URL}/does-not-exist").mock(
        return_value=httpx.Response(404, json={"error": "not found"})
    )

    with pytest.raises(ZohoAPIError):
        await contacts_client.get_contact("does-not-exist", scope="personal")


async def test_count_contacts_returns_breakdown_by_scope_and_total(
    respx_mock, contacts_client
):
    self_route = respx_mock.get(ZOHO_CONTACTS_SELF_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "contacts": {
                    "contacts": 212,
                    "archived": 0,
                    "inactive": 0,
                    "account_id": "709548548",
                }
            },
        )
    )
    org_route = respx_mock.get(ZOHO_CONTACTS_ORG_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "contacts": {
                    "contacts": 4,
                    "archived": 0,
                    "inactive": 0,
                    "account_id": "709548547",
                }
            },
        )
    )

    result = await contacts_client.count_contacts()

    assert result == {
        "personal": {"contacts": 212, "archived": 0, "inactive": 0},
        "organization": {"contacts": 4, "archived": 0, "inactive": 0},
        "total": 216,
    }
    assert self_route.calls.last.request.url.params["fields"] == "count"
    assert org_route.calls.last.request.url.params["fields"] == "count"


async def test_count_contacts_reflects_nonzero_archived_and_inactive(
    respx_mock, contacts_client
):
    respx_mock.get(ZOHO_CONTACTS_SELF_URL).mock(
        return_value=httpx.Response(
            200,
            json={"contacts": {"contacts": 200, "archived": 10, "inactive": 2}},
        )
    )
    respx_mock.get(ZOHO_CONTACTS_ORG_URL).mock(
        return_value=httpx.Response(
            200,
            json={"contacts": {"contacts": 3, "archived": 1, "inactive": 0}},
        )
    )

    result = await contacts_client.count_contacts()

    assert result == {
        "personal": {"contacts": 200, "archived": 10, "inactive": 2},
        "organization": {"contacts": 3, "archived": 1, "inactive": 0},
        "total": 203,
    }


async def test_count_contacts_raises_clearly_on_malformed_response(
    respx_mock, contacts_client
):
    respx_mock.get(ZOHO_CONTACTS_SELF_URL).mock(
        return_value=httpx.Response(200, json={"unexpected": "shape"})
    )
    respx_mock.get(ZOHO_CONTACTS_ORG_URL).mock(
        return_value=httpx.Response(
            200, json={"contacts": {"contacts": 4, "archived": 0, "inactive": 0}}
        )
    )

    with pytest.raises(ZohoAPIError):
        await contacts_client.count_contacts()


async def test_count_contacts_wraps_http_errors_as_zoho_api_error(
    respx_mock, contacts_client
):
    respx_mock.get(ZOHO_CONTACTS_SELF_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid token"})
    )
    respx_mock.get(ZOHO_CONTACTS_ORG_URL).mock(
        return_value=httpx.Response(200, json={"contacts": {"contacts": 4}})
    )

    with pytest.raises(ZohoAPIError):
        await contacts_client.count_contacts()
