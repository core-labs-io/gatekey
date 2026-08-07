"""Unit tests for the legacy POST /v1/completions endpoint (Phase 1.2, BD-9)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gatekey.api.v1.gateway import common as gateway_common
from gatekey.providers import openai as openai_mod
from gatekey.schemas.chat import ChatCompletionUsage, CompletionChoice, CompletionResponse
from gatekey.services.model_policy import ModelPolicySnapshot
from gatekey.services.proxy_keys import ApiKeyCredential, ServiceAccountCredential

from tests.unit.gateway_test_support import build_authenticated_app

_COMPLETIONS_URL = "/v1/completions"


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


def test_legacy_completion_openai_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return CompletionResponse(
            id="cmpl-test",
            created=1_700_000_000,
            model=native_model_id,
            choices=[CompletionChoice(index=0, text="completed text", finish_reason="stop")],
            usage=ChatCompletionUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )

    monkeypatch.setattr(openai_mod, "create_completion", _fake_create)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _COMPLETIONS_URL,
            json={"model": "gpt-4o", "prompt": "once upon a time"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["text"] == "completed text"


def test_legacy_completion_rejects_streaming(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _COMPLETIONS_URL,
            json={"model": "gpt-4o", "prompt": "hi", "stream": True},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_request"


def test_legacy_completion_rejects_non_openai_model(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _COMPLETIONS_URL,
            json={"model": "claude-sonnet-5", "prompt": "hi"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_request"


def test_legacy_completion_rejects_wrong_capability_openai_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # text-embedding-3-small routes to openai but is EMBEDDINGS-only.
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _COMPLETIONS_URL,
            json={"model": "text-embedding-3-small", "prompt": "hi"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_request"


def test_legacy_completion_unknown_model_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _COMPLETIONS_URL,
            json={"model": "not-a-real-model", "prompt": "hi"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


# --- model policy denial (Phase 1.3, AC-2/AC-3) -------------------------------


def test_legacy_completion_denied_model_returns_403_and_never_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail_if_called(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("credential fetch must not happen for a policy-denied model")

    async def _fail_if_dispatched(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("provider must never be called for a policy-denied model")

    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _fail_if_called)
    monkeypatch.setattr(openai_mod, "create_completion", _fail_if_dispatched)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        app.state.model_policy_cache.set(
            ModelPolicySnapshot(mode="denylist", models=frozenset({"gpt-4o"}))
        )
        response = client.post(
            _COMPLETIONS_URL,
            json={"model": "gpt-4o", "prompt": "once upon a time"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "model_denied"
