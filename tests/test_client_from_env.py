"""Tests for `_build_zoho_clients_from_env`.

This is the only place `ZOHO_ALLOW_AUTO_SEND` is turned into the
`allow_auto_send` flag, and it had no coverage at all. Every other send-gate
test constructs `ZohoClient` with an explicit Python boolean, which verifies
where the gate lives but never how it gets set.

That gap matters because the failure is silent and severe: replacing the parse
with something that looks equivalent -- `bool(os.environ.get(...))` -- would
make `ZOHO_ALLOW_AUTO_SEND=false` sitting in a .env file *enable live sending*,
and no other test in the suite would notice.
"""

import pytest

from zoho_mcp import server


@pytest.fixture
def env(monkeypatch):
    """Minimal viable environment, with the auth flow stubbed out.

    `load_env` is neutered so a developer's real .env can't leak into the
    assertions, and the keyring lookup is stubbed so no OS credential store
    is touched.
    """
    monkeypatch.setattr(server, "load_env", lambda: None)
    monkeypatch.setattr(server, "load_refresh_token", lambda: "fake-refresh-token")
    monkeypatch.setenv("ZOHO_CLIENT_ID", "id")
    monkeypatch.setenv("ZOHO_CLIENT_SECRET", "secret")
    monkeypatch.setenv("ZOHO_ACCOUNT_ID", "acct")
    monkeypatch.setenv("ZOHO_CALENDAR_UID", "cal")
    monkeypatch.delenv("ZOHO_ALLOW_AUTO_SEND", raising=False)
    monkeypatch.delenv("ZOHO_STRIP_INVISIBLE_CHARS", raising=False)
    return monkeypatch


# Case-insensitive with surrounding whitespace ignored -- matching what the
# README, .env.example, and SECURITY.md all promise.
@pytest.mark.parametrize("value", ["true", "TRUE", "True", " true ", "\ttrue\n"])
async def test_auto_send_enabled_only_for_true(env, value):
    env.setenv("ZOHO_ALLOW_AUTO_SEND", value)

    client, *_ = server._build_zoho_clients_from_env()

    assert client._allow_auto_send is True


# Anything else must leave sending off. "1" and "yes" are included because
# they're the values someone would plausibly *assume* work -- silently
# enabling outbound mail on a truthiness check would be the worst outcome here.
@pytest.mark.parametrize(
    "value", ["false", "FALSE", "0", "1", "yes", "no", "", "  ", "maybe", "True!"]
)
async def test_auto_send_disabled_for_everything_else(env, value):
    env.setenv("ZOHO_ALLOW_AUTO_SEND", value)

    client, *_ = server._build_zoho_clients_from_env()

    assert client._allow_auto_send is False


async def test_auto_send_disabled_when_unset(env):
    client, *_ = server._build_zoho_clients_from_env()

    assert client._allow_auto_send is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("TRUE", True), ("false", False), ("1", False), ("", False)],
)
async def test_strip_invisible_chars_parses_the_same_way(env, value, expected):
    env.setenv("ZOHO_STRIP_INVISIBLE_CHARS", value)

    client, *_ = server._build_zoho_clients_from_env()

    assert client._strip_invisible_chars is expected


async def test_starts_unauthenticated_rather_than_refusing_to_start(env):
    # Refusing to start would make `authenticate` unreachable, which is the
    # only way an MCPB install can be authorized at all. The error surfaces
    # per-call from get_access_token instead -- see
    # tests/zoho/test_deferred_authentication.py.
    env.setattr(server, "load_refresh_token", lambda: None)

    _, _, token_manager, _ = server._build_zoho_clients_from_env()

    assert token_manager.is_authenticated is False


async def test_missing_credentials_do_not_stop_the_server_starting(env):
    # `authenticate` reports these missing in the conversation, where the
    # user can act on it; a KeyError here would just be a dead server.
    env.delenv("ZOHO_CLIENT_ID", raising=False)
    env.delenv("ZOHO_CLIENT_SECRET", raising=False)

    client, *_ = server._build_zoho_clients_from_env()

    assert client is not None


async def test_account_and_calendar_ids_are_passed_through(env):
    env.setenv("ZOHO_ACCOUNT_ID", "acct-123")
    env.setenv("ZOHO_CALENDAR_UID", "cal-456")

    client, *_ = server._build_zoho_clients_from_env()

    assert client._account_id_cache == "acct-123"
    assert client._calendar_uid_cache == "cal-456"


async def test_missing_account_and_calendar_ids_are_left_for_discovery(env):
    # Neither is required any more -- ZohoClient looks them up. A KeyError
    # here would put the ids back on the setup checklist.
    env.delenv("ZOHO_ACCOUNT_ID", raising=False)
    env.delenv("ZOHO_CALENDAR_UID", raising=False)

    client, *_ = server._build_zoho_clients_from_env()

    assert client._account_id_cache is None
    assert client._calendar_uid_cache is None


# A key left in .env with no value is the likely shape of a half-finished
# setup; it has to read as "not configured", not as an empty path segment.
@pytest.mark.parametrize("blank", ["", "   "])
async def test_blank_ids_are_treated_as_unconfigured(env, blank):
    env.setenv("ZOHO_ACCOUNT_ID", blank)
    env.setenv("ZOHO_CALENDAR_UID", blank)

    client, *_ = server._build_zoho_clients_from_env()

    assert client._account_id_cache is None
    assert client._calendar_uid_cache is None
