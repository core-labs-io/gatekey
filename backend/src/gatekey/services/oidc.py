"""OIDC relying-party helpers (Phase 2, BD-3).

Standard, provider-agnostic authorization-code flow with PKCE (S256) for a
confidential client - see `docs/design/phase-2-multi-tenant-governance-
design.md` section 2.1. The browser never talks to the IdP's token endpoint;
`exchange_code` runs server-to-server through the shared pooled
`httpx.AsyncClient` on `app.state` (same client-reuse precedent as provider
inference calls).

ID-token validation (issuer, audience, expiry, signature via JWKS) uses
PyJWT (`PyJWT[crypto]`) - a minimal, well-known library - rather than any
hand-rolled crypto. Only asymmetric algorithms are accepted (see
`_ALLOWED_ID_TOKEN_ALGORITHMS`), so an HS256 alg-confusion token signed with
the public key material can never validate.

Login-state cookie signing-key choice (documented per BD-3): the short-lived
state/nonce/PKCE-verifier payload travels in a signed cookie (no DB row for
a value that lives seconds), signed as an HS256 JWT with a key DERIVED from
`GATEKEY_MASTER_KEY` (HMAC-SHA256 over a fixed domain-separation label) -
NOT from `GATEKEY_ADMIN_TOKEN`. The master key is guaranteed 32 bytes of
real random material (validated at startup), while the admin token is an
operator-chosen string with no minimum-entropy guarantee; deriving (rather
than using the master key directly) keeps the AES-256 encryption key and
this MAC key cryptographically independent.

The discovery-document and JWKS caches are short-TTL, in-process dicts -
same "process-local, no cross-worker convergence" trade as
`ModelPolicyCache`, fine for documents that are public and change rarely.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from gatekey.config import Settings
from gatekey.errors import OidcUnavailableError, UnauthorizedError

logger = logging.getLogger("gatekey")

# Name of the short-lived signed cookie carrying state/nonce/PKCE verifier
# between /sso/login and /sso/callback.
LOGIN_STATE_COOKIE_NAME = "gatekey_oidc_login"
# A login round trip through the IdP takes seconds; 10 minutes is generous.
LOGIN_STATE_TTL_SECONDS = 600

_DISCOVERY_CACHE_TTL_SECONDS = 300.0

_ALLOWED_ID_TOKEN_ALGORITHMS = [
    "RS256", "RS384", "RS512",
    "PS256", "PS384", "PS512",
    "ES256", "ES384", "ES512",
]

_LOGIN_STATE_KEY_LABEL = b"gatekey-oidc-login-state-v1"

# issuer_url -> (monotonic_expiry, document). Module-level short-TTL caches;
# cleared implicitly by TTL, explicitly by tests via `_clear_caches`.
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _clear_caches() -> None:
    """Test hook - reset the in-process discovery/JWKS caches."""
    _discovery_cache.clear()
    _jwks_cache.clear()


async def fetch_discovery_document(
    http_client: httpx.AsyncClient, issuer_url: str
) -> dict[str, Any]:
    """Fetch (and short-TTL cache) the issuer's OIDC discovery document."""
    issuer = issuer_url.rstrip("/")
    cached = _discovery_cache.get(issuer)
    if cached is not None and cached[0] > time.monotonic():
        return cached[1]
    url = f"{issuer}/.well-known/openid-configuration"
    try:
        response = await http_client.get(url)
        response.raise_for_status()
        document = response.json()
    except Exception as exc:
        # Never echo the IdP response body - availability info only.
        raise OidcUnavailableError(
            "Failed to fetch the OIDC discovery document from the configured issuer."
        ) from exc
    _discovery_cache[issuer] = (time.monotonic() + _DISCOVERY_CACHE_TTL_SECONDS, document)
    return document


async def _fetch_jwks(
    http_client: httpx.AsyncClient, jwks_uri: str, *, force: bool = False
) -> dict[str, Any]:
    cached = _jwks_cache.get(jwks_uri)
    if not force and cached is not None and cached[0] > time.monotonic():
        return cached[1]
    try:
        response = await http_client.get(jwks_uri)
        response.raise_for_status()
        jwks = response.json()
    except Exception as exc:
        raise OidcUnavailableError(
            "Failed to fetch the OIDC signing keys (JWKS) from the configured issuer."
        ) from exc
    _jwks_cache[jwks_uri] = (time.monotonic() + _DISCOVERY_CACHE_TTL_SECONDS, jwks)
    return jwks


def build_authorization_request(
    settings: Settings, discovery: dict[str, Any]
) -> tuple[str, dict[str, str]]:
    """Build the IdP authorization URL plus the login-state payload.

    Returns `(authorization_url, login_state)` where `login_state` is the
    `{state, nonce, code_verifier}` dict destined for the signed cookie.
    """
    state = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(16)
    code_verifier = secrets.token_urlsafe(32)
    code_challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    params = {
        "response_type": "code",
        "client_id": settings.GATEKEY_OIDC_CLIENT_ID,
        "redirect_uri": settings.GATEKEY_OIDC_REDIRECT_URI,
        "scope": "openid profile email",
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    url = f"{discovery['authorization_endpoint']}?{urlencode(params)}"
    return url, {"state": state, "nonce": nonce, "code_verifier": code_verifier}


def _login_state_key(settings: Settings) -> bytes:
    """Derive the login-state MAC key from the master key - module docstring."""
    return hmac.new(settings.master_key_bytes(), _LOGIN_STATE_KEY_LABEL, hashlib.sha256).digest()


def encode_login_state(payload: dict[str, str], settings: Settings) -> str:
    """Sign the state/nonce/verifier payload as a short-lived HS256 JWT."""
    return jwt.encode(
        {**payload, "exp": int(time.time()) + LOGIN_STATE_TTL_SECONDS},
        _login_state_key(settings),
        algorithm="HS256",
    )


def decode_login_state(value: str, settings: Settings) -> dict[str, Any] | None:
    """Verify/decode the login-state cookie. None on any failure (bad
    signature, expired, malformed) - the caller maps that to one generic 401."""
    try:
        return jwt.decode(value, _login_state_key(settings), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


async def exchange_code(
    http_client: httpx.AsyncClient,
    discovery: dict[str, Any],
    settings: Settings,
    *,
    code: str,
    code_verifier: str,
) -> dict[str, Any]:
    """Exchange the authorization code at the IdP token endpoint
    (server-to-server, confidential client + PKCE verifier)."""
    try:
        response = await http_client.post(
            discovery["token_endpoint"],
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.GATEKEY_OIDC_REDIRECT_URI,
                "client_id": settings.GATEKEY_OIDC_CLIENT_ID,
                "client_secret": settings.GATEKEY_OIDC_CLIENT_SECRET,
                "code_verifier": code_verifier,
            },
        )
    except Exception as exc:
        raise OidcUnavailableError("The OIDC token endpoint was unreachable.") from exc
    if response.status_code != 200:
        # A rejected code/verifier is an auth failure, not availability;
        # generic message, never the IdP's response body.
        logger.info("oidc_code_exchange_rejected", extra={"status": response.status_code})
        raise UnauthorizedError("SSO login failed.")
    return response.json()


async def validate_id_token(
    http_client: httpx.AsyncClient,
    discovery: dict[str, Any],
    settings: Settings,
    *,
    id_token: str,
    expected_nonce: str,
) -> dict[str, Any]:
    """Validate the ID token (signature via JWKS, issuer, audience, expiry,
    nonce) and return its claims."""
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError:
        raise UnauthorizedError("SSO login failed.") from None

    kid = header.get("kid")
    jwks = await _fetch_jwks(http_client, discovery["jwks_uri"])
    key = _find_key(jwks, kid)
    if key is None:
        # Key rotation may have outrun the cache - refetch once, bypassing it.
        jwks = await _fetch_jwks(http_client, discovery["jwks_uri"], force=True)
        key = _find_key(jwks, kid)
    if key is None:
        raise UnauthorizedError("SSO login failed.")

    try:
        claims = jwt.decode(
            id_token,
            key=key,
            algorithms=_ALLOWED_ID_TOKEN_ALGORITHMS,
            audience=settings.GATEKEY_OIDC_CLIENT_ID,
            issuer=discovery["issuer"],
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError:
        raise UnauthorizedError("SSO login failed.") from None

    token_nonce = claims.get("nonce")
    if not isinstance(token_nonce, str) or not hmac.compare_digest(token_nonce, expected_nonce):
        raise UnauthorizedError("SSO login failed.")
    return claims


def _find_key(jwks: dict[str, Any], kid: str | None) -> Any | None:
    """Pick the JWK matching `kid` (or the only key if the IdP omits kids).

    Returns the PyJWT key object usable by `jwt.decode`, or None.
    """
    keys = jwks.get("keys") or []
    matching = [k for k in keys if kid is None or k.get("kid") == kid]
    if not matching:
        return None
    try:
        return jwt.PyJWK.from_dict(matching[0]).key
    except jwt.PyJWKError:
        return None
