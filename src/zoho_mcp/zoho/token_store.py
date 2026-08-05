"""Where the Zoho refresh token lives at rest.

Split out of ``auth.py`` because the two answer different questions.
``auth.py`` implements Zoho's OAuth protocol -- how a refresh token becomes an
access token -- and that is fixed by the vendor. This module answers "what
persists the refresh token", and the answer changes with the deployment: a
desktop install has an OS credential store, a host reachable from a phone does
not.

The two implementations are deliberately asymmetric about failure.
``KeyringTokenStore`` reads leniently (an unreachable store means "not
authorized yet") and writes loudly (silently losing a token the user just
granted fails later, far from the cause). ``EnvTokenStore`` cannot write at
all and says so, rather than appearing to succeed.
"""

import os
from typing import Protocol, runtime_checkable

import keyring

_KEYRING_SERVICE = "zoho-mcp"
_KEYRING_REFRESH_TOKEN_KEY = "zoho_refresh_token"

ENV_REFRESH_TOKEN_VAR = "ZOHO_REFRESH_TOKEN"


class TokenStoreError(Exception):
    """Raised when a refresh token cannot be persisted.

    Only ``store`` raises it. ``load`` reports "no token" instead, because
    every reason a load fails -- absent store, locked store, no D-Bus, nothing
    written yet -- leaves the caller doing the same thing: ask the user to
    authorize.
    """


@runtime_checkable
class TokenStore(Protocol):
    """Persistence for the one refresh token this single-user server holds."""

    @property
    def is_writable(self) -> bool:
        """Whether ``store`` can succeed on this deployment.

        Exists so a caller can find out *before* doing expensive or
        user-visible work. ``authenticate`` opens a browser and asks a human
        to consent; discovering afterwards that the result cannot be saved
        wastes the consent and teaches the user nothing about the fix.
        """

    def load(self) -> str | None:
        """Return the stored refresh token, or ``None`` if there isn't one."""

    def store(self, refresh_token: str) -> None:
        """Persist ``refresh_token``.

        Raises:
            TokenStoreError: if this store cannot persist it.
        """


class KeyringTokenStore:
    """The OS credential store, via ``keyring``. The desktop default."""

    @property
    def is_writable(self) -> bool:
        # True as a contract, not as a probe: keyring offers no way to ask
        # whether a write would succeed without performing one, and a probe
        # write would leave a stray credential behind. A machine with no
        # backend still fails loudly at `store`, which is where the
        # actionable message lives.
        return True

    def load(self) -> str | None:
        """Read the stored token, treating an unreachable store as no token.

        Plenty of Linux machines have no Secret Service backend at all --
        headless servers, minimal desktops, WSL, containers -- and ``keyring``
        raises there rather than returning nothing. Since the server reads
        this during startup, an escaping exception killed the process before
        any of the deferred-auth handling could run, which is how a missing
        backend turned into "the extension won't start" instead of "you need
        to authenticate".

        Deliberately broad: a locked store, an absent D-Bus and a backend that
        raises something keyring doesn't wrap all mean the same thing here.
        """
        try:
            return keyring.get_password(_KEYRING_SERVICE, _KEYRING_REFRESH_TOKEN_KEY)
        except Exception:  # noqa: BLE001 -- any store failure means "no token"
            return None

    def store(self, refresh_token: str) -> None:
        """Persist the token, failing loudly if the store refuses.

        Raises:
            TokenStoreError: if the credential store is unavailable.
        """
        try:
            keyring.set_password(
                _KEYRING_SERVICE, _KEYRING_REFRESH_TOKEN_KEY, refresh_token
            )
        except Exception as e:  # noqa: BLE001 -- surfaced, not swallowed
            raise TokenStoreError(
                f"Could not save the Zoho refresh token to this machine's "
                f"credential store: {e}. On Linux this usually means no Secret "
                f"Service backend is installed -- try installing gnome-keyring or "
                f"the `keyrings.alt` package, then authenticate again."
            ) from e


class EnvTokenStore:
    """A token supplied through the environment. Read-only, for hosted runs.

    A host has no OS credential store to speak of, so the token is provisioned
    the way every other secret on a server is: obtained once elsewhere (with
    ``zoho-mcp-setup`` on a machine that *does* have a browser and a keyring)
    and handed to the process by its operator.

    Read-only is the point rather than a limitation. The process cannot
    persist a credential it was not given, so there is nothing a conversation
    can do to change which account this server talks to -- the same reasoning
    that keeps ``ZOHO_ALLOW_AUTO_SEND`` out of reach of the tools.
    """

    @property
    def is_writable(self) -> bool:
        return False

    def load(self) -> str | None:
        """Read the token from the environment, blank counting as absent."""
        return os.environ.get(ENV_REFRESH_TOKEN_VAR, "").strip() or None

    def store(self, refresh_token: str) -> None:
        """Always raises -- this store is read-only.

        Raises:
            TokenStoreError: always, naming the variable the operator must set.
        """
        raise TokenStoreError(
            f"This server reads its Zoho refresh token from {ENV_REFRESH_TOKEN_VAR} "
            f"and cannot write one back. Obtain a token by running "
            f"`zoho-mcp-setup` on a machine with a browser, then set "
            f"{ENV_REFRESH_TOKEN_VAR} in this server's environment and restart it."
        )
