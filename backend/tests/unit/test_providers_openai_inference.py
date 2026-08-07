"""Unit tests for the Phase 1.2 (BD-7a) inference methods in providers/openai.py.

Uses `httpx.MockTransport` (no live network calls). Unlike the Phase 1.1
validator tests, these inference methods take the `httpx.AsyncClient` as an
explicit parameter, so tests just construct one directly with a mock
transport rather than monkeypatching `httpx.AsyncClient`.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gatekey.providers.base import ProviderCallError
from gatekey.providers.openai import (
    OPENAI_CHAT_COMPLETIONS_URL,
    OPENAI_COMPLETIONS_URL,
    OPENAI_EMBEDDINGS_URL,
    create_chat_completion,
    create_completion,
    create_embeddings,
    stream_chat_completion,
)
from gatekey.schemas.chat import ChatCompletionRequest, CompletionRequest, EmbeddingsRequest
from gatekey.services.proxy_keys import ApiKeyCredential

CREDENTIAL = ApiKeyCredential(provider="openai", api_key="sk-test-openai")


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_create_chat_completion_passthrough_request_and_response():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer sk-test-openai"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-abc123",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "gpt-4o",
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
        model="gpt-4o",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.5,
        n=2,
    )

    async with _client(handler) as client:
        response = await create_chat_completion(client, "gpt-4o", request, CREDENTIAL)

    # Request translation: native_model_id used, extra fields carried, stream forced False.
    assert captured["body"]["model"] == "gpt-4o"
    assert captured["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert captured["body"]["temperature"] == 0.5
    assert captured["body"]["n"] == 2
    assert captured["body"]["stream"] is False

    # Response translation: pure passthrough field mapping.
    assert response.id == "chatcmpl-abc123"
    assert response.model == "gpt-4o"
    assert response.choices[0].message.content == "hi there"
    assert response.choices[0].finish_reason == "stop"
    assert response.usage.total_tokens == 7


@pytest.mark.asyncio
async def test_create_chat_completion_maps_error_status_to_provider_call_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    request = ChatCompletionRequest(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])

    async with _client(handler) as client:
        with pytest.raises(ProviderCallError) as exc_info:
            await create_chat_completion(client, "gpt-4o", request, CREDENTIAL)

    assert exc_info.value.status_code == 429
    assert "rate limited" not in exc_info.value.message


@pytest.mark.asyncio
async def test_create_chat_completion_network_error_maps_to_provider_call_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    request = ChatCompletionRequest(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])

    async with _client(handler) as client:
        with pytest.raises(ProviderCallError) as exc_info:
            await create_chat_completion(client, "gpt-4o", request, CREDENTIAL)

    assert exc_info.value.status_code is None


@pytest.mark.asyncio
async def test_stream_chat_completion_relays_chunks_and_stops_on_done():
    sse_body = (
        'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"gpt-4o",'
        '"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
        'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"gpt-4o",'
        '"choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}\n\n'
        'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"gpt-4o",'
        '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    request = ChatCompletionRequest(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
    )

    chunks = []
    async with _client(handler) as client:
        async for chunk in stream_chat_completion(client, "gpt-4o", request, CREDENTIAL):
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
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stream=True
    )

    async with _client(handler) as client:
        with pytest.raises(ProviderCallError) as exc_info:
            async for _ in stream_chat_completion(client, "gpt-4o", request, CREDENTIAL):
                pass

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_create_completion_legacy_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == OPENAI_COMPLETIONS_URL
        body = json.loads(request.content)
        assert body["prompt"] == "once upon a time"
        assert body["stream"] is False
        return httpx.Response(
            200,
            json={
                "id": "cmpl-1",
                "object": "text_completion",
                "created": 1,
                "model": "gpt-4o",
                "choices": [{"index": 0, "text": "...", "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            },
        )

    request = CompletionRequest(model="gpt-4o", prompt="once upon a time")

    async with _client(handler) as client:
        response = await create_completion(client, "gpt-4o", request, CREDENTIAL)

    assert response.choices[0].text == "..."


@pytest.mark.asyncio
async def test_create_embeddings():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == OPENAI_EMBEDDINGS_URL
        body = json.loads(request.content)
        assert body["input"] == ["a", "b"]
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"object": "embedding", "embedding": [0.1, 0.2], "index": 0},
                    {"object": "embedding", "embedding": [0.3, 0.4], "index": 1},
                ],
                "model": "text-embedding-3-small",
                "usage": {"prompt_tokens": 2, "total_tokens": 2},
            },
        )

    request = EmbeddingsRequest(model="text-embedding-3-small", input=["a", "b"])

    async with _client(handler) as client:
        response = await create_embeddings(client, "text-embedding-3-small", request, CREDENTIAL)

    assert len(response.data) == 2
    assert response.data[1].embedding == [0.3, 0.4]


def test_chat_completions_url_is_openai_native():
    assert OPENAI_CHAT_COMPLETIONS_URL == "https://api.openai.com/v1/chat/completions"
