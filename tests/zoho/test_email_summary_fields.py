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
        "flagid": "flag_not_set",
        "priority": "3",
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
        "flag",
        "priority",
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


# Whitespace collapsing is unconditional, unlike the invisible-character
# stripping above. Deleting invisible characters changes content, so it stays
# opt-in; collapsing a run of spaces in a *preview string* discards nothing --
# every mail client does it visually -- and it catches the padding variants
# that have width, which no list of codepoints would keep up with. Observed
# live: after stripping U+034F, a real snippet was still ~139 characters of
# U+2007 FIGURE SPACE around 40 characters of text.
def test_runs_of_spaces_collapse_to_one():
    record = normalize_email_summary(
        raw_email(summary="See   details      about  you"), TIMEZONE
    )

    assert record["snippet"] == "See details about you"


@pytest.mark.parametrize(
    "codepoint",
    [0x2007, 0x00A0, 0x2002, 0x2003, 0x200A, 0x3000],
    ids=["figure", "nbsp", "en", "em", "hair", "ideographic"],
)
def test_every_space_variant_collapses(codepoint):
    space = chr(codepoint)
    record = normalize_email_summary(
        raw_email(summary=f"Deal{space}{space}{space}inside"), TIMEZONE
    )

    assert record["snippet"] == "Deal inside"


def test_tabs_and_newlines_collapse_too():
    record = normalize_email_summary(
        raw_email(summary="Deal\r\n\tinside\n\nnow"), TIMEZONE
    )

    assert record["snippet"] == "Deal inside now"


def test_leading_and_trailing_whitespace_is_trimmed():
    record = normalize_email_summary(raw_email(summary="   Deal inside   "), TIMEZONE)

    assert record["snippet"] == "Deal inside"


def test_collapsing_happens_even_with_stripping_off():
    # The point of making this unconditional: nobody has to find a flag.
    record = normalize_email_summary(
        raw_email(summary="Deal      inside"), TIMEZONE, strip_invisible_chars=False
    )

    assert record["snippet"] == "Deal inside"


def test_padding_is_stripped_before_whitespace_is_collapsed():
    # Order is load-bearing. Collapsing first leaves the invisible characters
    # as non-space tokens; removing them afterwards then leaves the double
    # spaces they were separating. Stripping first avoids that entirely.
    padded = (
        f"Deal{COMBINING_GRAPHEME_JOINER} {COMBINING_GRAPHEME_JOINER} "
        f"{COMBINING_GRAPHEME_JOINER} inside"
    )

    record = normalize_email_summary(
        raw_email(summary=padded), TIMEZONE, strip_invisible_chars=True
    )

    assert record["snippet"] == "Deal inside"


def test_single_spaces_between_words_are_untouched():
    record = normalize_email_summary(raw_email(summary="Deal inside now"), TIMEZONE)

    assert record["snippet"] == "Deal inside now"


def test_a_whitespace_only_snippet_becomes_empty():
    record = normalize_email_summary(raw_email(summary="  \r\n\t  "), TIMEZONE)

    assert record["snippet"] == ""


def test_subject_whitespace_is_left_alone():
    # Same scope decision as the stripping: subjects are shown to humans in
    # every mail client, so senders don't pad them, and a subject's exact
    # spacing is more plausibly deliberate than a generated preview's.
    record = normalize_email_summary(raw_email(subject="Big   Sale"), TIMEZONE)

    assert record["subject"] == "Big   Sale"


# flagid and priority were withheld until now because a real mailbox couldn't
# distinguish their values -- flagid was `flag_not_set` on all 120 messages
# sampled and priority was "3" on all of them. With deliberate test data they
# resolve:
#
#   priority "2" <-> `Importance: High`   header on the same message
#   priority "3" <-> `Importance: Medium` header on the same message
#
# read out of the message source, which pins Zoho's `priority` to the
# X-Priority convention where lower means more important. flagid came back as
# 'important' -- a flag *type name*, not a boolean.
def test_priority_is_a_label_not_a_number():
    assert normalize_email_summary(raw_email(priority="2"), TIMEZONE)["priority"] == (
        "high"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1", "highest"),
        ("2", "high"),  # verified live against `Importance: High`
        ("3", "normal"),  # verified live against `Importance: Medium`
        ("4", "low"),  # verified live against `Importance: Low`
        ("5", "lowest"),
    ],
)
def test_the_priority_scale(value, expected):
    record = normalize_email_summary(raw_email(priority=value), TIMEZONE)

    assert record["priority"] == expected


def test_an_absent_priority_reads_as_normal():
    raw = raw_email()
    del raw["priority"]

    assert normalize_email_summary(raw, TIMEZONE)["priority"] == "normal"


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_priority_reads_as_normal(blank):
    # Blank is absent, not unrecognized -- there's nothing to pass through.
    record = normalize_email_summary(raw_email(priority=blank), TIMEZONE)

    assert record["priority"] == "normal"


@pytest.mark.parametrize("value", ["7", "urgent", "0"])
def test_an_unrecognized_priority_is_passed_through_not_guessed(value):
    # Calling an unknown value "normal" would hide it. Anything outside the
    # documented scale comes through as-is, so it's visible rather than
    # silently flattened into a wrong answer.
    record = normalize_email_summary(raw_email(priority=value), TIMEZONE)

    assert record["priority"] == value


def test_the_flag_name_is_surfaced():
    record = normalize_email_summary(raw_email(flagid="important"), TIMEZONE)

    assert record["flag"] == "important"


@pytest.mark.parametrize("value", ["flag_not_set", "", "   "])
def test_an_unflagged_message_has_an_empty_flag(value):
    assert normalize_email_summary(raw_email(flagid=value), TIMEZONE)["flag"] == ""


def test_an_absent_flagid_reads_as_unflagged():
    raw = raw_email()
    del raw["flagid"]

    assert normalize_email_summary(raw, TIMEZONE)["flag"] == ""


@pytest.mark.parametrize("name", ["important", "followup", "someflagnobodyhasseen"])
def test_flag_names_pass_through_unchanged(name):
    # 'important' and 'followup' are both observed live. The third is the
    # point of the design: this originally guessed "follow_up", and the real
    # value has no separator -- so an enumeration built from that guess would
    # have silently dropped a flag the user had actually set.
    record = normalize_email_summary(raw_email(flagid=name), TIMEZONE)

    assert record["flag"] == name
