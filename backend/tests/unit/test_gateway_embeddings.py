"""Unit tests for POST /v1/embeddings (Phase 1.2, BD-9)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gatekey.api.v1.gateway import common as gateway_common
from gatekey.providers import openai as openai_mod
from gatekey.providers import vertex_ai as vertex_mod
from gatekey.schemas.chat import EmbeddingItem, EmbeddingsResponse, EmbeddingsUsage
from gatekey.services.model_policy import ModelPolicySnapshot
from gatekey.services.proxy_keys import ApiKeyCredential, ServiceAccountCredential

from tests.unit.gateway_test_support import build_authenticated_app

_EMBEDDINGS_URL = "/v1/embeddings"


async def _fake_credential(session, provider, *, key_provider):  # noqa: ANN001, ARG001
    if provider == "openai":
        return ApiKeyCredential(provider="openai", api_key="sk-test")
    if provider == "vertex_ai":
        return ServiceAccountCredential(
            provider="vertex_ai",
            service_account_json={"client_email": "svc@test.iam.gserviceaccount.com"},
            project_id="test-project",
            location="us-central1",
        )
    raise AssertionError(f"unexpected provider {provider!r}")


@pytest.fixture(autouse=True)
def _patch_credential_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _fake_credential)


def test_embeddings_openai_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return EmbeddingsResponse(
            data=[EmbeddingItem(embedding=[0.1, 0.2, 0.3], index=0)],
            model=native_model_id,
            usage=EmbeddingsUsage(prompt_tokens=3, total_tokens=3),
        )

    monkeypatch.setattr(openai_mod, "create_embeddings", _fake_create)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _EMBEDDINGS_URL,
            json={"model": "text-embedding-3-small", "input": "hello world"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["embedding"] == [0.1, 0.2, 0.3]


def test_embeddings_vertex_ai_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(
        client, native_model_id, request, credential, token_cache, *, timeout_seconds=60.0
    ):  # noqa: ANN001, ARG001
        return EmbeddingsResponse(
            data=[EmbeddingItem(embedding=[0.4, 0.5], index=0)],
            model=native_model_id,
            usage=EmbeddingsUsage(prompt_tokens=2, total_tokens=2),
        )

    monkeypatch.setattr(vertex_mod, "create_embeddings", _fake_create)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _EMBEDDINGS_URL,
            json={"model": "gemini-embedding-001", "input": "hello world"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["data"][0]["embedding"] == [0.4, 0.5]


def test_embeddings_wrong_capability_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # gpt-4o is CHAT-only, not registered for embeddings.
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _EMBEDDINGS_URL,
            json={"model": "gpt-4o", "input": "hello"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_request"


def test_embeddings_anthropic_model_rejected_via_capability_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No Anthropic model is ever registered with EMBEDDINGS capability - the
    # capability check alone is what rejects this, no special-case needed.
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _EMBEDDINGS_URL,
            json={"model": "claude-sonnet-5", "input": "hello"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_request"


def test_embeddings_ollama_model_rejected_via_capability_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No Ollama model is ever registered with EMBEDDINGS capability (AC-A1-4/
    # AC-E1-4) - mirrors test_embeddings_anthropic_model_rejected_via_
    # capability_check exactly, for the new provider, no special-case
    # dispatch needed for it to be rejected the same way.
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _EMBEDDINGS_URL,
            json={"model": "ollama/llama3.1", "input": "hello"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_request"


def test_embeddings_openrouter_model_rejected_via_capability_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No OpenRouter model is ever registered with EMBEDDINGS capability
    # (AC-A3-3/AC-E1-4 symmetry) - mirrors the Anthropic/Ollama cases above.
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _EMBEDDINGS_URL,
            json={"model": "openrouter/openai/gpt-4o-mini", "input": "hello"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_request"


def test_embeddings_unknown_model_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _EMBEDDINGS_URL,
            json={"model": "not-a-real-model", "input": "hello"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_embeddings_missing_input_field_returns_structured_422(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _EMBEDDINGS_URL,
            json={"model": "text-embedding-3-small"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_embeddings_denied_model_returns_403_and_never_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail_if_called(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("credential fetch must not happen for a policy-denied model")

    async def _fail_if_dispatched(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("provider must never be called for a policy-denied model")

    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _fail_if_called)
    monkeypatch.setattr(openai_mod, "create_embeddings", _fail_if_dispatched)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        app.state.model_policy_cache.set(
            ModelPolicySnapshot(mode="denylist", models=frozenset({"text-embedding-3-small"}))
        )
        response = client.post(
            _EMBEDDINGS_URL,
            json={"model": "text-embedding-3-small", "input": "hello world"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "model_denied"


def test_embeddings_provider_not_configured_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    from gatekey.services.proxy_keys import ProviderKeyNotConfiguredError

    async def _raise_not_configured(session, provider, *, key_provider):  # noqa: ANN001, ARG001
        raise ProviderKeyNotConfiguredError(provider)

    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _raise_not_configured)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _EMBEDDINGS_URL,
            json={"model": "text-embedding-3-small", "input": "hello"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "provider_not_configured"
