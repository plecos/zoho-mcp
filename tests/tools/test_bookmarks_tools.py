from zoho_mcp.tools.bookmarks import create_bookmark, get_bookmark, list_bookmarks


class FakeZohoClient:
    def __init__(self):
        self.list_bookmarks_calls = []
        self.list_bookmarks_result = [{"id": "1", "title": "Roadmap Template"}]
        self.get_bookmark_calls = []
        self.create_bookmark_calls = []
        self.create_bookmark_result = {"id": "b-1"}
        self.get_bookmark_result = {"id": "1", "title": "Roadmap Template"}

    async def list_bookmarks(
        self, limit=20, after=0, group_id=None, oldest_first=False
    ):
        self.list_bookmarks_calls.append(
            {
                "limit": limit,
                "after": after,
                "group_id": group_id,
                "oldest_first": oldest_first,
            }
        )
        return self.list_bookmarks_result

    async def create_bookmark(self, url, title, summary="", group_id=None):
        self.create_bookmark_calls.append(
            {"url": url, "title": title, "summary": summary, "group_id": group_id}
        )
        return self.create_bookmark_result

    async def get_bookmark(self, bookmark_id):
        self.get_bookmark_calls.append(bookmark_id)
        return self.get_bookmark_result


async def test_list_bookmarks_delegates_to_client():
    client = FakeZohoClient()

    result = await list_bookmarks(client, limit=5, after=10)

    assert client.list_bookmarks_calls == [
        {"limit": 5, "after": 10, "group_id": None, "oldest_first": False}
    ]
    assert result == client.list_bookmarks_result


async def test_list_bookmarks_defaults_limit_and_after():
    client = FakeZohoClient()

    await list_bookmarks(client)

    assert client.list_bookmarks_calls == [
        {"limit": 20, "after": 0, "group_id": None, "oldest_first": False}
    ]


async def test_get_bookmark_delegates_to_client_with_bookmark_id():
    client = FakeZohoClient()

    result = await get_bookmark(client, bookmark_id="1")

    assert client.get_bookmark_calls == ["1"]
    assert result == client.get_bookmark_result


async def test_list_bookmarks_forwards_group_id():
    client = FakeZohoClient()

    await list_bookmarks(client, group_id="g-1")

    assert client.list_bookmarks_calls[0]["group_id"] == "g-1"


async def test_list_bookmarks_forwards_oldest_first():
    client = FakeZohoClient()

    await list_bookmarks(client, oldest_first=True)

    assert client.list_bookmarks_calls[0]["oldest_first"] is True


async def test_create_bookmark_delegates_to_client():
    client = FakeZohoClient()

    result = await create_bookmark(client, url="https://x.com", title="T", summary="s")

    assert client.create_bookmark_calls == [
        {"url": "https://x.com", "title": "T", "summary": "s", "group_id": None}
    ]
    assert result == client.create_bookmark_result


async def test_create_bookmark_forwards_group_id():
    client = FakeZohoClient()

    await create_bookmark(client, url="https://x.com", title="T", group_id="g-1")

    assert client.create_bookmark_calls[0]["group_id"] == "g-1"
