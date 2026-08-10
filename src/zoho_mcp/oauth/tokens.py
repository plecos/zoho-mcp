"""Minting and verifying the access/refresh tokens this server issues.

Part of OAuth #2 (see :mod:`zoho_mcp.oauth`). The tokens are HS256 JWTs this
server both signs and verifies, so the signing key never leaves the process
and there is no public JWKS to publish -- Claude only ever *presents* a token
back, it never verifies one.

The cryptography is joserfc's, not ours: signing, signature checking, and
algorithm handling all go through it, and verification is pinned to HS256 so a
token arriving with ``alg: none`` or any other algorithm is rejected rather
than trusted. What this module owns is the claim shape, the lifetimes, and the
access-vs-refresh distinction -- and turning every joserfc failure into one
``TokenError`` the request gate can treat uniformly.
"""

import json
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from joserfc.jwt import JWTClaimsRegistry

# Verification is pinned to this one algorithm. Accepting a set would reopen
# the downgrade the `alg: none` test guards against.
_ALGORITHM = "HS256"

ACCESS_TOKEN_USE = "access"
REFRESH_TOKEN_USE = "refresh"

DEFAULT_ACCESS_TTL = timedelta(hours=1)
DEFAULT_REFRESH_TTL = timedelta(days=30)

# Custom claim naming which of the two kinds a token is. `verify(expect_use=)`
# checks it so an access token can't stand in for a refresh token or vice
# versa -- they share a key and audience and differ only here.
_TOKEN_USE_CLAIM = "tok"

_SIGNING_KEY_FIELD = "signing_key"


class TokenError(Exception):
    """Raised when a token cannot be minted, or fails verification.

    Every joserfc failure mode -- bad signature, wrong algorithm, expired,
    wrong issuer or audience, malformed input -- collapses to this, because
    the caller (the request gate) does the same thing for all of them: refuse
    the request. Keeping joserfc's exception types out of the caller also
    keeps a library traceback from ever reaching a client.
    """


@dataclass(frozen=True)
class TokenInfo:
    """The verified contents of a token the gate can act on.

    Attributes:
        subject: who the token was issued for (the operator).
        scopes: the granted scopes, already split from the space-delimited
            ``scope`` claim.
        token_use: ``"access"`` or ``"refresh"``.
        expires_at: the token's expiry, as a timezone-aware UTC datetime.
        jti: the token's unique id.
        claims: the raw validated claim set, for anything not surfaced above.
    """

    subject: str
    scopes: tuple[str, ...]
    token_use: str
    expires_at: datetime
    jti: str
    claims: dict


class TokenSigner:
    """Signs and verifies this server's tokens against one HS256 key."""

    def __init__(
        self,
        *,
        signing_key: str,
        issuer: str,
        audience: str,
        access_ttl: timedelta = DEFAULT_ACCESS_TTL,
        refresh_ttl: timedelta = DEFAULT_REFRESH_TTL,
    ) -> None:
        """Build a signer.

        Args:
            signing_key: the HS256 secret. Must be non-blank; a short or empty
                key is the one construction-time error worth failing on.
            issuer: the ``iss`` this server stamps and demands back.
            audience: the ``aud`` -- this server's own resource URL, so a token
                minted for a different resource can't be replayed here
                (RFC 8707).
            access_ttl: lifetime of access tokens.
            refresh_ttl: lifetime of refresh tokens; longer, since rotation
                depends on it outliving the access tokens issued beside it.

        Raises:
            TokenError: if ``signing_key`` is blank.
        """
        if not signing_key or not signing_key.strip():
            raise TokenError("A blank signing key cannot sign or verify tokens")
        self._key = OctKey.import_key(signing_key)
        self._issuer = issuer
        self._audience = audience
        self._access_ttl = access_ttl
        self._refresh_ttl = refresh_ttl

    def mint_access_token(self, subject: str, scopes: Sequence[str]) -> str:
        """Issue a short-lived access token bearing ``scopes``."""
        return self._mint(subject, scopes, ACCESS_TOKEN_USE, self._access_ttl)

    def mint_refresh_token(self, subject: str, scopes: Sequence[str]) -> str:
        """Issue a longer-lived refresh token carrying the scopes to rotate."""
        return self._mint(subject, scopes, REFRESH_TOKEN_USE, self._refresh_ttl)

    def _mint(
        self, subject: str, scopes: Sequence[str], token_use: str, ttl: timedelta
    ) -> str:
        now = datetime.now(timezone.utc)
        claims = {
            "iss": self._issuer,
            "aud": self._audience,
            "sub": subject,
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + ttl).timestamp()),
            "jti": secrets.token_urlsafe(16),
            "scope": " ".join(scopes),
            _TOKEN_USE_CLAIM: token_use,
        }
        return jwt.encode({"alg": _ALGORITHM}, claims, self._key)

    def verify(self, token: str, *, expect_use: str | None = None) -> TokenInfo:
        """Verify a token and return its contents, or raise.

        Signature and algorithm are checked first (pinned to HS256), then the
        registered claims (``iss``/``aud``/``exp``/``sub``), then -- if asked
        -- that the token is of the expected kind.

        Args:
            token: the compact JWT string.
            expect_use: if given, require this ``token_use``; the gate passes
                ``"access"``, the token endpoint passes ``"refresh"``.

        Returns:
            The validated :class:`TokenInfo`.

        Raises:
            TokenError: for any signature, algorithm, claim, or kind failure,
                and for a malformed token string.
        """
        try:
            decoded = jwt.decode(token, self._key, algorithms=[_ALGORITHM])
        except (JoseError, ValueError) as e:
            raise TokenError(f"Token could not be decoded or verified: {e}") from e

        claims = decoded.claims
        registry = JWTClaimsRegistry(
            iss={"essential": True, "value": self._issuer},
            aud={"essential": True, "value": self._audience},
            sub={"essential": True},
            exp={"essential": True},
        )
        try:
            registry.validate(claims)
        except JoseError as e:
            raise TokenError(f"Token claims failed validation: {e}") from e

        token_use = claims.get(_TOKEN_USE_CLAIM)
        if not isinstance(token_use, str):
            # Every token this server mints carries the kind claim; one that
            # lacks it was not issued here, whatever else validated.
            raise TokenError("Token is missing its use claim")
        if expect_use is not None and token_use != expect_use:
            raise TokenError(f"Expected a {expect_use} token but got {token_use!r}")

        return TokenInfo(
            subject=claims["sub"],
            scopes=tuple(claims.get("scope", "").split()),
            token_use=token_use,
            expires_at=datetime.fromtimestamp(claims["exp"], timezone.utc),
            jti=claims.get("jti", ""),
            claims=dict(claims),
        )


def load_or_create_signing_key(path: Path) -> str:
    """Return the persisted HS256 signing key, generating one on first use.

    The key is auto-generated (the chosen no-hoop default: an operator sets no
    key of their own) and persisted, because a key that changed on restart
    would silently invalidate every token already issued -- forcing Claude to
    re-authorize on every process bounce. It is written ``0600``: this is the
    secret that forges access to every tool, so it must not be readable by
    other users on a shared host.

    Args:
        path: JSON file to read the key from, or create it at.

    Returns:
        The signing key.
    """
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        return data[_SIGNING_KEY_FIELD]

    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    # Create with 0600 from the start rather than writing then chmod-ing, so
    # the secret is never briefly world-readable between the two steps.
    fd = path.open("x", encoding="utf-8")
    try:
        path.chmod(0o600)
        json.dump({_SIGNING_KEY_FIELD: key}, fd)
    finally:
        fd.close()
    return key
