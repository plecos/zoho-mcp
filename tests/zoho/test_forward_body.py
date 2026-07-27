"""The pure body-assembly half of forwarding.

Zoho's `action=forward` returns a content-free 500 on every documented-valid
request (see docs/zoho-api-notes.md), and Zoho's own web client doesn't use it
either -- it assembles the forward body in the browser and posts the result.
These functions are our equivalent of that assembly step, so they carry the
formatting the old read-then-recompose path destroyed.
"""

import pytest

from zoho_mcp.zoho.client import (
    ZohoAPIError,
    build_forward_body,
    forward_subject,
)

ORIGINAL = "<div><b>Quarterly numbers</b></div><table><tr><td>Q1</td></tr></table>"
HEADERS = {
    "from": "Jamie Rivera <jamie@example.com>",
    "to": "<ken@example.com>",
    "date": "Mon, 27 Jul 2026 08:24:04 -0700",
    "subject": "Quarterly numbers",
}


class TestForwardSubject:
    def test_prefixes_fwd(self):
        assert forward_subject("Quarterly numbers") == "Fwd: Quarterly numbers"

    @pytest.mark.parametrize(
        "already",
        ["Fwd: Quarterly numbers", "fwd: Quarterly numbers", "FWD: Quarterly numbers"],
    )
    def test_does_not_stack_a_second_prefix(self, already):
        # Forwarding a forward is ordinary; "Fwd: Fwd: Fwd:" is not.
        assert forward_subject(already) == already

    def test_strips_surrounding_whitespace_before_deciding(self):
        assert forward_subject("  Fwd: Numbers  ") == "Fwd: Numbers"

    @pytest.mark.parametrize("empty", ["", "   "])
    def test_handles_a_subjectless_original(self, empty):
        assert forward_subject(empty) == "Fwd:"

    def test_treats_a_missing_subject_as_subjectless(self):
        assert forward_subject(None) == "Fwd:"


class TestBuildForwardBody:
    def test_includes_the_note_the_original_and_the_header_block(self):
        body = build_forward_body(
            note="Passing this along.", headers=HEADERS, original_html=ORIGINAL
        )

        assert "Passing this along." in body
        assert ORIGINAL in body
        assert "Forwarded message" in body

    def test_quotes_the_original_in_a_blockquote(self):
        # Matches what Zoho's own client emits, so the draft renders in Zoho
        # Mail the way a natively-forwarded one does.
        body = build_forward_body(note="", headers=HEADERS, original_html=ORIGINAL)

        assert '<blockquote id="blockquote_zmail"' in body
        assert body.index("Forwarded message") < body.index("<blockquote")

    def test_header_block_carries_from_to_date_and_subject(self):
        body = build_forward_body(note="", headers=HEADERS, original_html=ORIGINAL)

        assert "From: Jamie Rivera &lt;jamie@example.com&gt;" in body
        assert "To: &lt;ken@example.com&gt;" in body
        assert "Date: Mon, 27 Jul 2026 08:24:04 -0700" in body
        assert "Subject: Quarterly numbers" in body

    def test_escapes_header_values_rather_than_letting_them_inject_markup(self):
        # Header values come from whoever sent the mail -- i.e. untrusted --
        # and land in a document the user will open.
        body = build_forward_body(
            note="",
            headers={**HEADERS, "from": '<script>alert("x")</script>'},
            original_html=ORIGINAL,
        )

        assert "<script>" not in body
        assert "&lt;script&gt;" in body

    def test_omits_header_lines_that_are_absent(self):
        body = build_forward_body(
            note="", headers={"from": "a@example.com"}, original_html=ORIGINAL
        )

        assert "From: a@example.com" in body
        assert "To:" not in body
        assert "Date:" not in body

    def test_leaves_zohos_relative_image_references_exactly_as_stored(self):
        # Verified end-to-end: Zoho resolves these into real MIME image parts
        # when the draft is sent, so rewriting them is work the vendor undoes.
        original = '<img src="/mail/ImageDisplay?na=1&amp;f=1.png&amp;mode=inline">'

        body = build_forward_body(note="", headers=HEADERS, original_html=original)

        assert original in body

    def test_empty_note_produces_a_body_that_is_still_a_valid_forward(self):
        body = build_forward_body(note="", headers=HEADERS, original_html=ORIGINAL)

        assert ORIGINAL in body
        assert "Forwarded message" in body

    def test_note_is_placed_above_the_quoted_original(self):
        body = build_forward_body(
            note="MY NOTE", headers=HEADERS, original_html=ORIGINAL
        )

        assert body.index("MY NOTE") < body.index("Forwarded message")

    @pytest.mark.parametrize("bad_html", [None, 42, {"content": "x"}])
    def test_rejects_a_non_string_original(self, bad_html):
        with pytest.raises(ZohoAPIError, match="content"):
            build_forward_body(note="", headers=HEADERS, original_html=bad_html)

    def test_survives_headers_holding_non_string_values(self):
        # Upstream data we don't control; a wrong type should not be a crash.
        body = build_forward_body(
            note="", headers={"from": 42, "subject": None}, original_html=ORIGINAL
        )

        assert "From: 42" in body
        assert "Subject:" not in body

    def test_rejects_headers_that_are_not_a_mapping(self):
        with pytest.raises(ZohoAPIError, match="headers"):
            build_forward_body(note="", headers=["from", "a@b.com"], original_html="")
