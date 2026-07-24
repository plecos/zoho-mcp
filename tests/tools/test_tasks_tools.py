from zoho_mcp.tools.tasks import get_task, list_tasks


class FakeZohoClient:
    def __init__(self):
        self.list_tasks_calls = []
        self.list_tasks_result = ([{"id": "1", "title": "Renew passport"}], False)
        self.get_task_calls = []
        self.get_task_result = {"id": "1", "title": "Renew passport"}

    async def list_tasks(self, limit=20, offset=0, group_id=None, view=None):
        self.list_tasks_calls.append(
            {"limit": limit, "offset": offset, "group_id": group_id, "view": view}
        )
        return self.list_tasks_result

    async def get_task(self, task_id):
        self.get_task_calls.append(task_id)
        return self.get_task_result


async def test_list_tasks_delegates_to_client_and_shapes_result():
    client = FakeZohoClient()

    result = await list_tasks(client, limit=5, offset=10)

    assert client.list_tasks_calls == [
        {"limit": 5, "offset": 10, "group_id": None, "view": None}
    ]
    assert result == {"tasks": client.list_tasks_result[0], "has_more": False}


async def test_list_tasks_defaults_limit_and_offset():
    client = FakeZohoClient()

    await list_tasks(client)

    assert client.list_tasks_calls == [
        {"limit": 20, "offset": 0, "group_id": None, "view": None}
    ]


async def test_list_tasks_surfaces_has_more_true():
    client = FakeZohoClient()
    client.list_tasks_result = ([], True)

    result = await list_tasks(client)

    assert result["has_more"] is True


async def test_get_task_delegates_to_client_with_task_id():
    client = FakeZohoClient()

    result = await get_task(client, task_id="1")

    assert client.get_task_calls == ["1"]
    assert result == client.get_task_result


async def test_list_tasks_forwards_group_id():
    client = FakeZohoClient()

    await list_tasks(client, group_id="zg-1")

    assert client.list_tasks_calls[0]["group_id"] == "zg-1"


async def test_list_tasks_forwards_view():
    client = FakeZohoClient()

    await list_tasks(client, view="assigned_to_me")

    assert client.list_tasks_calls[0]["view"] == "assigned_to_me"
