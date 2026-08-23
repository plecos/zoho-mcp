"""Tests for the JWT layer of the self-contained OAuth authorization server.

This is OAuth #2 -- a token this server issues to an MCP *client* (Claude) and
later verifies. It is unrelated to the Zoho refresh token, which is OAuth #1.

The cryptography belongs to joserfc; these tests pin the parts that are *ours*
to get right: the claim shape, the access/refresh distinction, lifetimes, and
-- most of all -- that every way a token can be wrong (tampered, wrong key,
expired, wrong issuer/audience, `alg: none`, garbage) is rejected as a clean
``TokenError`` rather than trusted or leaked as a joserfc traceback. That last
group is the whole security value of the layer, so it gets the most tests.
"""

import base64
import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import time_machine
from joserfc import jwt
from joserfc.jwk import OctKey

from zoho_mcp.oauth.tokens import (
    ACCESS_TOKEN_USE,
    MAX_SIGNING_KEY_BYTES,
    MIN_SIGNING_KEY_BYTES,
    REFRESH_TOKEN_USE,
    TokenError,
    TokenInfo,
    TokenSigner,
    load_or_create_signing_key,
)

ISSUER = "https://mail.example.com"
AUDIENCE = "https://mail.example.com/mcp"
KEY = "k" * 43  # what secrets.token_urlsafe(32) produces, roughly


def signer(**overrides) -> TokenSigner:
    kwargs = {"signing_key": KEY, "issuer": ISSUER, "audience": AUDIENCE}
    kwargs.update(overrides)
    return TokenSigner(**kwargs)


# --- minting and round-tripping --------------------------------------------


def test_an_access_token_round_trips_with_its_claims():
    s = signer()
    token = s.mint_access_token("owner", ["zoho.tools"])

    info = s.verify(token)

    assert isinstance(info, TokenInfo)
    assert info.subject == "owner"
    assert info.scopes == ("zoho.tools",)
    assert info.token_use == ACCESS_TOKEN_USE


def test_a_refresh_token_round_trips_and_is_marked_refresh():
    s = signer()
    info = s.verify(s.mint_refresh_token("owner", ["zoho.tools"]))

    assert info.token_use == REFRESH_TOKEN_USE
    assert info.subject == "owner"
    assert info.scopes == ("zoho.tools",)


def test_every_token_carries_a_unique_id():
    s = signer()
    a = s.verify(s.mint_access_token("owner", ["zoho.tools"]))
    b = s.verify(s.mint_access_token("owner", ["zoho.tools"]))

    assert a.jti and b.jti and a.jti != b.jti


def test_empty_scopes_are_an_empty_tuple_not_a_blank_string():
    s = signer()
    info = s.verify(s.mint_access_token("owner", []))

    assert info.scopes == ()


def test_multiple_scopes_survive_the_space_join_and_split():
    s = signer()
    info = s.verify(s.mint_access_token("owner", ["a", "b", "c"]))

    assert info.scopes == ("a", "b", "c")


def test_the_refresh_token_outlives_the_access_token():
    # Rotation depends on the refresh token still being valid long after the
    # access token it was issued alongside has expired.
    s = signer(access_ttl=timedelta(minutes=5), refresh_ttl=timedelta(days=7))
    access = s.verify(s.mint_access_token("owner", ["a"]))
    refresh = s.verify(s.mint_refresh_token("owner", ["a"]))

    assert refresh.expires_at > access.expires_at


# --- the access/refresh distinction ----------------------------------------


def test_a_refresh_token_is_refused_where_an_access_token_is_required():
    # The request gate asks for access; a refresh token (longer-lived, minted
    # for the token endpoint) must not open the tools.
    s = signer()
    refresh = s.mint_refresh_token("owner", ["a"])

    with pytest.raises(TokenError):
        s.verify(refresh, expect_use=ACCESS_TOKEN_USE)


def test_an_access_token_is_refused_where_a_refresh_token_is_required():
    s = signer()
    access = s.mint_access_token("owner", ["a"])

    with pytest.raises(TokenError):
        s.verify(access, expect_use=REFRESH_TOKEN_USE)


def test_expect_use_matching_the_token_passes():
    s = signer()
    access = s.mint_access_token("owner", ["a"])

    assert s.verify(access, expect_use=ACCESS_TOKEN_USE).subject == "owner"


# --- rejection paths (the security surface) --------------------------------


def test_a_tampered_token_is_rejected():
    s = signer()
    token = s.mint_access_token("owner", ["a"])
    tampered = token[:-3] + ("AAA" if not token.endswith("AAA") else "BBB")

    with pytest.raises(TokenError):
        s.verify(tampered)


def test_a_token_signed_with_another_key_is_rejected():
    minted = signer(signing_key="k" * 43).mint_access_token("owner", ["a"])

    with pytest.raises(TokenError):
        signer(signing_key="d" * 43).verify(minted)


def test_an_expired_token_is_rejected():
    s = signer(access_ttl=timedelta(minutes=5))
    with time_machine.travel(datetime(2026, 1, 1, tzinfo=UTC), tick=False):
        token = s.mint_access_token("owner", ["a"])

    with (
        time_machine.travel(datetime(2026, 1, 1, 0, 6, tzinfo=UTC), tick=False),
        pytest.raises(TokenError),
    ):
        s.verify(token)


def test_a_token_from_another_issuer_is_rejected():
    minted = signer(issuer="https://evil.example.com").mint_access_token("o", ["a"])

    with pytest.raises(TokenError):
        signer().verify(minted)


def test_a_token_for_another_audience_is_rejected():
    # RFC 8707 audience binding: a token minted for a different resource must
    # not be replayable against this one.
    minted = signer(audience="https://other.example.com/mcp").mint_access_token(
        "o", ["a"]
    )

    with pytest.raises(TokenError):
        signer().verify(minted)


def test_a_token_with_alg_none_is_rejected():
    # The classic JWT downgrade: an attacker strips the signature and sets the
    # algorithm to "none". Verification is pinned to HS256, so this is refused
    # rather than accepted as unsigned-but-valid.
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=")
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {"iss": ISSUER, "aud": AUDIENCE, "sub": "owner", "exp": 9999999999}
        ).encode()
    ).rstrip(b"=")
    forged = f"{header.decode()}.{payload.decode()}."

    with pytest.raises(TokenError):
        signer().verify(forged)


def test_a_token_signed_with_a_stronger_algorithm_is_still_rejected():
    # Not just "none" -- anything other than the pinned HS256, so an attacker
    # can't pick an algorithm the verifier didn't intend to accept.
    key = OctKey.import_key(KEY)
    # joserfc won't encode HS384 unless explicitly allowed -- force it, so the
    # token exists to be rejected by the HS256-pinned verifier.
    forged = jwt.encode(
        {"alg": "HS384"}, {"iss": ISSUER, "aud": AUDIENCE}, key, algorithms=["HS384"]
    )

    with pytest.raises(TokenError):
        signer().verify(forged)


@pytest.mark.parametrize("garbage", ["", "not-a-jwt", "a.b.c", "....", "  "])
def test_malformed_strings_are_rejected_cleanly(garbage):
    with pytest.raises(TokenError):
        signer().verify(garbage)


def test_a_token_without_our_use_claim_is_rejected():
    # A token that validates on signature, issuer and audience but lacks the
    # kind claim was not minted here -- treat it as forged, not as a default.
    key = OctKey.import_key(KEY)
    foreign = jwt.encode(
        {"alg": "HS256"},
        {"iss": ISSUER, "aud": AUDIENCE, "sub": "owner", "exp": 9999999999},
        key,
    )

    with pytest.raises(TokenError):
        signer().verify(foreign)


def test_a_blank_signing_key_is_refused_at_construction():
    with pytest.raises(TokenError):
        signer(signing_key="   ")


def test_a_signing_key_below_the_minimum_length_is_refused():
    # RFC 7518: an HS256 key must be at least as large as the hash output
    # (256 bits / 32 bytes). A shorter one is a weak key, not a valid one.
    with pytest.raises(TokenError):
        signer(signing_key="x" * (MIN_SIGNING_KEY_BYTES - 1))


def test_a_signing_key_at_the_minimum_length_is_accepted():
    s = signer(signing_key="x" * MIN_SIGNING_KEY_BYTES)

    assert s.verify(s.mint_access_token("owner", ["a"])).subject == "owner"


def test_a_signing_key_at_the_maximum_length_is_accepted():
    s = signer(signing_key="x" * MAX_SIGNING_KEY_BYTES)

    assert s.verify(s.mint_access_token("owner", ["a"])).subject == "owner"


def test_a_signing_key_above_the_maximum_length_is_refused():
    # Past HMAC's block size a longer key adds no strength, so an oversized one
    # is a paste mistake (a whole file, a PEM blob) worth catching early.
    with pytest.raises(TokenError):
        signer(signing_key="x" * (MAX_SIGNING_KEY_BYTES + 1))


# --- the signing key on disk -----------------------------------------------


def test_load_or_create_generates_a_key_when_the_file_is_absent(tmp_path):
    path = tmp_path / "sub" / "signing_key.json"

    key = load_or_create_signing_key(path)

    assert key and key.strip()
    assert path.exists()


def test_load_or_create_returns_the_same_key_on_the_next_call(tmp_path):
    path = tmp_path / "signing_key.json"

    first = load_or_create_signing_key(path)
    second = load_or_create_signing_key(path)

    assert first == second


def test_the_generated_key_survives_a_restart(tmp_path):
    # A regenerated key would silently invalidate every token already issued,
    # forcing a re-auth on every process bounce -- so persistence is the point.
    path = tmp_path / "signing_key.json"
    key = load_or_create_signing_key(path)

    # A brand-new process reading the same file must sign identically.
    reloaded = load_or_create_signing_key(path)
    assert (
        TokenSigner(signing_key=key, issuer=ISSUER, audience=AUDIENCE)
        .verify(
            TokenSigner(
                signing_key=reloaded, issuer=ISSUER, audience=AUDIENCE
            ).mint_access_token("owner", ["a"])
        )
        .subject
        == "owner"
    )


def test_the_key_file_is_not_world_readable(tmp_path):
    # It forges access to every tool; it must not be readable by other users
    # on a shared host.
    path = tmp_path / "signing_key.json"
    load_or_create_signing_key(path)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode & 0o077 == 0, oct(mode)


def test_the_key_file_is_json_with_the_key_under_a_named_field(tmp_path):
    path = tmp_path / "signing_key.json"
    key = load_or_create_signing_key(path)

    data = json.loads(path.read_text())
    assert data["signing_key"] == key


def test_a_process_that_loses_the_create_race_reads_the_winners_key(
    tmp_path, monkeypatch
):
    # The documented "two server processes" scenario: both check for the file
    # before either writes it, so the loser still tries to create and must
    # recover by reading the winner's key rather than crashing on the
    # exclusive-create collision. Simulated by forcing the create path even
    # though the file already exists.
    path = tmp_path / "signing_key.json"
    winner = load_or_create_signing_key(path)

    monkeypatch.setattr(Path, "exists", lambda self: False)

    assert load_or_create_signing_key(path) == winner
