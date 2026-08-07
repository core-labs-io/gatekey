"""Unit tests for schemas/chat.py (Phase 1.2, BD-8)."""

from __future__ import annotations

from gatekey.schemas.chat import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    CompletionRequest,
    EmbeddingsRequest,
)


def test_chat_completion_request_ignores_unknown_fields():
    # Real OpenAI SDKs send extra fields (e.g. "user") by default - these
    # must be silently dropped, not rejected, per the design doc's
    # "drop-in replacement" deliverable.
    request = ChatCompletionRequest.model_validate(
        {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
            "user": "some-end-user-id",
            "stream_options": {"include_usage": True},
        }
    )
    assert request.model == "gpt-4o"
    assert not hasattr(request, "user")


def test_chat_completion_request_defaults():
    request = ChatCompletionRequest(model="gpt-4o", messages=[{"role": "user", "content": "hi"}])
    assert request.stream is False
    assert request.n is None
    assert request.temperature is None


def test_chat_completion_request_stop_accepts_string_or_list():
    r1 = ChatCompletionRequest(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stop="END"
    )
    r2 = ChatCompletionRequest(
        model="gpt-4o", messages=[{"role": "user", "content": "hi"}], stop=["END", "STOP"]
    )
    assert r1.stop == "END"
    assert r2.stop == ["END", "STOP"]


def test_chat_completion_response_round_trip():
    response = ChatCompletionResponse(
        id="chatcmpl-1",
        created=1700000000,
        model="gpt-4o",
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hi"},
                "finish_reason": "stop",
            }
        ],
        usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    )
    assert response.object == "chat.completion"
    dumped = response.model_dump()
    assert dumped["choices"][0]["message"]["content"] == "hi"


def test_chat_completion_chunk_object_literal():
    chunk = ChatCompletionChunk(
        id="chatcmpl-1",
        created=1,
        model="gpt-4o",
        choices=[{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    )
    assert chunk.object == "chat.completion.chunk"


def test_completion_request_stream_field_exists_but_unenforced_here():
    # BD-9 (route layer) is responsible for rejecting stream=True against
    # the legacy endpoint; the schema itself just carries the field.
    request = CompletionRequest(model="gpt-4o", prompt="once upon a time", stream=True)
    assert request.stream is True


def test_embeddings_request_input_accepts_string_or_list():
    r1 = EmbeddingsRequest(model="text-embedding-3-small", input="hello")
    r2 = EmbeddingsRequest(model="text-embedding-3-small", input=["hello", "world"])
    assert r1.input == "hello"
    assert r2.input == ["hello", "world"]
