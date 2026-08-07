"""Unit tests for providers/ollama.py's inference methods.

Uses `httpx.MockTransport` (no live network calls). Mirrors
test_providers_openai_inference.py's coverage shape, plus the AC-A1-7
regression test unique to Ollama: two `OllamaCredential`s differing only
in `base_url` must produce two different outbound URLs for the same
`create_chat_completion` call.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gatekey.providers.base import ProviderCallError
from gatekey.providers.ollama import (
    _chat_completions_url,
    create_chat_completion,
    stream_chat_completion,
)
from gatekey.schemas.chat import ChatCompletionRequest
from gatekey.services.proxy_keys import OllamaCredential

CREDENTIAL = OllamaCredential(
    provider="ollama", base_url="http://localhost:11434", bearer_token="my-bearer"
)
CREDENTIAL_NO_BEARER = OllamaCredential(
    provider="ollama", base_url="http://localhost:11434", bearer_token=""
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_create_chat_completion_passthrough_request_and_response():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer my-bearer"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-abc123",
                "object": "chat.completion",
                "created": 1700000000,
                "model": "llama3.1",
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
        model="ollama/llama3.1",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.5,
    )

    async with _client(handler) as client:
        response = await create_chat_completion(client, "llama3.1", request, CREDENTIAL)

    assert captured["url"] == "http://localhost:11434/v1/chat/completions"
    assert captured["body"]["model"] == "llama3.1"
    assert captured["body"]["stream"] is False
    assert response.id == "chatcmpl-abc123"
    assert response.choices[0].message.content == "hi there"


@pytest.mark.asyncio
async def test_create_chat_completion_uses_placeholder_bearer_when_none_configured():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer ollama"
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1,
                "model": "llama3.1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    request = ChatCompletionRequest(model="ollama/llama3.1", messages=[{"role": "user", "content": "hi"}])

    async with _client(handler) as client:
        await create_chat_completion(client, "llama3.1", request, CREDENTIAL_NO_BEARER)


@pytest.mark.asyncio
async def test_create_chat_completion_maps_error_status_to_provider_call_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited"}})

    request = ChatCompletionRequest(model="ollama/llama3.1", messages=[{"role": "user", "content": "hi"}])

    async with _client(handler) as client:
        with pytest.raises(ProviderCallError) as exc_info:
            await create_chat_completion(client, "llama3.1", request, CREDENTIAL)

    assert exc_info.value.status_code == 429
    assert "rate limited" not in exc_info.value.message


@pytest.mark.asyncio
async def test_create_chat_completion_network_error_maps_to_provider_call_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    request = ChatCompletionRequest(model="ollama/llama3.1", messages=[{"role": "user", "content": "hi"}])

    async with _client(handler) as client:
        with pytest.raises(ProviderCallError) as exc_info:
            await create_chat_completion(client, "llama3.1", request, CREDENTIAL)

    assert exc_info.value.status_code is None


@pytest.mark.asyncio
async def test_stream_chat_completion_relays_chunks_and_stops_on_done():
    sse_body = (
        'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"llama3.1",'
        '"choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
        'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"llama3.1",'
        '"choices":[{"index":0,"delta":{"content":"Hi"},"finish_reason":null}]}\n\n'
        'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"llama3.1",'
        '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    request = ChatCompletionRequest(
        model="ollama/llama3.1", messages=[{"role": "user", "content": "hi"}], stream=True
    )

    chunks = []
    async with _client(handler) as client:
        async for chunk in stream_chat_completion(client, "llama3.1", request, CREDENTIAL):
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
        model="ollama/llama3.1", messages=[{"role": "user", "content": "hi"}], stream=True
    )

    async with _client(handler) as client:
        with pytest.raises(ProviderCallError) as exc_info:
            async for _ in stream_chat_completion(client, "llama3.1", request, CREDENTIAL):
                pass

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_stream_chat_completion_network_error_maps_to_provider_call_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    request = ChatCompletionRequest(
        model="ollama/llama3.1", messages=[{"role": "user", "content": "hi"}], stream=True
    )

    async with _client(handler) as client:
        with pytest.raises(ProviderCallError) as exc_info:
            async for _ in stream_chat_completion(client, "llama3.1", request, CREDENTIAL):
                pass

    assert exc_info.value.status_code is None


# ---------------------------------------------------------------------------
# AC-A1-7 regression test - the one shape unique to Ollama among all four
# providers: base_url is per-credential, not a fixed module constant, so two
# credentials differing only in base_url must route to two different URLs.
# ---------------------------------------------------------------------------


def test_chat_completions_url_varies_by_credential_base_url():
    credential_a = OllamaCredential(provider="ollama", base_url="http://host-a:11434", bearer_token="")
    credential_b = OllamaCredential(provider="ollama", base_url="http://host-b:11434", bearer_token="")

    url_a = _chat_completions_url(credential_a)
    url_b = _chat_completions_url(credential_b)

    assert url_a != url_b
    assert url_a == "http://host-a:11434/v1/chat/completions"
    assert url_b == "http://host-b:11434/v1/chat/completions"


@pytest.mark.asyncio
async def test_create_chat_completion_two_credentials_hit_different_urls():
    captured_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "id": "c1",
                "object": "chat.completion",
                "created": 1,
                "model": "llama3.1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hi"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    request = ChatCompletionRequest(model="ollama/llama3.1", messages=[{"role": "user", "content": "hi"}])
    credential_a = OllamaCredential(provider="ollama", base_url="http://host-a:11434", bearer_token="")
    credential_b = OllamaCredential(provider="ollama", base_url="http://host-b:11434", bearer_token="")

    async with _client(handler) as client:
        await create_chat_completion(client, "llama3.1", request, credential_a)
        await create_chat_completion(client, "llama3.1", request, credential_b)

    assert captured_urls == [
        "http://host-a:11434/v1/chat/completions",
        "http://host-b:11434/v1/chat/completions",
    ]
    assert captured_urls[0] != captured_urls[1]
