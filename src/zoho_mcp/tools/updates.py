"""MCP tool wrapper for release-version reporting.

Shapes LLM-facing output only. No HTTP and no version logic of its own --
``ReleaseChecker`` is injected by ``server.py``, never constructed here.
"""

from zoho_mcp.releases import ReleaseChecker


async def check_for_updates(checker: ReleaseChecker) -> dict:
    """Report whether a newer version of this server has been published.

    Reads nothing from the user's Zoho account. When the operator has opted
    in, it calls GitHub's releases API -- the one place this server talks to
    a host other than Zoho -- and compares the published version with the
    running one. Left off, it reports the installed version and where to look
    without making any network call.

    There is deliberately no tool that performs the upgrade. An installed
    MCPB bundle cannot replace itself: the host records a hash of what it
    installed, owns the server's process lifecycle, and a tool that fetched
    code from the network into the directory the server runs from would be
    reachable from a conversation, whose input includes untrusted email.

    Args:
        checker: injected release checker.

    Returns:
        Always ``installed_version`` and ``checked``. When ``checked`` is
        False -- the setting is off, or the installed version can't be read
        -- a ``reason`` and ``releases_url``, and no comparison was made.
        Otherwise ``update_available``, ``latest_version`` and
        ``release_url``, plus ``download_url`` and ``how_to_install`` steps
        when an update exists, or a ``note`` when the installed version is
        ahead of the latest release.

    Raises:
        ReleaseCheckError: if GitHub can't be reached, rate-limits the
            request, or returns a release that can't be read.
    """
    return await checker.check()
