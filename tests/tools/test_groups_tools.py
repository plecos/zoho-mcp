from zoho_mcp.tools.groups import list_groups


class FakeZohoClient:
    def __init__(self):
        self.list_groups_calls = 0
        self.list_groups_result = [
            {"id": "53658048", "name": "Marketing", "service": "tasks"}
        ]

    async def list_groups(self):
        self.list_groups_calls += 1
        return self.list_groups_result


async def test_list_groups_delegates_to_client():
    client = FakeZohoClient()

    result = await list_groups(client)

    assert client.list_groups_calls == 1
    assert result == client.list_groups_result


async def test_list_groups_passes_through_empty_result():
    # An account with no groups is the common case, not an error --
    # the wrapper must not turn it into one.
    client = FakeZohoClient()
    client.list_groups_result = []

    assert await list_groups(client) == []
