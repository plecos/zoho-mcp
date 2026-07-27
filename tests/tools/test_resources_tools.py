from zoho_mcp.tools.resources import list_branches, list_resources


class FakeZohoClient:
    def __init__(self):
        self.list_branches_calls = 0
        self.list_branches_result = [{"id": "branch-1", "name": "Example Branch"}]
        self.list_resources_calls = []
        self.list_resources_result = [{"id": "resource-1", "name": "Meeting Room"}]

    async def list_branches(self):
        self.list_branches_calls += 1
        return self.list_branches_result

    async def list_resources(self, branch_id, building_id, floor_id):
        self.list_resources_calls.append(
            {
                "branch_id": branch_id,
                "building_id": building_id,
                "floor_id": floor_id,
            }
        )
        return self.list_resources_result


async def test_list_branches_delegates_to_client():
    client = FakeZohoClient()

    result = await list_branches(client)

    assert client.list_branches_calls == 1
    assert result == {"branches": client.list_branches_result, "count": 1}


async def test_list_resources_delegates_to_client_with_ids():
    client = FakeZohoClient()

    result = await list_resources(
        client, branch_id="branch-1", building_id="building-1", floor_id="floor-1"
    )

    assert client.list_resources_calls == [
        {"branch_id": "branch-1", "building_id": "building-1", "floor_id": "floor-1"}
    ]
    assert result == {"resources": client.list_resources_result, "count": 1}
