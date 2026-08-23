"""Tests for the authorization server's storage.

Three concerns, three lifetimes: registered clients survive restarts (Claude
keeps its ``client_id`` and expects us to remember it), authorization codes
live seconds and are single-use, and the refresh-token ledger records which
``jti`` is current so a rotated-out token can't be replayed.

The persistent stores are checked for the properties that make them safe to
trust across a restart or a crash: the file survives a new process, it is
never group/world-readable, and a corrupt file fails loudly rather than
silently dropping state.
"""

import stat
from datetime import UTC, datetime, timedelta

import pytest
import time_machine

from zoho_mcp.oauth.store import (
    AuthorizationCode,
    AuthorizationCodeStore,
    ClientStore,
    RefreshTokenStore,
    RegisteredClient,
    StoreError,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def a_client(client_id="client-1", secret=None) -> RegisteredClient:
    return RegisteredClient(
        client_id=client_id,
        client_secret=secret,
        redirect_uris=("https://claude.ai/api/mcp/auth_callback",),
        metadata={"client_name": "Claude", "token_endpoint_auth_method": "none"},
        issued_at=1_750_000_000,
    )


def a_code(code="code-1", expires_at=None) -> AuthorizationCode:
    return AuthorizationCode(
        code=code,
        client_id="client-1",
        redirect_uri="https://claude.ai/api/mcp/auth_callback",
        scope="zoho.tools",
        code_challenge="abc123",
        code_challenge_method="S256",
        subject="owner",
        expires_at=expires_at or (NOW + timedelta(minutes=5)),
    )


# --- ClientStore -----------------------------------------------------------


def test_a_registered_client_round_trips_through_the_store(tmp_path):
    store = ClientStore(tmp_path / "clients.json")
    client = a_client()

    store.add(client)

    assert store.get("client-1") == client


def test_an_unknown_client_id_returns_none(tmp_path):
    store = ClientStore(tmp_path / "clients.json")

    assert store.get("nope") is None


def test_a_client_survives_a_restart(tmp_path):
    path = tmp_path / "clients.json"
    ClientStore(path).add(a_client())

    # A brand-new process reading the same file must see the client.
    assert ClientStore(path).get("client-1") == a_client()


def test_a_client_without_a_secret_round_trips_as_none_not_blank(tmp_path):
    # A public PKCE client (Claude's usual shape) has no secret; the absence
    # must survive as None, not collapse to "" -- the two mean different things
    # to the endpoint-auth check.
    path = tmp_path / "clients.json"
    ClientStore(path).add(a_client(secret=None))

    assert ClientStore(path).get("client-1").client_secret is None


def test_a_client_secret_round_trips_when_present(tmp_path):
    path = tmp_path / "clients.json"
    ClientStore(path).add(a_client(secret="s3cr3t"))

    assert ClientStore(path).get("client-1").client_secret == "s3cr3t"


def test_several_clients_are_kept_independently(tmp_path):
    store = ClientStore(tmp_path / "clients.json")
    store.add(a_client("client-1"))
    store.add(a_client("client-2"))

    assert store.get("client-1").client_id == "client-1"
    assert store.get("client-2").client_id == "client-2"


def test_the_client_file_is_not_group_or_world_readable(tmp_path):
    path = tmp_path / "clients.json"
    ClientStore(path).add(a_client())

    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, oct(path.stat().st_mode)


def test_an_absent_client_file_is_an_empty_store(tmp_path):
    store = ClientStore(tmp_path / "does-not-exist.json")

    assert store.get("client-1") is None


def test_a_corrupt_client_file_fails_loudly(tmp_path):
    path = tmp_path / "clients.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(StoreError):
        ClientStore(path)


def test_writing_leaves_no_temporary_file_behind(tmp_path):
    path = tmp_path / "clients.json"
    ClientStore(path).add(a_client())

    assert [p.name for p in tmp_path.iterdir()] == ["clients.json"]


# --- AuthorizationCodeStore ------------------------------------------------


def test_an_authorization_code_round_trips(tmp_path):
    store = AuthorizationCodeStore()
    code = a_code()

    store.add(code)

    with time_machine.travel(NOW, tick=False):
        assert store.get("code-1") == code


def test_an_unknown_code_returns_none():
    assert AuthorizationCodeStore().get("nope") is None


def test_a_code_is_single_use_via_delete():
    store = AuthorizationCodeStore()
    store.add(a_code())

    with time_machine.travel(NOW, tick=False):
        assert store.get("code-1") is not None
        store.delete("code-1")
        assert store.get("code-1") is None


def test_an_expired_code_reads_as_absent():
    store = AuthorizationCodeStore()
    store.add(a_code(expires_at=NOW + timedelta(minutes=5)))

    with time_machine.travel(NOW + timedelta(minutes=6), tick=False):
        assert store.get("code-1") is None


def test_deleting_one_code_leaves_the_others():
    store = AuthorizationCodeStore()
    store.add(a_code("code-1"))
    store.add(a_code("code-2"))

    store.delete("code-1")

    with time_machine.travel(NOW, tick=False):
        assert store.get("code-1") is None
        assert store.get("code-2") is not None


def test_deleting_an_unknown_code_is_a_no_op():
    AuthorizationCodeStore().delete("never-existed")


# --- RefreshTokenStore (strict rotation) -----------------------------------


def test_a_remembered_refresh_token_is_current(tmp_path):
    store = RefreshTokenStore(tmp_path / "refresh.json")
    store.remember("client-1", "jti-1", NOW + timedelta(days=30))

    with time_machine.travel(NOW, tick=False):
        assert store.is_current("client-1", "jti-1") is True


def test_a_rotated_out_jti_is_no_longer_current(tmp_path):
    # Strict rotation: remembering a new jti invalidates the previous one, so
    # a replayed old refresh token is refused.
    store = RefreshTokenStore(tmp_path / "refresh.json")
    store.remember("client-1", "jti-old", NOW + timedelta(days=30))
    store.remember("client-1", "jti-new", NOW + timedelta(days=30))

    with time_machine.travel(NOW, tick=False):
        assert store.is_current("client-1", "jti-old") is False
        assert store.is_current("client-1", "jti-new") is True


def test_an_unknown_client_has_no_current_token(tmp_path):
    store = RefreshTokenStore(tmp_path / "refresh.json")

    assert store.is_current("client-1", "jti-1") is False


def test_an_expired_refresh_entry_is_not_current(tmp_path):
    store = RefreshTokenStore(tmp_path / "refresh.json")
    store.remember("client-1", "jti-1", NOW + timedelta(days=30))

    with time_machine.travel(NOW + timedelta(days=31), tick=False):
        assert store.is_current("client-1", "jti-1") is False


def test_revoking_a_client_clears_its_current_token(tmp_path):
    store = RefreshTokenStore(tmp_path / "refresh.json")
    store.remember("client-1", "jti-1", NOW + timedelta(days=30))

    store.revoke("client-1")

    with time_machine.travel(NOW, tick=False):
        assert store.is_current("client-1", "jti-1") is False


def test_the_refresh_ledger_survives_a_restart(tmp_path):
    path = tmp_path / "refresh.json"
    RefreshTokenStore(path).remember("client-1", "jti-1", NOW + timedelta(days=30))

    with time_machine.travel(NOW, tick=False):
        assert RefreshTokenStore(path).is_current("client-1", "jti-1") is True


def test_clients_do_not_share_a_refresh_slot(tmp_path):
    store = RefreshTokenStore(tmp_path / "refresh.json")
    store.remember("client-1", "jti-1", NOW + timedelta(days=30))
    store.remember("client-2", "jti-2", NOW + timedelta(days=30))

    with time_machine.travel(NOW, tick=False):
        assert store.is_current("client-1", "jti-1") is True
        assert store.is_current("client-2", "jti-2") is True
        assert store.is_current("client-1", "jti-2") is False


def test_the_refresh_file_is_not_group_or_world_readable(tmp_path):
    path = tmp_path / "refresh.json"
    RefreshTokenStore(path).remember("client-1", "jti-1", NOW + timedelta(days=30))

    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, oct(path.stat().st_mode)


def test_an_absent_refresh_file_is_an_empty_ledger(tmp_path):
    store = RefreshTokenStore(tmp_path / "does-not-exist.json")

    assert store.is_current("client-1", "jti-1") is False


def test_a_corrupt_refresh_file_fails_loudly(tmp_path):
    path = tmp_path / "refresh.json"
    path.write_text("}{", encoding="utf-8")

    with pytest.raises(StoreError):
        RefreshTokenStore(path)
