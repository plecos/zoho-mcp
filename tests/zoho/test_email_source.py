"""Tests for `get_email_source` -- the parsed headers of a message.

Zoho's `originalmessage` endpoint hands back the whole RFC 822 source as one
string (28,469 characters for an ordinary message on a real account). Dumping
that into a context window is the opposite of useful, and it leaves the model
to parse MIME itself.

So the MIME is parsed here and the result is a JSON record: a curated set of
headers, the `Received` chain as a list, and the names of everything else
that was present so nothing is silently hidden. The raw source is available
via `include_raw`, capped, for the cases where only the real bytes will do.

RFC 2047 decoding is part of the job for the same reason timezone conversion
is: a subject arrives as `=?utf-8?B?...?=`, and a model asked to decode
base64 in its head will sometimes get it wrong and never say so.
"""

import httpx
import pytest

from zoho_mcp.zoho.client import (
    MAX_RAW_MESSAGE_CHARS,
    ZohoAPIError,
    ZohoClient,
    normalize_email_source,
)

ACCOUNT_ID = "acct-555"
MESSAGE_ID = "555000111"

SAMPLE_SOURCE = """\
Delivered-To: me@example.com
Received: by 10.0.0.1 with SMTP id abc; Fri, 24 Jul 2026 09:00:01 -0700
Received: from mail.example.net (mail.example.net [203.0.113.7])
 by mx.example.com with ESMTPS id xyz; Fri, 24 Jul 2026 09:00:00 -0700
Authentication-Results: mx.example.com; spf=pass; dkim=pass; dmarc=pass
Received-SPF: pass (example.com: domain of a@example.net designates 203.0.113.7)
Return-Path: <bounce@example.net>
From: Dana Lee <dana@example.net>
To: me@example.com
Cc: team@example.com
Reply-To: replies@example.net
Subject: Quarterly numbers
Date: Fri, 24 Jul 2026 09:00:00 -0700
Message-ID: <abc123@example.net>
In-Reply-To: <prior@example.net>
References: <first@example.net> <prior@example.net>
List-Unsubscribe: <https://example.net/u/1>
DKIM-Signature: v=1; a=rsa-sha256; b=AAAABBBBCCCCDDDD
X-Custom-Thing: whatever
Content-Type: text/plain; charset="utf-8"

Body text that is not a header.
"""


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


def source_url() -> str:
    return (
        f"https://mail.zoho.com/api/accounts/{ACCOUNT_ID}"
        f"/messages/{MESSAGE_ID}/originalmessage"
    )


def mock_source(respx_mock, content=SAMPLE_SOURCE):
    return respx_mock.get(source_url()).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": {"code": 200, "description": "success"},
                # messageId comes back as an int here while every other
                # endpoint sends it as a string -- see the notes.
                "data": {"messageId": int(MESSAGE_ID), "content": content},
            },
        )
    )


# --- normalize_email_source -------------------------------------------


def test_curated_headers_are_extracted_under_snake_case_names():
    record = normalize_email_source(MESSAGE_ID, SAMPLE_SOURCE, include_raw=False)

    assert record["headers"]["from"] == "Dana Lee <dana@example.net>"
    assert record["headers"]["to"] == "me@example.com"
    assert record["headers"]["cc"] == "team@example.com"
    assert record["headers"]["reply_to"] == "replies@example.net"
    assert record["headers"]["subject"] == "Quarterly numbers"
    assert record["headers"]["message_id"] == "<abc123@example.net>"
    assert record["headers"]["in_reply_to"] == "<prior@example.net>"
    assert record["headers"]["return_path"] == "<bounce@example.net>"
    assert record["headers"]["list_unsubscribe"] == "<https://example.net/u/1>"


def test_authentication_headers_are_kept_because_they_are_the_point():
    # The reason to look at a message's source at all is usually "is this
    # really from who it says" -- these are the headers that answer it.
    record = normalize_email_source(MESSAGE_ID, SAMPLE_SOURCE, include_raw=False)

    assert "spf=pass" in record["headers"]["authentication_results"]
    assert record["headers"]["received_spf"].startswith("pass")


def test_the_received_chain_is_a_list_in_order():
    record = normalize_email_source(MESSAGE_ID, SAMPLE_SOURCE, include_raw=False)

    assert len(record["received_chain"]) == 2
    assert record["received_chain"][0].startswith("by 10.0.0.1")
    assert "mail.example.net" in record["received_chain"][1]


def test_received_headers_are_not_duplicated_into_the_header_map():
    record = normalize_email_source(MESSAGE_ID, SAMPLE_SOURCE, include_raw=False)

    assert "received" not in record["headers"]


def test_uncurated_headers_are_named_but_not_valued():
    # DKIM-Signature is the reason: its value is a long base64 blob with no
    # value to a reader, but hiding its existence would be misleading.
    record = normalize_email_source(MESSAGE_ID, SAMPLE_SOURCE, include_raw=False)

    assert "DKIM-Signature" in record["other_header_names"]
    assert "X-Custom-Thing" in record["other_header_names"]
    assert "AAAABBBBCCCCDDDD" not in str(record)


def test_absent_headers_are_omitted_rather_than_set_to_none():
    record = normalize_email_source(
        MESSAGE_ID, "From: a@example.com\nSubject: Hi\n\nBody\n", include_raw=False
    )

    assert set(record["headers"]) == {"from", "subject"}


def test_rfc2047_encoded_headers_are_decoded():
    source = (
        "From: a@example.com\nSubject: =?utf-8?B?UXVhcnRlcmx5IG51bWJlcnM=?=\n\nBody\n"
    )

    record = normalize_email_source(MESSAGE_ID, source, include_raw=False)

    assert record["headers"]["subject"] == "Quarterly numbers"


def test_a_repeated_header_keeps_every_value():
    source = (
        "From: a@example.com\n"
        "Authentication-Results: mx1; spf=pass\n"
        "Authentication-Results: mx2; dkim=fail\n"
        "\nBody\n"
    )

    record = normalize_email_source(MESSAGE_ID, source, include_raw=False)

    assert "spf=pass" in record["headers"]["authentication_results"]
    assert "dkim=fail" in record["headers"]["authentication_results"]


def test_the_body_is_never_mistaken_for_headers():
    record = normalize_email_source(MESSAGE_ID, SAMPLE_SOURCE, include_raw=False)

    assert "Body text that is not a header." not in str(record)


def test_raw_is_withheld_unless_asked_for():
    record = normalize_email_source(MESSAGE_ID, SAMPLE_SOURCE, include_raw=False)

    assert record["raw"] is None
    assert record["raw_truncated"] is False


def test_raw_is_returned_verbatim_when_asked_for():
    record = normalize_email_source(MESSAGE_ID, SAMPLE_SOURCE, include_raw=True)

    assert record["raw"] == SAMPLE_SOURCE
    assert record["raw_truncated"] is False


def test_oversized_raw_is_truncated_and_flagged():
    source = "From: a@example.com\n\n" + ("x" * (MAX_RAW_MESSAGE_CHARS + 10))

    record = normalize_email_source(MESSAGE_ID, source, include_raw=True)

    assert len(record["raw"]) == MAX_RAW_MESSAGE_CHARS
    assert record["raw_truncated"] is True


def test_size_is_reported_even_when_raw_is_withheld():
    record = normalize_email_source(MESSAGE_ID, SAMPLE_SOURCE, include_raw=False)

    assert record["size_chars"] == len(SAMPLE_SOURCE)


def test_a_message_with_no_headers_at_all_is_not_an_error():
    record = normalize_email_source(MESSAGE_ID, "just text", include_raw=False)

    assert record["headers"] == {}
    assert record["received_chain"] == []
    assert record["other_header_names"] == []


def test_non_string_content_raises_rather_than_producing_a_junk_record():
    with pytest.raises(ZohoAPIError, match="Malformed original message"):
        normalize_email_source(MESSAGE_ID, None, include_raw=False)


# --- ZohoClient.get_email_source --------------------------------------


async def test_get_email_source_calls_the_documented_endpoint(respx_mock, zoho_client):
    # Note the URL has no folderId -- unlike every other per-message
    # endpoint, this one is account-scoped only.
    route = mock_source(respx_mock)

    record = await zoho_client.get_email_source(MESSAGE_ID)

    assert route.called
    assert record["id"] == MESSAGE_ID
    assert record["headers"]["subject"] == "Quarterly numbers"


async def test_get_email_source_passes_include_raw_through(respx_mock, zoho_client):
    mock_source(respx_mock)

    record = await zoho_client.get_email_source(MESSAGE_ID, include_raw=True)

    assert record["raw"] == SAMPLE_SOURCE


async def test_get_email_source_defaults_to_withholding_raw(respx_mock, zoho_client):
    mock_source(respx_mock)

    record = await zoho_client.get_email_source(MESSAGE_ID)

    assert record["raw"] is None


async def test_a_response_missing_content_is_a_clear_error(respx_mock, zoho_client):
    respx_mock.get(source_url()).mock(
        return_value=httpx.Response(200, json={"data": {"messageId": 1}})
    )

    with pytest.raises(ZohoAPIError, match="Malformed original message"):
        await zoho_client.get_email_source(MESSAGE_ID)


async def test_a_response_with_no_data_object_is_a_clear_error(respx_mock, zoho_client):
    respx_mock.get(source_url()).mock(
        return_value=httpx.Response(200, json={"status": {"code": 200}})
    )

    with pytest.raises(ZohoAPIError, match="Malformed original message"):
        await zoho_client.get_email_source(MESSAGE_ID)


async def test_get_email_source_wraps_a_zoho_error(respx_mock, zoho_client):
    respx_mock.get(source_url()).mock(
        return_value=httpx.Response(404, text="No such message")
    )

    with pytest.raises(ZohoAPIError, match="404"):
        await zoho_client.get_email_source(MESSAGE_ID)


@pytest.mark.parametrize("blank", ["", "   "])
async def test_get_email_source_rejects_a_blank_message_id(zoho_client, blank):
    with pytest.raises(ZohoAPIError, match="message_id"):
        await zoho_client.get_email_source(blank)
