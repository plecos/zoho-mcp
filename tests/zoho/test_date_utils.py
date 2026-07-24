from datetime import date, datetime, timezone

import time_machine

from zoho_mcp.zoho.client import _today_in_timezone


def test_today_in_timezone_returns_utc_date_for_utc():
    with time_machine.travel(
        datetime(2026, 7, 18, 15, 0, 0, tzinfo=timezone.utc), tick=False
    ):
        assert _today_in_timezone("UTC") == date(2026, 7, 18)


def test_today_in_timezone_resolves_pacific_boundary_not_utc_boundary():
    # 2026-07-18T02:00:00 UTC is 2026-07-17T19:00:00-07:00 in Los Angeles --
    # UTC has already rolled over to the 18th, but it's still the evening of
    # the 17th in Pacific time. This is exactly the case that caused
    # search_emails(days_back=0) to return the wrong day when "today" was
    # computed naively from a UTC clock instead of the mailbox's timezone.
    with time_machine.travel(
        datetime(2026, 7, 18, 2, 0, 0, tzinfo=timezone.utc), tick=False
    ):
        assert _today_in_timezone("America/Los_Angeles") == date(2026, 7, 17)


def test_today_in_timezone_matches_utc_date_once_past_the_boundary():
    # Later the same UTC day, once Pacific has also rolled over to the 18th.
    with time_machine.travel(
        datetime(2026, 7, 18, 15, 0, 0, tzinfo=timezone.utc), tick=False
    ):
        assert _today_in_timezone("America/Los_Angeles") == date(2026, 7, 18)
