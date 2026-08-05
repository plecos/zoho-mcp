"""Tests for where the Zoho refresh token lives at rest.

Split out of `test_auth.py` alongside the code, because storage is a
different question from the OAuth protocol: the protocol is fixed by Zoho,
while the answer to "what persists this" changes with the deployment. A
desktop install has an OS credential store; a host reachable from a phone
does not.

The two implementations are deliberately asymmetric. `KeyringTokenStore`
reads leniently and writes loudly -- an unreachable store means "not
authorized yet" on the way in, but silently losing a token the user just
granted is the failure mode with no diagnostic trail. `EnvTokenStore` cannot
write at all, and says so rather than pretending.
"""

import keyring.errors
import pytest

from zoho_mcp.zoho.token_store import (
    ENV_REFRESH_TOKEN_VAR,
    EnvTokenStore,
    KeyringTokenStore,
    TokenStore,
    TokenStoreError,
)


# Found by the release workflow's Linux verify job: a headless machine with no
# Secret Service backend made `keyring.get_password` raise NoKeyringError, and
# because the server reads the token during startup that killed the process
# before any of the deferred-auth handling could run. Headless servers,
# minimal desktops, WSL and containers are all in that state by default.
class _NoBackend:
    """Stand-in for keyring on a machine with no usable backend."""

    @staticmethod
    def get_password(service, username):
        raise keyring.errors.NoKeyringError("no backend")

    @staticmethod
    def set_password(service, username, password):
        raise keyring.errors.NoKeyringError("no backend")


# --- the protocol itself ---------------------------------------------------


@pytest.mark.parametrize("store", [KeyringTokenStore(), EnvTokenStore()])
def test_both_stores_satisfy_the_protocol(store):
    assert isinstance(store, TokenStore)


def test_the_two_stores_disagree_about_writability():
    # The whole reason the protocol carries `is_writable`: `authenticate` has
    # to know before it opens a browser, not after the user has consented.
    assert KeyringTokenStore().is_writable is True
    assert EnvTokenStore().is_writable is False


# --- KeyringTokenStore -----------------------------------------------------


def test_keyring_store_loads_what_was_stored(monkeypatch):
    monkeypatch.setattr(
        "zoho_mcp.zoho.token_store.keyring.get_password",
        lambda service, key: (
            "abc123" if (service, key) == ("zoho-mcp", "zoho_refresh_token") else None
        ),
    )

    assert KeyringTokenStore().load() == "abc123"


def test_keyring_store_loads_none_when_nothing_is_stored(monkeypatch):
    monkeypatch.setattr(
        "zoho_mcp.zoho.token_store.keyring.get_password", lambda service, key: None
    )

    assert KeyringTokenStore().load() is None


def test_keyring_store_stores_under_the_expected_service_and_key(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        "zoho_mcp.zoho.token_store.keyring.set_password",
        lambda service, key, value: calls.update(service=service, key=key, value=value),
    )

    KeyringTokenStore().store("abc123")

    assert calls == {
        "service": "zoho-mcp",
        "key": "zoho_refresh_token",
        "value": "abc123",
    }


def test_keyring_store_reads_a_missing_backend_as_no_token(monkeypatch):
    monkeypatch.setattr("zoho_mcp.zoho.token_store.keyring", _NoBackend)

    assert KeyringTokenStore().load() is None


def test_keyring_store_survives_any_backend_failure_on_load(monkeypatch):
    # Locked stores, D-Bus not running, a backend that raises something
    # keyring doesn't wrap -- all mean the same thing to the caller.
    class _Exploding:
        @staticmethod
        def get_password(service, username):
            raise RuntimeError("dbus is not running")

    monkeypatch.setattr("zoho_mcp.zoho.token_store.keyring", _Exploding)

    assert KeyringTokenStore().load() is None


def test_keyring_store_raises_something_actionable_when_it_cannot_write(monkeypatch):
    # Storing must NOT be silent. A user who ran `authenticate`, got a success
    # back, and then found themselves unauthenticated on restart would have
    # nothing to go on.
    monkeypatch.setattr("zoho_mcp.zoho.token_store.keyring", _NoBackend)

    with pytest.raises(TokenStoreError, match="credential store"):
        KeyringTokenStore().store("refresh-1")


# --- EnvTokenStore ---------------------------------------------------------


def test_env_store_loads_the_token_from_the_environment(monkeypatch):
    monkeypatch.setenv(ENV_REFRESH_TOKEN_VAR, "refresh-from-env")

    assert EnvTokenStore().load() == "refresh-from-env"


def test_env_store_loads_none_when_the_variable_is_unset(monkeypatch):
    monkeypatch.delenv(ENV_REFRESH_TOKEN_VAR, raising=False)

    assert EnvTokenStore().load() is None


@pytest.mark.parametrize("value", ["", "   ", "\n\t "])
def test_env_store_reads_a_blank_variable_as_no_token(monkeypatch, value):
    # A key left in a .env file with no value is the same situation as no key
    # at all, and must not present as a token the refresh will then reject
    # with `invalid_client`.
    monkeypatch.setenv(ENV_REFRESH_TOKEN_VAR, value)

    assert EnvTokenStore().load() is None


def test_env_store_strips_surrounding_whitespace(monkeypatch):
    # Shell exports and .env files both pick up trailing newlines easily, and
    # Zoho rejects the token rather than trimming it.
    monkeypatch.setenv(ENV_REFRESH_TOKEN_VAR, "  refresh-from-env\n")

    assert EnvTokenStore().load() == "refresh-from-env"


def test_env_store_refuses_to_store_and_names_the_variable(monkeypatch):
    # The process cannot persist anything its operator did not put in the
    # environment, so the error has to say what the operator must set.
    with pytest.raises(TokenStoreError, match=ENV_REFRESH_TOKEN_VAR):
        EnvTokenStore().store("refresh-1")


def test_env_store_does_not_mutate_the_environment_when_storing(monkeypatch):
    # A store() that "worked" by writing os.environ would hand a conversation
    # a way to change this process's credentials -- the same hole the send
    # gate exists to close.
    monkeypatch.delenv(ENV_REFRESH_TOKEN_VAR, raising=False)

    with pytest.raises(TokenStoreError):
        EnvTokenStore().store("refresh-1")

    assert EnvTokenStore().load() is None
