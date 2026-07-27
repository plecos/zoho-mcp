"""Tests for release-version reporting.

Two things are under test here and they fail in opposite directions. The
comparison must not claim an update that doesn't exist (noise the user learns
to ignore), and it must not claim to be current when it isn't (silence that
defeats the point). Both are reachable from a malformed `tag_name`, which is
third-party data, so every parse failure is asserted to raise rather than
guess.

The GitHub payload shapes here were taken from a real
`GET /repos/plecos/zoho-mcp/releases/latest` response, not from the docs.
"""

import httpx
import pytest

from zoho_mcp.releases import (
    CACHE_TTL_SECONDS,
    GITHUB_LATEST_RELEASE_URL,
    RELEASES_PAGE_URL,
    ReleaseCheckError,
    ReleaseChecker,
    installed_version,
    parse_version,
)

INSTALLED = "0.1.0"


def release_payload(tag: str = "v0.2.0", *, assets: list[dict] | None = None) -> dict:
    """The subset of GitHub's release JSON this code reads.

    Field names confirmed live: `tag_name`, `html_url`, and each asset's
    `name`/`browser_download_url`.
    """
    version = tag.lstrip("v")
    if assets is None:
        assets = [
            {
                "name": f"zoho-mcp-{version}.mcpb",
                "browser_download_url": (
                    f"https://github.com/plecos/zoho-mcp/releases/download/"
                    f"{tag}/zoho-mcp-{version}.mcpb"
                ),
            }
        ]
    return {
        "tag_name": tag,
        "html_url": f"https://github.com/plecos/zoho-mcp/releases/tag/{tag}",
        "assets": assets,
    }


@pytest.fixture
async def http_client():
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def checker(http_client):
    """A checker with the network check turned on, as the setting does."""
    return ReleaseChecker(http_client, installed=INSTALLED, enabled=True)


@pytest.fixture
def disabled_checker(http_client):
    return ReleaseChecker(http_client, installed=INSTALLED, enabled=False)


@pytest.fixture
def latest_release_route(respx_mock):
    return respx_mock.get(GITHUB_LATEST_RELEASE_URL)


# ---------------------------------------------------------------------------
# parse_version -- pure, and the only thing standing between a malformed tag
# and a wrong answer.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0.1.0", (0, 1, 0)),
        ("v0.1.0", (0, 1, 0)),
        ("1.2.3", (1, 2, 3)),
        ("v10.20.30", (10, 20, 30)),
        ("0.0.0", (0, 0, 0)),
    ],
)
def test_parse_version_reads_a_plain_semver_tag(text, expected):
    assert parse_version(text) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "v",
        "latest",
        "1.2",
        "1",
        "1.2.3.4",
        "1.2.3-rc1",
        "v1.2.3+build",
        "1.2.x",
        "-1.2.3",
        " 1.2.3 ",
        None,
        3,
        ["1", "2", "3"],
    ],
)
def test_parse_version_rejects_anything_that_is_not_three_integers(bad):
    # Guessing at a tag we don't recognise is how a comparison silently
    # returns the wrong answer -- refuse instead.
    with pytest.raises(ReleaseCheckError):
        parse_version(bad)


def test_parse_version_compares_by_component_not_lexically():
    # "0.10.0" < "0.9.0" as strings, which is the classic way to tell someone
    # they're up to date when they are ten minor versions behind.
    assert parse_version("0.10.0") > parse_version("0.9.0")


def test_installed_version_reports_this_package_not_the_mcp_sdk():
    import tomllib
    from pathlib import Path

    pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
    declared = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"][
        "version"
    ]

    assert installed_version() == declared


# ---------------------------------------------------------------------------
# The gate. It lives in ReleaseChecker rather than the tool wrapper for the
# same reason send_email's does: the layer that issues the request is the one
# nothing can route around.
# ---------------------------------------------------------------------------


async def test_a_disabled_checker_issues_no_request_at_all(
    latest_release_route, disabled_checker
):
    # Unlike the send gate, "made no request" really is the invariant here --
    # the setting exists so that a user who hasn't opted in never touches
    # github.com. Assert on the route, not on the returned dict.
    await disabled_checker.check()

    assert not latest_release_route.called


async def test_a_disabled_checker_says_so_and_names_the_setting(disabled_checker):
    result = await disabled_checker.check()

    assert result["checked"] is False
    assert result["installed_version"] == INSTALLED
    assert "update_available" not in result
    assert "ZOHO_CHECK_FOR_UPDATES" in result["reason"]
    assert result["releases_url"] == RELEASES_PAGE_URL


async def test_an_unknown_installed_version_issues_no_request_either(
    latest_release_route, http_client
):
    # Without a version to compare against, the fetch could not answer the
    # question it exists to answer.
    checker = ReleaseChecker(http_client, installed="unknown", enabled=True)

    result = await checker.check()

    assert not latest_release_route.called
    assert result["checked"] is False
    assert "update_available" not in result


# ---------------------------------------------------------------------------
# The comparison.
# ---------------------------------------------------------------------------


async def test_a_newer_release_is_reported_as_available(latest_release_route, checker):
    latest_release_route.mock(return_value=httpx.Response(200, json=release_payload()))

    result = await checker.check()

    assert result["checked"] is True
    assert result["update_available"] is True
    assert result["installed_version"] == INSTALLED
    assert result["latest_version"] == "0.2.0"
    assert result["release_url"].endswith("/releases/tag/v0.2.0")
    assert result["download_url"].endswith("zoho-mcp-0.2.0.mcpb")


async def test_an_available_update_carries_the_install_quirks(
    latest_release_route, checker
):
    # Both of these are documented host behaviours that bite mid-upgrade, and
    # the moment someone reads this result is the moment they need them.
    latest_release_route.mock(return_value=httpx.Response(200, json=release_payload()))

    result = await checker.check()

    steps = " ".join(result["how_to_install"]).lower()
    assert "uninstall" in steps
    assert "client id" in steps


async def test_the_same_version_is_reported_as_up_to_date(
    latest_release_route, checker
):
    latest_release_route.mock(
        return_value=httpx.Response(200, json=release_payload(f"v{INSTALLED}"))
    )

    result = await checker.check()

    assert result["update_available"] is False
    assert result["latest_version"] == INSTALLED
    assert "how_to_install" not in result


async def test_an_installed_version_ahead_of_the_release_is_not_an_update(
    latest_release_route, http_client
):
    # A local build off main. Telling someone to "upgrade" to an older
    # bundle would be worse than saying nothing.
    checker = ReleaseChecker(http_client, installed="0.3.0", enabled=True)
    latest_release_route.mock(
        return_value=httpx.Response(200, json=release_payload("v0.2.0"))
    )

    result = await checker.check()

    assert result["update_available"] is False
    assert "newer" in result["note"].lower()


async def test_a_release_without_a_bundle_asset_still_reports_the_version(
    latest_release_route, checker
):
    latest_release_route.mock(
        return_value=httpx.Response(200, json=release_payload(assets=[]))
    )

    result = await checker.check()

    assert result["update_available"] is True
    assert "download_url" not in result
    assert result["release_url"].endswith("/releases/tag/v0.2.0")


async def test_the_bundle_asset_is_picked_out_from_among_others(
    latest_release_route, checker
):
    latest_release_route.mock(
        return_value=httpx.Response(
            200,
            json=release_payload(
                assets=[
                    {
                        "name": "checksums.txt",
                        "browser_download_url": "https://example.invalid/checksums.txt",
                    },
                    {
                        "name": "zoho-mcp-0.2.0.mcpb",
                        "browser_download_url": "https://example.invalid/bundle.mcpb",
                    },
                ]
            ),
        )
    )

    result = await checker.check()

    assert result["download_url"] == "https://example.invalid/bundle.mcpb"


# ---------------------------------------------------------------------------
# Transport and malformed upstream data. GitHub is a third party; assume it
# can omit a field, return the wrong type, or not answer at all.
# ---------------------------------------------------------------------------


async def test_an_unreachable_github_reports_that_and_the_releases_page(
    latest_release_route, checker
):
    latest_release_route.mock(side_effect=httpx.ConnectError("no route to host"))

    with pytest.raises(ReleaseCheckError) as excinfo:
        await checker.check()

    message = str(excinfo.value)
    assert "reach" in message.lower()
    assert RELEASES_PAGE_URL in message


async def test_a_rate_limited_check_says_it_is_rate_limited(
    latest_release_route, checker
):
    # 60 requests an hour, unauthenticated, per IP -- shared with anything
    # else on the machine that talks to the GitHub API.
    latest_release_route.mock(
        return_value=httpx.Response(
            403,
            headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1753639200"},
            json={"message": "API rate limit exceeded"},
        )
    )

    with pytest.raises(ReleaseCheckError, match="rate limit"):
        await checker.check()


async def test_a_404_says_there_are_no_releases_rather_than_not_found(
    latest_release_route, checker
):
    # What GitHub returns for a repo that has never published one.
    latest_release_route.mock(return_value=httpx.Response(404, json={}))

    with pytest.raises(ReleaseCheckError, match="no published release"):
        await checker.check()


async def test_an_unexpected_status_names_the_code_without_dumping_the_body(
    latest_release_route, checker
):
    latest_release_route.mock(
        return_value=httpx.Response(500, text="<html>secret internals</html>")
    )

    with pytest.raises(ReleaseCheckError) as excinfo:
        await checker.check()

    assert "500" in str(excinfo.value)
    assert "secret internals" not in str(excinfo.value)


async def test_a_non_json_body_raises_cleanly(latest_release_route, checker):
    latest_release_route.mock(return_value=httpx.Response(200, text="not json at all"))

    with pytest.raises(ReleaseCheckError):
        await checker.check()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"tag_name": None},
        {"tag_name": ""},
        {"tag_name": "nightly"},
        {"tag_name": 3},
        {"tag_name": "v0.2.0", "assets": "not a list"},
        {"tag_name": "v0.2.0", "assets": [{"name": "x.mcpb"}]},
        [],
    ],
)
async def test_malformed_release_data_raises_rather_than_leaking(
    latest_release_route, checker, payload
):
    latest_release_route.mock(return_value=httpx.Response(200, json=payload))

    with pytest.raises(ReleaseCheckError):
        await checker.check()


# ---------------------------------------------------------------------------
# Caching. A Claude Desktop process can stay up for days, so this is a TTL
# rather than the life-of-the-instance caching used for the mailbox timezone
# -- a check that can never see a release published after startup is a check
# that doesn't work.
#
# The clock is injected rather than travelled: `time_machine` doesn't patch
# `time.monotonic`, and monotonic is the right clock for a duration (an NTP
# correction must not extend or expire the cache).
# ---------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_a_second_check_inside_the_ttl_reuses_the_first_answer(
    latest_release_route, checker
):
    latest_release_route.mock(return_value=httpx.Response(200, json=release_payload()))

    first = await checker.check()
    second = await checker.check()

    assert latest_release_route.call_count == 1
    assert first == second


@pytest.mark.parametrize(
    ("elapsed", "expected_calls"),
    [
        (CACHE_TTL_SECONDS - 1, 1),
        # An answer that has *reached* the TTL is expired, not still valid.
        # Arbitrary, but pinned so it can't drift.
        (CACHE_TTL_SECONDS, 2),
        (CACHE_TTL_SECONDS + 1, 2),
    ],
)
async def test_the_cache_expires_once_the_ttl_has_elapsed(
    latest_release_route, http_client, elapsed, expected_calls
):
    clock = FakeClock()
    checker = ReleaseChecker(
        http_client, installed=INSTALLED, enabled=True, clock=clock
    )
    latest_release_route.mock(return_value=httpx.Response(200, json=release_payload()))

    await checker.check()
    clock.advance(elapsed)
    await checker.check()

    assert latest_release_route.call_count == expected_calls


async def test_a_failed_check_is_not_cached(latest_release_route, checker):
    # Caching a transient network failure for an hour would turn a blip into
    # an hour of the same error.
    latest_release_route.mock(side_effect=httpx.ConnectError("blip"))
    with pytest.raises(ReleaseCheckError):
        await checker.check()

    latest_release_route.mock(return_value=httpx.Response(200, json=release_payload()))
    result = await checker.check()

    assert result["update_available"] is True
