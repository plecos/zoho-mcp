import json
from pathlib import Path

import pytest

from zoho_mcp.zoho.client import (
    ZohoAPIError,
    normalize_email_content,
    normalize_email_summary,
    normalize_event,
    normalize_event_detail,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"
MAILBOX_TZ = "America/Los_Angeles"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_normalize_email_summary_maps_core_fields():
    raw = load_fixture("mail_list_response.json")["data"][0]

    result = normalize_email_summary(raw, MAILBOX_TZ)

    assert result["id"] == "1730217600123456789"
    assert result["from"] == "jamie.rivera@example.com"
    assert result["subject"] == "Q3 Roadmap Sync"
    assert result["snippet"] == "Let's sync on the Q3 roadmap tomorrow morning."
    assert result["folder_id"] == "1122334455"


def test_normalize_email_summary_marks_read_when_status_is_1():
    raw = load_fixture("mail_list_response.json")["data"][0]
    raw["status"] = "1"

    result = normalize_email_summary(raw, MAILBOX_TZ)

    assert result["read"] is True


def test_normalize_email_summary_marks_unread_when_status_is_0():
    raw = load_fixture("mail_list_response.json")["data"][0]
    raw["status"] = "0"

    result = normalize_email_summary(raw, MAILBOX_TZ)

    assert result["read"] is False


def test_normalize_email_summary_treats_unrecognized_status_as_unread():
    # Only "0" (confirmed on a freshly-sent, unopened test email) and "1"
    # (confirmed on already-read mail) are verified. Default unknown
    # values to unread -- the safer failure mode is "flagged as unread
    # when it's actually read" over silently hiding a real unread email.
    raw = load_fixture("mail_list_response.json")["data"][0]
    raw["status"] = "3"

    result = normalize_email_summary(raw, MAILBOX_TZ)

    assert result["read"] is False


def test_normalize_email_summary_converts_epoch_date_to_mailbox_timezone():
    raw = load_fixture("mail_list_response.json")["data"][0]

    result = normalize_email_summary(raw, MAILBOX_TZ)

    # Returned in the mailbox's own offset, not UTC -- so an LLM client
    # never has to convert (or forget to convert) a timezone it doesn't
    # actually know.
    assert result["date"] == "2024-10-29T09:00:00-07:00"


def test_normalize_email_summary_raises_clear_error_on_missing_field():
    raw = load_fixture("mail_list_response.json")["data"][0]
    del raw["subject"]

    with pytest.raises(ZohoAPIError, match="email summary"):
        normalize_email_summary(raw, MAILBOX_TZ)


def test_normalize_email_summary_raises_clear_error_on_non_numeric_date():
    raw = load_fixture("mail_list_response.json")["data"][0]
    raw["receivedTime"] = "not-a-number"

    with pytest.raises(ZohoAPIError, match="email summary"):
        normalize_email_summary(raw, MAILBOX_TZ)


def test_normalize_email_content_strips_html_to_plain_text():
    raw = load_fixture("mail_content_response.json")["data"]

    result = normalize_email_content(raw)

    assert result["id"] == "1730217600123456789"
    assert "<" not in result["text"]
    assert ">" not in result["text"]
    assert "Hi Ken" in result["text"]
    assert "Q3 roadmap" in result["text"]
    assert "Jamie" in result["text"]


def test_normalize_email_content_raises_clear_error_on_missing_field():
    raw = load_fixture("mail_content_response.json")["data"]
    del raw["content"]

    with pytest.raises(ZohoAPIError, match="email content"):
        normalize_email_content(raw)


def test_normalize_email_content_keeps_invisible_padding_by_default():
    combining_grapheme_joiner = chr(0x034F)
    raw = {"messageId": "1", "content": f"<p>Hi{combining_grapheme_joiner} Ken</p>"}

    result = normalize_email_content(raw)

    assert combining_grapheme_joiner in result["text"]


def test_normalize_email_content_strips_invisible_padding_when_enabled():
    combining_grapheme_joiner = chr(0x034F)
    raw = {"messageId": "1", "content": f"<p>Hi{combining_grapheme_joiner} Ken</p>"}

    result = normalize_email_content(raw, strip_invisible_chars=True)

    assert combining_grapheme_joiner not in result["text"]
    assert "Hi" in result["text"] and "Ken" in result["text"]


def test_normalize_email_content_preserves_zwj_emoji_sequences_even_when_stripping():
    # Family emoji is 3 codepoints joined by ZERO WIDTH JOINER (U+200D).
    # Stripping ZWJ would silently break the emoji into separate glyphs.
    zwj = chr(0x200D)
    family_emoji = "\U0001f468" + zwj + "\U0001f469" + zwj + "\U0001f467"
    raw = {"messageId": "1", "content": f"<p>{family_emoji}</p>"}

    result = normalize_email_content(raw, strip_invisible_chars=True)

    assert result["text"] == family_emoji


def test_normalize_event_maps_core_fields_and_converts_times():
    raw = load_fixture("calendar_events_response.json")["events"][0]

    result = normalize_event(raw, MAILBOX_TZ)

    assert result["id"] == "evt-998877"
    assert result["title"] == "Q3 Roadmap Sync"
    # Source is already -0700 (Pacific Daylight Time on this date), so
    # converting to America/Los_Angeles round-trips to the same wall time.
    assert result["start"] == "2024-10-29T09:00:00-07:00"
    assert result["end"] == "2024-10-29T10:00:00-07:00"


def test_normalize_event_extracts_attendees_with_status():
    raw = load_fixture("calendar_events_response.json")["events"][0]

    result = normalize_event(raw, MAILBOX_TZ)

    assert result["attendees"] == [
        {"email": "jamie.rivera@example.com", "status": "accepted"},
        {"email": "morgan.lee@example.com", "status": "needsaction"},
    ]


def test_normalize_event_raises_clear_error_on_missing_field():
    raw = load_fixture("calendar_events_response.json")["events"][0]
    del raw["title"]

    with pytest.raises(ZohoAPIError, match="event"):
        normalize_event(raw, MAILBOX_TZ)


def test_normalize_event_raises_clear_error_on_malformed_time_string():
    raw = load_fixture("calendar_events_response.json")["events"][0]
    raw["dateandtime"]["start"] = "not-a-timestamp"

    with pytest.raises(ZohoAPIError, match="event"):
        normalize_event(raw, MAILBOX_TZ)


def test_normalize_event_handles_all_day_date_only_format():
    raw = load_fixture("calendar_events_response.json")["events"][1]

    result = normalize_event(raw, MAILBOX_TZ)

    assert result["id"] == "evt-allday-1"
    assert result["title"] == "Company Holiday"
    assert result["start"] == "2024-11-02"
    assert result["end"] == "2024-11-03"


def test_normalize_event_raises_clear_error_on_malformed_attendee():
    raw = load_fixture("calendar_events_response.json")["events"][0]
    raw["attendees"] = [{"email": "jamie.rivera@example.com"}]  # missing "status"

    with pytest.raises(ZohoAPIError, match="event"):
        normalize_event(raw, MAILBOX_TZ)


def test_normalize_event_detail_maps_core_fields():
    raw = load_fixture("calendar_event_detail_response.json")["events"][0]

    result = normalize_event_detail(raw)

    assert result["id"] == "evt-recurring-1"
    assert result["title"] == "Team Sync"
    assert result["organizer"] == "user@example.com"
    assert result["location"] == "https://meet.example.com/abc"
    assert result["recurrence"] == "FREQ=WEEKLY;INTERVAL=1;BYDAY=MO"


def test_normalize_event_detail_extracts_full_attendee_list():
    # Confirmed live: list_events' per-occurrence attendees can show only
    # the caller's own entry, while the single-event endpoint returns
    # every invitee -- this is the whole reason get_event exists.
    raw = load_fixture("calendar_event_detail_response.json")["events"][0]

    result = normalize_event_detail(raw)

    assert result["attendees"] == [
        {"email": "jamie.rivera@example.com", "status": "accepted"},
        {"email": "morgan.lee@example.com", "status": "needsaction"},
    ]


def test_normalize_event_detail_treats_null_description_as_empty():
    # Zoho sends an explicit null (not an absent key) for an unset
    # description -- confirmed against the live API. raw.get(key, "")
    # would not catch this since the key is present.
    raw = load_fixture("calendar_event_detail_response.json")["events"][0]

    result = normalize_event_detail(raw)

    assert result["description"] == ""


def test_normalize_event_detail_defaults_missing_optional_fields():
    raw = {"uid": "evt-1", "title": "Solo block", "organizer": "user@example.com"}

    result = normalize_event_detail(raw)

    assert result["location"] == ""
    assert result["description"] == ""
    assert result["recurrence"] == ""
    assert result["attendees"] == []


def test_normalize_event_detail_raises_clear_error_on_missing_field():
    raw = load_fixture("calendar_event_detail_response.json")["events"][0]
    del raw["organizer"]

    with pytest.raises(ZohoAPIError, match="event"):
        normalize_event_detail(raw)


def test_normalize_event_detail_raises_clear_error_on_malformed_attendee():
    raw = load_fixture("calendar_event_detail_response.json")["events"][0]
    raw["attendees"] = [{"email": "jamie.rivera@example.com"}]  # missing "status"

    with pytest.raises(ZohoAPIError, match="event"):
        normalize_event_detail(raw)
