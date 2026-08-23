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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from authlib.oauth2 import AuthorizationServer
from authlib.oauth2.rfc6749 import ClientMixin, OAuth2Request
from authlib.oauth2.rfc6749.grants import AuthorizationCodeGrant, RefreshTokenGrant
from authlib.oauth2.rfc6749.requests import BasicOAuth2Payload, JsonPayload, JsonRequest
from authlib.oauth2.rfc6750 import BearerTokenGenerator
from authlib.oauth2.rfc7591 import ClientRegistrationEndpoint
from authlib.oauth2.rfc7636 import CodeChallenge

from zoho_mcp.oauth.store import (
    AuthorizationCode,
    AuthorizationCodeStore,
    ClientStore,
    RefreshTokenStore,
    RegisteredClient,
)
from zoho_mcp.oauth.tokens import REFRESH_TOKEN_USE, TokenError, TokenSigner

# Authorization codes are exchanged within seconds of issue; five minutes is
# generous headroom for a slow consent-page round trip.
DEFAULT_CODE_TTL = timedelta(minutes=5)
# The operator subject stamped into tokens. Single-user, so it's a constant
# name rather than a looked-up identity.
DEFAULT_SUBJECT = "owner"
# Every endpoint auth method this server accepts: "none" for Claude's public
# PKCE client, the secret-bearing methods for a confidential one.
_TOKEN_ENDPOINT_AUTH_METHODS = ["none", "client_secret_post", "client_secret_basic"]


def _now() -> datetime:
    return datetime.now(UTC)


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


@dataclass(frozen=True)
class OAuthResponse:
    """A framework-neutral response the HTTP layer turns into a real one.

    Keeps this module free of any web framework: Authlib hands ``(status, body,
    headers)`` to :meth:`ZohoAuthorizationServer.handle_response`, which wraps
    them here, and ``http_app`` renders it into a Starlette response.
    """

    status: int
    body: Any
    headers: list


def build_oauth2_request(
    method: str, uri: str, params: dict, headers: dict | None = None
) -> OAuth2Request:
    """Build an Authlib ``OAuth2Request`` with its payload already attached.

    Authlib 1.7 split the request from its parsed parameters (the "payload"),
    and its grants read everything through ``request.payload``. Both the HTTP
    layer and the tests build a request this way, so the payload wiring lives
    in one place rather than being re-derived per caller.
    """
    request = OAuth2Request(method, uri, headers=headers)
    fields = dict(params)
    request.payload = BasicOAuth2Payload(fields)
    # Authlib 1.7 reads authorize-request parameters from request.payload but
    # still reads token-request parameters (code, redirect_uri, code_verifier,
    # client auth) from request.form, which is backed by this. Populating both
    # from the same fields lets one builder serve both endpoints. Assigned
    # directly rather than through the constructor's deprecated ``body=`` so no
    # deprecation warning fires.
    request._body = fields
    return request


class _AuthlibAuthorizationCode:
    """Adapts a stored :class:`AuthorizationCode` to what Authlib's grant reads.

    Keeps ``store.py``'s record free of Authlib naming: the grant wants
    ``get_redirect_uri``/``get_scope`` methods, ``code_challenge`` and
    ``code_challenge_method`` attributes (read by the PKCE extension), and a
    ``user_id``; the record stays a plain dataclass.
    """

    def __init__(self, record: AuthorizationCode) -> None:
        self.record = record
        self.code_challenge = record.code_challenge
        self.code_challenge_method = record.code_challenge_method
        self.user_id = record.subject

    def get_redirect_uri(self) -> str:
        return self.record.redirect_uri

    def get_scope(self) -> str:
        return self.record.scope


class ZohoAuthorizationCodeGrant(AuthorizationCodeGrant):
    """The authorization-code grant, backed by our code store.

    PKCE is enforced by the ``CodeChallenge`` extension registered alongside
    it; this class only persists and retrieves codes. ``self.server`` is the
    :class:`ZohoAuthorizationServer`, which carries the stores.
    """

    TOKEN_ENDPOINT_AUTH_METHODS = _TOKEN_ENDPOINT_AUTH_METHODS

    def save_authorization_code(self, code: str, request: Any) -> str:
        data = request.payload.data
        self.server.code_store.add(
            AuthorizationCode(
                code=code,
                client_id=request.client.get_client_id(),
                redirect_uri=request.payload.redirect_uri,
                scope=request.scope or "",
                code_challenge=data.get("code_challenge", ""),
                code_challenge_method=data.get("code_challenge_method", ""),
                subject=request.user,
                expires_at=_now() + self.server.code_ttl,
            )
        )
        return code

    def query_authorization_code(
        self, code: str, client: Any
    ) -> _AuthlibAuthorizationCode | None:
        record = self.server.code_store.get(code)
        if record is None or record.client_id != client.get_client_id():
            return None
        return _AuthlibAuthorizationCode(record)

    def delete_authorization_code(
        self, authorization_code: _AuthlibAuthorizationCode
    ) -> None:
        self.server.code_store.delete(authorization_code.record.code)

    def authenticate_user(self, authorization_code: _AuthlibAuthorizationCode) -> str:
        return authorization_code.user_id


class _AuthlibRefreshToken:
    """The credential Authlib's refresh grant carries between its steps."""

    def __init__(self, client_id: str, subject: str, scope: str) -> None:
        self._client_id = client_id
        self.user_id = subject
        self._scope = scope

    def check_client(self, client: Any) -> bool:
        return client.get_client_id() == self._client_id

    def get_scope(self) -> str:
        return self._scope


class ZohoRefreshTokenGrant(RefreshTokenGrant):
    """Refresh grant with strict rotation and reuse detection.

    ``authenticate_refresh_token`` verifies the JWT *and* checks it is the
    client's current one. A token that verifies but has been rotated out is a
    replay -- possibly a stolen token -- so the whole chain is revoked and the
    request refused. Issuing the new token records its jti as current, which is
    what strands the old one, so ``revoke_old_credential`` has nothing to do.
    """

    TOKEN_ENDPOINT_AUTH_METHODS = _TOKEN_ENDPOINT_AUTH_METHODS
    INCLUDE_NEW_REFRESH_TOKEN = True

    def authenticate_refresh_token(
        self, refresh_token: str
    ) -> _AuthlibRefreshToken | None:
        try:
            info = self.server.signer.verify(
                refresh_token, expect_use=REFRESH_TOKEN_USE
            )
        except TokenError:
            return None
        client_id = self.request.client.get_client_id()
        if not self.server.refresh_store.is_current(client_id, info.jti):
            # Verifies but isn't current: a rotated-out token being replayed.
            # Treat it as theft and strand the chain rather than reissue.
            self.server.refresh_store.revoke(client_id)
            return None
        return _AuthlibRefreshToken(client_id, info.subject, " ".join(info.scopes))

    def authenticate_user(self, credential: _AuthlibRefreshToken) -> str:
        return credential.user_id

    def revoke_old_credential(self, credential: _AuthlibRefreshToken) -> None:
        # Deliberately empty: remembering the new refresh jti (in save_token)
        # already overwrote the client's slot, so the old token is no longer
        # current. Revoking here would wipe the slot we just advanced.
        return None


class _JsonPayload(JsonPayload):
    """A concrete JSON payload -- Authlib's base leaves ``data`` abstract."""

    def __init__(self, data: dict) -> None:
        self._data = data

    @property
    def data(self) -> dict:
        return self._data


def build_json_request(
    method: str, uri: str, json_body: dict, headers: dict | None = None
) -> JsonRequest:
    """Build an Authlib ``JsonRequest`` with its JSON payload attached.

    The registration endpoint reads the request body from ``request.payload.
    data``; this is the JSON counterpart of :func:`build_oauth2_request`.
    """
    request = JsonRequest(method, uri, headers=headers)
    request.payload = _JsonPayload(dict(json_body))
    return request


class ZohoClientRegistration(ClientRegistrationEndpoint):
    """Dynamic Client Registration (RFC 7591), backed by the client store.

    Open registration: Claude self-registers without an initial access token,
    which is fine because registration grants no access on its own -- the gate
    that matters is the operator approving at ``/authorize``. A client that
    asks for ``token_endpoint_auth_method: none`` (Claude's PKCE shape) is
    stored as public, with no secret; anything else gets a generated secret.
    """

    def authenticate_token(self, request: Any) -> bool:
        return True

    def get_server_metadata(self) -> None:
        # No server-metadata constraints on what a client may register for;
        # our grants and the operator gate are what actually bound a client.
        return None

    def resolve_public_key(self, request: Any) -> None:
        # Only needed to verify a software_statement JWT, which we don't accept.
        return None

    def generate_client_secret(self, request: Any) -> str:
        if request.payload.data.get("token_endpoint_auth_method", "none") == "none":
            return ""  # public client: PKCE stands in for a secret
        return super().generate_client_secret(request)

    def save_client(
        self, client_info: dict, client_metadata: dict, request: Any
    ) -> RegisteredClient:
        record = RegisteredClient(
            client_id=client_info["client_id"],
            # "" (a public client) is stored as None so the endpoint-auth check
            # treats it as public rather than as an empty-secret confidential.
            client_secret=client_info["client_secret"] or None,
            redirect_uris=tuple(client_metadata.get("redirect_uris", ())),
            metadata=dict(client_metadata),
            issued_at=client_info["client_id_issued_at"],
        )
        self.server.client_store.add(record)
        return record


class ZohoAuthorizationServer(AuthorizationServer):
    """Authlib's authorization server, bound to our stores and JWT signer.

    Supplies the framework adapters Authlib's base leaves abstract. They stay
    framework-neutral: requests arrive already built (the HTTP layer or a test
    constructs the ``OAuth2Request``), and responses go out as
    :class:`OAuthResponse` for the HTTP layer to render -- so nothing here
    imports a web framework.
    """

    def __init__(
        self,
        *,
        signer: TokenSigner,
        client_store: ClientStore,
        code_store: AuthorizationCodeStore,
        refresh_store: RefreshTokenStore,
        code_ttl: timedelta,
    ) -> None:
        super().__init__(scopes_supported=list(SUPPORTED_SCOPES))
        self.signer = signer
        self.client_store = client_store
        self.code_store = code_store
        self.refresh_store = refresh_store
        self.code_ttl = code_ttl
        self.register_token_generator(
            "default",
            build_token_generator(signer, int(signer.access_ttl.total_seconds())),
        )
        self.register_grant(ZohoAuthorizationCodeGrant, [CodeChallenge(required=True)])
        self.register_grant(ZohoRefreshTokenGrant)
        self.register_endpoint(ZohoClientRegistration)

    def query_client(self, client_id: str) -> AuthlibClient | None:
        record = self.client_store.get(client_id)
        return AuthlibClient(record) if record is not None else None

    def save_token(self, token: dict, request: Any) -> None:
        client = request.client
        if client is not None:
            record_issued_refresh_token(
                self.refresh_store, self.signer, token, client.get_client_id()
            )

    def create_oauth2_request(self, request: Any) -> OAuth2Request:
        # The request is already an OAuth2Request built by the caller; there is
        # nothing to parse from a framework object here.
        return request

    def create_json_request(self, request: Any) -> Any:
        return request

    def handle_response(self, status: int, body: Any, headers: list) -> OAuthResponse:
        return OAuthResponse(status=status, body=body, headers=headers)

    def send_signal(self, name: str, *args: Any, **kwargs: Any) -> None:
        # Authlib emits framework signals (e.g. token issued); this server has
        # no signal system, so they are dropped rather than raising the base's
        # NotImplementedError.
        return None


def create_authorization_server(
    *,
    signer: TokenSigner,
    client_store: ClientStore,
    code_store: AuthorizationCodeStore,
    refresh_store: RefreshTokenStore,
    code_ttl: timedelta = DEFAULT_CODE_TTL,
) -> ZohoAuthorizationServer:
    """Assemble the authorization server from the stores and signer."""
    return ZohoAuthorizationServer(
        signer=signer,
        client_store=client_store,
        code_store=code_store,
        refresh_store=refresh_store,
        code_ttl=code_ttl,
    )
