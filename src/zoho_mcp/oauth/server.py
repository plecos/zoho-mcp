"""The Authlib authorization server, wired to our stores and token signer.

This is where OAuth #2's protocol machinery lives. Authlib owns the parts that
carry interop and spec risk -- the RFC 6749 grant state machine, OAuth2 error
responses, PKCE (RFC 7636), dynamic client registration (RFC 7591) -- and this
module supplies the hooks that connect that machinery to *our* storage
(``store.py``) and *our* stateless JWTs (``tokens.py``).

Built bottom-up: the client wrapper first, since every grant reaches a client
through it, then the grants and token generation, then assembly.
"""

import hmac
from typing import Any

from authlib.oauth2.rfc6749 import ClientMixin
from authlib.oauth2.rfc6750 import BearerTokenGenerator

from zoho_mcp.oauth.store import RefreshTokenStore, RegisteredClient
from zoho_mcp.oauth.tokens import REFRESH_TOKEN_USE, TokenSigner

# The single scope this server grants for now. Kept as a tuple so widening to
# per-tool scopes later is a data change, not a shape change.
DEFAULT_SCOPE = "zoho.tools"
SUPPORTED_SCOPES: tuple[str, ...] = (DEFAULT_SCOPE,)

_ALLOWED_GRANT_TYPES = frozenset({"authorization_code", "refresh_token"})
_CONFIDENTIAL_AUTH_METHODS = frozenset({"client_secret_basic", "client_secret_post"})


class AuthlibClient(ClientMixin):
    """Adapts a stored :class:`RegisteredClient` to Authlib's client interface.

    Authlib's grants call these methods to make the security decisions that
    gate a flow: which redirect URIs are legitimate, whether the client may use
    a grant, and how it authenticates. The stored record is the source of
    truth; nothing here trusts the request.
    """

    def __init__(
        self,
        client: RegisteredClient,
        supported_scopes: tuple[str, ...] = SUPPORTED_SCOPES,
    ) -> None:
        self._client = client
        self._supported_scopes = supported_scopes

    def get_client_id(self) -> str:
        return self._client.client_id

    def get_default_redirect_uri(self) -> str | None:
        # None -- not "" -- when none is registered, so Authlib treats it as
        # "no default" and requires the request to name one it can match.
        return self._client.redirect_uris[0] if self._client.redirect_uris else None

    def check_redirect_uri(self, redirect_uri: str) -> bool:
        # Exact membership only. A prefix or suffix match here is an open
        # redirect: an attacker appends their own host and rides the flow.
        return redirect_uri in self._client.redirect_uris

    def check_client_secret(self, client_secret: str) -> bool:
        if self._client.client_secret is None:
            return False
        return hmac.compare_digest(client_secret, self._client.client_secret)

    def check_endpoint_auth_method(self, method: str, endpoint: str) -> bool:
        # A public client (no secret, the shape Claude registers as with PKCE)
        # authenticates as "none"; a confidential one by presenting its secret.
        if self._client.client_secret is None:
            return method == "none"
        return method in _CONFIDENTIAL_AUTH_METHODS

    def check_response_type(self, response_type: str) -> bool:
        return response_type == "code"

    def check_grant_type(self, grant_type: str) -> bool:
        return grant_type in _ALLOWED_GRANT_TYPES

    def get_allowed_scope(self, scope: str) -> str:
        if not scope:
            return ""
        granted = [s for s in scope.split() if s in self._supported_scopes]
        return " ".join(granted)


def build_token_generator(
    signer: TokenSigner, access_ttl_seconds: int
) -> BearerTokenGenerator:
    """Wire our JWT signer into Authlib's bearer-token generator.

    Authlib calls the two generators with ``(client, grant_type, user, scope)``
    when issuing a token; ``user`` is whatever the grant's ``authenticate_user``
    returned (the operator subject), and ``scope`` is the granted, space-joined
    scope. We turn those into our own signed JWTs -- the tokens stay stateless
    and this stays the only place minting is wired to the OAuth flow.
    """

    def access(client: Any, grant_type: str, user: str, scope: str) -> str:
        return signer.mint_access_token(user, scope.split() if scope else [])

    def refresh(client: Any, grant_type: str, user: str, scope: str) -> str:
        return signer.mint_refresh_token(user, scope.split() if scope else [])

    def expires(client: Any, grant_type: str) -> int:
        return access_ttl_seconds

    return BearerTokenGenerator(access, refresh, expires)


def record_issued_refresh_token(
    store: RefreshTokenStore, signer: TokenSigner, token: dict, client_id: str
) -> None:
    """Record a freshly issued refresh token as the client's current one.

    Called from ``save_token`` -- the one hook every issued token passes
    through, whether from the initial code exchange or a later refresh -- so
    the rotation ledger is advanced in exactly one place. Recording the new
    ``jti`` is also what strands the previous one: :meth:`RefreshTokenStore.
    remember` overwrites the slot, so the old refresh token stops being
    current the instant a new one is minted.

    A token dict without a refresh token (a grant issued without rotation) is a
    no-op.
    """
    refresh_token = token.get("refresh_token")
    if not refresh_token:
        return
    info = signer.verify(refresh_token, expect_use=REFRESH_TOKEN_USE)
    store.remember(client_id, info.jti, info.expires_at)
