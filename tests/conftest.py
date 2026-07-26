"""Suite-wide fixtures.

The one thing here exists to keep the developer's own machine out of the
assertions. It caught a real regression: once `get_access_token` learned to
consult the OS credential store, three tests that asserted "unauthenticated
servers refuse calls" started passing on a laptop with a real Zoho token in
Credential Manager and would have failed in CI -- testing nothing in either
place.
"""

import pytest


@pytest.fixture(autouse=True)
def no_real_credential_store(monkeypatch):
    """Make the OS credential store look empty to every test by default.

    `ZohoTokenManager` reads it when it holds no refresh token, which means
    any test touching that path would otherwise depend on whether the person
    running it happens to be authenticated. A test that genuinely wants a
    stored token overrides this by patching the same name.
    """
    monkeypatch.setattr("zoho_mcp.zoho.auth.load_refresh_token", lambda: None)
