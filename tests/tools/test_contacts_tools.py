from zoho_mcp.tools.contacts import count_contacts, get_contact, search_contacts


class FakeContactsClient:
    def __init__(self):
        self.search_calls = []
        self.get_contact_calls = []
        self.count_calls = 0
        self.search_result = (
            [{"id": "1", "scope": "personal", "first_name": "Jamie"}],
            False,
        )
        self.get_contact_result = {"id": "1", "scope": "personal", "first_name": "Jamie"}
        self.count_result = {
            "personal": {"contacts": 212, "archived": 0, "inactive": 0},
            "organization": {"contacts": 4, "archived": 0, "inactive": 0},
            "total": 216,
        }

    async def search_contacts(self, query="", limit=20, status="active"):
        self.search_calls.append({"query": query, "limit": limit, "status": status})
        return self.search_result

    async def get_contact(self, contact_id, scope):
        self.get_contact_calls.append({"contact_id": contact_id, "scope": scope})
        return self.get_contact_result

    async def count_contacts(self):
        self.count_calls += 1
        return self.count_result


async def test_search_contacts_delegates_to_client_and_shapes_result():
    client = FakeContactsClient()

    result = await search_contacts(client, query="Jamie", limit=5)

    assert client.search_calls == [{"query": "Jamie", "limit": 5, "status": "active"}]
    assert result == {"contacts": client.search_result[0], "has_more": False}


async def test_search_contacts_surfaces_has_more_true():
    client = FakeContactsClient()
    client.search_result = ([], True)

    result = await search_contacts(client)

    assert result["has_more"] is True


async def test_search_contacts_defaults_query_and_limit():
    client = FakeContactsClient()

    await search_contacts(client)

    assert client.search_calls == [{"query": "", "limit": 20, "status": "active"}]


async def test_search_contacts_forwards_explicit_status():
    client = FakeContactsClient()

    await search_contacts(client, status="archived")

    assert client.search_calls == [{"query": "", "limit": 20, "status": "archived"}]


async def test_get_contact_delegates_to_client_with_contact_id_and_scope():
    client = FakeContactsClient()

    result = await get_contact(client, contact_id="1", scope="personal")

    assert client.get_contact_calls == [{"contact_id": "1", "scope": "personal"}]
    assert result == client.get_contact_result


async def test_count_contacts_delegates_to_client():
    client = FakeContactsClient()

    result = await count_contacts(client)

    assert client.count_calls == 1
    assert result == client.count_result
