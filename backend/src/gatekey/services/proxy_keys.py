"""Decrypts configured provider keys for outbound proxy calls (Phase 1.2, BD-6).

Why this module exists (not `services.provider_keys`)
-------------------------------------------------------
`services.provider_keys` deliberately exposes no "get decrypted key"
function - see its module docstring. That module owns the encrypted-at-rest
CRUD surface for the admin API; this module is the one and only place that
turns a `ProviderKey` row back into plaintext, and it exists specifically
because Phase 1.2's gateway route handlers need the raw credential to make
an outbound provider call. It reuses `provider_keys.get_key()` for the row
lookup (no duplicated query logic) and `encryption.decrypt_secret()` /
`encryption.build_aad()` for the actual decryption - it does not touch the
`provider_keys` table directly.

Secret hygiene (hard requirement, not a nice-to-have)
------------------------------------------------------
Every `ProviderCredential` returned from here holds decrypted secret
material and overrides `__repr__`/`__str__` to return a redacted
placeholder (see `ProviderCredential`) - this is a defense-in-depth
backstop so an accidental `logger.info(credential)`, f-string
interpolation, or exception-message interpolation can never leak
plaintext, even by mistake. Do not add a field to a credential dataclass
without checking the redacted repr still covers it, and never call
`.api_key` / `.service_account_json` for anything other than handing the
value directly to an outbound HTTP client.

Nothing in this module ever logs decrypted plaintext, including on error
paths - see `get_decrypted_provider_credential`'s docstring for exactly
which exceptions can propagate and why each one is safe to log/return.
"""

from __future__ import annotations

import json
from abc import ABC
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.provider_key import ProviderKey
from gatekey.services.encryption import KeyProvider, build_aad, decrypt_secret
from gatekey.services.provider_keys import get_key

_API_KEY_PROVIDERS = ("openai", "anthropic", "openrouter")     # AC-B1-1: openrouter added
_SERVICE_ACCOUNT_PROVIDERS = ("vertex_ai",)
_BASE_URL_BEARER_PROVIDERS = ("ollama",)                        # NEW, AC-B2-2


class ProviderKeyNotConfiguredError(Exception):
    """No `ProviderKey` row exists for this org/provider.

    Follows the same plain-`Exception`-with-`.message` pattern as
    `ProviderKeyServiceError` and its subclasses in `services.provider_keys`
    (this module does not import FastAPI/`errors.py` - the route-handler
    layer is responsible for catching this and translating it into a 404
    `errors.NotFoundError`, same as `provider_keys.get_key()` returning
    `None` is already handled in `api/v1/admin/providers.py`).
    """

    def __init__(self, provider: str) -> None:
        message = f"No key configured for provider '{provider}'."
        super().__init__(message)
        self.message = message
        self.provider = provider


class CredentialDecodeError(Exception):
    """Decryption succeeded but the plaintext isn't the expected JSON shape.

    This indicates data corruption (the row's AEAD tag verified, but the
    resulting plaintext doesn't match what `add_or_replace_key` originally
    serialized) rather than an authentication failure - `decrypt_secret`
    already raises `encryption.DecryptionError` for the auth-failure case.

    The message here is a fixed string and deliberately never includes
    `str(exc)` from the underlying `json.JSONDecodeError`/`KeyError`/
    `TypeError` - those exception messages can echo back a fragment of the
    decrypted plaintext (e.g. `json.JSONDecodeError` includes surrounding
    characters from the input it failed to parse), so they must never be
    logged or surfaced.
    """

    def __init__(self) -> None:
        super().__init__("Failed to decode a decrypted provider credential.")


class UnsupportedProviderCredentialError(Exception):
    """A `ProviderKey` row exists for a provider this module doesn't know how
    to shape into a credential.

    Should not be reachable in practice - `provider` is a Postgres enum
    column constrained to `providers.registry.SUPPORTED_PROVIDERS` - but
    kept as an explicit, safe failure mode (the provider name itself is not
    secret material) rather than an unhandled `KeyError`/`AttributeError`
    if this module falls behind a future provider addition.
    """

    def __init__(self, provider: str) -> None:
        message = f"No credential shape defined for provider '{provider}'."
        super().__init__(message)
        self.message = message
        self.provider = provider


class ProviderCredential(ABC):
    """Base class for decrypted provider credentials.

    Every subclass holds decrypted secret material. `__repr__`/`__str__`
    are overridden here to always return a redacted placeholder - never
    override either of these in a subclass to return anything else. This
    is what makes `logger.info(credential)`, `f"{credential}"`, or
    accidentally interpolating a credential into an exception message safe
    even if a future caller does it by mistake.
    """

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"<{type(self).__name__} REDACTED>"

    __str__ = __repr__

    def to_secret_payload(self) -> dict[str, Any]:
        """Inverse of `_credential_from_row`: reshape this decrypted
        credential back into the `secret_payload` dict shape
        `providers.base.ProviderValidator.validate()` expects (the same
        shape an admin's raw request body has on the key-creation path -
        see `services.provider_keys._serialize_secret_payload`/`_build_key_
        metadata`).

        Phase 4 (design doc section 6.2): used by `services.provider_key_
        health.refresh_single_provider_key_health` to validate the REAL
        decrypted credential on a health check, instead of a placeholder
        literal. The returned dict holds live secret material - callers
        must follow the same discipline as everywhere else in this module
        (never log it, never interpolate it into an exception message);
        handing it straight to `validator.validate()` is the only sanctioned
        use.
        """
        raise NotImplementedError


@dataclass(frozen=True, repr=False)
class ApiKeyCredential(ProviderCredential):
    """Decrypted bearer-style API key credential (openai, anthropic, openrouter)."""

    provider: str
    api_key: str

    def to_secret_payload(self) -> dict[str, Any]:
        return {"api_key": self.api_key}


@dataclass(frozen=True, repr=False)
class ServiceAccountCredential(ProviderCredential):
    """Decrypted service-account credential (vertex_ai).

    `project_id`/`location` are not secret - they come from the non-secret
    `ProviderKey.key_metadata` column - but are carried on the same
    credential object since a vertex_ai outbound call needs all three
    together. Only `service_account_json` is redacted-worthy; the redacted
    `__repr__`/`__str__` inherited from `ProviderCredential` still covers
    the whole object for simplicity and to avoid a caller having to
    remember which fields are safe to log.
    """

    provider: str
    service_account_json: dict[str, Any]
    project_id: str
    location: str

    def to_secret_payload(self) -> dict[str, Any]:
        return {
            "service_account_json": self.service_account_json,
            "project_id": self.project_id,
            "location": self.location,
        }


@dataclass(frozen=True, repr=False)
class OllamaCredential(ProviderCredential):
    """Decrypted base-url-plus-optional-bearer credential (ollama only).

    `bearer_token` is never `None` - an empty string means "not configured"
    (AC-B2-1/AC-B2-3), matching the one-representation discipline
    `OllamaKeyRequest` already enforces at the API layer (schemas/
    provider_key.py). `base_url` is not itself secret (sourced from the
    non-secret `key_metadata` column, same pattern as
    `ServiceAccountCredential.project_id`/`.location`), but the whole
    object still gets the inherited redacted `__repr__`/`__str__` for
    consistency - no per-field secrecy special-casing.
    """

    provider: str
    base_url: str
    bearer_token: str

    def to_secret_payload(self) -> dict[str, Any]:
        return {"base_url": self.base_url, "bearer_token": self.bearer_token}


def _credential_from_row(row: ProviderKey, provider: str, *, key_provider: KeyProvider) -> ProviderCredential:
    """Decrypt an already-fetched `ProviderKey` row into a `ProviderCredential`.

    Shared by `get_decrypted_provider_credential` (looks the row up itself)
    and `get_decrypted_provider_credential_from_row` (Phase 4: the caller
    already has the row, e.g. from `services.provider_key_health.
    select_provider_key`, and passing it straight through avoids a second DB
    round trip on the failover-aware credential-fetch path - design doc
    section 8's `fetch_credential`, extended, keyed by provider_key_id).
    """
    aad = build_aad(str(DEFAULT_ORG_ID), provider)
    # DecryptionError propagates as-is - see the public functions' docstrings.
    plaintext = decrypt_secret(
        row.ciphertext,
        nonce=row.nonce,
        auth_tag=row.auth_tag,
        aad=aad,
        key_provider=key_provider,
    )

    try:
        if provider in _API_KEY_PROVIDERS:
            api_key = json.loads(plaintext)
            if not isinstance(api_key, str):
                raise TypeError("decoded api_key credential was not a string")
            return ApiKeyCredential(provider=provider, api_key=api_key)

        if provider in _SERVICE_ACCOUNT_PROVIDERS:
            service_account_json = json.loads(plaintext)
            if not isinstance(service_account_json, dict):
                raise TypeError("decoded service_account credential was not an object")
            metadata = row.key_metadata
            return ServiceAccountCredential(
                provider=provider,
                service_account_json=service_account_json,
                project_id=metadata["project_id"],
                location=metadata["location"],
            )

        if provider in _BASE_URL_BEARER_PROVIDERS:
            bearer_token = json.loads(plaintext)
            if not isinstance(bearer_token, str):
                raise TypeError("decoded ollama bearer_token credential was not a string")
            metadata = row.key_metadata
            return OllamaCredential(
                provider=provider,
                base_url=metadata["base_url"],
                bearer_token=bearer_token,
            )
    except (json.JSONDecodeError, TypeError, KeyError):
        # Deliberately swallow the underlying exception rather than
        # chaining it with `from exc` / logging it - see
        # `CredentialDecodeError`'s docstring for why its message could
        # otherwise carry a fragment of the decrypted plaintext.
        raise CredentialDecodeError() from None

    raise UnsupportedProviderCredentialError(provider)


async def get_decrypted_provider_credential(
    session: AsyncSession,
    provider: str,
    *,
    key_provider: KeyProvider,
) -> ProviderCredential:
    """Fetch and decrypt the PRIMARY configured key for `provider`, for
    outbound calls.

    Raises:
        `ProviderKeyNotConfiguredError` - no key configured for `provider`
            in this org. Route handlers should turn this into a 404.
        `encryption.DecryptionError` - the row failed to decrypt (wrong
            master key, or the ciphertext/nonce/auth_tag/AAD don't match).
            This is `encryption`'s own error class; its message is already
            static/generic and safe to log/return as-is (see that module's
            docstring) - this function does not catch or wrap it.
        `CredentialDecodeError` - decryption succeeded but the plaintext
            wasn't valid JSON in the expected shape (data corruption, not
            an auth failure).
        `UnsupportedProviderCredentialError` - `provider` isn't one of the
            known credential shapes (should not happen in practice; see
            that class's docstring).

    Never logs decrypted plaintext, on any of the above paths.
    """
    row = await get_key(session, provider)
    if row is None:
        raise ProviderKeyNotConfiguredError(provider)
    return _credential_from_row(row, provider, key_provider=key_provider)


async def get_decrypted_provider_credential_from_row(
    row: ProviderKey, provider: str, *, key_provider: KeyProvider
) -> ProviderCredential:
    """Phase 4 (design doc section 3.3/8): decrypt an ALREADY-FETCHED
    `ProviderKey` row - the failover-aware credential-fetch path
    (`api.v1.gateway.common.call_provider_with_failover`) already has the
    row from `select_provider_key`/a backup lookup, so this skips the extra
    `get_key` round trip. `async` (even though nothing here actually awaits)
    to match `get_decrypted_provider_credential`'s call-site shape - keeps
    both functions swappable behind the identical `await ...(...)` calling
    convention. Same exceptions as `get_decrypted_provider_credential`
    above, minus `ProviderKeyNotConfiguredError` (the caller is responsible
    for the row existing at all)."""
    return _credential_from_row(row, provider, key_provider=key_provider)
