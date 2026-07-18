import json
from pathlib import Path

import pytest

from zoho_mcp.zoho.client import (
    ZohoAPIError,
    normalize_email_content,
    normalize_email_summary,
    normalize_event,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_normalize_email_summary_maps_core_fields():
    raw = load_fixture("mail_list_response.json")["data"][0]

    result = normalize_email_summary(raw)

    assert result["id"] == "1730217600123456789"
    assert result["from"] == "jamie.rivera@example.com"
    assert result["subject"] == "Q3 Roadmap Sync"
    assert result["snippet"] == "Let's sync on the Q3 roadmap tomorrow morning."
    assert result["folder_id"] == "1122334455"


def test_normalize_email_summary_converts_epoch_date_to_iso8601():
    raw = load_fixture("mail_list_response.json")["data"][0]

    result = normalize_email_summary(raw)

    assert result["date"] == "2024-10-29T16:00:00+00:00"


def test_normalize_email_summary_raises_clear_error_on_missing_field():
    raw = load_fixture("mail_list_response.json")["data"][0]
    del raw["subject"]

    with pytest.raises(ZohoAPIError, match="email summary"):
        normalize_email_summary(raw)


def test_normalize_email_summary_raises_clear_error_on_non_numeric_date():
    raw = load_fixture("mail_list_response.json")["data"][0]
    raw["sentDateInGMT"] = "not-a-number"

    with pytest.raises(ZohoAPIError, match="email summary"):
        normalize_email_summary(raw)


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


def test_normalize_event_maps_core_fields_and_converts_times():
    raw = load_fixture("calendar_events_response.json")["events"][0]

    result = normalize_event(raw)

    assert result["id"] == "evt-998877"
    assert result["title"] == "Q3 Roadmap Sync"
    assert result["start"] == "2024-10-29T16:00:00+00:00"
    assert result["end"] == "2024-10-29T17:00:00+00:00"


def test_normalize_event_extracts_attendees_with_status():
    raw = load_fixture("calendar_events_response.json")["events"][0]

    result = normalize_event(raw)

    assert result["attendees"] == [
        {"email": "jamie.rivera@example.com", "status": "accepted"},
        {"email": "morgan.lee@example.com", "status": "needsaction"},
    ]


def test_normalize_event_raises_clear_error_on_missing_field():
    raw = load_fixture("calendar_events_response.json")["events"][0]
    del raw["title"]

    with pytest.raises(ZohoAPIError, match="event"):
        normalize_event(raw)


def test_normalize_event_raises_clear_error_on_malformed_time_string():
    raw = load_fixture("calendar_events_response.json")["events"][0]
    raw["start"] = "not-a-timestamp"

    with pytest.raises(ZohoAPIError, match="event"):
        normalize_event(raw)


def test_normalize_event_raises_clear_error_on_malformed_attendee():
    raw = load_fixture("calendar_events_response.json")["events"][0]
    raw["attendees"] = [{"email": "jamie.rivera@example.com"}]  # missing "status"

    with pytest.raises(ZohoAPIError, match="event"):
        normalize_event(raw)
