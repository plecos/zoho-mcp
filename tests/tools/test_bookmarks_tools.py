from zoho_mcp.tools.bookmarks import get_bookmark, list_bookmarks


class FakeZohoClient:
    def __init__(self):
        self.list_bookmarks_calls = []
        self.list_bookmarks_result = [{"id": "1", "title": "Roadmap Template"}]
        self.get_bookmark_calls = []
        self.get_bookmark_result = {"id": "1", "title": "Roadmap Template"}

    async def list_bookmarks(self, limit=20, after=0, group_id=None):
        self.list_bookmarks_calls.append(
            {"limit": limit, "after": after, "group_id": group_id}
        )
        return self.list_bookmarks_result

    async def get_bookmark(self, bookmark_id):
        self.get_bookmark_calls.append(bookmark_id)
        return self.get_bookmark_result


async def test_list_bookmarks_delegates_to_client():
    client = FakeZohoClient()

    result = await list_bookmarks(client, limit=5, after=10)

    assert client.list_bookmarks_calls == [{"limit": 5, "after": 10, "group_id": None}]
    assert result == client.list_bookmarks_result


async def test_list_bookmarks_defaults_limit_and_after():
    client = FakeZohoClient()

    await list_bookmarks(client)

    assert client.list_bookmarks_calls == [{"limit": 20, "after": 0, "group_id": None}]


async def test_get_bookmark_delegates_to_client_with_bookmark_id():
    client = FakeZohoClient()

    result = await get_bookmark(client, bookmark_id="1")

    assert client.get_bookmark_calls == ["1"]
    assert result == client.get_bookmark_result


async def test_list_bookmarks_forwards_group_id():
    client = FakeZohoClient()

    await list_bookmarks(client, group_id="g-1")

    assert client.list_bookmarks_calls[0]["group_id"] == "g-1"
