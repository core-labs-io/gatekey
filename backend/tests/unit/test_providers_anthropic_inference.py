"""Unit tests for the Phase 1.2 (BD-7b) inference methods in providers/anthropic.py.

Uses `httpx.MockTransport` (no live network calls). Covers request
translation (OpenAI shape -> Anthropic Messages shape), response
translation (Anthropic -> OpenAI-compatible shape) including the
stop_reason mapping table, the streaming chunk-translation sequence, and
n > 1 rejection.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gatekey.providers.anthropic import (
    ANTHROPIC_API_VERSION,
    ANTHROPIC_MESSAGES_URL,
    create_chat_completion,
    stream_chat_completion,
)
from gatekey.providers.base import ProviderCallError, UnsupportedRequestError
from gatekey.schemas.chat import ChatCompletionRequest
from gatekey.services.proxy_keys import ApiKeyCredential

CREDENTIAL = ApiKeyCredential(provider="anthropic", api_key="sk-ant-test")


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _anthropic_response(
    *,
    text: str = "hello",
    stop_reason: str = "end_turn",
    input_tokens: int = 10,
    output_tokens: int = 3,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_123",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "model": "claude-sonnet-5",
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        },
    )


@pytest.mark.asyncio
async def test_request_translation_extracts_system_and_defaults_max_tokens():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        assert request.headers["x-api-key"] == "sk-ant-test"
        assert request.headers["anthropic-version"] == ANTHROPIC_API_VERSION
        assert str(request.url) == ANTHROPIC_MESSAGES_URL
        return _anthropic_response()

    request = ChatCompletionRequest(
        model="claude-sonnet-5",
        messages=[
            {"role": "system", "content": "Be terse."},
            {"role": "system", "content": "Never lie."},
            {"role": "user", "content": "hi"},
        ],
    )

    async with _client(handler) as client:
        await create_chat_completion(
            client, "claude-sonnet-5", request, CREDENTIAL
        )

    body = captured["body"]
    assert body["system"] == "Be terse.\nNever lie."
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    # max_tokens omitted by caller -> Anthropic-required default.
    assert body["max_tokens"] == 1024
    assert body["stream"] is False


@pytest.mark.asyncio
async def test_temperature_clamped_to_anthropic_max():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _anthropic_response()

    request = ChatCompletionRequest(
        model="claude-sonnet-5",
        messages=[{"role": "user", "content": "hi"}],
        temperature=1.8,
    )

    async with _client(handler) as client:
        await create_chat_completion(
            client, "claude-sonnet-5", request, CREDENTIAL
        )

    assert captured["body"]["temperature"] == 1.0


@pytest.mark.asyncio
async def test_stop_string_wrapped_into_stop_sequences_array():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _anthropic_response()

    request = ChatCompletionRequest(
        model="claude-sonnet-5",
        messages=[{"role": "user", "content": "hi"}],
        stop="###",
    )

    async with _client(handler) as client:
        await create_chat_completion(
            client, "claude-sonnet-5", request, CREDENTIAL
        )

    assert captured["body"]["stop_sequences"] == ["###"]


@pytest.mark.asyncio
async def test_stop_list_passed_through():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _anthropic_response()

    request = ChatCompletionRequest(
        model="claude-sonnet-5",
        messages=[{"role": "user", "content": "hi"}],
        stop=["###", "STOP"],
    )

    async with _client(handler) as client:
        await create_chat_completion(
            client, "claude-sonnet-5", request, CREDENTIAL
        )

    assert captured["body"]["stop_sequences"] == ["###", "STOP"]


@pytest.mark.parametrize(
    "anthropic_stop_reason,expected_finish_reason",
    [
        ("end_turn", "stop"),
        ("stop_sequence", "stop"),
        ("max_tokens", "length"),
        ("tool_use", "tool_calls"),
        ("some_future_reason", "stop"),  # unmapped falls back to "stop"
    ],
)
@pytest.mark.asyncio
async def test_stop_reason_mapping_table(anthropic_stop_reason, expected_finish_reason):
    def handler(request: httpx.Request) -> httpx.Response:
        return _anthropic_response(stop_reason=anthropic_stop_reason)

    request = ChatCompletionRequest(
        model="claude-sonnet-5", messages=[{"role": "user", "content": "hi"}]
    )

    async with _client(handler) as client:
        response = await create_chat_completion(
            client, "claude-sonnet-5", request, CREDENTIAL
        )

    assert response.choices[0].finish_reason == expected_finish_reason


@pytest.mark.asyncio
async def test_response_translation_concatenates_text_blocks_and_maps_usage():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg_456",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Hello "},
                    {"type": "text", "text": "world"},
                ],
                "model": "claude-sonnet-5",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 7, "output_tokens": 4},
            },
        )

    request = ChatCompletionRequest(
        model="claude-sonnet-5", messages=[{"role": "user", "content": "hi"}]
    )

    async with _client(handler) as client:
        response = await create_chat_completion(
            client, "claude-sonnet-5", request, CREDENTIAL
        )

    assert response.id == "msg_456"
    assert response.object == "chat.completion"
    assert response.choices[0].message.role == "assistant"
    assert response.choices[0].message.content == "Hello world"
    assert response.usage.prompt_tokens == 7
    assert response.usage.completion_tokens == 4
    assert response.usage.total_tokens == 11


@pytest.mark.asyncio
async def test_n_greater_than_one_rejected_before_any_http_call():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not make an HTTP call when n > 1")

    request = ChatCompletionRequest(
        model="claude-sonnet-5",
        messages=[{"role": "user", "content": "hi"}],
        n=2,
    )

    async with _client(handler) as client:
        with pytest.raises(UnsupportedRequestError):
            await create_chat_completion(
                client, "claude-sonnet-5", request, CREDENTIAL
            )


@pytest.mark.asyncio
async def test_n_greater_than_one_rejected_for_streaming_too():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not make an HTTP call when n > 1")

    request = ChatCompletionRequest(
        model="claude-sonnet-5",
        messages=[{"role": "user", "content": "hi"}],
        n=3,
        stream=True,
    )

    async with _client(handler) as client:
        with pytest.raises(UnsupportedRequestError):
            async for _ in stream_chat_completion(
                client, "claude-sonnet-5", request, CREDENTIAL
            ):
                pass


@pytest.mark.asyncio
async def test_error_status_maps_to_provider_call_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(529, json={"error": {"message": "overloaded"}})

    request = ChatCompletionRequest(
        model="claude-sonnet-5", messages=[{"role": "user", "content": "hi"}]
    )

    async with _client(handler) as client:
        with pytest.raises(ProviderCallError) as exc_info:
            await create_chat_completion(
                client, "claude-sonnet-5", request, CREDENTIAL
            )

    assert exc_info.value.status_code == 529
    assert "overloaded" not in exc_info.value.message


@pytest.mark.asyncio
async def test_streaming_chunk_sequence_role_then_deltas_then_finish():
    sse_body = (
        "event: message_start\n"
        'data: {"type":"message_start","message":{"id":"msg_1","model":"claude-sonnet-5",'
        '"role":"assistant","content":[],"usage":{"input_tokens":10}}}\n\n'
        "event: content_block_start\n"
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hel"}}\n\n'
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"lo"}}\n\n'
        "event: content_block_stop\n"
        'data: {"type":"content_block_stop","index":0}\n\n'
        "event: message_delta\n"
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},'
        '"usage":{"output_tokens":2}}\n\n'
        "event: message_stop\n"
        'data: {"type":"message_stop"}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    request = ChatCompletionRequest(
        model="claude-sonnet-5",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )

    chunks = []
    async with _client(handler) as client:
        async for chunk in stream_chat_completion(
            client, "claude-sonnet-5", request, CREDENTIAL
        ):
            chunks.append(chunk)

    # Exact sequence: role-first empty chunk, then per-delta content
    # chunks, then a final finish_reason chunk, then a terminal usage chunk
    # (Phase 1.4 - Budget Basic). Nothing yielded for
    # content_block_start/content_block_stop/message_stop.
    assert len(chunks) == 5
    usage_chunk = chunks[-1]
    assert usage_chunk.choices == []
    assert usage_chunk.usage.prompt_tokens == 10
    assert usage_chunk.usage.completion_tokens == 2
    assert usage_chunk.usage.total_tokens == 12
    assert chunks[0].choices[0].delta.role == "assistant"
    assert chunks[0].choices[0].delta.content == ""
    assert chunks[0].choices[0].finish_reason is None

    assert chunks[1].choices[0].delta.content == "Hel"
    assert chunks[1].choices[0].finish_reason is None

    assert chunks[2].choices[0].delta.content == "lo"
    assert chunks[2].choices[0].finish_reason is None

    assert chunks[3].choices[0].delta.content is None
    assert chunks[3].choices[0].finish_reason == "stop"

    # All chunks share the same id/model (one logical response).
    assert len({c.id for c in chunks}) == 1
    assert all(c.model == "claude-sonnet-5" for c in chunks)
