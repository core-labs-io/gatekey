"""Unit tests for services/provider_keys.py - the parts that don't need a DB.

The upsert/list/get/delete DB interactions themselves are covered by
`tests/integration/test_provider_keys_api.py` against a real Postgres
(they rely on Postgres-specific `INSERT ... ON CONFLICT` semantics that
aren't meaningfully mockable). This file covers:
  - the non-VALID validation-status -> exception mapping, which returns
    before any DB access happens (so a poisoned sentinel `session` object
    is enough to prove no DB call was attempted), and
  - the pure secret-serialization / metadata-building helpers.
"""

from __future__ import annotations

import json

import pytest

from gatekey.providers.base import ProviderValidator, ValidationResult, ValidationStatus
from gatekey.services.encryption import EnvKeyProvider
from gatekey.services.provider_keys import (
    InvalidProviderKeyError,
    ProviderUnreachableError,
    ProviderValidationUnknownError,
    _build_key_metadata,
    _serialize_secret_payload,
    add_or_replace_key,
)


class _StubValidator(ProviderValidator):
    def __init__(self, result: ValidationResult) -> None:
        self._result = result

    async def validate(self, secret_payload):  # noqa: ANN001
        return self._result


class _ExplodingSessionSentinel:
    """Stands in for `session` in tests that must never touch the DB.

    Any attribute access (e.g. `.execute`) raises immediately, proving the
    non-VALID code path in `add_or_replace_key` returns before attempting
    any database interaction.
    """

    def __getattr__(self, name: str):
        raise AssertionError(f"session.{name} must not be accessed on a non-VALID result")


def _key_provider() -> EnvKeyProvider:
    import os

    return EnvKeyProvider(os.urandom(32))


@pytest.mark.asyncio
async def test_invalid_key_raises_without_touching_db():
    validator = _StubValidator(ValidationResult(status=ValidationStatus.INVALID_KEY, detail="no"))
    with pytest.raises(InvalidProviderKeyError):
        await add_or_replace_key(
            _ExplodingSessionSentinel(),
            "openai",
            {"api_key": "sk-whatever"},
            validator_registry={"openai": validator},
            key_provider=_key_provider(),
        )


@pytest.mark.asyncio
async def test_provider_unreachable_raises_without_touching_db():
    validator = _StubValidator(
        ValidationResult(status=ValidationStatus.PROVIDER_UNREACHABLE, detail="timeout")
    )
    with pytest.raises(ProviderUnreachableError):
        await add_or_replace_key(
            _ExplodingSessionSentinel(),
            "openai",
            {"api_key": "sk-whatever"},
            validator_registry={"openai": validator},
            key_provider=_key_provider(),
        )


@pytest.mark.asyncio
async def test_unknown_error_raises_without_touching_db():
    validator = _StubValidator(ValidationResult(status=ValidationStatus.UNKNOWN_ERROR))
    with pytest.raises(ProviderValidationUnknownError):
        await add_or_replace_key(
            _ExplodingSessionSentinel(),
            "openai",
            {"api_key": "sk-whatever"},
            validator_registry={"openai": validator},
            key_provider=_key_provider(),
        )


def test_serialize_secret_payload_openai_anthropic_openrouter_is_just_the_api_key():
    for provider in ("openai", "anthropic", "openrouter"):
        plaintext = _serialize_secret_payload(provider, {"api_key": "sk-abc123"})
        assert json.loads(plaintext) == "sk-abc123"


def test_serialize_secret_payload_ollama_is_the_bearer_token():
    plaintext = _serialize_secret_payload(
        "ollama", {"base_url": "http://localhost:11434", "bearer_token": "my-secret-bearer"}
    )
    assert json.loads(plaintext) == "my-secret-bearer"


def test_serialize_secret_payload_ollama_with_no_bearer_token_still_produces_serializable_value():
    """AC-B2-5: an Ollama key saved with no bearer_token must still produce
    a non-empty, decryptable ciphertext - never a NULL-equivalent
    placeholder that would violate ciphertext/nonce/auth_tag's NOT NULL
    constraint or silently skip the encrypt step."""
    plaintext_omitted = _serialize_secret_payload("ollama", {"base_url": "http://localhost:11434"})
    plaintext_none = _serialize_secret_payload(
        "ollama", {"base_url": "http://localhost:11434", "bearer_token": None}
    )
    assert json.loads(plaintext_omitted) == ""
    assert json.loads(plaintext_none) == ""
    # Not NULL/empty-bytes - a real, well-formed JSON-encoded empty string.
    assert plaintext_omitted == b'""'
    assert plaintext_none == b'""'


def test_serialize_secret_payload_vertex_ai_is_the_service_account_json():
    sa_json = {"type": "service_account", "private_key": "-----BEGIN..."}
    plaintext = _serialize_secret_payload(
        "vertex_ai",
        {"service_account_json": sa_json, "project_id": "proj-1", "location": "us-central1"},
    )
    assert json.loads(plaintext) == sa_json


def test_build_key_metadata_vertex_ai_carries_project_and_location_not_secret():
    metadata = _build_key_metadata(
        "vertex_ai",
        {
            "service_account_json": {"private_key": "should-not-appear"},
            "project_id": "proj-1",
            "location": "us-central1",
        },
    )
    assert metadata == {"project_id": "proj-1", "location": "us-central1"}
    assert "private_key" not in json.dumps(metadata)


def test_build_key_metadata_openai_anthropic_openrouter_is_empty():
    assert _build_key_metadata("openai", {"api_key": "sk-abc"}) == {}
    assert _build_key_metadata("anthropic", {"api_key": "sk-abc"}) == {}
    assert _build_key_metadata("openrouter", {"api_key": "sk-abc"}) == {}


def test_build_key_metadata_ollama_carries_base_url_not_bearer_token():
    metadata = _build_key_metadata(
        "ollama", {"base_url": "http://localhost:11434", "bearer_token": "should-not-appear"}
    )
    assert metadata == {"base_url": "http://localhost:11434"}
    assert "should-not-appear" not in json.dumps(metadata)
