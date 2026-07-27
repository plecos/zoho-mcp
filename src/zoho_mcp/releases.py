"""Which version is installed, and which one is published.

Deliberately not under ``zoho/``: GitHub is a different vendor with its own
contract, so this shares no base URL, no auth header and no error type with
the Zoho clients.

Reporting only. This module never downloads or installs anything -- see
README's "Updating" section for why an MCPB bundle cannot safely upgrade
itself.
"""

import time
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version

import httpx

DISTRIBUTION_NAME = "zoho-mcp"
GITHUB_LATEST_RELEASE_URL = (
    "https://api.github.com/repos/plecos/zoho-mcp/releases/latest"
)
RELEASES_PAGE_URL = "https://github.com/plecos/zoho-mcp/releases"
BUNDLE_SUFFIX = ".mcpb"
UNKNOWN_VERSION = "unknown"
CACHE_TTL_SECONDS = 3600
_REQUEST_TIMEOUT_SECONDS = 10.0

# GitHub serves this endpoint to anyone, but at 60 requests an hour per IP --
# shared with everything else on the machine that talks to its API. Hence the
# cache, and hence a rate-limit message that says so rather than "403".
_GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# Said at the moment someone is about to act on it. Both are host behaviours
# observed on a real Claude Desktop install and written up in README --
# they're what turns a routine upgrade into a re-entry of credentials.
_INSTALL_STEPS = (
    "Download the .mcpb from the release page.",
    "In Claude Desktop, disable this extension and restart Claude before"
    " installing over it -- a running server holds files in its own directory"
    " open, and the uninstall can fail.",
    "Open the downloaded .mcpb and confirm the install. Installing a bundle"
    " whose version matches the installed one does NOT replace it -- the host"
    " uninstalls the extension instead, so check the version first.",
    "Re-enter your Zoho client id and client secret: the host deletes an"
    " extension's settings when it replaces it. Your authorization survives"
    " (the refresh token is in the OS credential store), so there is no need"
    " to run `authenticate` again.",
    "Quit and reopen Claude Desktop. Settings are read only when the server"
    " process starts.",
)


class ReleaseCheckError(Exception):
    """Raised when the published release can't be fetched or can't be read."""


def installed_version() -> str:
    """Return this package's version, or ``"unknown"`` if it can't be read.

    Reads the installed distribution's metadata rather than any file in the
    tree, which is what makes it correct inside an MCPB bundle: the host
    resolves the project into the extension's own virtualenv at install time.
    """
    try:
        return distribution_version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return UNKNOWN_VERSION


def parse_version(text: object) -> tuple[int, int, int]:
    """Parse ``0.1.0`` or ``v0.1.0`` into a comparable ``(major, minor, patch)``.

    Hand-rolled rather than pulling in ``packaging`` as a runtime dependency:
    ``pyproject.toml`` ships inside the bundle and every installer pays for
    what's in it, which is a lot to add for one comparison over tags this
    project controls the format of.

    Anything else raises. A tag is third-party data, and a parser that
    guesses is a parser that reports "up to date" to someone who isn't.

    Raises:
        ReleaseCheckError: if ``text`` isn't exactly three dot-separated
            non-negative integers, optionally prefixed with ``v``.
    """
    if not isinstance(text, str):
        raise ReleaseCheckError(f"version must be a string, got {type(text).__name__}")

    parts = text.removeprefix("v").split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ReleaseCheckError(
            f"could not read {text!r} as a version number -- expected "
            "three dot-separated integers, e.g. 0.2.0"
        )
    major, minor, patch = (int(part) for part in parts)
    return major, minor, patch


class ReleaseChecker:
    """Compares the installed version against the latest published release.

    The opt-in flag is enforced here rather than in the tool wrapper, for the
    same reason ``send_email``'s gate lives in ``ZohoClient``: this is the
    layer that issues the request, so nothing can route around it. Disabled,
    ``check`` makes no network call at all.
    """

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        *,
        installed: str | None = None,
        enabled: bool = False,
        clock: Callable[[], float] = time.monotonic,
        ttl_seconds: float = CACHE_TTL_SECONDS,
    ) -> None:
        """
        Args:
            http_client: shared client, reused rather than opening another.
            installed: the version to compare against; defaults to this
                package's own.
            enabled: whether the operator has opted in to the GitHub call.
            clock: monotonic time source, injected for tests.
            ttl_seconds: how long a successful answer stays cached.
        """
        self._http_client = http_client
        self._installed = installed if installed is not None else installed_version()
        self._enabled = enabled
        self._clock = clock
        self._ttl_seconds = ttl_seconds
        self._cached: dict | None = None
        self._cached_at = 0.0

    async def check(self) -> dict:
        """Report the installed version and, if enabled, the published one.

        Returns:
            Always ``installed_version`` and ``checked``. When ``checked`` is
            False (the setting is off, or the installed version can't be
            read) it carries ``reason`` and ``releases_url`` and no
            comparison. Otherwise ``update_available``, ``latest_version``
            and ``release_url``, plus ``download_url`` and ``how_to_install``
            when an update exists, or ``note`` when the installed version is
            ahead of the release.

        Raises:
            ReleaseCheckError: if GitHub can't be reached, refuses the
                request, or returns something unreadable.
        """
        if not self._enabled:
            return self._not_checked(
                "Checking for updates is turned off. Turn on "
                '"Check for new versions of this extension" in this '
                "extension's settings (ZOHO_CHECK_FOR_UPDATES=true outside a "
                "bundle install) to enable it, or check the releases page "
                "yourself."
            )
        if self._installed == UNKNOWN_VERSION:
            return self._not_checked(
                "Could not determine which version of zoho-mcp is installed, "
                "so there is nothing to compare a release against. Check the "
                "releases page yourself."
            )

        if self._cached is not None and self._clock() - self._cached_at < (
            self._ttl_seconds
        ):
            return self._cached

        result = self._compare(await self._fetch_latest_release())
        # Only successes are cached; a transient failure that stuck for an
        # hour would turn a blip into an outage.
        self._cached = result
        self._cached_at = self._clock()
        return result

    def _not_checked(self, reason: str) -> dict:
        return {
            "installed_version": self._installed,
            "checked": False,
            "reason": reason,
            "releases_url": RELEASES_PAGE_URL,
        }

    async def _fetch_latest_release(self) -> dict:
        """GET the latest release, translating every failure into a message.

        ``/releases/latest`` excludes drafts and prereleases by definition, so
        no filtering is needed here.
        """
        try:
            response = await self._http_client.get(
                GITHUB_LATEST_RELEASE_URL,
                headers=_GITHUB_HEADERS,
                timeout=_REQUEST_TIMEOUT_SECONDS,
            )
        except httpx.RequestError as e:
            raise ReleaseCheckError(
                f"Could not reach GitHub to check for updates ({e.__class__.__name__})."
                f" Check the releases page yourself: {RELEASES_PAGE_URL}"
            ) from e

        if response.status_code == 404:
            raise ReleaseCheckError(
                "GitHub reports no published release for zoho-mcp yet, so "
                "there is nothing to compare against."
            )
        if response.status_code in (403, 429) and (
            response.headers.get("x-ratelimit-remaining") == "0"
        ):
            raise ReleaseCheckError(
                "GitHub's rate limit for unauthenticated requests (60 an hour "
                "per IP address) has been reached. Try again later, or check "
                f"the releases page: {RELEASES_PAGE_URL}"
            )
        if response.status_code >= 400:
            # Deliberately no body: an error page is unbounded, untrusted
            # text, and it would be going straight into a context window.
            raise ReleaseCheckError(
                f"GitHub returned HTTP {response.status_code} when asked for "
                f"the latest release. Check the releases page yourself: "
                f"{RELEASES_PAGE_URL}"
            )

        try:
            payload = response.json()
        except ValueError as e:
            raise ReleaseCheckError(
                "GitHub's response was not JSON, so the latest version could "
                "not be read."
            ) from e
        if not isinstance(payload, dict):
            raise ReleaseCheckError(
                "GitHub returned an unexpected shape for the latest release."
            )
        return payload

    def _compare(self, release: dict) -> dict:
        tag = release.get("tag_name")
        # Validates that the tag is a string as well as parsing it, which is
        # what lets `str(tag)` below be a no-op rather than a coercion.
        latest = parse_version(tag)
        installed = parse_version(self._installed)
        if latest > installed:
            return self._update(str(tag), release)
        return self._no_update(str(tag), ahead=installed > latest)

    def _no_update(self, tag: str, *, ahead: bool) -> dict:
        result = {
            "installed_version": self._installed,
            "checked": True,
            "latest_version": tag.removeprefix("v"),
            "update_available": False,
            "releases_url": RELEASES_PAGE_URL,
        }
        if ahead:
            result["note"] = (
                f"The installed version is newer than the latest published "
                f"release ({tag.removeprefix('v')}), which usually means this "
                f"is a local build rather than an installed bundle."
            )
        return result

    def _update(self, tag: str, release: dict) -> dict:
        result = {
            "installed_version": self._installed,
            "checked": True,
            "latest_version": tag.removeprefix("v"),
            "update_available": True,
            "release_url": _release_url(release, tag),
            "how_to_install": list(_INSTALL_STEPS),
        }
        download_url = _bundle_download_url(release)
        if download_url is not None:
            result["download_url"] = download_url
        return result


def _release_url(release: dict, tag: str) -> str:
    url = release.get("html_url")
    if isinstance(url, str) and url:
        return url
    return f"{RELEASES_PAGE_URL}/tag/{tag}"


def _bundle_download_url(release: dict) -> str | None:
    """The release's ``.mcpb`` asset, if it has one.

    A release without one isn't an error -- it's a release whose bundle
    hasn't finished uploading, or one published by hand -- so the version is
    still worth reporting.

    Raises:
        ReleaseCheckError: if ``assets`` isn't a list of objects.
    """
    assets = release.get("assets", [])
    if not isinstance(assets, list):
        raise ReleaseCheckError(
            "GitHub returned an unexpected shape for the release's assets."
        )
    for asset in assets:
        if not isinstance(asset, dict):
            raise ReleaseCheckError(
                "GitHub returned an unexpected shape for a release asset."
            )
        name = asset.get("name")
        if isinstance(name, str) and name.endswith(BUNDLE_SUFFIX):
            url = asset.get("browser_download_url")
            if not isinstance(url, str) or not url:
                raise ReleaseCheckError(
                    f"The release's {name} asset has no download URL."
                )
            return url
    return None
