"""Unit tests for POST /v1/chat/completions (Phase 1.2, BD-9)."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from gatekey.api import deps as deps_module
from gatekey.api.v1.gateway import common as gateway_common
from gatekey.api.v1.gateway.chat import _STREAM_EMPTY, _sse_event_stream
from gatekey.providers import anthropic as anthropic_mod
from gatekey.providers import ollama as ollama_mod
from gatekey.providers import openai as openai_mod
from gatekey.providers import openrouter as openrouter_mod
from gatekey.providers import vertex_ai as vertex_mod
from gatekey.providers.base import ProviderCallError
from gatekey.providers.base import UnsupportedRequestError as ProviderUnsupportedRequestError
from gatekey.schemas.chat import (
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatCompletionResponse,
    ChatCompletionUsage,
    ChatMessage,
)
from gatekey.services.model_policy import ModelPolicySnapshot
from gatekey.services import usage_logs as usage_logs_service
from gatekey.services.proxy_keys import (
    ApiKeyCredential,
    OllamaCredential,
    ProviderKeyNotConfiguredError,
    ServiceAccountCredential,
)

from tests.unit.gateway_test_support import build_app_with_real_auth, build_authenticated_app

_CHAT_URL = "/v1/chat/completions"


def _basic_body(model: str, *, stream: bool = False) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": stream,
    }


def _fake_response(native_model_id: str, text: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="chatcmpl-test",
        created=1_700_000_000,
        model=native_model_id,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=text),
                finish_reason="stop",
            )
        ],
        usage=ChatCompletionUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


async def _fake_credential(session, provider, *, key_provider):  # noqa: ANN001, ARG001
    if provider == "openai":
        return ApiKeyCredential(provider="openai", api_key="sk-test")
    if provider == "anthropic":
        return ApiKeyCredential(provider="anthropic", api_key="sk-ant-test")
    if provider == "vertex_ai":
        return ServiceAccountCredential(
            provider="vertex_ai",
            service_account_json={"client_email": "svc@test.iam.gserviceaccount.com"},
            project_id="test-project",
            location="us-central1",
        )
    if provider == "ollama":
        return OllamaCredential(provider="ollama", base_url="http://localhost:11434", bearer_token="")
    if provider == "openrouter":
        return ApiKeyCredential(provider="openrouter", api_key="sk-or-test")
    raise AssertionError(f"unexpected provider {provider!r}")


@pytest.fixture(autouse=True)
def _patch_credential_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _fake_credential)


def _three_streaming_chunks(native_model_id: str):
    async def _gen(client, model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        yield ChatCompletionChunk(
            id="chatcmpl-test",
            created=1_700_000_000,
            model=native_model_id,
            choices=[
                ChatCompletionChunkChoice(
                    index=0,
                    delta=ChatCompletionChunkDelta(role="assistant", content=""),
                    finish_reason=None,
                )
            ],
        )
        yield ChatCompletionChunk(
            id="chatcmpl-test",
            created=1_700_000_000,
            model=native_model_id,
            choices=[
                ChatCompletionChunkChoice(
                    index=0, delta=ChatCompletionChunkDelta(content="hi there"), finish_reason=None
                )
            ],
        )
        yield ChatCompletionChunk(
            id="chatcmpl-test",
            created=1_700_000_000,
            model=native_model_id,
            choices=[
                ChatCompletionChunkChoice(index=0, delta=ChatCompletionChunkDelta(), finish_reason="stop")
            ],
        )

    return _gen


# --- successful non-streaming chat completion, one per provider -------------


def test_chat_completion_non_streaming_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return _fake_response(native_model_id, "hello from openai")

    monkeypatch.setattr(openai_mod, "create_chat_completion", _fake_create)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json=_basic_body("gpt-4o"),
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "gpt-4o"
    assert body["choices"][0]["message"]["content"] == "hello from openai"


def test_chat_completion_non_streaming_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return _fake_response(native_model_id, "hello from anthropic")

    monkeypatch.setattr(anthropic_mod, "create_chat_completion", _fake_create)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json=_basic_body("claude-sonnet-5"),
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "hello from anthropic"


def test_chat_completion_non_streaming_vertex_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(
        client, native_model_id, request, credential, token_cache, *, timeout_seconds=60.0
    ):  # noqa: ANN001, ARG001
        return _fake_response(native_model_id, "hello from vertex")

    monkeypatch.setattr(vertex_mod, "create_chat_completion", _fake_create)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json=_basic_body("gemini-2.5-pro"),
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "hello from vertex"


def test_chat_completion_non_streaming_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return _fake_response(native_model_id, "hello from ollama")

    monkeypatch.setattr(ollama_mod, "create_chat_completion", _fake_create)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json=_basic_body("ollama/llama3.1"),
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "hello from ollama"


def test_chat_completion_non_streaming_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return _fake_response(native_model_id, "hello from openrouter")

    monkeypatch.setattr(openrouter_mod, "create_chat_completion", _fake_create)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json=_basic_body("openrouter/openai/gpt-4o-mini"),
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "hello from openrouter"


# --- streaming, end-to-end SSE -----------------------------------------------


def test_chat_completion_streaming_openai_yields_done_terminator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai_mod, "stream_chat_completion", _three_streaming_chunks("gpt-4o"))

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        with client.stream(
            "POST",
            _CHAT_URL,
            json=_basic_body("gpt-4o", stream=True),
            headers={"Authorization": "Bearer gk_sk_test"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            raw_frames = [line for line in response.iter_lines() if line]

    assert raw_frames[-1] == "data: [DONE]"
    data_frames = [line for line in raw_frames if line != "data: [DONE]"]
    assert len(data_frames) == 3
    parsed = [json.loads(line[len("data: "):]) for line in data_frames]
    assert parsed[0]["choices"][0]["delta"]["role"] == "assistant"
    assert parsed[1]["choices"][0]["delta"]["content"] == "hi there"
    assert parsed[2]["choices"][0]["finish_reason"] == "stop"


def test_chat_completion_streaming_ollama_yields_done_terminator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ollama_mod, "stream_chat_completion", _three_streaming_chunks("llama3.1"))

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        with client.stream(
            "POST",
            _CHAT_URL,
            json=_basic_body("ollama/llama3.1", stream=True),
            headers={"Authorization": "Bearer gk_sk_test"},
        ) as response:
            assert response.status_code == 200
            raw_frames = [line for line in response.iter_lines() if line]

    assert raw_frames[-1] == "data: [DONE]"
    data_frames = [line for line in raw_frames if line != "data: [DONE]"]
    assert len(data_frames) == 3


def test_chat_completion_streaming_openrouter_yields_done_terminator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        openrouter_mod, "stream_chat_completion", _three_streaming_chunks("openai/gpt-4o-mini")
    )

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        with client.stream(
            "POST",
            _CHAT_URL,
            json=_basic_body("openrouter/openai/gpt-4o-mini", stream=True),
            headers={"Authorization": "Bearer gk_sk_test"},
        ) as response:
            assert response.status_code == 200
            raw_frames = [line for line in response.iter_lines() if line]

    assert raw_frames[-1] == "data: [DONE]"
    data_frames = [line for line in raw_frames if line != "data: [DONE]"]
    assert len(data_frames) == 3


def test_chat_completion_streaming_empty_upstream_still_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _empty_gen(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return
        yield  # pragma: no cover - makes this an async generator function

    monkeypatch.setattr(openai_mod, "stream_chat_completion", _empty_gen)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        with client.stream(
            "POST",
            _CHAT_URL,
            json=_basic_body("gpt-4o", stream=True),
            headers={"Authorization": "Bearer gk_sk_test"},
        ) as response:
            assert response.status_code == 200
            raw_frames = [line for line in response.iter_lines() if line]

    assert raw_frames == ["data: [DONE]"]


# --- error mapping ------------------------------------------------------------


def test_chat_completion_unknown_model_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json=_basic_body("not-a-real-model"),
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_chat_completion_provider_not_configured_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _raise_not_configured(session, provider, *, key_provider):  # noqa: ANN001, ARG001
        raise ProviderKeyNotConfiguredError(provider)

    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _raise_not_configured)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json=_basic_body("gpt-4o"),
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "provider_not_configured"


def test_chat_completion_ollama_provider_not_configured_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mirrors test_chat_completion_provider_not_configured_returns_404 but
    # for a new-provider model, confirming the 404-on-no-key behavior is
    # provider-agnostic (fetch_credential/ProviderKeyNotConfiguredError is
    # never special-cased per provider) rather than only exercised for the
    # original 3 providers.
    async def _raise_not_configured(session, provider, *, key_provider):  # noqa: ANN001, ARG001
        raise ProviderKeyNotConfiguredError(provider)

    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _raise_not_configured)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json=_basic_body("ollama/llama3.1"),
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "provider_not_configured"


def test_chat_completion_openrouter_provider_not_configured_returns_404(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_not_configured(session, provider, *, key_provider):  # noqa: ANN001, ARG001
        raise ProviderKeyNotConfiguredError(provider)

    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _raise_not_configured)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json=_basic_body("openrouter/openai/gpt-4o-mini"),
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "provider_not_configured"


def test_chat_completion_wrong_capability_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    # text-embedding-3-small is registered as EMBEDDINGS-only.
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json=_basic_body("text-embedding-3-small"),
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_request"


# --- model policy denial (Phase 1.3, AC-2/AC-3) -------------------------------


def _deny_gpt_4o_via_denylist(app) -> None:
    """Push a denylist snapshot straight into the app's process-local cache.

    Mirrors design doc section 2.3 - no admin PUT/DB round trip needed for
    a unit test; `app.state.model_policy_cache` is set by the real lifespan
    (see `main.py`) before this is called, so this simulates "an admin has
    already configured a denylist" without touching the database.
    """
    app.state.model_policy_cache.set(
        ModelPolicySnapshot(mode="denylist", models=frozenset({"gpt-4o"}))
    )


def test_chat_completion_denied_model_non_streaming_returns_403_and_never_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail_if_called(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("credential fetch must not happen for a policy-denied model")

    async def _fail_if_dispatched(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("provider must never be called for a policy-denied model")

    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _fail_if_called)
    monkeypatch.setattr(openai_mod, "create_chat_completion", _fail_if_dispatched)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        _deny_gpt_4o_via_denylist(app)
        response = client.post(
            _CHAT_URL,
            json=_basic_body("gpt-4o"),
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 403
    # AC-2's exact required envelope/message text, checked with full
    # equality (not just a substring/code check) - locks down the precise
    # wording, not just "some 403 happened". Phase 2 (BD-13) extended the
    # message to name the blocking layer ("org policy" here); `code` and
    # status are unchanged.
    body = response.json()
    # Tier 4 adds a per-request correlation id to every error body - assert
    # it separately, then keep the exact-wording lockdown on the rest.
    request_id = body["error"].pop("request_id")
    assert request_id == response.headers["X-Request-ID"]
    assert body == {
        "error": {
            "code": "model_denied",
            "message": (
                "Model 'gpt-4o' is not permitted by this organization's "
                "model access policy (org policy)."
            ),
        }
    }


def test_chat_completion_denied_model_streaming_returns_403_and_never_dispatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fail_if_called(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("credential fetch must not happen for a policy-denied model")

    async def _fail_if_dispatched(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("provider must never be called for a policy-denied model")
        yield  # pragma: no cover - makes this an async generator function

    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _fail_if_called)
    monkeypatch.setattr(openai_mod, "stream_chat_completion", _fail_if_dispatched)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        _deny_gpt_4o_via_denylist(app)
        response = client.post(
            _CHAT_URL,
            json=_basic_body("gpt-4o", stream=True),
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    # The policy check runs before the `if body.stream:` split (design doc
    # section 3.3) - a streaming request for a denied model must get a
    # normal structured-error JSON response (403), never a 200 with a
    # broken/empty SSE stream.
    assert response.status_code == 403
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "model_denied"


def test_chat_completion_allowlist_permits_listed_model(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return _fake_response(native_model_id, "hello from openai")

    monkeypatch.setattr(openai_mod, "create_chat_completion", _fake_create)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        app.state.model_policy_cache.set(
            ModelPolicySnapshot(mode="allowlist", models=frozenset({"gpt-4o"}))
        )
        response = client.post(
            _CHAT_URL,
            json=_basic_body("gpt-4o"),
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 200


@pytest.mark.parametrize("variant", ["GPT-4o", " gpt-4o", "gpt-4o ", "Gpt-4O"])
def test_chat_completion_allowlisted_model_case_or_whitespace_variant_is_404_not_403_not_200(
    monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    """End-to-end adversarial case for design doc section 3.1: an allowlist
    containing exactly "gpt-4o" must not be bypassable (nor incorrectly
    over-blocked) by a case/whitespace variant. Because `resolve_route()`
    does an exact-match dict lookup, the variant must be rejected as
    `model_not_found` (404) *before* the policy check ever runs - never a
    200 (policy check too permissive on variants) and never a 403 with
    `model_denied` (would imply a second, inconsistent notion of model
    identity exists somewhere in the gateway).
    """
    async def _fail_if_called(*args, **kwargs):  # noqa: ANN001, ARG001
        raise AssertionError("credential fetch must not happen for an unregistered model")

    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _fail_if_called)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        app.state.model_policy_cache.set(
            ModelPolicySnapshot(mode="allowlist", models=frozenset({"gpt-4o"}))
        )
        response = client.post(
            _CHAT_URL,
            json=_basic_body(variant),
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_chat_completion_missing_auth_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_app_with_real_auth()
    with TestClient(app) as client:
        response = client.post(_CHAT_URL, json=_basic_body("gpt-4o"))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_chat_completion_invalid_auth_token_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_lookup(session, secret_hash):  # noqa: ANN001, ARG001
        return None

    monkeypatch.setattr(deps_module, "get_active_service_account_by_hash", _fake_lookup)

    app = build_app_with_real_auth()
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json=_basic_body("gpt-4o"),
            headers={"Authorization": f"Bearer gk_sk_{'a' * 40}"},
        )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_chat_completion_n_greater_than_one_against_anthropic_returns_400(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_unsupported(
        client, native_model_id, request, credential, *, timeout_seconds=60.0
    ):  # noqa: ANN001, ARG001
        raise ProviderUnsupportedRequestError("n > 1 not supported")

    monkeypatch.setattr(anthropic_mod, "create_chat_completion", _raise_unsupported)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json={**_basic_body("claude-sonnet-5"), "n": 2},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_request"


def test_chat_completion_provider_call_error_maps_to_502_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_call_error(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        raise ProviderCallError("OpenAI returned HTTP 500 during inference.", status_code=500)

    monkeypatch.setattr(openai_mod, "create_chat_completion", _raise_call_error)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json=_basic_body("gpt-4o"),
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_upstream_error"


def test_chat_completion_provider_call_error_passes_through_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_rate_limited(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        raise ProviderCallError("OpenAI returned HTTP 429 during inference.", status_code=429)

    monkeypatch.setattr(openai_mod, "create_chat_completion", _raise_rate_limited)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json=_basic_body("gpt-4o"),
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "provider_upstream_error"


def test_chat_completion_idempotency_key_rejected_when_too_long(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json=_basic_body("gpt-4o"),
            headers={
                "Authorization": "Bearer gk_sk_test",
                "Idempotency-Key": "x" * 256,
            },
        )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unsupported_request"


def test_chat_completion_idempotency_key_accepted_when_reasonable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return _fake_response(native_model_id, "ok")

    monkeypatch.setattr(openai_mod, "create_chat_completion", _fake_create)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json=_basic_body("gpt-4o"),
            headers={
                "Authorization": "Bearer gk_sk_test",
                "Idempotency-Key": "client-generated-key-123",
            },
        )
    assert response.status_code == 200


# --- malformed request body -> structured 422, not a 500 --------------------


def test_chat_completion_missing_messages_field_returns_structured_422(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json={"model": "gpt-4o"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"


def test_chat_completion_messages_wrong_type_returns_structured_422(monkeypatch: pytest.MonkeyPatch) -> None:
    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL,
            json={"model": "gpt-4o", "messages": "not-a-list"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --- resource cleanup on client disconnect (code-review finding) -------------


async def test_sse_event_stream_closes_upstream_generator_on_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_sse_event_stream` must `aclose()` the upstream provider generator as
    soon as it observes `request.is_disconnected() -> True`, rather than
    relying on async-generator GC finalization to eventually release the
    pooled httpx connection (code-review finding, Phase 1.2 sign-off) - this
    is a direct unit test of the generator function itself, since QA already
    established that `TestClient`/`ASGITransport` cannot simulate a real
    client disconnect end-to-end (`request.is_disconnected()` never returns
    True through that stack)."""

    closed = False

    class _FakeRemaining:
        def __aiter__(self):
            return self

        async def __anext__(self):
            return _fake_response("gpt-4o", "unused").choices[0].message  # never reached

        async def aclose(self):
            nonlocal closed
            closed = True

    class _FakeRequest:
        async def is_disconnected(self) -> bool:
            return True

    async def _fake_record_usage_log(session, **kwargs):  # noqa: ANN001, ARG001
        return None

    monkeypatch.setattr(usage_logs_service, "record_usage_log", _fake_record_usage_log)

    gen = _sse_event_stream(
        request=_FakeRequest(),
        first_item=_STREAM_EMPTY,
        remaining=_FakeRemaining(),
        timer=gateway_common.LatencyTimer(),
        request_id="test-request-id",
        provider="openai",
        model="gpt-4o",
        idempotency_key=None,
        session=object(),
        user_id=uuid.uuid4(),
        team_id=None,
        service_account_key_id=uuid.uuid4(),
        personal_api_key_id=None,
        client_wants_usage=False,
    )

    frames = [chunk async for chunk in gen]

    assert closed is True
    # No [DONE] terminator on the disconnect path - a client that has
    # already gone does not need it, and there is no guarantee the
    # connection can still accept bytes (see chat.py finally block).
    assert frames == []


# --- Tier 4: mid-stream provider failure emits an SSE error frame, not [DONE] --


def test_chat_completion_streaming_midstream_error_emits_error_frame_not_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider failure AFTER chunks have streamed must be distinguishable
    from a complete response: one final `data: {"error": ...}` frame, and
    never `data: [DONE]` (Tier 4 ops/DX polish)."""

    async def _two_chunks_then_error(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        yield ChatCompletionChunk(
            id="chatcmpl-test",
            created=1_700_000_000,
            model=native_model_id,
            choices=[
                ChatCompletionChunkChoice(
                    index=0,
                    delta=ChatCompletionChunkDelta(role="assistant", content=""),
                    finish_reason=None,
                )
            ],
        )
        yield ChatCompletionChunk(
            id="chatcmpl-test",
            created=1_700_000_000,
            model=native_model_id,
            choices=[
                ChatCompletionChunkChoice(
                    index=0,
                    delta=ChatCompletionChunkDelta(content="partial answ"),
                    finish_reason=None,
                )
            ],
        )
        raise ProviderCallError("OpenAI returned HTTP 500 during inference.", status_code=500)

    monkeypatch.setattr(openai_mod, "stream_chat_completion", _two_chunks_then_error)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        with client.stream(
            "POST",
            _CHAT_URL,
            json=_basic_body("gpt-4o", stream=True),
            headers={"Authorization": "Bearer gk_sk_test"},
        ) as response:
            assert response.status_code == 200  # headers were already sent
            raw_frames = [line for line in response.iter_lines() if line]

    assert all(line != "data: [DONE]" for line in raw_frames)
    last = json.loads(raw_frames[-1][len("data: "):])
    assert last["error"]["code"] == "provider_upstream_error"
    assert "request_id" in last["error"]
    # The successfully-streamed chunks were still delivered before the frame.
    first = json.loads(raw_frames[0][len("data: "):])
    assert first["choices"][0]["delta"]["role"] == "assistant"
