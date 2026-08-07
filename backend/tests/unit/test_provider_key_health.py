"""Unit tests for `services/provider_key_health.py`'s
`refresh_single_provider_key_health` - specifically the Phase 4 fix that
replaced a hardcoded `{"api_key": "placeholder"}` validation payload with
the key's REAL decrypted credential (see that function's docstring, "FIX
1"/"FIX 2").

Follows `test_proxy_keys.py`'s pattern: builds a real `ProviderKey` ORM
instance in-process (no DB I/O) with real ciphertext produced by the real
`encryption.encrypt_secret`, so `refresh_single_provider_key_health`
exercises the REAL decrypt path end to end, not a mocked one. Only
`providers.registry.build_validator_registry` is monkeypatched (to a spy
validator, so the test can assert on exactly what `secret_payload` it
received) and the DB session is a minimal in-process fake (this module's
own `session.execute(update(...))`/`session.commit()` calls are not
exercised against real Postgres - see `test_provider_keys_api.py`/
`test_phase4_admin_extras.py` for the real-Postgres integration coverage of
the admin endpoint this function backs).
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.provider_key import ProviderKey, ProviderName
from gatekey.providers.base import ValidationResult, ValidationStatus
from gatekey.services import provider_key_health
from gatekey.services.encryption import EnvKeyProvider, build_aad, encrypt_secret
from gatekey.services.shared_state import InProcessSharedStateStore


class _FakeSession:
    """Stands in for `AsyncSession` - `execute()`/`commit()` are no-ops that
    just record what was asked of them, mirroring the `_FakeSession`
    pattern already used in `test_scheduler_service.py`. This function's own
    UPDATE-statement mechanics are covered against real Postgres by the
    integration suite; this file's job is the credential/validation-result
    plumbing above that."""

    def __init__(self) -> None:
        self.executed: list[object] = []
        self.commit_count = 0

    async def execute(self, stmt):  # noqa: ANN001
        self.executed.append(stmt)
        return None

    async def commit(self) -> None:
        self.commit_count += 1


def _key_provider() -> EnvKeyProvider:
    return EnvKeyProvider(os.urandom(32))


def _make_api_key_row(
    *, provider: ProviderName, api_key: str, key_provider: EnvKeyProvider
) -> ProviderKey:
    aad = build_aad(str(DEFAULT_ORG_ID), provider.value)
    encrypted = encrypt_secret(json.dumps(api_key).encode("utf-8"), aad=aad, key_provider=key_provider)
    return ProviderKey(
        id=uuid.uuid4(),
        org_id=DEFAULT_ORG_ID,
        provider=provider,
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
        key_metadata={},
    )


class _SpyValidator:
    """Records every `secret_payload` it's called with and always returns a
    caller-supplied canned `ValidationResult`."""

    def __init__(self, result: ValidationResult) -> None:
        self._result = result
        self.calls: list[dict] = []

    async def validate(self, secret_payload):  # noqa: ANN001
        self.calls.append(secret_payload)
        return self._result


def _patch_validator_registry(monkeypatch: pytest.MonkeyPatch, provider: str, validator: _SpyValidator) -> None:
    def _fake_build_validator_registry(timeout_seconds: float = 8.0):  # noqa: ARG001
        return {provider: validator}

    monkeypatch.setattr(
        "gatekey.providers.registry.build_validator_registry", _fake_build_validator_registry
    )


# ---------------------------------------------------------------------------
# FIX 1: the validator is called with the REAL decrypted credential, not the
# literal string "placeholder".
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_sends_real_decrypted_key_not_placeholder_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_provider = _key_provider()
    real_api_key = "sk-REAL-OPENAI-SECRET-VALUE-never-a-placeholder"
    row = _make_api_key_row(provider=ProviderName.OPENAI, api_key=real_api_key, key_provider=key_provider)

    spy = _SpyValidator(ValidationResult(status=ValidationStatus.VALID))
    _patch_validator_registry(monkeypatch, "openai", spy)

    session = _FakeSession()
    health_store = InProcessSharedStateStore()

    status, error = await provider_key_health.refresh_single_provider_key_health(
        session, health_store, row, key_provider=key_provider, timeout_seconds=3.0
    )

    assert status == "healthy"
    assert error is None
    # The validator must have received the ACTUAL decrypted key...
    assert spy.calls == [{"api_key": real_api_key}]
    # ...and specifically NOT the old hardcoded placeholder literal.
    assert spy.calls[0]["api_key"] != "placeholder"


@pytest.mark.asyncio
async def test_health_check_success_records_success_in_shared_health_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same shared-state health record that `select_provider_key` reads
    for proactive failover routing must reflect a real VALID outcome."""
    key_provider = _key_provider()
    row = _make_api_key_row(
        provider=ProviderName.OPENAI, api_key="sk-another-real-secret", key_provider=key_provider
    )
    spy = _SpyValidator(ValidationResult(status=ValidationStatus.VALID))
    _patch_validator_registry(monkeypatch, "openai", spy)

    session = _FakeSession()
    health_store = InProcessSharedStateStore()

    await provider_key_health.refresh_single_provider_key_health(
        session, health_store, row, key_provider=key_provider, timeout_seconds=3.0
    )

    recorded = await provider_key_health.get_health(health_store, row.id)
    assert recorded is not None
    assert recorded.status == "healthy"
    assert recorded.consecutive_failures == 0


# ---------------------------------------------------------------------------
# FIX 2: a non-exception, non-VALID `ValidationResult` (INVALID_KEY /
# PROVIDER_UNREACHABLE / UNKNOWN_ERROR) must be treated as a real failure -
# the pre-fix code discarded the return value entirely and always reported
# "healthy" as long as no exception was raised.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_reports_unavailable_when_key_is_actually_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_provider = _key_provider()
    row = _make_api_key_row(
        provider=ProviderName.OPENAI, api_key="sk-revoked-key", key_provider=key_provider
    )
    spy = _SpyValidator(
        ValidationResult(
            status=ValidationStatus.INVALID_KEY,
            detail="OpenAI rejected the key (HTTP 401).",
        )
    )
    _patch_validator_registry(monkeypatch, "openai", spy)

    session = _FakeSession()
    health_store = InProcessSharedStateStore()

    status, error = await provider_key_health.refresh_single_provider_key_health(
        session, health_store, row, key_provider=key_provider, timeout_seconds=3.0
    )

    assert status == "unavailable"
    assert error == "OpenAI rejected the key (HTTP 401)."
    # And the shared health store used by failover routing must reflect the
    # failure too (not silently stay "healthy"/absent).
    recorded = await provider_key_health.get_health(health_store, row.id)
    assert recorded is not None
    assert recorded.consecutive_failures == 1


@pytest.mark.asyncio
async def test_health_check_reports_unavailable_when_provider_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_provider = _key_provider()
    row = _make_api_key_row(
        provider=ProviderName.OPENAI, api_key="sk-some-key", key_provider=key_provider
    )
    spy = _SpyValidator(
        ValidationResult(
            status=ValidationStatus.PROVIDER_UNREACHABLE,
            detail="Timed out contacting OpenAI.",
        )
    )
    _patch_validator_registry(monkeypatch, "openai", spy)

    session = _FakeSession()
    health_store = InProcessSharedStateStore()

    status, error = await provider_key_health.refresh_single_provider_key_health(
        session, health_store, row, key_provider=key_provider, timeout_seconds=3.0
    )

    assert status == "unavailable"
    assert error == "Timed out contacting OpenAI."


# ---------------------------------------------------------------------------
# Secret hygiene: a decrypt-time failure's persisted `last_error` must never
# contain a fragment of the corrupted/undecodable plaintext.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_decrypt_failure_never_leaks_secret_fragment_into_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_provider = _key_provider()
    secret_fragment = "SUPER-SECRET-FRAGMENT-should-never-leak"
    aad = build_aad(str(DEFAULT_ORG_ID), "openai")
    # Malformed (non-JSON) plaintext - decrypts fine (right key/AAD) but
    # fails to decode, exercising `services.proxy_keys.CredentialDecodeError`
    # - same corruption shape as `test_proxy_keys.py`'s equivalent test.
    encrypted = encrypt_secret(
        f"{{not valid json: {secret_fragment}".encode("utf-8"), aad=aad, key_provider=key_provider
    )
    row = ProviderKey(
        id=uuid.uuid4(),
        org_id=DEFAULT_ORG_ID,
        provider=ProviderName.OPENAI,
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
        key_metadata={},
    )

    session = _FakeSession()
    health_store = InProcessSharedStateStore()

    status, error = await provider_key_health.refresh_single_provider_key_health(
        session, health_store, row, key_provider=key_provider, timeout_seconds=3.0
    )

    assert status == "unavailable"
    assert error is not None
    assert secret_fragment not in error
    assert error == "Failed to decode a decrypted provider credential."


@pytest.mark.asyncio
async def test_health_check_wrong_org_key_decrypt_failure_never_leaks_key_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `DecryptionError` (wrong AAD/tampered ciphertext) must likewise
    never surface the plaintext it failed to recover - `encryption.
    DecryptionError`'s message is documented static/generic."""
    key_provider = _key_provider()
    wrong_aad = build_aad(str(DEFAULT_ORG_ID), "anthropic")
    real_secret = "sk-should-never-appear-in-any-error-text"
    encrypted = encrypt_secret(
        json.dumps(real_secret).encode("utf-8"), aad=wrong_aad, key_provider=key_provider
    )
    row = ProviderKey(
        id=uuid.uuid4(),
        org_id=DEFAULT_ORG_ID,
        provider=ProviderName.OPENAI,  # mislabeled - real AAD was for anthropic
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
        key_metadata={},
    )

    session = _FakeSession()
    health_store = InProcessSharedStateStore()

    status, error = await provider_key_health.refresh_single_provider_key_health(
        session, health_store, row, key_provider=key_provider, timeout_seconds=3.0
    )

    assert status == "unavailable"
    assert error is not None
    assert real_secret not in error
