"""Unit tests for schemas/provider_key.py."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from gatekey.schemas.provider_key import (
    AnthropicKeyRequest,
    OllamaKeyRequest,
    OpenAIKeyRequest,
    OpenRouterKeyRequest,
    ProviderKeyListItemResponse,
    ProviderKeyResponse,
    VertexAIKeyRequest,
)


@pytest.mark.parametrize("schema_cls", [OpenAIKeyRequest, AnthropicKeyRequest])
def test_openai_anthropic_request_accepts_valid_key(schema_cls):
    model = schema_cls(api_key="sk-valid-looking-key")
    assert model.api_key == "sk-valid-looking-key"


@pytest.mark.parametrize("schema_cls", [OpenAIKeyRequest, AnthropicKeyRequest])
@pytest.mark.parametrize("bad_value", ["", "   "])
def test_openai_anthropic_request_rejects_blank_key(schema_cls, bad_value):
    with pytest.raises(ValidationError):
        schema_cls(api_key=bad_value)


@pytest.mark.parametrize("schema_cls", [OpenAIKeyRequest, AnthropicKeyRequest])
def test_openai_anthropic_request_rejects_oversized_key(schema_cls):
    with pytest.raises(ValidationError):
        schema_cls(api_key="x" * 5000)


@pytest.mark.parametrize("schema_cls", [OpenAIKeyRequest, AnthropicKeyRequest])
def test_openai_anthropic_request_forbids_extra_fields(schema_cls):
    with pytest.raises(ValidationError):
        schema_cls(api_key="sk-valid", unexpected_field="nope")


def test_vertex_ai_request_accepts_valid_payload():
    model = VertexAIKeyRequest(
        service_account_json={"type": "service_account", "private_key": "x"},
        project_id="my-project",
        location="us-central1",
    )
    assert model.project_id == "my-project"
    assert model.location == "us-central1"
    assert model.service_account_json["type"] == "service_account"


def test_vertex_ai_request_rejects_empty_service_account_json():
    with pytest.raises(ValidationError):
        VertexAIKeyRequest(service_account_json={}, project_id="p", location="us-central1")


@pytest.mark.parametrize("field", ["project_id", "location"])
def test_vertex_ai_request_rejects_blank_project_id_or_location(field):
    kwargs = {
        "service_account_json": {"type": "service_account"},
        "project_id": "p",
        "location": "us-central1",
    }
    kwargs[field] = "   "
    with pytest.raises(ValidationError):
        VertexAIKeyRequest(**kwargs)


@pytest.mark.parametrize("schema_cls", [OpenAIKeyRequest, AnthropicKeyRequest, OpenRouterKeyRequest])
def test_openai_anthropic_openrouter_request_accepts_valid_key(schema_cls):
    model = schema_cls(api_key="sk-valid-looking-key")
    assert model.api_key == "sk-valid-looking-key"


@pytest.mark.parametrize("bad_value", ["", "   "])
def test_openrouter_request_rejects_blank_key(bad_value):
    with pytest.raises(ValidationError):
        OpenRouterKeyRequest(api_key=bad_value)


def test_openrouter_request_rejects_oversized_key():
    with pytest.raises(ValidationError):
        OpenRouterKeyRequest(api_key="x" * 5000)


def test_openrouter_request_forbids_extra_fields():
    with pytest.raises(ValidationError):
        OpenRouterKeyRequest(api_key="sk-valid", unexpected_field="nope")


def test_ollama_request_accepts_base_url_and_bearer_token():
    model = OllamaKeyRequest(base_url="http://localhost:11434", bearer_token="secret")
    assert model.base_url == "http://localhost:11434"
    assert model.bearer_token == "secret"


def test_ollama_request_accepts_base_url_with_no_bearer_token():
    model = OllamaKeyRequest(base_url="http://localhost:11434")
    assert model.bearer_token is None


@pytest.mark.parametrize("bad_value", ["", "   "])
def test_ollama_request_normalizes_blank_bearer_token_to_none(bad_value):
    model = OllamaKeyRequest(base_url="http://localhost:11434", bearer_token=bad_value)
    assert model.bearer_token is None


def test_ollama_request_accepts_https_base_url():
    model = OllamaKeyRequest(base_url="https://ollama.internal.example.com")
    assert model.base_url == "https://ollama.internal.example.com"


@pytest.mark.parametrize("bad_value", ["", "   "])
def test_ollama_request_rejects_blank_base_url(bad_value):
    with pytest.raises(ValidationError):
        OllamaKeyRequest(base_url=bad_value)


@pytest.mark.parametrize("bad_value", ["localhost:11434", "ftp://localhost:11434", "not-a-url"])
def test_ollama_request_rejects_base_url_missing_http_scheme(bad_value):
    with pytest.raises(ValidationError):
        OllamaKeyRequest(base_url=bad_value)


def test_ollama_request_forbids_extra_fields():
    with pytest.raises(ValidationError):
        OllamaKeyRequest(base_url="http://localhost:11434", unexpected_field="nope")


class _FakeProviderKeyRow:
    """Stands in for `gatekey.db.models.provider_key.ProviderKey`.

    Mirrors the DB-column-name/Python-attribute split
    (`key_metadata` attribute, `metadata` is the exposed schema field).
    """

    def __init__(self):
        self.provider = "openai"
        self.validated_at = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
        self.created_at = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
        self.updated_at = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
        self.key_metadata = {"project_id": "proj-1"}
        # Fields that must never leak into the response schema, even though
        # they exist on the real ORM row this stands in for.
        self.ciphertext = b"totally-secret-ciphertext"
        self.nonce = b"nonce-bytes"
        self.auth_tag = b"auth-tag-bytes"
        self.id = uuid.uuid4()
        self.org_id = uuid.uuid4()


def test_response_schema_maps_key_metadata_attribute_to_metadata_field():
    response = ProviderKeyResponse.model_validate(_FakeProviderKeyRow())
    assert response.metadata == {"project_id": "proj-1"}
    dumped = response.model_dump()
    assert dumped["metadata"] == {"project_id": "proj-1"}
    assert "key_metadata" not in dumped


def test_response_schema_has_no_secret_fields_defined_at_all():
    # Locks down the model shape itself - not just serialization exclusion -
    # so a future edit can't accidentally reintroduce a leaky field.
    assert set(ProviderKeyResponse.model_fields) == {
        "provider",
        "configured",
        "validated_at",
        "created_at",
        "updated_at",
        "metadata",
    }


def test_response_schema_serializes_provider_as_plain_string():
    response = ProviderKeyResponse.model_validate(_FakeProviderKeyRow())
    assert response.provider == "openai"
    assert isinstance(response.model_dump_json(), str)
    assert "ProviderName" not in response.model_dump_json()


# ============================================================================
# Phase 4 (Reliability & Cost Efficiency, multi-key/failover): `label`
# field on the `PUT .../key` request schemas (AC4.1.1/AC4.1.2).
# ============================================================================


@pytest.mark.parametrize(
    "schema_cls",
    [OpenAIKeyRequest, AnthropicKeyRequest, OpenRouterKeyRequest],
)
def test_key_request_label_defaults_to_default(schema_cls):
    """Every existing caller that never sets `label` keeps upserting the
    same single 'Default'-labeled row - see `schemas/provider_key.py`'s
    module docstring."""
    model = schema_cls(api_key="sk-valid-looking-key")
    assert model.label == "Default"


@pytest.mark.parametrize(
    "schema_cls",
    [OpenAIKeyRequest, AnthropicKeyRequest, OpenRouterKeyRequest],
)
def test_key_request_accepts_custom_label(schema_cls):
    model = schema_cls(api_key="sk-valid-looking-key", label="backup-key-1")
    assert model.label == "backup-key-1"


@pytest.mark.parametrize(
    "schema_cls",
    [OpenAIKeyRequest, AnthropicKeyRequest, OpenRouterKeyRequest],
)
@pytest.mark.parametrize("bad_value", ["", "   "])
def test_key_request_rejects_blank_label(schema_cls, bad_value):
    with pytest.raises(ValidationError):
        schema_cls(api_key="sk-valid-looking-key", label=bad_value)


@pytest.mark.parametrize(
    "schema_cls",
    [OpenAIKeyRequest, AnthropicKeyRequest, OpenRouterKeyRequest],
)
def test_key_request_rejects_oversized_label(schema_cls):
    with pytest.raises(ValidationError):
        schema_cls(api_key="sk-valid-looking-key", label="x" * 500)


def test_vertex_ai_request_label_defaults_and_accepts_custom():
    kwargs = {
        "service_account_json": {"type": "service_account"},
        "project_id": "p",
        "location": "us-central1",
    }
    assert VertexAIKeyRequest(**kwargs).label == "Default"
    assert VertexAIKeyRequest(**kwargs, label="secondary").label == "secondary"


def test_ollama_request_label_defaults_and_accepts_custom():
    assert OllamaKeyRequest(base_url="http://localhost:11434").label == "Default"
    assert (
        OllamaKeyRequest(base_url="http://localhost:11434", label="gpu-node-2").label
        == "gpu-node-2"
    )


# ============================================================================
# `ProviderKeyListItemResponse` (Phase 4, AC4.1.7) - the per-KEY list view.
# ============================================================================


class _FakeProviderKeyListRow:
    """Stands in for `gatekey.db.models.provider_key.ProviderKey` - same
    "fields that must never leak exist on the real row but not on this
    schema" contract as `_FakeProviderKeyRow` above."""

    def __init__(self):
        self.id = uuid.uuid4()
        self.provider = "openai"
        self.label = "backup-key-1"
        self.is_primary = False
        self.backup_group_id = uuid.uuid4()
        self.health_status = "healthy"
        self.last_health_check = datetime(2026, 7, 11, 12, 0, 0, tzinfo=timezone.utc)
        self.last_error = None
        self.availability_24h = 0.995
        # Never-should-leak fields, present on the real ORM row.
        self.ciphertext = b"totally-secret-ciphertext"
        self.nonce = b"nonce-bytes"
        self.auth_tag = b"auth-tag-bytes"
        self.key_metadata = {"project_id": "proj-1"}
        self.org_id = uuid.uuid4()


def test_list_item_response_maps_fields_correctly():
    response = ProviderKeyListItemResponse.model_validate(_FakeProviderKeyListRow())
    assert response.provider == "openai"
    assert response.label == "backup-key-1"
    assert response.is_primary is False
    assert response.health_status == "healthy"
    assert response.availability_24h == 0.995


def test_list_item_response_has_no_secret_fields_defined_at_all():
    assert set(ProviderKeyListItemResponse.model_fields) == {
        "id",
        "provider",
        "label",
        "is_primary",
        "backup_group_id",
        "health_status",
        "last_health_check",
        "last_error",
        "availability_24h",
    }


def test_list_item_response_serializes_provider_as_plain_string():
    response = ProviderKeyListItemResponse.model_validate(_FakeProviderKeyListRow())
    dumped = response.model_dump_json()
    assert "ProviderName" not in dumped
    assert "ciphertext" not in dumped
    assert "totally-secret-ciphertext" not in dumped
