"""Tests for the fields `normalize_email_summary` surfaces beyond the basics.

Zoho returns 21 keys per message; this returns 13. The gap is deliberate,
and so is each thing on either side of it -- what's here earns its place by
letting a model decide something (is there an attachment worth fetching, who
else is on this, which labels does it already carry), and what's missing is
missing for a stated reason.

Everything below was checked against 120 real messages across
`/messages/view` and `/messages/search`. The findings that shaped it:

- Key sets vary *per row* on both endpoints. `toAddress` was absent on one
  message, `labelId` on 27 of 60. Every added field has to tolerate absence.
- `toAddress`/`ccAddress` arrive HTML-entity-encoded (`&lt;a@b.com&gt;`),
  and an empty CC is the literal string `"Not Provided"`.
- `sender` is not an address. In 25 of 60 rows it had no `@` at all while
  `fromAddress` did -- it's a display name, and it equals `fromAddress`
  when there isn't one.

Deliberately not surfaced, because this account couldn't distinguish their
values: `flagid` (always `flag_not_set`), `priority` (always `"3"`),
`threadId`/`threadCount` (present on 2 of 60 rows, with `threadCount: "0"`
and `threadId == messageId`, which settles nothing), and `sentDateInGMT`,
which is wrong rather than merely unverified.
"""

import pytest

from zoho_mcp.zoho.client import ZohoAPIError, normalize_email_summary

TIMEZONE = "America/Los_Angeles"


def raw_email(**overrides) -> dict:
    base = {
        "messageId": "555000111",
        "fromAddress": "sender@example.com",
        "sender": "sender@example.com",
        "subject": "Quarterly numbers",
        "receivedTime": "1785032043525",
        "summary": "Here are the numbers",
        "status": "1",
        "folderId": "555222333",
        "toAddress": "&lt;me@example.com&gt;",
        "ccAddress": "Not Provided",
        "hasAttachment": "0",
        "size": "24825",
        "labelId": ["555444555"],
    }
    return {**base, **overrides}


def normalize(**overrides) -> dict:
    return normalize_email_summary(raw_email(**overrides), TIMEZONE)


def test_the_record_has_exactly_the_intended_fields():
    # Pinned as a set so widening it again is a deliberate act, not a drift.
    assert set(normalize()) == {
        "id",
        "from",
        "from_name",
        "subject",
        "date",
        "snippet",
        "folder_id",
        "read",
        "to",
        "cc",
        "has_attachment",
        "size_bytes",
        "label_ids",
    }


def test_recipients_are_decoded_and_unwrapped_into_a_list():
    record = normalize(
        toAddress="&lt;a@example.com&gt;,&lt;b@example.com&gt;",
        ccAddress="&lt;c@example.com&gt;",
    )

    assert record["to"] == ["a@example.com", "b@example.com"]
    assert record["cc"] == ["c@example.com"]


def test_recipients_keep_their_display_names():
    record = normalize(toAddress="&quot;Dana Lee&quot; &lt;dana@example.com&gt;")

    assert record["to"] == ['"Dana Lee" <dana@example.com>']


def test_the_not_provided_sentinel_becomes_an_empty_list():
    # Zoho writes an absent CC as literal text, so passing it through would
    # make the model report a recipient named "Not Provided".
    record = normalize(ccAddress="Not Provided")

    assert record["cc"] == []


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_recipient_strings_become_an_empty_list(value):
    assert normalize(toAddress=value)["to"] == []


def test_missing_recipient_keys_become_empty_lists():
    raw = raw_email()
    del raw["toAddress"]
    del raw["ccAddress"]

    record = normalize_email_summary(raw, TIMEZONE)

    assert record["to"] == []
    assert record["cc"] == []


@pytest.mark.parametrize(
    ("value", "expected"), [("1", True), ("0", False), ("", False), ("2", False)]
)
def test_has_attachment_is_a_bool(value, expected):
    assert normalize(hasAttachment=value)["has_attachment"] is expected


def test_has_attachment_defaults_to_false_when_absent():
    raw = raw_email()
    del raw["hasAttachment"]

    assert normalize_email_summary(raw, TIMEZONE)["has_attachment"] is False


def test_size_is_an_int_not_a_quoted_string():
    assert normalize(size="24825")["size_bytes"] == 24825


def test_missing_or_unparseable_size_becomes_none():
    raw = raw_email()
    del raw["size"]

    assert normalize_email_summary(raw, TIMEZONE)["size_bytes"] is None
    assert normalize(size="huge")["size_bytes"] is None


def test_label_ids_default_to_an_empty_list_when_absent():
    # 27 of 60 real messages had no labelId key at all.
    raw = raw_email()
    del raw["labelId"]

    assert normalize_email_summary(raw, TIMEZONE)["label_ids"] == []


def test_label_ids_pass_through_as_a_list():
    assert normalize(labelId=["a", "b"])["label_ids"] == ["a", "b"]


def test_a_non_list_label_id_is_wrapped_rather_than_exploded():
    # Zoho's own docs show an array; a bare string has not been observed,
    # but splitting a string into characters would be the worst outcome.
    assert normalize(labelId="solo")["label_ids"] == ["solo"]


def test_from_name_carries_the_display_name_when_there_is_one():
    record = normalize(sender="GitHub", fromAddress="noreply@github.com")

    assert record["from"] == "noreply@github.com"
    assert record["from_name"] == "GitHub"


def test_from_name_is_blank_when_sender_just_repeats_the_address():
    # True for 35 of 60 real messages -- repeating it would be noise.
    record = normalize(sender="a@example.com", fromAddress="a@example.com")

    assert record["from_name"] == ""


def test_from_name_is_blank_when_sender_is_absent():
    raw = raw_email()
    del raw["sender"]

    assert normalize_email_summary(raw, TIMEZONE)["from_name"] == ""


def test_from_name_is_html_decoded():
    assert (
        normalize(sender="Ben &amp; Co", fromAddress="b@example.com")["from_name"]
        == "Ben & Co"
    )


def test_the_original_fields_are_unchanged():
    record = normalize()

    assert record["id"] == "555000111"
    assert record["from"] == "sender@example.com"
    assert record["subject"] == "Quarterly numbers"
    assert record["folder_id"] == "555222333"
    assert record["read"] is True
    assert record["date"].startswith("2026-")


def test_a_missing_core_field_still_raises():
    # The added fields all degrade gracefully; the ones the record can't be
    # built without must not start doing the same.
    raw = raw_email()
    del raw["messageId"]

    with pytest.raises(ZohoAPIError, match="Malformed email summary"):
        normalize_email_summary(raw, TIMEZONE)


# The padding-stripping toggle originally covered `get_email` bodies only, so
# every listing carried the noise regardless. Observed live: a real marketing
# message's snippet was mostly U+034F, which is worse than a body -- a body is
# something you deliberately opened, while snippets arrive for every message
# in every listing.
COMBINING_GRAPHEME_JOINER = chr(0x034F)


def test_snippet_padding_is_kept_by_default():
    padded = f"Deal{COMBINING_GRAPHEME_JOINER} inside{COMBINING_GRAPHEME_JOINER}"

    record = normalize_email_summary(raw_email(summary=padded), TIMEZONE)

    assert record["snippet"] == padded


def test_snippet_padding_is_stripped_when_enabled():
    padded = f"Deal{COMBINING_GRAPHEME_JOINER} inside{COMBINING_GRAPHEME_JOINER}"

    record = normalize_email_summary(
        raw_email(summary=padded), TIMEZONE, strip_invisible_chars=True
    )

    assert record["snippet"] == "Deal inside"


@pytest.mark.parametrize("codepoint", [0x200B, 0xFEFF, 0x2060])
def test_every_padding_character_is_stripped_from_snippets(codepoint):
    record = normalize_email_summary(
        raw_email(summary=f"a{chr(codepoint)}b"), TIMEZONE, strip_invisible_chars=True
    )

    assert record["snippet"] == "ab"


@pytest.mark.parametrize("codepoint", [0x200C, 0x200D])
def test_load_bearing_joiners_survive_snippet_stripping(codepoint):
    # ZWJ/ZWNJ carry real meaning in emoji sequences and in Persian and Indic
    # scripts. Stripping them corrupts content rather than tidying it, which
    # is why they are absent from _INVISIBLE_PADDING_CHARS -- assert it here
    # too, since a second call site is a second chance to get it wrong.
    joiner = chr(codepoint)

    record = normalize_email_summary(
        raw_email(summary=f"a{joiner}b"), TIMEZONE, strip_invisible_chars=True
    )

    assert record["snippet"] == f"a{joiner}b"


def test_subject_is_left_alone_even_when_stripping():
    # Deliberately narrower than the body/snippet case. The padding is a
    # preview-text trick, and a padded subject would look broken to a human
    # in any mail client, so senders don't do it. Widen this if one turns up.
    padded = f"Sale{COMBINING_GRAPHEME_JOINER}"

    record = normalize_email_summary(
        raw_email(subject=padded), TIMEZONE, strip_invisible_chars=True
    )

    assert record["subject"] == padded
