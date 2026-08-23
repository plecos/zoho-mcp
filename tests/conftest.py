"""Suite-wide fixtures.

The one thing here exists to keep the developer's own machine out of the
assertions. It caught a real regression: once `get_access_token` learned to
consult the OS credential store, three tests that asserted "unauthenticated
servers refuse calls" started passing on a laptop with a real Zoho token in
Credential Manager and would have failed in CI -- testing nothing in either
place.
"""

import pytest

from zoho_mcp.zoho.token_store import ENV_REFRESH_TOKEN_VAR


@pytest.fixture(autouse=True)
def no_real_credential_store(monkeypatch):
    """Make every token store look empty to every test by default.

    `ZohoTokenManager` reads its store when it holds no refresh token, which
    means any test touching that path would otherwise depend on whether the
    person running it happens to be authenticated. A test that genuinely wants
    a stored token overrides this by patching the same name, or by injecting
    its own store.

    Both stores are covered, not just keyring: a developer with
    ZOHO_REFRESH_TOKEN exported for a hosted run is the same hazard as one
    with a token in Credential Manager.
    """
    monkeypatch.setattr(
        "zoho_mcp.zoho.token_store.keyring.get_password", lambda service, key: None
    )
    monkeypatch.delenv(ENV_REFRESH_TOKEN_VAR, raising=False)
