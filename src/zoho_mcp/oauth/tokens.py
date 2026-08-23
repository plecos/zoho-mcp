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
import os
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import OctKey
from joserfc.jwt import JWTClaimsRegistry

# Verification is pinned to this one algorithm. Accepting a set would reopen
# the downgrade the `alg: none` test guards against.
_ALGORITHM = "HS256"

# RFC 7518 requires an HS256 key at least as large as the hash output (256
# bits / 32 bytes); a shorter one is weak, not merely non-standard. The upper
# bound is a sanity guard: HMAC folds any key past the 64-byte block size down
# to that size, so a much longer one buys no strength and is almost always a
# paste mistake (a whole file, a PEM blob).
MIN_SIGNING_KEY_BYTES = 32
MAX_SIGNING_KEY_BYTES = 512

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
            signing_key: the HS256 secret. Must be non-blank and between
                ``MIN_SIGNING_KEY_BYTES`` and ``MAX_SIGNING_KEY_BYTES`` bytes;
                a weak or malformed key is the one construction-time error
                worth failing on.
            issuer: the ``iss`` this server stamps and demands back.
            audience: the ``aud`` -- this server's own resource URL, so a token
                minted for a different resource can't be replayed here
                (RFC 8707).
            access_ttl: lifetime of access tokens.
            refresh_ttl: lifetime of refresh tokens; longer, since rotation
                depends on it outliving the access tokens issued beside it.

        Raises:
            TokenError: if ``signing_key`` is blank or outside the length
                bounds for HS256.
        """
        if not signing_key or not signing_key.strip():
            raise TokenError("A blank signing key cannot sign or verify tokens")
        # Measured in bytes, not characters: HMAC consumes the encoded key, and
        # a non-ASCII passphrase has more bytes than characters.
        key_bytes = len(signing_key.encode("utf-8"))
        if not MIN_SIGNING_KEY_BYTES <= key_bytes <= MAX_SIGNING_KEY_BYTES:
            raise TokenError(
                f"Signing key must be between {MIN_SIGNING_KEY_BYTES} and "
                f"{MAX_SIGNING_KEY_BYTES} bytes for HS256; got {key_bytes}"
            )
        self._key = OctKey.import_key(signing_key)
        self._issuer = issuer
        self._audience = audience
        self._access_ttl = access_ttl
        self._refresh_ttl = refresh_ttl

    @property
    def access_ttl(self) -> timedelta:
        """The access-token lifetime, so callers can advertise ``expires_in``."""
        return self._access_ttl

    def mint_access_token(self, subject: str, scopes: Sequence[str]) -> str:
        """Issue a short-lived access token bearing ``scopes``."""
        return self._mint(subject, scopes, ACCESS_TOKEN_USE, self._access_ttl)

    def mint_refresh_token(self, subject: str, scopes: Sequence[str]) -> str:
        """Issue a longer-lived refresh token carrying the scopes to rotate."""
        return self._mint(subject, scopes, REFRESH_TOKEN_USE, self._refresh_ttl)

    def _mint(
        self, subject: str, scopes: Sequence[str], token_use: str, ttl: timedelta
    ) -> str:
        now = datetime.now(UTC)
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
        except JoseError as e:
            # Every malformed/tampered/wrong-algorithm input joserfc sees is a
            # JoseError. A non-string token would raise TypeError instead, but
            # that is caller misuse (the gate only ever passes a str) and
            # should surface as the bug it is rather than be masked here.
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
            expires_at=datetime.fromtimestamp(claims["exp"], UTC),
            jti=claims.get("jti", ""),
            claims=dict(claims),
        )


def load_or_create_signing_key(path: Path) -> str:
    """Return the persisted HS256 signing key, generating one on first use.

    The key is auto-generated (the chosen no-hoop default: an operator sets no
    key of their own) and persisted, because a key that changed on restart
    would silently invalidate every token already issued -- forcing Claude to
    re-authorize on every process bounce. It is created ``0600`` atomically:
    this is the secret that forges access to every tool, so it must never be
    readable by other users on a shared host, not even for the instant between
    creating the file and restricting it.

    Concurrent first starts are tolerated. Two server processes launched
    together -- the documented "two clients meant two server processes"
    case -- can both find the file absent and both try to create it; the one
    that loses the exclusive create reads the winner's key instead of failing.

    Args:
        path: JSON file to read the key from, or create it at.

    Returns:
        The signing key.
    """
    if path.exists():
        return _read_signing_key(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    try:
        # O_EXCL makes creation fail rather than clobber if another process got
        # here first; the 0o600 mode is applied at creation, so the file is
        # never momentarily group/world-readable (umask can only remove bits,
        # and 0o600 already has none to spare).
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return _read_signing_key(path)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({_SIGNING_KEY_FIELD: key}, f)
    return key


def _read_signing_key(path: Path) -> str:
    return json.loads(path.read_text(encoding="utf-8"))[_SIGNING_KEY_FIELD]
