"""Unit tests for providers/openrouter.py's inference methods.

Uses `httpx.MockTransport` (no live network calls). Mirrors
test_providers_openai_inference.py's coverage shape.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gatekey.providers.base import ProviderCallError
from gatekey.providers.openrouter import (
    OPENROUTER_CHAT_COMPLETIONS_URL,
    create_chat_completion,
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


def test_chat_completions_url_is_fixed_openrouter_url():
    assert OPENROUTER_CHAT_COMPLETIONS_URL == "https://openrouter.ai/api/v1/chat/completions"
