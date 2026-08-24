"""Unit tests for services/proxy_keys.py - the plaintext-provider-key seam.

This module is Phase 1.2's single most security-critical addition: it is
the only place a decrypted provider key ever comes into existence. Every
test here exercises the *real* `get_decrypted_provider_credential`
function body (AAD construction, `decrypt_secret` call, JSON decoding,
credential-shape construction) end to end - the only thing monkeypatched
is `proxy_keys.get_key`, swapped for a stub that returns a real
`ProviderKey` ORM instance constructed directly in-process (no DB I/O:
`ProviderKey` is a plain SQLAlchemy declarative model and can be
instantiated without a session/engine). This is deliberately different
from every other gateway test in this suite, which monkeypatches
`get_decrypted_provider_credential` itself at the call site and therefore
never exercises this module's own decrypt/decode logic - see the
Phase 1.2 security sign-off for why that gap mattered enough to close
with a dedicated file.

No real Postgres is needed: `get_key()` itself (the `select(...)` +
`session.execute(...)` round trip) is covered separately by
`tests/integration/test_provider_keys_api.py` against real Postgres -
that's Postgres-specific `ON CONFLICT`/dialect-type territory this file
doesn't need to re-prove. What *is* unique to this module, and what this
file is the only place proving, is what `get_decrypted_provider_credential`
does with a row once it has one.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.provider_key import ProviderKey
from gatekey.services import proxy_keys
from gatekey.services.encryption import (
    DecryptionError,
    EnvKeyProvider,
    build_aad,
    encrypt_secret,
)
from gatekey.services.proxy_keys import (
    ApiKeyCredential,
    CredentialDecodeError,
    OllamaCredential,
    ProviderKeyNotConfiguredError,
    ServiceAccountCredential,
    get_decrypted_provider_credential,
)

class _UnusedSessionSentinel:
    """Stands in for `session` in every test here.

    `get_key` is monkeypatched below, so the real `session.execute(...)`
    path is never reached - any attribute access on this sentinel proves
    that, mirroring the identical pattern in `test_provider_keys_service.py`
    and `tests/unit/gateway_test_support.py`.
    """

    def __getattr__(self, name: str):
        raise AssertionError(f"session.{name} must not be accessed - get_key is monkeypatched")


def _key_provider() -> EnvKeyProvider:
    return EnvKeyProvider(os.urandom(32))


def _make_row(
    *,
    provider: str,
    ciphertext: bytes,
    nonce: bytes,
    auth_tag: bytes,
    key_metadata: dict | None = None,
) -> ProviderKey:
    """Build a `ProviderKey` ORM instance with no DB I/O.

    Plain Python object construction - SQLAlchemy declarative models don't
    require a session/engine to instantiate.
    """
    return ProviderKey(
        id=uuid.uuid4(),
        org_id=DEFAULT_ORG_ID,
        provider=provider,
        ciphertext=ciphertext,
        nonce=nonce,
        auth_tag=auth_tag,
        key_metadata=key_metadata or {},
    )


def _patch_get_key(monkeypatch: pytest.MonkeyPatch, row: ProviderKey | None) -> None:
    async def _fake_get_key(session, provider):  # noqa: ANN001, ARG001
        return row

    monkeypatch.setattr(proxy_keys, "get_key", _fake_get_key)


# ---------------------------------------------------------------------------
# 1. AAD mismatch -> encryption.DecryptionError, through the real call path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aad_mismatch_through_real_call_path_raises_decryption_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row whose ciphertext was bound to the wrong provider's AAD must
    fail to decrypt when fetched *as* the other provider - proving AAD
    binding holds through `get_decrypted_provider_credential` itself, not
    just in `encryption.py` in isolation (that's `test_encryption.py`'s
    job; this is the module under security review).

    Simulates the row having been mislabeled/corrupted at rest: the
    ciphertext was actually encrypted with AAD for "anthropic", but the
    row claims to be the "openai" row. `get_decrypted_provider_credential`
    always builds AAD from the `provider` argument it's called with, so
    calling it with provider="openai" against this row must fail the GCM
    authentication check.
    """
    key_provider = _key_provider()
    wrong_provider_aad = build_aad(str(DEFAULT_ORG_ID), "anthropic")
    encrypted = encrypt_secret(
        json.dumps("sk-mislabeled-secret-value").encode("utf-8"),
        aad=wrong_provider_aad,
        key_provider=key_provider,
    )
    row = _make_row(
        provider="openai",
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
    )
    _patch_get_key(monkeypatch, row)

    with pytest.raises(DecryptionError):
        await get_decrypted_provider_credential(
            _UnusedSessionSentinel(), "openai", key_provider=key_provider
        )


# ---------------------------------------------------------------------------
# 2. Malformed decrypted JSON -> CredentialDecodeError, with no leakage of
#    the underlying json/TypeError/KeyError message into the raised error.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_key_row_decrypts_to_non_string_raises_credential_decode_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """openai/anthropic row whose plaintext is valid JSON but a list, not a
    string - decryption succeeds (correct AAD/key), decoding must not."""
    key_provider = _key_provider()
    aad = build_aad(str(DEFAULT_ORG_ID), "openai")
    secret_fragment = "SECRET-ARRAY-ELEMENT-marker"
    encrypted = encrypt_secret(
        json.dumps([secret_fragment, "other"]).encode("utf-8"),
        aad=aad,
        key_provider=key_provider,
    )
    row = _make_row(
        provider="openai",
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
    )
    _patch_get_key(monkeypatch, row)

    with pytest.raises(CredentialDecodeError) as exc_info:
        await get_decrypted_provider_credential(
            _UnusedSessionSentinel(), "openai", key_provider=key_provider
        )

    _assert_no_leak(exc_info.value, secret_fragment)
    assert str(exc_info.value) == "Failed to decode a decrypted provider credential."


@pytest.mark.asyncio
async def test_vertex_ai_row_decrypts_to_string_not_object_raises_credential_decode_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vertex_ai row whose plaintext is valid JSON but a bare string, not
    an object - decryption succeeds, decoding must not."""
    key_provider = _key_provider()
    aad = build_aad(str(DEFAULT_ORG_ID), "vertex_ai")
    secret_fragment = "SECRET-STRING-not-an-object-marker"
    encrypted = encrypt_secret(
        json.dumps(secret_fragment).encode("utf-8"),
        aad=aad,
        key_provider=key_provider,
    )
    row = _make_row(
        provider="vertex_ai",
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
        key_metadata={"project_id": "proj-1", "location": "us-central1"},
    )
    _patch_get_key(monkeypatch, row)

    with pytest.raises(CredentialDecodeError) as exc_info:
        await get_decrypted_provider_credential(
            _UnusedSessionSentinel(), "vertex_ai", key_provider=key_provider
        )

    _assert_no_leak(exc_info.value, secret_fragment)
    assert str(exc_info.value) == "Failed to decode a decrypted provider credential."


@pytest.mark.asyncio
async def test_malformed_non_json_plaintext_raises_credential_decode_error_without_leaking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plaintext that isn't even valid JSON (data corruption survives the
    AEAD tag check but not JSON parsing). `json.JSONDecodeError` messages
    can echo fragments of the input they failed to parse - this is exactly
    the leak `CredentialDecodeError`'s fixed message exists to prevent."""
    key_provider = _key_provider()
    aad = build_aad(str(DEFAULT_ORG_ID), "openai")
    secret_fragment = "NOT-VALID-JSON-SECRET-marker"
    encrypted = encrypt_secret(
        f"{{not valid json at all: {secret_fragment}".encode("utf-8"),
        aad=aad,
        key_provider=key_provider,
    )
    row = _make_row(
        provider="openai",
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
    )
    _patch_get_key(monkeypatch, row)

    with pytest.raises(CredentialDecodeError) as exc_info:
        await get_decrypted_provider_credential(
            _UnusedSessionSentinel(), "openai", key_provider=key_provider
        )

    _assert_no_leak(exc_info.value, secret_fragment)


@pytest.mark.asyncio
async def test_ollama_row_decrypts_to_non_string_raises_credential_decode_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ollama row whose plaintext is valid JSON but a number, not a string -
    decryption succeeds (correct AAD/key), decoding must not."""
    key_provider = _key_provider()
    aad = build_aad(str(DEFAULT_ORG_ID), "ollama")
    encrypted = encrypt_secret(
        json.dumps(12345).encode("utf-8"),
        aad=aad,
        key_provider=key_provider,
    )
    row = _make_row(
        provider="ollama",
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
        key_metadata={"base_url": "http://localhost:11434"},
    )
    _patch_get_key(monkeypatch, row)

    with pytest.raises(CredentialDecodeError) as exc_info:
        await get_decrypted_provider_credential(
            _UnusedSessionSentinel(), "ollama", key_provider=key_provider
        )

    _assert_no_leak(exc_info.value, "12345")


@pytest.mark.asyncio
async def test_ollama_row_missing_base_url_metadata_raises_credential_decode_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ollama row with a well-shaped secret but `key_metadata` missing
    `base_url` - the `KeyError` path must also map to
    `CredentialDecodeError`, not propagate as a raw `KeyError`."""
    key_provider = _key_provider()
    aad = build_aad(str(DEFAULT_ORG_ID), "ollama")
    encrypted = encrypt_secret(
        json.dumps("my-bearer-token").encode("utf-8"),
        aad=aad,
        key_provider=key_provider,
    )
    row = _make_row(
        provider="ollama",
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
        key_metadata={},  # missing base_url
    )
    _patch_get_key(monkeypatch, row)

    with pytest.raises(CredentialDecodeError) as exc_info:
        await get_decrypted_provider_credential(
            _UnusedSessionSentinel(), "ollama", key_provider=key_provider
        )

    _assert_no_leak(exc_info.value, "base_url")


@pytest.mark.asyncio
async def test_ollama_row_decrypts_successfully_with_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_provider = _key_provider()
    aad = build_aad(str(DEFAULT_ORG_ID), "ollama")
    encrypted = encrypt_secret(
        json.dumps("secret-bearer").encode("utf-8"),
        aad=aad,
        key_provider=key_provider,
    )
    row = _make_row(
        provider="ollama",
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
        key_metadata={"base_url": "http://localhost:11434"},
    )
    _patch_get_key(monkeypatch, row)

    credential = await get_decrypted_provider_credential(
        _UnusedSessionSentinel(), "ollama", key_provider=key_provider
    )

    assert isinstance(credential, OllamaCredential)
    assert credential.base_url == "http://localhost:11434"
    assert credential.bearer_token == "secret-bearer"


@pytest.mark.asyncio
async def test_openrouter_row_with_trusted_providers_populates_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The residency-enforcement fields (`services.residency.
    resolve_model_region`'s openrouter branch; `providers.openrouter.py`'s
    `provider.only` injection) both trust this credential field - prove
    `get_decrypted_provider_credential` actually populates it from the
    row's non-secret `key_metadata`, not just the schema/service-layer
    write path."""
    key_provider = _key_provider()
    aad = build_aad(str(DEFAULT_ORG_ID), "openrouter")
    encrypted = encrypt_secret(
        json.dumps("sk-or-secret").encode("utf-8"), aad=aad, key_provider=key_provider
    )
    row = _make_row(
        provider="openrouter",
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
        key_metadata={"trusted_provider_slugs": ["openai", "anthropic"], "trusted_provider_region": "us"},
    )
    _patch_get_key(monkeypatch, row)

    credential = await get_decrypted_provider_credential(
        _UnusedSessionSentinel(), "openrouter", key_provider=key_provider
    )

    assert isinstance(credential, ApiKeyCredential)
    assert credential.api_key == "sk-or-secret"
    assert credential.trusted_provider_slugs == ("openai", "anthropic")


@pytest.mark.asyncio
async def test_openrouter_row_without_trusted_providers_leaves_credential_unrestricted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_provider = _key_provider()
    aad = build_aad(str(DEFAULT_ORG_ID), "openrouter")
    encrypted = encrypt_secret(
        json.dumps("sk-or-secret").encode("utf-8"), aad=aad, key_provider=key_provider
    )
    row = _make_row(
        provider="openrouter",
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
        key_metadata={},
    )
    _patch_get_key(monkeypatch, row)

    credential = await get_decrypted_provider_credential(
        _UnusedSessionSentinel(), "openrouter", key_provider=key_provider
    )

    assert isinstance(credential, ApiKeyCredential)
    assert credential.trusted_provider_slugs == ()


@pytest.mark.asyncio
async def test_openai_row_never_populates_trusted_provider_slugs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`trusted_provider_slugs` is openrouter-only - openai/anthropic must
    always get `()`, even if a row somehow carried unrelated metadata."""
    key_provider = _key_provider()
    aad = build_aad(str(DEFAULT_ORG_ID), "openai")
    encrypted = encrypt_secret(
        json.dumps("sk-openai-secret").encode("utf-8"), aad=aad, key_provider=key_provider
    )
    row = _make_row(
        provider="openai",
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
        key_metadata={"trusted_provider_slugs": ["should-be-ignored"], "trusted_provider_region": "us"},
    )
    _patch_get_key(monkeypatch, row)

    credential = await get_decrypted_provider_credential(
        _UnusedSessionSentinel(), "openai", key_provider=key_provider
    )

    assert isinstance(credential, ApiKeyCredential)
    assert credential.trusted_provider_slugs == ()


@pytest.mark.asyncio
async def test_ollama_row_decrypts_successfully_with_blank_bearer_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blank bearer_token (never configured) decodes to an empty string,
    not None - see OllamaCredential's docstring."""
    key_provider = _key_provider()
    aad = build_aad(str(DEFAULT_ORG_ID), "ollama")
    encrypted = encrypt_secret(
        json.dumps("").encode("utf-8"),
        aad=aad,
        key_provider=key_provider,
    )
    row = _make_row(
        provider="ollama",
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
        key_metadata={"base_url": "http://localhost:11434"},
    )
    _patch_get_key(monkeypatch, row)

    credential = await get_decrypted_provider_credential(
        _UnusedSessionSentinel(), "ollama", key_provider=key_provider
    )

    assert isinstance(credential, OllamaCredential)
    assert credential.bearer_token == ""


@pytest.mark.asyncio
async def test_vertex_ai_row_missing_metadata_key_raises_credential_decode_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """vertex_ai row with a well-shaped secret but `key_metadata` missing
    `project_id`/`location` - the `KeyError` path must also map to
    `CredentialDecodeError`, not propagate as a raw `KeyError`."""
    key_provider = _key_provider()
    aad = build_aad(str(DEFAULT_ORG_ID), "vertex_ai")
    encrypted = encrypt_secret(
        json.dumps({"type": "service_account", "private_key": "irrelevant-here"}).encode(
            "utf-8"
        ),
        aad=aad,
        key_provider=key_provider,
    )
    row = _make_row(
        provider="vertex_ai",
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
        key_metadata={},  # missing project_id/location
    )
    _patch_get_key(monkeypatch, row)

    with pytest.raises(CredentialDecodeError) as exc_info:
        await get_decrypted_provider_credential(
            _UnusedSessionSentinel(), "vertex_ai", key_provider=key_provider
        )

    _assert_no_leak(exc_info.value, "project_id")


def _assert_no_leak(exc: Exception, secret_fragment: str) -> None:
    assert secret_fragment not in str(exc)
    assert secret_fragment not in repr(exc)
    for arg in exc.args:
        assert secret_fragment not in str(arg)


# ---------------------------------------------------------------------------
# 3. repr()/str()/f-string redaction on both credential subclasses.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 5. `to_secret_payload()` - the inverse shape `services.provider_key_health.
#    refresh_single_provider_key_health` needs to re-validate a REAL
#    decrypted credential (Phase 4 fix - see that function's docstring).
#    Must exactly match the dict shape `services.provider_keys.
#    _serialize_secret_payload`/`_build_key_metadata` consume on the
#    key-creation path, since that's what each concrete validator's
#    `validate()` expects.
# ---------------------------------------------------------------------------


def test_api_key_credential_to_secret_payload_matches_validator_shape() -> None:
    credential = ApiKeyCredential(provider="openai", api_key="sk-real-secret")
    assert credential.to_secret_payload() == {"api_key": "sk-real-secret"}


def test_service_account_credential_to_secret_payload_matches_validator_shape() -> None:
    credential = ServiceAccountCredential(
        provider="vertex_ai",
        service_account_json={"type": "service_account", "private_key": "SECRET"},
        project_id="proj-1",
        location="us-central1",
    )
    assert credential.to_secret_payload() == {
        "service_account_json": {"type": "service_account", "private_key": "SECRET"},
        "project_id": "proj-1",
        "location": "us-central1",
    }


def test_ollama_credential_to_secret_payload_matches_validator_shape() -> None:
    credential = OllamaCredential(
        provider="ollama", base_url="http://localhost:11434", bearer_token="secret-bearer"
    )
    assert credential.to_secret_payload() == {
        "base_url": "http://localhost:11434",
        "bearer_token": "secret-bearer",
    }


@pytest.mark.asyncio
async def test_get_decrypted_credential_from_row_round_trips_to_secret_payload_the_validator_would_accept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: encrypt a real key, decrypt it back via the real
    `get_decrypted_provider_credential_from_row` call path (the one
    `refresh_single_provider_key_health` uses), and confirm
    `to_secret_payload()` reproduces exactly what an admin's raw request
    body would have looked like on the key-creation path - proving the two
    shapes actually match, not just that each independently looks
    plausible."""
    key_provider = _key_provider()
    real_api_key = "sk-round-trip-real-secret"
    aad = build_aad(str(DEFAULT_ORG_ID), "openai")
    encrypted = encrypt_secret(
        json.dumps(real_api_key).encode("utf-8"), aad=aad, key_provider=key_provider
    )
    row = _make_row(
        provider="openai",
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
    )

    credential = await proxy_keys.get_decrypted_provider_credential_from_row(
        row, "openai", key_provider=key_provider
    )

    assert credential.to_secret_payload() == {"api_key": real_api_key}


def test_api_key_credential_repr_str_fstring_are_redacted() -> None:
    secret = "sk-super-secret-value"
    credential = ApiKeyCredential(provider="openai", api_key=secret)

    rendered_repr = repr(credential)
    rendered_str = str(credential)
    rendered_fstring = f"{credential}"

    for rendered in (rendered_repr, rendered_str, rendered_fstring):
        assert rendered == "<ApiKeyCredential REDACTED>"
        assert secret not in rendered


def test_ollama_credential_repr_str_fstring_are_redacted() -> None:
    secret = "sk-ollama-super-secret-bearer"
    credential = OllamaCredential(
        provider="ollama", base_url="http://localhost:11434", bearer_token=secret
    )

    rendered_repr = repr(credential)
    rendered_str = str(credential)
    rendered_fstring = f"{credential}"

    for rendered in (rendered_repr, rendered_str, rendered_fstring):
        assert rendered == "<OllamaCredential REDACTED>"
        assert secret not in rendered
        # base_url is non-secret but the whole object is still redacted
        # uniformly per this class's docstring - no per-field special-casing.
        assert "localhost" not in rendered


def test_service_account_credential_repr_str_fstring_are_redacted() -> None:
    secret = "SUPER-SECRET"
    credential = ServiceAccountCredential(
        provider="vertex_ai",
        service_account_json={"private_key": secret},
        project_id="p",
        location="l",
    )

    rendered_repr = repr(credential)
    rendered_str = str(credential)
    rendered_fstring = f"{credential}"

    for rendered in (rendered_repr, rendered_str, rendered_fstring):
        assert rendered == "<ServiceAccountCredential REDACTED>"
        assert secret not in rendered
        # The whole object is redacted uniformly, per
        # `ServiceAccountCredential`'s docstring - the `project_id`/
        # `location` field *names* don't leak into the repr either, so a
        # caller never has to reason about which fields were "safe" to
        # include.
        assert "project_id" not in rendered
        assert "location" not in rendered


# ---------------------------------------------------------------------------
# 4. Provider not configured -> ProviderKeyNotConfiguredError.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_not_configured_raises_with_provider_and_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_get_key(monkeypatch, None)

    with pytest.raises(ProviderKeyNotConfiguredError) as exc_info:
        await get_decrypted_provider_credential(
            _UnusedSessionSentinel(), "openai", key_provider=_key_provider()
        )

    error = exc_info.value
    assert error.provider == "openai"
    assert error.message == "No key configured for provider 'openai'."
    assert str(error) == error.message
