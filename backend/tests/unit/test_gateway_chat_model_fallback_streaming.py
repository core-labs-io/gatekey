"""Direct end-to-end test of scenario (g) from the Model Catalog technical
design doc's own qa-engineer checklist (section 6, "Part B runtime"):

  "streaming: a pre-first-byte provider failure triggers the same fallback
  chain as non-streaming; a mid-stream failure (after the first chunk was
  already sent) does NOT trigger fallback and behaves exactly as it does
  today (existing `stream_error` SSE frame)."

The design doc's own confidence here is architectural reasoning (section
2.6's "Streaming - scope boundary, not a gap" note: `_streaming_call`'s
`call_fn` only ever calls `gen.__anext__()` ONCE), not a verified test -
this file is that verification, driving the REAL `chat.py` streaming branch
through a `TestClient`, with a mocked provider `stream_chat_completion` that
either fails before yielding anything (pre-first-byte) or fails after
yielding one chunk (mid-stream), and asserting the two behave differently
exactly as designed:

  - pre-first-byte: the fallback chain IS walked - the candidate serves the
    request, `X-Gatekey-Model-Fallback-Attempt: 1` is present, and the
    candidate's own `stream_chat_completion` is actually invoked.
  - mid-stream: the fallback chain is NEVER touched - the candidate's
    `stream_chat_completion` is never even called (proven directly, not just
    inferred from the output), the client instead receives the existing
    `stream_error`/`provider_upstream_error` SSE frame, and `X-Gatekey-
    Model-Fallback-Attempt: 0` is on the response (the dispatch itself
    "succeeded" from `dispatch_with_model_fallback()`'s point of view - the
    failure happened later, inside `_sse_event_stream`, a code path this
    function never re-enters).

Mirrors `test_gateway_chat.py`'s established `build_authenticated_app`/
`TestClient` harness and its existing `test_chat_completion_streaming_
midstream_error_emits_error_frame_not_done` test's streaming-provider-mock
shape (`stream_chat_completion` as an async generator that yields then
raises `ProviderCallError`).
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from gatekey.api.v1.gateway import common as gateway_common
from gatekey.providers import anthropic as anthropic_mod
from gatekey.providers import openai as openai_mod
from gatekey.providers.base import ProviderCallError
from gatekey.providers.model_registry import ModelCapability
from gatekey.schemas.chat import (
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
)
from gatekey.services.custom_models import CustomModelCacheEntry
from gatekey.services.proxy_keys import ApiKeyCredential

from tests.unit.gateway_test_support import build_authenticated_app

_CHAT_URL = "/v1/chat/completions"
_PRIMARY_MODEL_NAME = "streaming-fallback-primary"
_PRIMARY_NATIVE_ID = "streaming-fallback-primary-native"


async def _fake_credential(session, provider, *, key_provider):  # noqa: ANN001, ARG001
    """DB-free credential fake for both `openai` (the primary's provider)
    and `anthropic` (the fallback candidate's provider) - mirrors
    `test_gateway_chat.py`'s identical per-provider fake."""
    if provider == "openai":
        return ApiKeyCredential(provider="openai", api_key="sk-test")
    if provider == "anthropic":
        return ApiKeyCredential(provider="anthropic", api_key="sk-ant-test")
    raise AssertionError(f"unexpected provider {provider!r}")


@pytest.fixture(autouse=True)
def _patch_credential_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _fake_credential)


def _basic_stream_body(model: str) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }


def _install_primary_custom_model_with_anthropic_fallback(app) -> None:
    """Registers `_PRIMARY_MODEL_NAME` (provider=openai) with a single
    fallback candidate: the real registry model `claude-sonnet-5`
    (provider=anthropic) - a genuine cross-provider hop, not just a second
    openai candidate."""
    entry = CustomModelCacheEntry(
        id=uuid.uuid4(),
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id=_PRIMARY_NATIVE_ID,
        input_price_per_million_usd=Decimal("5.00"),
        output_price_per_million_usd=Decimal("15.00"),
        fallback_model_names=("claude-sonnet-5",),
    )
    app.state.custom_model_route_cache.set_all({_PRIMARY_MODEL_NAME: entry})


def _chunk(content: str = "", finish_reason: str | None = None) -> ChatCompletionChunk:
    return ChatCompletionChunk(
        id="chatcmpl-fallback-streaming-test",
        created=1_700_000_000,
        model="irrelevant",
        choices=[
            ChatCompletionChunkChoice(
                index=0,
                delta=ChatCompletionChunkDelta(content=content) if content else ChatCompletionChunkDelta(),
                finish_reason=finish_reason,
            )
        ],
    )


# ---------------------------------------------------------------------------
# Pre-first-byte failure -> the fallback chain IS walked.
# ---------------------------------------------------------------------------


def test_streaming_pre_first_byte_failure_triggers_fallback_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _openai_fails_before_any_yield(
        client, native_model_id, request, credential, *, timeout_seconds=60.0
    ):  # noqa: ANN001, ARG001
        raise ProviderCallError("OpenAI connection refused before any bytes streamed.", status_code=502)
        yield  # pragma: no cover - makes this an async generator function

    anthropic_calls: list[str] = []

    async def _anthropic_serves_it(
        client, native_model_id, request, credential, *, timeout_seconds=60.0
    ):  # noqa: ANN001, ARG001
        anthropic_calls.append(native_model_id)
        yield _chunk("fallback ", finish_reason=None)
        yield _chunk("answer", finish_reason=None)
        yield _chunk(finish_reason="stop")

    monkeypatch.setattr(openai_mod, "stream_chat_completion", _openai_fails_before_any_yield)
    monkeypatch.setattr(anthropic_mod, "stream_chat_completion", _anthropic_serves_it)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        _install_primary_custom_model_with_anthropic_fallback(app)
        with client.stream(
            "POST",
            _CHAT_URL,
            json=_basic_stream_body(_PRIMARY_MODEL_NAME),
            headers={"Authorization": "Bearer gk_sk_test"},
        ) as response:
            assert response.status_code == 200, response.read()
            assert response.headers["X-Gatekey-Model-Fallback-Attempt"] == "1"
            assert response.headers["X-Gatekey-Model-Fallback-From"] == _PRIMARY_MODEL_NAME
            raw_frames = [line for line in response.iter_lines() if line]

    # The candidate's own provider client was genuinely invoked - the chain
    # WAS walked, not just headers that happen to look right.
    assert anthropic_calls == ["claude-sonnet-5"]
    assert raw_frames[-1] == "data: [DONE]"
    data_frames = [line for line in raw_frames if line != "data: [DONE]"]
    parsed = [json.loads(line[len("data: "):]) for line in data_frames]
    combined_content = "".join(
        c["delta"].get("content") or "" for p in parsed for c in p["choices"]
    )
    assert combined_content == "fallback answer"
    # No error frame anywhere - a clean, fully-served fallback response.
    assert all("error" not in p for p in parsed)


# ---------------------------------------------------------------------------
# Mid-stream failure -> the fallback chain is NEVER touched.
# ---------------------------------------------------------------------------


def test_streaming_mid_stream_failure_does_not_trigger_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _openai_yields_one_chunk_then_fails(
        client, native_model_id, request, credential, *, timeout_seconds=60.0
    ):  # noqa: ANN001, ARG001
        yield _chunk("partial ", finish_reason=None)
        raise ProviderCallError("OpenAI connection dropped mid-stream.", status_code=500)

    anthropic_calls: list[str] = []

    async def _anthropic_must_never_be_called(
        client, native_model_id, request, credential, *, timeout_seconds=60.0
    ):  # noqa: ANN001, ARG001
        anthropic_calls.append(native_model_id)
        raise AssertionError("the fallback candidate must NEVER be dispatched on a mid-stream failure")
        yield  # pragma: no cover - makes this an async generator function

    monkeypatch.setattr(openai_mod, "stream_chat_completion", _openai_yields_one_chunk_then_fails)
    monkeypatch.setattr(anthropic_mod, "stream_chat_completion", _anthropic_must_never_be_called)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        _install_primary_custom_model_with_anthropic_fallback(app)
        with client.stream(
            "POST",
            _CHAT_URL,
            json=_basic_stream_body(_PRIMARY_MODEL_NAME),
            headers={"Authorization": "Bearer gk_sk_test"},
        ) as response:
            assert response.status_code == 200, response.read()  # headers already sent
            # `dispatch_with_model_fallback()` itself succeeded (it only
            # ever calls `gen.__anext__()` once, to get `first_item` - that
            # succeeded here) - the failure happens LATER, inside
            # `_sse_event_stream`, a code path this function never
            # re-enters. So the fallback-attempt header correctly reads "0"
            # even though a failure did eventually occur.
            assert response.headers["X-Gatekey-Model-Fallback-Attempt"] == "0"
            assert "X-Gatekey-Model-Fallback-From" not in response.headers
            raw_frames = [line for line in response.iter_lines() if line]

    # The critical assertion: the candidate was NEVER dispatched.
    assert anthropic_calls == []

    # Existing (pre-Part-B, Tier 4) mid-stream behavior, unchanged: the
    # successfully-streamed chunk is delivered, followed by one error frame,
    # never `[DONE]`.
    assert all(line != "data: [DONE]" for line in raw_frames)
    parsed = [json.loads(line[len("data: "):]) for line in raw_frames]
    assert parsed[0]["choices"][0]["delta"]["content"] == "partial "
    last = parsed[-1]
    assert last["error"]["code"] == "provider_upstream_error"
