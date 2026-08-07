"""Unit tests for the Phase 1.2 (BD-7c) inference methods in providers/vertex_ai.py.

Mocks google-auth credential construction/refresh and the HTTP transport -
no real network calls, no real GCP credentials required. Covers request
translation (OpenAI shape -> Gemini generateContent shape), response
translation including the finishReason mapping table, the streaming
incremental-relay chunk sequence, n > 1 rejection, and
`VertexAITokenCache` reuse/refresh behavior.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import google.auth.exceptions
import httpx
import pytest

from gatekey.providers.base import ProviderCallError, UnsupportedRequestError
from gatekey.providers.vertex_ai import (
    VertexAITokenCache,
    create_chat_completion,
    create_embeddings,
    stream_chat_completion,
)
from gatekey.schemas.chat import ChatCompletionRequest, EmbeddingsRequest
from gatekey.services.proxy_keys import ServiceAccountCredential

FAKE_SERVICE_ACCOUNT_JSON = {
    "type": "service_account",
    "project_id": "fake-project",
    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
    "client_email": "fake@fake-project.iam.gserviceaccount.com",
}

CREDENTIAL = ServiceAccountCredential(
    provider="vertex_ai",
    service_account_json=FAKE_SERVICE_ACCOUNT_JSON,
    project_id="fake-project",
    location="us-central1",
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class _FakeCredentials:
    def __init__(self, token: str = "fake-bearer-token", expiry: datetime | None = None):
        self.token = None
        self._final_token = token
        self.expiry = expiry
        self.refresh_calls = 0

    def refresh(self, request):
        self.refresh_calls += 1
        self.token = self._final_token


def _patch_credentials(monkeypatch: pytest.MonkeyPatch, credentials_by_call):
    """`credentials_by_call` is either a single fake or a list consumed in order."""
    calls = {"count": 0}
    if not isinstance(credentials_by_call, list):
        credentials_by_call = [credentials_by_call]

    def factory(info, scopes=None):
        idx = min(calls["count"], len(credentials_by_call) - 1)
        calls["count"] += 1
        return credentials_by_call[idx]

    monkeypatch.setattr(
        "gatekey.providers.vertex_ai.Credentials.from_service_account_info", factory
    )
    return calls


def _gemini_response(
    *,
    text: str = "hi there",
    finish_reason: str = "STOP",
    prompt_tokens: int = 8,
    candidates_tokens: int = 3,
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "candidates": [
                {
                    "content": {"role": "model", "parts": [{"text": text}]},
                    "finishReason": finish_reason,
                    "index": 0,
                }
            ],
            "usageMetadata": {
                "promptTokenCount": prompt_tokens,
                "candidatesTokenCount": candidates_tokens,
                "totalTokenCount": prompt_tokens + candidates_tokens,
            },
        },
    )


@pytest.mark.asyncio
async def test_request_translation_system_instruction_and_role_mapping(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_credentials(monkeypatch, _FakeCredentials())
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        assert request.headers["authorization"] == "Bearer fake-bearer-token"
        return _gemini_response()

    request = ChatCompletionRequest(
        model="gemini-2.5-pro",
        messages=[
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "how are you"},
        ],
        temperature=0.4,
        top_p=0.9,
        max_tokens=256,
        stop="STOP",
    )

    async with _client(handler) as client:
        await create_chat_completion(
            client, "gemini-2.5-pro", request, CREDENTIAL, VertexAITokenCache()
        )

    body = captured["body"]
    assert body["systemInstruction"] == {"parts": [{"text": "Be concise."}]}
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "hi"}]},
        {"role": "model", "parts": [{"text": "hello"}]},
        {"role": "user", "parts": [{"text": "how are you"}]},
    ]
    assert body["generationConfig"] == {
        "temperature": 0.4,
        "topP": 0.9,
        "maxOutputTokens": 256,
        "stopSequences": ["STOP"],
    }
    assert "generateContent" in captured["url"]
    assert "streamGenerateContent" not in captured["url"]


@pytest.mark.asyncio
async def test_stop_list_passed_through(monkeypatch: pytest.MonkeyPatch):
    _patch_credentials(monkeypatch, _FakeCredentials())
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _gemini_response()

    request = ChatCompletionRequest(
        model="gemini-2.5-pro",
        messages=[{"role": "user", "content": "hi"}],
        stop=["A", "B"],
    )

    async with _client(handler) as client:
        await create_chat_completion(
            client, "gemini-2.5-pro", request, CREDENTIAL, VertexAITokenCache()
        )

    assert captured["body"]["generationConfig"]["stopSequences"] == ["A", "B"]


@pytest.mark.parametrize(
    "gemini_finish_reason,expected_finish_reason",
    [
        ("STOP", "stop"),
        ("MAX_TOKENS", "length"),
        ("SAFETY", "content_filter"),
        ("RECITATION", "content_filter"),
        ("OTHER", "stop"),
        ("SOME_FUTURE_REASON", "stop"),  # unmapped falls back to "stop"
    ],
)
@pytest.mark.asyncio
async def test_finish_reason_mapping_table(
    monkeypatch: pytest.MonkeyPatch, gemini_finish_reason, expected_finish_reason
):
    _patch_credentials(monkeypatch, _FakeCredentials())

    def handler(request: httpx.Request) -> httpx.Response:
        return _gemini_response(finish_reason=gemini_finish_reason)

    request = ChatCompletionRequest(model="gemini-2.5-pro", messages=[{"role": "user", "content": "hi"}])

    async with _client(handler) as client:
        response = await create_chat_completion(
            client, "gemini-2.5-pro", request, CREDENTIAL, VertexAITokenCache()
        )

    assert response.choices[0].finish_reason == expected_finish_reason


@pytest.mark.asyncio
async def test_response_translation_maps_usage_and_generates_id(monkeypatch: pytest.MonkeyPatch):
    _patch_credentials(monkeypatch, _FakeCredentials())

    def handler(request: httpx.Request) -> httpx.Response:
        return _gemini_response(text="hello world", prompt_tokens=11, candidates_tokens=5)

    request = ChatCompletionRequest(model="gemini-2.5-pro", messages=[{"role": "user", "content": "hi"}])

    async with _client(handler) as client:
        response = await create_chat_completion(
            client, "gemini-2.5-pro", request, CREDENTIAL, VertexAITokenCache()
        )

    assert response.choices[0].message.content == "hello world"
    assert response.choices[0].message.role == "assistant"
    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 5
    assert response.usage.total_tokens == 16
    assert response.id.startswith("chatcmpl-")
    assert response.model == "gemini-2.5-pro"


@pytest.mark.asyncio
async def test_n_greater_than_one_rejected_before_any_http_call(monkeypatch: pytest.MonkeyPatch):
    _patch_credentials(monkeypatch, _FakeCredentials())

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not make an HTTP call when n > 1")

    request = ChatCompletionRequest(
        model="gemini-2.5-pro", messages=[{"role": "user", "content": "hi"}], n=2
    )

    async with _client(handler) as client:
        with pytest.raises(UnsupportedRequestError):
            await create_chat_completion(
                client, "gemini-2.5-pro", request, CREDENTIAL, VertexAITokenCache()
            )


@pytest.mark.asyncio
async def test_error_status_maps_to_provider_call_error(monkeypatch: pytest.MonkeyPatch):
    _patch_credentials(monkeypatch, _FakeCredentials())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "permission denied"}})

    request = ChatCompletionRequest(model="gemini-2.5-pro", messages=[{"role": "user", "content": "hi"}])

    async with _client(handler) as client:
        with pytest.raises(ProviderCallError) as exc_info:
            await create_chat_completion(
                client, "gemini-2.5-pro", request, CREDENTIAL, VertexAITokenCache()
            )

    assert exc_info.value.status_code == 403
    assert "permission denied" not in exc_info.value.message


@pytest.mark.asyncio
async def test_streaming_incremental_relay_sequence(monkeypatch: pytest.MonkeyPatch):
    _patch_credentials(monkeypatch, _FakeCredentials())

    sse_body = (
        'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"Hel"}]},"index":0}]}\n\n'
        'data: {"candidates":[{"content":{"role":"model","parts":[{"text":"lo"}]},"index":0}]}\n\n'
        'data: {"candidates":[{"content":{"role":"model","parts":[{"text":""}]},'
        '"finishReason":"STOP","index":0}],'
        '"usageMetadata":{"promptTokenCount":4,"candidatesTokenCount":2,"totalTokenCount":6}}\n\n'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert "streamGenerateContent" in str(request.url)
        assert "alt=sse" in str(request.url)
        return httpx.Response(200, content=sse_body, headers={"content-type": "text/event-stream"})

    request = ChatCompletionRequest(
        model="gemini-2.5-pro", messages=[{"role": "user", "content": "hi"}], stream=True
    )

    chunks = []
    async with _client(handler) as client:
        async for chunk in stream_chat_completion(
            client, "gemini-2.5-pro", request, CREDENTIAL, VertexAITokenCache()
        ):
            chunks.append(chunk)

    # role-first empty chunk, then one content chunk per delta, then a
    # finish_reason chunk (empty-text final frame contributes no extra
    # content chunk since its text is ""), then a terminal usage chunk
    # (Phase 1.4 - Budget Basic).
    assert len(chunks) == 5
    usage_chunk = chunks[-1]
    assert usage_chunk.choices == []
    assert usage_chunk.usage.prompt_tokens == 4
    assert usage_chunk.usage.completion_tokens == 2
    assert usage_chunk.usage.total_tokens == 6
    assert chunks[0].choices[0].delta.role == "assistant"
    assert chunks[0].choices[0].delta.content == ""
    assert chunks[1].choices[0].delta.content == "Hel"
    assert chunks[2].choices[0].delta.content == "lo"
    assert chunks[3].choices[0].delta.content is None
    assert chunks[3].choices[0].finish_reason == "stop"
    assert len({c.id for c in chunks}) == 1


# ---------------------------------------------------------------------------
# VertexAITokenCache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_cache_reuses_token_within_freshness_window(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeCredentials(
        token="fresh-token", expiry=datetime.now(timezone.utc) + timedelta(minutes=30)
    )
    _patch_credentials(monkeypatch, fake)

    cache = VertexAITokenCache(refresh_margin_seconds=300.0)

    token1 = await cache.get_token(CREDENTIAL)
    token2 = await cache.get_token(CREDENTIAL)

    assert token1 == "fresh-token"
    assert token2 == "fresh-token"
    assert fake.refresh_calls == 1  # second get_token() served from cache, no re-refresh


@pytest.mark.asyncio
async def test_token_cache_refreshes_when_within_margin_of_expiry(monkeypatch: pytest.MonkeyPatch):
    almost_expired = _FakeCredentials(
        token="stale-token", expiry=datetime.now(timezone.utc) + timedelta(seconds=60)
    )
    fresh = _FakeCredentials(
        token="new-token", expiry=datetime.now(timezone.utc) + timedelta(minutes=60)
    )
    calls = _patch_credentials(monkeypatch, [almost_expired, fresh])

    # 5-minute margin, but cached token has only 60s left -> must refresh.
    cache = VertexAITokenCache(refresh_margin_seconds=300.0)

    token1 = await cache.get_token(CREDENTIAL)
    token2 = await cache.get_token(CREDENTIAL)

    assert token1 == "stale-token"
    assert token2 == "new-token"
    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_token_cache_keys_by_service_account_identity(monkeypatch: pytest.MonkeyPatch):
    fake = _FakeCredentials(
        token="token-a", expiry=datetime.now(timezone.utc) + timedelta(minutes=30)
    )
    _patch_credentials(monkeypatch, fake)

    other_credential = ServiceAccountCredential(
        provider="vertex_ai",
        service_account_json={
            **FAKE_SERVICE_ACCOUNT_JSON,
            "client_email": "other@fake-project.iam.gserviceaccount.com",
        },
        project_id="fake-project",
        location="us-central1",
    )

    cache = VertexAITokenCache()
    await cache.get_token(CREDENTIAL)
    await cache.get_token(other_credential)

    # Different service account identity -> separate cache entry -> two refreshes.
    assert fake.refresh_calls == 2


@pytest.mark.asyncio
async def test_token_cache_refresh_failure_raises_provider_call_error(
    monkeypatch: pytest.MonkeyPatch,
):
    class _FailingCredentials(_FakeCredentials):
        def refresh(self, request):
            raise google.auth.exceptions.RefreshError("credentials rejected")

    _patch_credentials(monkeypatch, _FailingCredentials())

    cache = VertexAITokenCache()
    with pytest.raises(ProviderCallError) as exc_info:
        await cache.get_token(CREDENTIAL)

    assert "credentials rejected" not in exc_info.value.message


def test_cached_vertex_token_repr_str_are_redacted():
    """`_CachedVertexToken` holds a live OAuth bearer token - `repr()`/
    `str()` must never leak it, mirroring `ProviderCredential` in
    `services/proxy_keys.py`."""
    from gatekey.providers.vertex_ai import _CachedVertexToken

    secret_token = "ya29.super-secret-bearer-token-value"
    entry = _CachedVertexToken(
        token=secret_token, expiry=datetime.now(timezone.utc) + timedelta(hours=1)
    )

    for rendered in (repr(entry), str(entry), f"{entry}"):
        assert rendered == "<_CachedVertexToken REDACTED>"
        assert secret_token not in rendered


# ---------------------------------------------------------------------------
# create_embeddings
# ---------------------------------------------------------------------------


def _predict_response(*, values_and_token_counts: list[tuple[list[float], int]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "predictions": [
                {
                    "embeddings": {
                        "values": values,
                        "statistics": {"token_count": token_count},
                    }
                }
                for values, token_count in values_and_token_counts
            ]
        },
    )


@pytest.mark.asyncio
async def test_embeddings_request_body_shape_single_string_input(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_credentials(monkeypatch, _FakeCredentials())
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        assert request.headers["authorization"] == "Bearer fake-bearer-token"
        return _predict_response(values_and_token_counts=[([0.1, 0.2], 3)])

    request = EmbeddingsRequest(model="gemini-embedding-001", input="hello world")

    async with _client(handler) as client:
        await create_embeddings(
            client, "gemini-embedding-001", request, CREDENTIAL, VertexAITokenCache()
        )

    assert captured["body"] == {"instances": [{"content": "hello world"}]}
    assert ":predict" in captured["url"]
    assert "generateContent" not in captured["url"]


@pytest.mark.asyncio
async def test_embeddings_request_body_shape_list_input(monkeypatch: pytest.MonkeyPatch):
    _patch_credentials(monkeypatch, _FakeCredentials())
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return _predict_response(
            values_and_token_counts=[([0.1], 2), ([0.2], 3), ([0.3], 1)]
        )

    request = EmbeddingsRequest(model="gemini-embedding-001", input=["a", "b", "c"])

    async with _client(handler) as client:
        await create_embeddings(
            client, "gemini-embedding-001", request, CREDENTIAL, VertexAITokenCache()
        )

    assert captured["body"] == {
        "instances": [{"content": "a"}, {"content": "b"}, {"content": "c"}]
    }


@pytest.mark.asyncio
async def test_embeddings_response_maps_multiple_predictions_in_order_and_sums_tokens(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_credentials(monkeypatch, _FakeCredentials())

    def handler(request: httpx.Request) -> httpx.Response:
        return _predict_response(
            values_and_token_counts=[
                ([0.1, 0.2, 0.3], 4),
                ([0.4, 0.5, 0.6], 6),
            ]
        )

    request = EmbeddingsRequest(model="gemini-embedding-001", input=["first", "second"])

    async with _client(handler) as client:
        response = await create_embeddings(
            client, "gemini-embedding-001", request, CREDENTIAL, VertexAITokenCache()
        )

    assert response.model == "gemini-embedding-001"
    assert len(response.data) == 2
    assert response.data[0].embedding == [0.1, 0.2, 0.3]
    assert response.data[0].index == 0
    assert response.data[1].embedding == [0.4, 0.5, 0.6]
    assert response.data[1].index == 1
    # Vertex has no distinct completion-token concept for embeddings -
    # prompt_tokens mirrors total_tokens, both summed across predictions.
    assert response.usage.total_tokens == 10
    assert response.usage.prompt_tokens == 10


@pytest.mark.asyncio
async def test_embeddings_error_status_maps_to_provider_call_error(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_credentials(monkeypatch, _FakeCredentials())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "permission denied"}})

    request = EmbeddingsRequest(model="gemini-embedding-001", input="hi")

    async with _client(handler) as client:
        with pytest.raises(ProviderCallError) as exc_info:
            await create_embeddings(
                client, "gemini-embedding-001", request, CREDENTIAL, VertexAITokenCache()
            )

    assert exc_info.value.status_code == 403
    assert "permission denied" not in exc_info.value.message
