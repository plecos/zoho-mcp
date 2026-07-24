from zoho_mcp.tools.mail import (
    add_label,
    create_draft,
    reply_draft,
    send_email,
    get_email,
    list_attachments,
    list_emails,
    list_folders,
    list_labels,
    list_signatures,
    mark_as_read,
    mark_as_unread,
    move_email,
    remove_label,
    search_emails,
)


class FakeZohoClient:
    def __init__(self):
        self.search_calls = []
        self.get_email_calls = []
        self.search_result = [{"id": "1", "subject": "hi"}]
        self.list_emails_calls = []
        self.list_emails_result = [{"id": "1", "subject": "hi", "read": False}]
        self.get_email_result = {"id": "1", "text": "hello"}
        self.list_attachments_calls = []
        self.list_attachments_result = [{"id": "attach-1", "name": "roadmap.pdf"}]
        self.list_folders_calls = 0
        self.list_folders_result = [{"id": "folder-1", "name": "Inbox"}]
        self.list_labels_calls = 0
        self.list_labels_result = [{"id": "label-1", "name": "Notification"}]
        self.list_signatures_calls = 0
        self.list_signatures_result = [{"id": "sig-1", "name": "default"}]
        self.create_draft_calls = []
        self.reply_draft_calls = []
        self.send_email_calls = []
        self.compose_result = {"id": "msg-1"}
        self.mark_as_read_calls = []
        self.mark_as_unread_calls = []
        self.move_email_calls = []
        self.add_label_calls = []
        self.remove_label_calls = []

    async def search_emails(self, query="", limit=20, days_back=None):
        self.search_calls.append(
            {"query": query, "limit": limit, "days_back": days_back}
        )
        return self.search_result

    async def list_emails(self, status="all", folder_id=None, limit=20, start=1):
        self.list_emails_calls.append(
            {
                "status": status,
                "folder_id": folder_id,
                "limit": limit,
                "start": start,
            }
        )
        return self.list_emails_result

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

    async def create_draft(self, to, subject, content, cc=None, bcc=None):
        self.create_draft_calls.append(
            {"to": to, "subject": subject, "content": content, "cc": cc, "bcc": bcc}
        )
        return self.compose_result

    async def reply_draft(self, message_id, content, reply_all=False):
        self.reply_draft_calls.append(
            {"message_id": message_id, "content": content, "reply_all": reply_all}
        )
        return self.compose_result

    async def send_email(self, to, subject, content, cc=None, bcc=None):
        self.send_email_calls.append(
            {"to": to, "subject": subject, "content": content, "cc": cc, "bcc": bcc}
        )
        return self.compose_result

    async def mark_as_read(self, message_ids):
        self.mark_as_read_calls.append({"message_ids": message_ids})

    async def mark_as_unread(self, message_ids):
        self.mark_as_unread_calls.append({"message_ids": message_ids})

    async def move_email(self, message_ids, folder_id):
        self.move_email_calls.append(
            {"message_ids": message_ids, "folder_id": folder_id}
        )

    async def add_label(self, message_ids, label_id):
        self.add_label_calls.append({"message_ids": message_ids, "label_id": label_id})

    async def remove_label(self, message_ids, label_id):
        self.remove_label_calls.append(
            {"message_ids": message_ids, "label_id": label_id}
        )


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


async def test_list_emails_delegates_to_client_with_defaults():
    client = FakeZohoClient()

    result = await list_emails(client)

    assert client.list_emails_calls == [
        {"status": "all", "folder_id": None, "limit": 20, "start": 1}
    ]
    assert result == client.list_emails_result


async def test_list_emails_forwards_status_folder_id_limit_and_start():
    client = FakeZohoClient()

    await list_emails(client, status="unread", folder_id="folder-9", limit=50, start=21)

    assert client.list_emails_calls == [
        {"status": "unread", "folder_id": "folder-9", "limit": 50, "start": 21}
    ]


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


async def test_mark_as_read_delegates_to_client():
    client = FakeZohoClient()

    result = await mark_as_read(client, message_ids=["msg-1", "msg-2"])

    assert client.mark_as_read_calls == [{"message_ids": ["msg-1", "msg-2"]}]
    assert result is None


async def test_mark_as_unread_delegates_to_client():
    client = FakeZohoClient()

    result = await mark_as_unread(client, message_ids=["msg-1", "msg-2"])

    assert client.mark_as_unread_calls == [{"message_ids": ["msg-1", "msg-2"]}]
    assert result is None


async def test_move_email_delegates_to_client():
    client = FakeZohoClient()

    result = await move_email(
        client, message_ids=["msg-1", "msg-2"], folder_id="folder-2"
    )

    assert client.move_email_calls == [
        {"message_ids": ["msg-1", "msg-2"], "folder_id": "folder-2"}
    ]
    assert result is None


async def test_add_label_delegates_to_client():
    client = FakeZohoClient()

    result = await add_label(client, message_ids=["msg-1", "msg-2"], label_id="label-2")

    assert client.add_label_calls == [
        {"message_ids": ["msg-1", "msg-2"], "label_id": "label-2"}
    ]
    assert result is None


async def test_remove_label_delegates_to_client():
    client = FakeZohoClient()

    result = await remove_label(
        client, message_ids=["msg-1", "msg-2"], label_id="label-2"
    )

    assert client.remove_label_calls == [
        {"message_ids": ["msg-1", "msg-2"], "label_id": "label-2"}
    ]
    assert result is None


async def test_create_draft_delegates_to_client():
    client = FakeZohoClient()

    result = await create_draft(
        client, to=["a@x.com"], subject="S", content="B", cc=["c@x.com"]
    )

    assert client.create_draft_calls == [
        {
            "to": ["a@x.com"],
            "subject": "S",
            "content": "B",
            "cc": ["c@x.com"],
            "bcc": None,
        }
    ]
    assert result == client.compose_result


async def test_reply_draft_delegates_to_client():
    client = FakeZohoClient()

    await reply_draft(client, message_id="m-1", content="B", reply_all=True)

    assert client.reply_draft_calls == [
        {"message_id": "m-1", "content": "B", "reply_all": True}
    ]


async def test_send_email_delegates_to_client():
    client = FakeZohoClient()

    await send_email(client, to=["a@x.com"], subject="S", content="B")

    assert client.send_email_calls[0]["to"] == ["a@x.com"]
