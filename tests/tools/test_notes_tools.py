from zoho_mcp.tools.notes import get_note, list_notes


class FakeZohoClient:
    def __init__(self):
        self.list_notes_calls = []
        self.list_notes_result = [{"id": "1", "title": "Dinner party ideas"}]
        self.get_note_calls = []
        self.get_note_result = {"id": "1", "title": "Dinner party ideas"}

    async def list_notes(self, limit=20, after=0, group_id=None):
        self.list_notes_calls.append(
            {"limit": limit, "after": after, "group_id": group_id}
        )
        return self.list_notes_result

    async def get_note(self, note_id):
        self.get_note_calls.append(note_id)
        return self.get_note_result


async def test_list_notes_delegates_to_client():
    client = FakeZohoClient()

    result = await list_notes(client, limit=5, after=10)

    assert client.list_notes_calls == [{"limit": 5, "after": 10, "group_id": None}]
    assert result == client.list_notes_result


async def test_list_notes_defaults_limit_and_after():
    client = FakeZohoClient()

    await list_notes(client)

    assert client.list_notes_calls == [{"limit": 20, "after": 0, "group_id": None}]


async def test_get_note_delegates_to_client_with_note_id():
    client = FakeZohoClient()

    result = await get_note(client, note_id="1")

    assert client.get_note_calls == ["1"]
    assert result == client.get_note_result


async def test_list_notes_forwards_group_id():
    client = FakeZohoClient()

    await list_notes(client, group_id="g-1")

    assert client.list_notes_calls[0]["group_id"] == "g-1"
