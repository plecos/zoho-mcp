from zoho_mcp.tools.mail import (
    get_email,
    list_attachments,
    list_folders,
    list_labels,
    list_signatures,
    search_emails,
)


class FakeZohoClient:
    def __init__(self):
        self.search_calls = []
        self.get_email_calls = []
        self.search_result = [{"id": "1", "subject": "hi"}]
        self.get_email_result = {"id": "1", "text": "hello"}
        self.list_attachments_calls = []
        self.list_attachments_result = [{"id": "attach-1", "name": "roadmap.pdf"}]
        self.list_folders_calls = 0
        self.list_folders_result = [{"id": "folder-1", "name": "Inbox"}]
        self.list_labels_calls = 0
        self.list_labels_result = [{"id": "label-1", "name": "Notification"}]
        self.list_signatures_calls = 0
        self.list_signatures_result = [{"id": "sig-1", "name": "default"}]

    async def search_emails(self, query="", limit=20, days_back=None):
        self.search_calls.append(
            {"query": query, "limit": limit, "days_back": days_back}
        )
        return self.search_result

    async def get_email(self, message_id, folder_id):
        self.get_email_calls.append({"message_id": message_id, "folder_id": folder_id})
        return self.get_email_result

    async def list_attachments(self, message_id, folder_id):
        self.list_attachments_calls.append(
            {"message_id": message_id, "folder_id": folder_id}
        )
        return self.list_attachments_result

    async def list_folders(self):
        self.list_folders_calls += 1
        return self.list_folders_result

    async def list_labels(self):
        self.list_labels_calls += 1
        return self.list_labels_result

    async def list_signatures(self):
        self.list_signatures_calls += 1
        return self.list_signatures_result


async def test_search_emails_delegates_to_client_and_returns_result():
    client = FakeZohoClient()

    result = await search_emails(client, query="roadmap", limit=5)

    assert client.search_calls == [{"query": "roadmap", "limit": 5, "days_back": None}]
    assert result == client.search_result


async def test_search_emails_defaults_limit():
    client = FakeZohoClient()

    await search_emails(client, query="roadmap")

    assert client.search_calls == [{"query": "roadmap", "limit": 20, "days_back": None}]


async def test_search_emails_passes_days_back_through():
    client = FakeZohoClient()

    await search_emails(client, query="", days_back=0)

    assert client.search_calls == [{"query": "", "limit": 20, "days_back": 0}]


async def test_get_email_delegates_to_client_with_message_and_folder_id():
    client = FakeZohoClient()

    result = await get_email(client, message_id="msg-1", folder_id="folder-1")

    assert client.get_email_calls == [{"message_id": "msg-1", "folder_id": "folder-1"}]
    assert result == client.get_email_result


async def test_list_attachments_delegates_to_client_with_message_and_folder_id():
    client = FakeZohoClient()

    result = await list_attachments(client, message_id="msg-1", folder_id="folder-1")

    assert client.list_attachments_calls == [
        {"message_id": "msg-1", "folder_id": "folder-1"}
    ]
    assert result == client.list_attachments_result


async def test_list_folders_delegates_to_client():
    client = FakeZohoClient()

    result = await list_folders(client)

    assert client.list_folders_calls == 1
    assert result == client.list_folders_result


async def test_list_labels_delegates_to_client():
    client = FakeZohoClient()

    result = await list_labels(client)

    assert client.list_labels_calls == 1
    assert result == client.list_labels_result


async def test_list_signatures_delegates_to_client():
    client = FakeZohoClient()

    result = await list_signatures(client)

    assert client.list_signatures_calls == 1
    assert result == client.list_signatures_result
