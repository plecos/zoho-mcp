from zoho_mcp.releases import ReleaseCheckError
from zoho_mcp.tools.updates import check_for_updates

import pytest


class FakeReleaseChecker:
    def __init__(self, result=None, error=None):
        self.calls = 0
        self.result = result or {
            "installed_version": "0.1.0",
            "checked": True,
            "latest_version": "0.2.0",
            "update_available": True,
        }
        self.error = error

    async def check(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.result


async def test_check_for_updates_delegates_to_the_checker():
    checker = FakeReleaseChecker()

    result = await check_for_updates(checker)

    assert checker.calls == 1
    assert result == checker.result


async def test_check_for_updates_passes_the_not_checked_result_through():
    # The setting being off is a normal answer, not an error -- a wrapper
    # that turned it into one would make "off" look broken.
    checker = FakeReleaseChecker(
        result={
            "installed_version": "0.1.0",
            "checked": False,
            "reason": "Checking for updates is turned off.",
        }
    )

    assert await check_for_updates(checker) == checker.result


async def test_check_for_updates_lets_a_check_failure_propagate():
    # FastMCP renders the exception message as the tool error; the message is
    # written for that, so swallowing it here would lose the actionable part.
    checker = FakeReleaseChecker(error=ReleaseCheckError("Could not reach GitHub"))

    with pytest.raises(ReleaseCheckError, match="Could not reach GitHub"):
        await check_for_updates(checker)
