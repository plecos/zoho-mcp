"""Zoho OAuth token refresh and refresh-token storage.

Knows nothing about Mail/Calendar payloads -- only how to turn a stored
refresh token into a live access token, and where that refresh token lives
at rest (the OS credential store via ``keyring``).
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import keyring

ZOHO_TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"
ZOHO_AUTHORIZATION_URL = "https://accounts.zoho.com/oauth/v2/auth"

# Refresh this many seconds early so a request never races an in-flight expiry.
REFRESH_SAFETY_MARGIN_SECONDS = 60
# Floor for a token's usable lifetime, so an unexpectedly short
# expires_in can't collapse into a refresh-per-request loop.
MIN_TOKEN_LIFETIME_SECONDS = 30

_KEYRING_SERVICE = "zoho-mcp"
_KEYRING_REFRESH_TOKEN_KEY = "zoho_refresh_token"


class ZohoAuthError(Exception):
    """Raised when Zoho rejects a token refresh (expired/revoked/invalid refresh token)."""


class ZohoTokenManager:
    """Caches a Zoho access token in memory, refreshing it only once expired."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._http_client = http_client
        self._access_token: str | None = None
        self._expires_at: datetime | None = None

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing it first if needed.

        Raises:
            ZohoAuthError: if Zoho rejects the refresh (e.g. revoked token).
        """
        if self._access_token is None or self._is_expired():
            await self._refresh()
        assert self._access_token is not None
        return self._access_token

    def _is_expired(self) -> bool:
        assert self._expires_at is not None
        return datetime.now(timezone.utc) >= self._expires_at

    async def _refresh(self) -> None:
        try:
            response = await self._http_client.post(
                ZOHO_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                },
            )
        except httpx.HTTPError as e:
            raise ZohoAuthError(
                f"Network error refreshing Zoho access token: {e}"
            ) from e

        try:
            payload = response.json()
        except ValueError as e:
            raise ZohoAuthError(
                f"Zoho token endpoint returned a non-JSON response: {e}"
            ) from e

        if response.status_code != 200 or "access_token" not in payload:
            raise ZohoAuthError(
                payload.get("error", "unknown error refreshing Zoho access token")
            )
        self._access_token = payload["access_token"]

        # Coerced rather than trusted: Zoho sends strings where numbers are
        # documented elsewhere (status "1", isFavorite "false", color "-1"), and
        # an un-coerced string here raised TypeError out of every tool call.
        raw_expires_in = payload.get("expires_in", 3600)
        try:
            expires_in = int(float(str(raw_expires_in).strip()))
        except (TypeError, ValueError) as e:
            raise ZohoAuthError(
                f"Zoho returned an unusable expires_in ({raw_expires_in!r})"
            ) from e

        # Floor the lifetime at the margin. Without this a short-lived token
        # puts expires_at in the past, so every call re-refreshes -- one token
        # POST per API call, which is a direct route to being rate-limited.
        lifetime = max(
            expires_in - REFRESH_SAFETY_MARGIN_SECONDS, MIN_TOKEN_LIFETIME_SECONDS
        )
        self._expires_at = datetime.now(timezone.utc) + timedelta(seconds=lifetime)


def build_authorization_url(
    client_id: str, redirect_uri: str, scopes: list[str]
) -> str:
    """Build the one-time Zoho consent URL for the initial OAuth setup.

    Requests offline access (``access_type=offline`` + ``prompt=consent``) so
    the resulting authorization code exchanges for a refresh token, not just
    a short-lived access token.
    """
    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
    }
    return f"{ZOHO_AUTHORIZATION_URL}?{urlencode(params)}"


async def exchange_code_for_tokens(
    http_client: httpx.AsyncClient,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
) -> dict:
    """Exchange a one-time authorization code for an access + refresh token.

    Raises:
        ZohoAuthError: if the request fails, the response isn't JSON, or
            Zoho doesn't return a refresh token (e.g. offline access wasn't
            actually granted).
    """
    try:
        response = await http_client.post(
            ZOHO_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    except httpx.HTTPError as e:
        raise ZohoAuthError(f"Network error exchanging authorization code: {e}") from e

    try:
        payload = response.json()
    except ValueError as e:
        raise ZohoAuthError(
            f"Zoho token endpoint returned a non-JSON response: {e}"
        ) from e

    if response.status_code != 200 or "refresh_token" not in payload:
        raise ZohoAuthError(
            payload.get("error", "unknown error exchanging authorization code")
        )
    return payload


def extract_authorization_code(query_params: dict[str, list[str]]) -> str:
    """Extract the authorization code from the OAuth callback's query params.

    Args:
        query_params: the callback URL's query string, parsed with
            ``urllib.parse.parse_qs`` (each value is a list of strings).

    Raises:
        ZohoAuthError: if Zoho reported an error, or no code is present.
    """
    if "error" in query_params:
        raise ZohoAuthError(f"Zoho denied authorization: {query_params['error'][0]}")
    if "code" not in query_params:
        raise ZohoAuthError("OAuth callback did not include an authorization code")
    return query_params["code"][0]


def load_refresh_token() -> str | None:
    """Read the stored Zoho refresh token from the OS credential store."""
    return keyring.get_password(_KEYRING_SERVICE, _KEYRING_REFRESH_TOKEN_KEY)


def store_refresh_token(refresh_token: str) -> None:
    """Persist the Zoho refresh token to the OS credential store."""
    keyring.set_password(_KEYRING_SERVICE, _KEYRING_REFRESH_TOKEN_KEY, refresh_token)
