"""Tests for reading an attachment's actual content.

Zoho's attachment endpoint returns a raw byte stream, but a tool result is
not a file download -- it lands in a context window. So the bytes never
reach the model: `get_attachment` returns a JSON record describing the
attachment and, when the content is genuinely readable text, the text
itself.

Two live-verified quirks drive the design, both recorded in
docs/zoho-api-notes.md:

- The response's `Content-Type` is always `application/octet-stream`, even
  for a gzip file, so it says nothing about what the attachment actually
  is.
- `attachmentinfo` carries only id, name, and size -- no media type at all.

Which leaves the filename extension as the only type *hint*, and
decodability as the only type *fact*. The record reports both separately.
"""

import httpx
import pytest

from zoho_mcp.zoho.client import (
    MAX_ATTACHMENT_FETCH_BYTES,
    MAX_ATTACHMENT_TEXT_CHARS,
    ZohoAPIError,
    ZohoClient,
    normalize_attachment_content,
)

ACCOUNT_ID = "acct-555"
FOLDER_ID = "folder-555"
MESSAGE_ID = "msg-555"
ATTACHMENT_ID = "att-555"

PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


class FakeTokenManager:
    async def get_access_token(self) -> str:
        return "fake-access-token"


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def zoho_client(http_client):
    return ZohoClient(
        token_manager=FakeTokenManager(),
        http_client=http_client,
        account_id=ACCOUNT_ID,
    )


def attachment_url() -> str:
    return (
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}"
        f"/folders/{FOLDER_ID}/messages/{MESSAGE_ID}"
        f"/attachments/{ATTACHMENT_ID}"
    )


# --- normalize_attachment_content -------------------------------------


def test_utf8_content_is_returned_as_readable_text():
    record = normalize_attachment_content(
        ATTACHMENT_ID, "notes.txt", 11, b"hello world"
    )

    assert record["is_text"] is True
    assert record["text"] == "hello world"
    assert record["truncated"] is False
    assert record["media_type"] == "text/plain"
    assert record["note"] == ""


def test_binary_content_reports_why_there_is_no_text():
    record = normalize_attachment_content(
        ATTACHMENT_ID, "logo.png", len(PNG_BYTES), PNG_BYTES
    )

    assert record["is_text"] is False
    assert record["text"] is None
    assert record["media_type"] == "image/png"
    assert "not text" in record["note"]


def test_decodable_bytes_containing_a_null_are_still_binary():
    # A NUL byte decodes fine as UTF-8 but means the payload isn't text;
    # returning it would put control characters into the context window.
    record = normalize_attachment_content(ATTACHMENT_ID, "data.txt", 7, b"ab\x00cdef")

    assert record["is_text"] is False
    assert record["text"] is None


def test_a_text_extension_does_not_make_undecodable_bytes_text():
    # The extension is a hint for media_type only -- decodability is what
    # decides. Trusting .csv here would hand the model mojibake.
    record = normalize_attachment_content(
        ATTACHMENT_ID, "export.csv", 4, b"\xff\xfe\x00\x01"
    )

    assert record["media_type"] == "text/csv"
    assert record["is_text"] is False


def test_a_binary_extension_does_not_hide_content_that_is_readable():
    # The inverse: .gz is normally binary, but the call is made on the
    # bytes, not the name.
    record = normalize_attachment_content(ATTACHMENT_ID, "report.gz", 5, b"plain")

    assert record["media_type"] == "application/gzip"
    assert record["is_text"] is True
    assert record["text"] == "plain"


def test_long_text_is_truncated_and_says_so():
    body = ("x" * MAX_ATTACHMENT_TEXT_CHARS) + "TAIL"

    record = normalize_attachment_content(
        ATTACHMENT_ID, "big.log", len(body), body.encode()
    )

    assert record["is_text"] is True
    assert len(record["text"]) == MAX_ATTACHMENT_TEXT_CHARS
    assert record["truncated"] is True
    assert "truncated" in record["note"]


def test_text_exactly_at_the_limit_is_not_marked_truncated():
    body = "x" * MAX_ATTACHMENT_TEXT_CHARS

    record = normalize_attachment_content(
        ATTACHMENT_ID, "exact.log", len(body), body.encode()
    )

    assert record["truncated"] is False
    assert record["note"] == ""


def test_empty_attachment_is_empty_text_not_binary():
    record = normalize_attachment_content(ATTACHMENT_ID, "empty.txt", 0, b"")

    assert record["is_text"] is True
    assert record["text"] == ""


def test_unfetched_content_reports_the_size_that_stopped_it():
    # data=None is how the client says "I refused to download this".
    record = normalize_attachment_content(ATTACHMENT_ID, "huge.pdf", 9_000_000, None)

    assert record["is_text"] is False
    assert record["text"] is None
    assert "9000000" in record["note"] or "9,000,000" in record["note"]


def test_unknown_and_absent_extensions_fall_back_to_octet_stream():
    assert (
        normalize_attachment_content(ATTACHMENT_ID, "thing.zzz", 1, b"a")["media_type"]
        == "application/octet-stream"
    )
    assert (
        normalize_attachment_content(ATTACHMENT_ID, "README", 1, b"a")["media_type"]
        == "application/octet-stream"
    )


def test_media_type_lookup_ignores_extension_case():
    record = normalize_attachment_content(ATTACHMENT_ID, "INVITE.ICS", 1, b"a")

    assert record["media_type"] == "text/calendar"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.txt", "text/plain"),
        ("a.csv", "text/csv"),
        ("a.md", "text/markdown"),
        ("a.log", "text/plain"),
        ("a.json", "application/json"),
        ("a.xml", "application/xml"),
        ("a.html", "text/html"),
        ("a.ics", "text/calendar"),
        ("a.eml", "message/rfc822"),
        ("a.pdf", "application/pdf"),
        ("a.png", "image/png"),
        ("a.gif", "image/gif"),
        ("a.jpg", "image/jpeg"),
        ("a.gz", "application/gzip"),
        ("a.zip", "application/zip"),
    ],
)
def test_extension_to_media_type_mapping(name, expected):
    assert normalize_attachment_content("i", name, 1, b"a")["media_type"] == expected


def test_the_record_carries_the_ids_needed_to_refer_back_to_it():
    record = normalize_attachment_content(
        ATTACHMENT_ID, "notes.txt", 11, b"hello world"
    )

    assert record["id"] == ATTACHMENT_ID
    assert record["name"] == "notes.txt"
    assert record["size_bytes"] == 11


# --- ZohoClient.get_attachment ----------------------------------------


async def test_get_attachment_calls_the_documented_endpoint(respx_mock, zoho_client):
    route = respx_mock.get(attachment_url()).mock(
        return_value=httpx.Response(200, content=b"hello world")
    )

    record = await zoho_client.get_attachment(MESSAGE_ID, FOLDER_ID, ATTACHMENT_ID)

    assert route.called
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Zoho-oauthtoken fake-access-token"
    assert request.headers["Accept"] == "application/octet-stream"
    assert record["text"] == "hello world"


async def test_get_attachment_names_the_file_from_content_disposition(
    respx_mock, zoho_client
):
    # attachmentinfo isn't fetched here, so the filename has to come off the
    # download response -- and Zoho percent-encodes it.
    respx_mock.get(attachment_url()).mock(
        return_value=httpx.Response(
            200,
            content=b"a,b\n1,2\n",
            headers={"content-disposition": "attachment; filename = q1%21report.csv"},
        )
    )

    record = await zoho_client.get_attachment(MESSAGE_ID, FOLDER_ID, ATTACHMENT_ID)

    assert record["name"] == "q1!report.csv"
    assert record["media_type"] == "text/csv"


async def test_get_attachment_falls_back_to_the_id_when_unnamed(
    respx_mock, zoho_client
):
    respx_mock.get(attachment_url()).mock(
        return_value=httpx.Response(200, content=b"body")
    )

    record = await zoho_client.get_attachment(MESSAGE_ID, FOLDER_ID, ATTACHMENT_ID)

    assert record["name"] == ATTACHMENT_ID


async def test_oversized_attachment_is_never_downloaded(respx_mock, zoho_client):
    too_big = MAX_ATTACHMENT_FETCH_BYTES + 1
    route = respx_mock.get(attachment_url()).mock(
        return_value=httpx.Response(
            200,
            content=b"x" * 32,
            headers={"content-length": str(too_big)},
        )
    )

    record = await zoho_client.get_attachment(MESSAGE_ID, FOLDER_ID, ATTACHMENT_ID)

    assert route.called
    assert record["is_text"] is False
    assert record["text"] is None
    assert record["size_bytes"] == too_big
    assert str(too_big) in record["note"]


async def test_attachment_exactly_at_the_fetch_limit_is_downloaded(
    respx_mock, zoho_client
):
    body = b"x" * 64
    respx_mock.get(attachment_url()).mock(
        return_value=httpx.Response(
            200,
            content=body,
            headers={"content-length": str(MAX_ATTACHMENT_FETCH_BYTES)},
        )
    )

    record = await zoho_client.get_attachment(MESSAGE_ID, FOLDER_ID, ATTACHMENT_ID)

    assert record["is_text"] is True


async def test_missing_content_length_still_reports_the_real_size(
    respx_mock, zoho_client
):
    respx_mock.get(attachment_url()).mock(
        return_value=httpx.Response(200, content=b"hello")
    )

    record = await zoho_client.get_attachment(MESSAGE_ID, FOLDER_ID, ATTACHMENT_ID)

    assert record["size_bytes"] == 5


async def test_get_attachment_wraps_a_zoho_error(respx_mock, zoho_client):
    respx_mock.get(attachment_url()).mock(
        return_value=httpx.Response(404, text="Attachment not found")
    )

    with pytest.raises(ZohoAPIError, match="404"):
        await zoho_client.get_attachment(MESSAGE_ID, FOLDER_ID, ATTACHMENT_ID)


async def test_get_attachment_wraps_a_transport_error(respx_mock, zoho_client):
    respx_mock.get(attachment_url()).mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(ZohoAPIError, match="failed"):
        await zoho_client.get_attachment(MESSAGE_ID, FOLDER_ID, ATTACHMENT_ID)


@pytest.mark.parametrize("blank", ["", "   "])
async def test_get_attachment_rejects_a_blank_attachment_id(zoho_client, blank):
    with pytest.raises(ZohoAPIError, match="attachment_id"):
        await zoho_client.get_attachment(MESSAGE_ID, FOLDER_ID, blank)
