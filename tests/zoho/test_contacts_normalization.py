import json
from pathlib import Path

import pytest

from zoho_mcp.zoho.client import ZohoAPIError
from zoho_mcp.zoho.contacts_client import normalize_contact

FIXTURES = Path(__file__).parent.parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_normalize_contact_maps_core_fields():
    raw = load_fixture("contacts_list_response.json")["contacts"][0]

    result = normalize_contact(raw, "personal")

    assert result["id"] == "44205000000003001"
    assert result["first_name"] == "Jamie"
    assert result["last_name"] == "Rivera"
    assert result["emails"] == ["jamie.rivera@example.com", "jamie.r.alt@example.com"]


def test_normalize_contact_tags_scope():
    # contact_id is not globally unique across scopes -- confirmed live,
    # fetching an org contact_id through the personal endpoint returns a
    # 200 with a different, partial record rather than a 404. Every
    # normalized contact must carry the scope it actually came from so
    # get_contact can be pointed at the right endpoint later.
    raw = load_fixture("contacts_list_response.json")["contacts"][0]

    assert normalize_contact(raw, "personal")["scope"] == "personal"
    assert normalize_contact(raw, "organization")["scope"] == "organization"


def test_normalize_contact_maps_phones_notes_nickname_and_birthday():
    raw = load_fixture("contacts_list_response.json")["contacts"][0]

    result = normalize_contact(raw, "personal")

    assert result["phones"] == [{"number": "5551234567", "type": "mobile"}]
    assert result["notes"] == "Met at the Q3 offsite"
    assert result["nickname"] == "Jam"
    assert result["birthday"] == "1975-10-16"


def test_normalize_contact_formats_birthday_without_year():
    # Many people record just month/day, omitting year for privacy --
    # confirmed as a real, valid combination against the live API.
    raw = {
        "contact_id": "1",
        "first_name": "Solo",
        "emails": [],
        "birth_month": "3",
        "birth_day": "5",
    }

    result = normalize_contact(raw, "personal")

    assert result["birthday"] == "03-05"


def test_normalize_contact_defaults_missing_optional_fields():
    # Real contacts frequently omit last_name/company entirely (not just
    # empty strings) -- confirmed against the live API.
    raw = {"contact_id": "1", "first_name": "Solo", "emails": []}

    result = normalize_contact(raw, "personal")

    assert result["last_name"] == ""
    assert result["company"] == ""
    assert result["emails"] == []
    assert result["phones"] == []
    assert result["notes"] == ""
    assert result["nickname"] == ""
    assert result["birthday"] == ""


def test_normalize_contact_treats_null_phones_and_emails_as_empty():
    # Zoho returns an explicit null (not an absent key) for list-type
    # fields with no data -- confirmed against the live API. raw.get(key, [])
    # would not catch this since the key is present.
    raw = {
        "contact_id": "1",
        "first_name": "Solo",
        "emails": None,
        "phones": None,
    }

    result = normalize_contact(raw, "personal")

    assert result["emails"] == []
    assert result["phones"] == []


def test_normalize_contact_raises_clear_error_on_missing_id():
    raw = load_fixture("contacts_list_response.json")["contacts"][0]
    del raw["contact_id"]

    with pytest.raises(ZohoAPIError, match="contact"):
        normalize_contact(raw, "personal")


def test_normalize_contact_raises_clear_error_on_malformed_email_entry():
    raw = load_fixture("contacts_list_response.json")["contacts"][0]
    raw["emails"] = [{"not_email_id": "oops"}]

    with pytest.raises(ZohoAPIError, match="contact"):
        normalize_contact(raw, "personal")


# _format_birthday calls int() on these, so a non-numeric value raised a bare
# ValueError. That would break an entire search_contacts result -- every other
# contact included -- because of one odd record.
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("birth_month", "Jan"),
        ("birth_day", "third"),
        ("birth_year", "unknown"),
    ],
)
def test_normalize_contact_raises_clear_error_on_non_numeric_birthday(field, value):
    raw = {"contact_id": "1", "first_name": "A", "birth_month": "3", "birth_day": "4"}
    raw[field] = value

    with pytest.raises(ZohoAPIError, match="contact"):
        normalize_contact(raw, "personal")
