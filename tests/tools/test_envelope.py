"""The one place an enumeration tool's item count is computed.

Every list-returning tool routes its result through `counted`, so `count`
can't drift from the list it describes -- the failure this exists to stop is
a caller (human or LLM) tallying the items itself and getting a different
number than the one that's actually there.
"""

import pytest

from zoho_mcp.tools.envelope import counted


def test_counted_reports_the_length_of_the_list_it_returns():
    items = [{"id": "1"}, {"id": "2"}, {"id": "3"}]

    assert counted("emails", items) == {"emails": items, "count": 3}


def test_counted_reports_zero_for_an_empty_result():
    # An empty enumeration is a normal answer, not an error -- and "0" is
    # exactly the answer that most needs to come from the server rather
    # than from a caller eyeballing an empty list.
    assert counted("notes", []) == {"notes": [], "count": 0}


def test_counted_carries_extra_fields_alongside_the_count():
    items = [{"id": "1"}]

    assert counted("tasks", items, has_more=True) == {
        "tasks": items,
        "count": 1,
        "has_more": True,
    }


def test_counted_refuses_an_extra_field_that_would_overwrite_the_count():
    # Silently letting a caller pass its own `count` would reintroduce the
    # exact bug this helper exists to make impossible.
    with pytest.raises(ValueError, match="count"):
        counted("emails", [{"id": "1"}], count=99)


def test_counted_refuses_an_extra_field_that_would_overwrite_the_items():
    with pytest.raises(ValueError, match="emails"):
        counted("emails", [{"id": "1"}], **{"emails": []})
