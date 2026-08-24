"""Unit tests for providers/openrouter.py's inference methods.

Uses `httpx.MockTransport` (no live network calls). Mirrors
test_providers_openai_inference.py's coverage shape.
"""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from gatekey.providers.base import ProviderCallError
from gatekey.providers.openrouter import (
    OPENROUTER_CHAT_COMPLETIONS_URL,
    OPENROUTER_MODELS_URL,
    create_chat_completion,
    list_models,
    stream_chat_completion,
)
from gatekey.schemas.chat import ChatCompletionRequest
from gatekey.services.proxy_keys import ApiKeyCredential

CREDENTIAL = ApiKeyCredential(provider="openrouter", api_key="sk-or-test")


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_create_chat_completion_passthrough_request_and_response():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == OPENROUTER_CHAT_COMPLETIONS_URL
        captured["body"] = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer sk-or-test"
        return httpx.Response(
            200,
            json={
                "id": "gen-abc123",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "openai/gpt-4o-mini",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi there"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )

    request = ChatCompletionRequest(
        model="openrouter/openai/gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.5,
    )

    async with _client(handler) as client:
        response = await create_chat_completion(
            client, "openai/gpt-4o-mini", request, CREDENTIAL
        )

    assert captured["body"]["model"] == "openai/gpt-4o-mini"
    assert captured["body"]["stream"] is False
    assert response.id == "gen-abc123"
    assert response.choices[0].message.content == "hi there"


@pytest.mark.asyncio
async def test_create_chat_completion_omits_provider_field_when_no_trust_list_configured():
    """Regression guard: `CREDENTIAL`'s default `trusted_provider_slugs=()`
    (no admin residency config) must never add a `provider` key to the
    outbound body - byte-for-byte pre-residency-enforcement-feature
    behavior for every org that hasn't configured this."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "gen-no-restriction",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "openai/gpt-4o-mini",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    request = ChatCompletionRequest(
        model="openrouter/openai/gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
    )
    async with _client(handler) as client:
        await create_chat_completion(client, "openai/gpt-4o-mini", request, CREDENTIAL)

    assert "provider" not in captured["body"]


@pytest.mark.asyncio
async def test_create_chat_completion_restricts_to_trusted_providers_when_configured():
    """Residency enforcement: a credential carrying `trusted_provider_slugs`
    (from `ProviderKey.key_metadata`, admin-configured via
    `OpenRouterKeyRequest`) must add `provider.only` to EVERY outbound
    request, unconditionally - see `services.residency.resolve_model_region`'s
    openrouter branch, which trusts this is always applied."""
    captured = {}
    trusting_credential = ApiKeyCredential(
        provider="openrouter", api_key="sk-or-test", trusted_provider_slugs=("openai", "anthropic")
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "gen-restricted",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "openai/gpt-4o-mini",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    request = ChatCompletionRequest(
        model="openrouter/openai/gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
    )
    async with _client(handler) as client:
        await create_chat_completion(client, "openai/gpt-4o-mini", request, trusting_credential)

    assert captured["body"]["provider"] == {"only": ["openai", "anthropic"]}


@pytest.mark.asyncio
async def test_stream_chat_completion_restricts_to_trusted_providers_when_configured():
    """Streaming must apply the exact same `provider.only` restriction as
    non-streaming - see `test_create_chat_completion_restricts_to_trusted_
    providers_when_configured` above."""
    captured = {}
    trusting_credential = ApiKeyCredential(
        provider="openrouter", api_key="sk-or-test", trusted_provider_slugs=("fireworks",)
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content="data: [DONE]\n\n", headers={"content-type": "text/event-stream"})

    request = ChatCompletionRequest(
        model="openrouter/openai/gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )
    async with _client(handler) as client:
        async for _ in stream_chat_completion(client, "openai/gpt-4o-mini", request, trusting_credential):
            pass

    assert captured["body"]["provider"] == {"only": ["fireworks"]}


@pytest.mark.asyncio
async def test_create_chat_completion_tolerates_null_content_from_reasoning_model():
    """Post-ship crash fix: this is the EXACT live failure - a real drift-
    canary run against `openrouter/meta/muse-spark-1.2` (a reasoning
    model) returned `content: null` and crashed with an unhandled pydantic
    ValidationError before this fix (`choices[].message.content` used to
    require a non-null `str`)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "gen-null-content",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "meta/muse-spark-1.2",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": None}, "finish_reason": "length"}
                ],
                "usage": {"prompt_tokens": 15, "completion_tokens": 674, "total_tokens": 689},
            },
        )

    request = ChatCompletionRequest(
        model="openrouter/meta/muse-spark-1.2", messages=[{"role": "user", "content": "hi"}]
    )
    async with _client(handler) as client:
        response = await create_chat_completion(client, "meta/muse-spark-1.2", request, CREDENTIAL)

    assert response.choices[0].message.content is None
    assert response.choices[0].finish_reason == "length"


@pytest.mark.asyncio
async def test_create_chat_completion_maps_error_status_to_provider_call_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    request = ChatCompletionRequest(
        model="openrouter/openai/gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
    )

    async with _client(handler) as client:
        with pytest.raises(ProviderCallError) as exc_info:
            await create_chat_completion(client, "openai/gpt-4o-mini", request, CREDENTIAL)

    assert exc_info.value.status_code == 429
    assert "rate limited" not in exc_info.value.message


@pytest.mark.asyncio
async def test_create_chat_completion_server_error_maps_to_provider_call_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    request = ChatCompletionRequest(
        model="openrouter/openai/gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
    )

    async with _client(handler) as client:
        with pytest.raises(ProviderCallError) as exc_info:
            await create_chat_completion(client, "openai/gpt-4o-mini", request, CREDENTIAL)

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_create_chat_completion_network_error_maps_to_provider_call_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    request = ChatCompletionRequest(
        model="openrouter/openai/gpt-4o-mini", messages=[{"role": "user", "content": "hi"}]
    )

    async with _client(handler) as client:
        with pytest.raises(ProviderCallError) as exc_info:
            await create_chat_completion(client, "openai/gpt-4o-mini", request, CREDENTIAL)

    assert exc_info.value.status_code is None


@pytest.mark.asyncio
async def test_stream_chat_completion_relays_chunks_and_stops_on_done():
    sse_body = (
        'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"openai/gpt-4o-mini",'
        '"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
        'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"openai/gpt-4o-mini",'
        '"choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}\n\n'
        'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"openai/gpt-4o-mini",'
        '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    request = ChatCompletionRequest(
        model="openrouter/openai/gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )

    chunks = []
    async with _client(handler) as client:
        async for chunk in stream_chat_completion(client, "openai/gpt-4o-mini", request, CREDENTIAL):
            chunks.append(chunk)

    assert len(chunks) == 3
    assert chunks[0].choices[0].delta.role == "assistant"
    assert chunks[1].choices[0].delta.content == "Hi"
    assert chunks[2].choices[0].finish_reason == "stop"


@pytest.mark.asyncio
async def test_stream_chat_completion_error_status_raises_provider_call_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    request = ChatCompletionRequest(
        model="openrouter/openai/gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )

    async with _client(handler) as client:
        with pytest.raises(ProviderCallError) as exc_info:
            async for _ in stream_chat_completion(client, "openai/gpt-4o-mini", request, CREDENTIAL):
                pass

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_stream_chat_completion_network_error_maps_to_provider_call_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    request = ChatCompletionRequest(
        model="openrouter/openai/gpt-4o-mini",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )

    async with _client(handler) as client:
        with pytest.raises(ProviderCallError) as exc_info:
            async for _ in stream_chat_completion(client, "openai/gpt-4o-mini", request, CREDENTIAL):
                pass

    assert exc_info.value.status_code is None


# ---------------------------------------------------------------------------
# list_models() - Model Catalog technical design doc section 1.5
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_models_maps_entries_with_live_pricing():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == OPENROUTER_MODELS_URL
        # No Authorization header for this specific call - public catalog,
        # see module docstring "Model Catalog" section.
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "openai/gpt-4o-mini",
                        "name": "OpenAI: GPT-4o-mini",
                        "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
                    }
                ]
            },
        )

    async with _client(handler) as client:
        entries = await list_models(client)

    assert len(entries) == 1
    entry = entries[0]
    assert entry.native_model_id == "openai/gpt-4o-mini"
    assert entry.display_name == "OpenAI: GPT-4o-mini"
    # 0.00000015 * 1_000_000 == 0.15, 0.0000006 * 1_000_000 == 0.60
    assert entry.input_price_per_million_usd == Decimal("0.150000")
    assert entry.output_price_per_million_usd == Decimal("0.600000")


@pytest.mark.asyncio
async def test_list_models_negative_sentinel_pricing_blanks_both_fields():
    """OpenRouter's `"-1"` variable/negotiated-pricing sentinel must never
    surface as a nonsense negative price - both price fields blank out for
    the whole entry, not just the sentinel-bearing one (Model Catalog
    technical design doc section 1.2)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "some/variable-priced-model",
                        "name": "Variable Priced Model",
                        "pricing": {"prompt": "-1", "completion": "0.000001"},
                    }
                ]
            },
        )

    async with _client(handler) as client:
        entries = await list_models(client)

    assert len(entries) == 1
    assert entries[0].input_price_per_million_usd is None
    assert entries[0].output_price_per_million_usd is None


@pytest.mark.asyncio
async def test_list_models_non_numeric_pricing_blanks_both_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "some/weird-model",
                        "name": "Weird Model",
                        "pricing": {"prompt": "not-a-number", "completion": "0.000001"},
                    }
                ]
            },
        )

    async with _client(handler) as client:
        entries = await list_models(client)

    assert entries[0].input_price_per_million_usd is None
    assert entries[0].output_price_per_million_usd is None


@pytest.mark.asyncio
async def test_list_models_maps_error_status_to_provider_call_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async with _client(handler) as client:
        with pytest.raises(ProviderCallError) as exc_info:
            await list_models(client)

    assert exc_info.value.status_code == 500


def test_chat_completions_url_is_fixed_openrouter_url():
    assert OPENROUTER_CHAT_COMPLETIONS_URL == "https://openrouter.ai/api/v1/chat/completions"
