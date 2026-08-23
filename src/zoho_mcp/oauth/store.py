"""Storage for the self-contained authorization server.

Three concerns with three lifetimes, kept in one module because they are all
"the authorization server's persistence" and share the atomic-write helper:

- :class:`ClientStore` -- clients registered via DCR. Persistent, because
  Claude keeps its ``client_id`` and expects us to still know it (and its
  redirect URIs) after a restart.
- :class:`AuthorizationCodeStore` -- codes issued at ``/authorize`` and spent
  once at ``/token``. In-memory: they live seconds, and a restart mid-flow
  just makes the client re-authorize.
- :class:`RefreshTokenStore` -- which refresh ``jti`` is current for each
  client. Persistent, so strict rotation still detects a replayed old token
  after a restart rather than forcing a re-auth on every bounce.

Deliberately free of any OAuth-framework types: the Authlib adapter calls
these plain CRUD methods, so swapping frameworks never touches storage. Same
separation as ``zoho/token_store.py``.

The persistent files are written atomically (temp file + ``os.replace``) and
``0600``: a crash mid-write can't leave a half-written ``clients.json``, and a
file that may hold a client secret is never group- or world-readable.
"""

import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


class StoreError(Exception):
    """Raised when a persistent store's file cannot be read or parsed.

    A corrupt state file fails loudly rather than silently starting empty:
    dropping every registered client (or every refresh slot) on the floor
    would masquerade as "please re-authorize" and hide real corruption.
    """


@dataclass(frozen=True)
class RegisteredClient:
    """A client registered through Dynamic Client Registration (RFC 7591).

    Attributes:
        client_id: the id this server assigned and the client presents.
        client_secret: the secret, or ``None`` for a public PKCE client --
            the shape Claude's connector registers as. The distinction is
            load-bearing for the endpoint-auth check, so it is preserved
            rather than normalized to ``""``.
        redirect_uris: the exact URIs the client may be redirected to; the
            authorization endpoint matches against these.
        metadata: the raw registration request, kept verbatim.
        issued_at: when the client was registered, epoch seconds.
    """

    client_id: str
    client_secret: str | None
    redirect_uris: tuple[str, ...]
    metadata: dict
    issued_at: int


@dataclass(frozen=True)
class AuthorizationCode:
    """A one-time authorization code bound to a client and a PKCE challenge.

    Attributes:
        code: the opaque code value.
        client_id: the client the code was issued to.
        redirect_uri: the redirect URI used, re-checked at the token endpoint.
        scope: the granted scope, space-delimited.
        code_challenge: the PKCE challenge; the verifier is matched against it.
        code_challenge_method: always ``"S256"`` -- plain is refused upstream.
        subject: who approved; always the operator in this single-user server.
        expires_at: when the code stops being redeemable (UTC).
    """

    code: str
    client_id: str
    redirect_uri: str
    scope: str
    code_challenge: str
    code_challenge_method: str
    subject: str
    expires_at: datetime


def _now() -> datetime:
    return datetime.now(UTC)


def _atomic_write_json(path: Path, data: object) -> None:
    """Write ``data`` as JSON to ``path`` atomically and ``0600``.

    A temp file in the same directory is written then ``os.replace``-d into
    place, so a reader never sees a half-written file and a crash can't corrupt
    the existing one. ``mkstemp`` creates the temp ``0600``, and the replace
    preserves that mode.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _read_json(path: Path) -> dict:
    """Read a JSON object from ``path``, raising ``StoreError`` if it's broken."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        raise StoreError(f"Could not read state file {path}: {e}") from e


class ClientStore:
    """Persistent record of clients registered via DCR."""

    def __init__(self, path: Path) -> None:
        """Load existing clients from ``path``, or start empty if it's absent.

        Raises:
            StoreError: if the file exists but can't be parsed.
        """
        self._path = path
        self._clients: dict[str, RegisteredClient] = {}
        if path.exists():
            for client_id, record in _read_json(path).items():
                self._clients[client_id] = _client_from_dict(client_id, record)

    def add(self, client: RegisteredClient) -> None:
        """Store ``client`` and flush to disk."""
        self._clients[client.client_id] = client
        self._flush()

    def get(self, client_id: str) -> RegisteredClient | None:
        """Return the client with this id, or ``None``."""
        return self._clients.get(client_id)

    def _flush(self) -> None:
        _atomic_write_json(
            self._path,
            {cid: _client_to_dict(c) for cid, c in self._clients.items()},
        )


def _client_to_dict(client: RegisteredClient) -> dict:
    return {
        "client_secret": client.client_secret,
        "redirect_uris": list(client.redirect_uris),
        "metadata": client.metadata,
        "issued_at": client.issued_at,
    }


def _client_from_dict(client_id: str, record: dict) -> RegisteredClient:
    # client_id is the dict key, not stored inside the record, so it is passed
    # back in here rather than read from the record.
    return RegisteredClient(
        client_id=client_id,
        client_secret=record["client_secret"],
        redirect_uris=tuple(record["redirect_uris"]),
        metadata=record["metadata"],
        issued_at=record["issued_at"],
    )


class AuthorizationCodeStore:
    """In-memory, single-use store of authorization codes.

    Not persisted: codes live seconds, and losing them to a restart mid-flow
    only costs the client one re-authorization.
    """

    def __init__(self) -> None:
        self._codes: dict[str, AuthorizationCode] = {}

    def add(self, code: AuthorizationCode) -> None:
        """Store an issued code."""
        self._codes[code.code] = code

    def get(self, code: str) -> AuthorizationCode | None:
        """Return the code if present and unexpired, else ``None``.

        An expired code is purged on the way out, so a stale entry can't linger
        and it reads exactly as an absent one.
        """
        record = self._codes.get(code)
        if record is None:
            return None
        if _now() >= record.expires_at:
            del self._codes[code]
            return None
        return record

    def delete(self, code: str) -> None:
        """Remove a code once spent. A no-op if it isn't there."""
        self._codes.pop(code, None)


class RefreshTokenStore:
    """Persistent record of the current refresh ``jti`` for each client.

    Strict rotation: a client has exactly one current refresh token at a time,
    and :meth:`remember` replacing it is what makes a replayed old token fail
    :meth:`is_current`. Bounded by design -- one entry per client, overwritten
    on each rotation, so nothing accumulates.
    """

    def __init__(self, path: Path) -> None:
        """Load the ledger from ``path``, or start empty if it's absent.

        Raises:
            StoreError: if the file exists but can't be parsed.
        """
        self._path = path
        # client_id -> {"jti": str, "expires_at": iso8601}
        self._current: dict[str, dict] = {}
        if path.exists():
            self._current = _read_json(path)

    def remember(self, client_id: str, jti: str, expires_at: datetime) -> None:
        """Record ``jti`` as the client's current refresh token, replacing any
        previous one, and flush."""
        self._current[client_id] = {
            "jti": jti,
            "expires_at": expires_at.isoformat(),
        }
        self._flush()

    def is_current(self, client_id: str, jti: str) -> bool:
        """Whether ``jti`` is the client's current, unexpired refresh token."""
        record = self._current.get(client_id)
        if record is None or record["jti"] != jti:
            return False
        return _now() < datetime.fromisoformat(record["expires_at"])

    def revoke(self, client_id: str) -> None:
        """Drop a client's refresh slot -- logout, or a reuse-detection response."""
        if self._current.pop(client_id, None) is not None:
            self._flush()

    def _flush(self) -> None:
        _atomic_write_json(self._path, self._current)
