"""Google Vertex AI provider key validation (Phase 1.1) and inference calls (Phase 1.2, BD-7c).

`secret_payload` shape:
    {
        "service_account_json": dict,
        "project_id": str,
        "location": str,
    }

Validation has two steps:
  1. Build credentials from the service account JSON and refresh them to
     obtain a bearer token (blocking call from `google-auth`; run via
     `asyncio.to_thread`).
  2. Call `GET .../locations/{location}/models?pageSize=1` with that token.

Both steps share a single 8-second timeout budget via
`asyncio.wait_for(...)` around the whole validation, rather than 8s per
step, per the non-functional requirement that key validation stays
bounded and fast on the admin key-entry path.

BD-7c - inference calls / translation contract
------------------------------------------------
Calls Gemini's `generateContent` / `streamGenerateContent` REST API
(`POST .../publishers/google/models/{model}:generateContent`). Like
`providers/anthropic.py`, this is a real field-by-field translation, not a
passthrough:

Request (`ChatCompletionRequest` -> Gemini `GenerateContentRequest`):
  - Every `system`-role message's `content` is concatenated (joined with
    `"\\n"`) into `systemInstruction.parts[0].text`; system messages are
    excluded from `contents`.
  - Role mapping for the rest: `assistant` -> `"model"`, `user` -> `"user"`.
  - `temperature`/`top_p`/`max_tokens`/`stop` nest under `generationConfig`
    as `temperature`/`topP`/`maxOutputTokens`/`stopSequences` respectively
    (a bare `stop` string is wrapped in a one-element list, same rule as
    Anthropic). `generationConfig` is omitted entirely if none of these
    are set.
  - `n > 1` is rejected with `providers.base.UnsupportedRequestError`
    before any HTTP call, same as Anthropic - Gemini's `candidateCount`
    parameter (multiple candidates) is out of scope for 1.2's translation
    contract.

Response (Gemini `GenerateContentResponse` -> `ChatCompletionResponse`):
  - `candidates[0].content.parts[*].text` concatenated -> the assistant
    message content. (1.2 only ever requests/expects a single candidate,
    per the `n > 1` rejection above.)
  - `candidates[0].finishReason` -> `finish_reason` via
    `_FINISH_REASON_MAP` below.
  - `usageMetadata.promptTokenCount`/`candidatesTokenCount`/
    `totalTokenCount` -> `prompt_tokens`/`completion_tokens`/
    `total_tokens`.
  - Gemini doesn't return a response id, so one is generated
    (`chatcmpl-<uuid4 hex>`).

Streaming uses `:streamGenerateContent?alt=sse` and is a true incremental
relay (each upstream SSE frame is translated and yielded as soon as it
arrives, not buffered until the stream ends) - see design doc section 6.7:
buffer-then-return would blow the gateway's latency budget for streaming
responses. Chunk sequence mirrors `providers/anthropic.py`: one role-first
empty-content chunk before the first candidate frame, one content chunk
per non-empty `parts[*].text` delta, and a finish_reason chunk once a
candidate frame reports `finishReason`. The generator yields nothing
further after the stream ends; the `[DONE]` sentinel is the route-handler
layer's responsibility, same as the other two providers.

Embeddings translation contract
--------------------------------
`create_embeddings` calls Vertex AI's `predict` REST API (`POST
.../publishers/google/models/{model}:predict`), not Gemini's
`generateContent` API - embeddings models on Vertex use a different
endpoint/body shape entirely:

Request (`EmbeddingsRequest` -> Vertex `PredictRequest`):
  - `request.input` (`str | list[str]`) is normalized to a list of
    strings, one per instance: `{"instances": [{"content": text}, ...]}`.

Response (Vertex `PredictResponse` -> `EmbeddingsResponse`):
  - `predictions[i].embeddings.values` -> `data[i].embedding`, with
    `data[i].index` set to `i` (order-preserving, one prediction per
    input instance).
  - `predictions[i].embeddings.statistics.token_count` is summed across
    all predictions -> `usage.total_tokens`. Vertex's embeddings API has
    no separate "completion tokens" concept the way chat completions do
    (there's no generated text, only an embedding vector), so
    `usage.prompt_tokens` is set to the same summed value as
    `usage.total_tokens` rather than left at 0 - the OpenAI-compatible
    response shape implies a `prompt_tokens`/`total_tokens` pair, and
    for embeddings both should reflect all tokens processed.

OAuth token caching (`VertexAITokenCache`)
--------------------------------------------
Re-running `credentials.refresh()` (a live network round-trip to Google's
token endpoint) on every gateway request would blow the ~150ms p99 gateway
latency budget. `VertexAITokenCache` is an in-process cache of refreshed
bearer tokens, keyed by service-account identity (`project_id` +
`client_email` - both non-secret), that reuses a cached token until fewer
than 5 minutes of its validity remain, then refreshes proactively.

Lifecycle: a `VertexAITokenCache` instance must be constructed exactly
once and held for the lifetime of the process (e.g. on `app.state` at app
startup - BD-9's job to wire this up), then passed into every
`create_chat_completion`/`stream_chat_completion` call. Do not construct a
new `VertexAITokenCache` per request - that defeats the entire point of
the cache. A per-service-account `asyncio.Lock` prevents concurrent
requests for the same service account from all independently paying a
refresh round-trip when the cached token goes stale at the same moment.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import google.auth.exceptions
import google.auth.transport.requests
import httpx
from google.oauth2.service_account import Credentials

from gatekey.providers.base import (
    ProviderCallError,
    ProviderValidator,
    UnsupportedRequestError,
    ValidationResult,
    ValidationStatus,
    map_http_status,
    map_httpx_exception,
    provider_call_error_from_exception,
    provider_call_error_from_response,
)
from gatekey.schemas.chat import (
    ChatCompletionChoice,
    ChatCompletionChunk,
    ChatCompletionChunkChoice,
    ChatCompletionChunkDelta,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatCompletionUsage,
    ChatMessage,
    EmbeddingItem,
    EmbeddingsRequest,
    EmbeddingsResponse,
    EmbeddingsUsage,
)

if TYPE_CHECKING:
    # See providers/openai.py's identical block for why this is
    # TYPE_CHECKING-only (avoids a circular import through
    # services.provider_keys -> providers.registry -> providers.vertex_ai).
    from gatekey.services.proxy_keys import ServiceAccountCredential

_PROVIDER_NAME = "Vertex AI"
_DEFAULT_INFERENCE_TIMEOUT_SECONDS = 60.0
_DEFAULT_TOKEN_REFRESH_MARGIN_SECONDS = 300.0  # 5 minutes

# Gemini `finishReason` -> OpenAI-compatible `finish_reason`. Anything not
# in this table falls back to `"stop"` rather than raising, same rationale
# as Anthropic's `_STOP_REASON_MAP`.
_FINISH_REASON_MAP: dict[str, str] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "OTHER": "stop",
}


def _vertex_models_url(project_id: str, location: str) -> str:
    return (
        f"https://{location}-aiplatform.googleapis.com/v1/"
        f"projects/{project_id}/locations/{location}/models?pageSize=1"
    )


_VERTEX_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]


class VertexAIValidator(ProviderValidator):
    def __init__(self, timeout_seconds: float = 8.0) -> None:
        self._timeout_seconds = timeout_seconds

    async def validate(self, secret_payload: dict[str, Any]) -> ValidationResult:
        service_account_json = secret_payload.get("service_account_json")
        project_id = secret_payload.get("project_id")
        location = secret_payload.get("location")

        if (
            not isinstance(service_account_json, dict)
            or not service_account_json
            or not isinstance(project_id, str)
            or not project_id
            or not isinstance(location, str)
            or not location
        ):
            return ValidationResult(
                status=ValidationStatus.UNKNOWN_ERROR,
                detail=(
                    "Malformed secret payload: expected 'service_account_json' "
                    "(object), 'project_id' (string), and 'location' (string)."
                ),
            )

        try:
            return await asyncio.wait_for(
                self._do_validate(service_account_json, project_id, location),
                timeout=self._timeout_seconds,
            )
        except asyncio.TimeoutError:
            return ValidationResult(
                status=ValidationStatus.PROVIDER_UNREACHABLE,
                detail="Timed out validating Vertex AI credentials.",
            )

    async def _do_validate(
        self, service_account_json: dict[str, Any], project_id: str, location: str
    ) -> ValidationResult:
        try:
            token = await asyncio.to_thread(
                self._refresh_token, service_account_json
            )
        except (
            google.auth.exceptions.MalformedError,
            google.auth.exceptions.GoogleAuthError,
        ) as exc:
            return self._auth_error_to_result(exc)
        except Exception:
            return ValidationResult(
                status=ValidationStatus.UNKNOWN_ERROR,
                detail="Unexpected error building Vertex AI credentials.",
            )

        if token is None:
            return ValidationResult(
                status=ValidationStatus.INVALID_KEY,
                detail="Vertex AI service account credentials failed to refresh.",
            )

        url = _vertex_models_url(project_id, location)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.get(url, headers=headers)
        except Exception as exc:
            return map_httpx_exception(exc, _PROVIDER_NAME)

        return map_http_status(response, _PROVIDER_NAME)

    @staticmethod
    def _refresh_token(service_account_json: dict[str, Any]) -> str | None:
        """Blocking: build credentials and refresh to obtain a bearer token.

        Run via `asyncio.to_thread` by the caller. Must never raise/return
        anything containing the raw private key material - `google-auth`
        exceptions are caught by the caller and mapped to a generic,
        safe-to-log `ValidationResult`.
        """
        credentials = Credentials.from_service_account_info(
            service_account_json, scopes=_VERTEX_SCOPES
        )
        request = google.auth.transport.requests.Request()
        credentials.refresh(request)
        return credentials.token

    @staticmethod
    def _auth_error_to_result(exc: Exception) -> ValidationResult:
        # `google.auth.exceptions.MalformedError` = bad service account JSON
        # structure. `RefreshError` (a `GoogleAuthError` subclass) generally
        # means the key/credentials were rejected by Google's auth servers.
        # Deliberately do not include `str(exc)` in the response - google-auth
        # error messages have been known to echo request/response fragments.
        if isinstance(exc, google.auth.exceptions.MalformedError):
            return ValidationResult(
                status=ValidationStatus.UNKNOWN_ERROR,
                detail="Malformed Vertex AI service account JSON.",
            )
        if isinstance(exc, google.auth.exceptions.RefreshError):
            return ValidationResult(
                status=ValidationStatus.INVALID_KEY,
                detail="Vertex AI rejected the service account credentials.",
            )
        return ValidationResult(
            status=ValidationStatus.PROVIDER_UNREACHABLE,
            detail="Error authenticating with Vertex AI.",
        )


@dataclass(repr=False)
class _CachedVertexToken:
    """Cached OAuth bearer token + expiry.

    `token` is live secret material (a bearer token usable against Google
    APIs until `expiry`) - `__repr__`/`__str__` are overridden to a
    redacted placeholder so `repr(cache_entry)` / accidental logging can
    never leak it, mirroring `ProviderCredential`'s pattern in
    `services/proxy_keys.py`.
    """

    token: str
    expiry: datetime  # always timezone-aware (UTC)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "<_CachedVertexToken REDACTED>"

    __str__ = __repr__


class VertexAITokenCache:
    """In-process cache of Vertex AI OAuth bearer tokens (BD-7c).

    See the module docstring's "OAuth token caching" section for the full
    rationale and lifecycle contract. Short version: construct exactly one
    instance at app startup, hold onto it (e.g. `app.state.vertex_token_cache`),
    and pass the same instance into every `create_chat_completion`/
    `stream_chat_completion` call for Vertex AI - never construct a new one
    per request.

    Not process-shared: this cache lives in a single gateway process's
    memory. If the gateway runs multiple worker processes, each pays its
    own refresh round-trip on first use per worker; there is no shared
    cross-process cache in 1.2 (no external cache/store is in scope for
    this phase - see `phase-1-core-gateway.md`'s "Out of Scope" list:
    "Caching, rate limiting, failover"). That's an acceptable tradeoff for
    a pilot deployment; flagged here in case a later phase wants Redis (or
    similar) for a multi-worker shared cache.
    """

    def __init__(self, refresh_margin_seconds: float = _DEFAULT_TOKEN_REFRESH_MARGIN_SECONDS) -> None:
        self._refresh_margin = timedelta(seconds=refresh_margin_seconds)
        self._entries: dict[str, _CachedVertexToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get_token(self, credential: ServiceAccountCredential) -> str:
        """Return a valid bearer token for `credential`, refreshing if the
        cached token (if any) has fewer than `refresh_margin_seconds` of
        validity left.

        Raises `providers.base.ProviderCallError` if a refresh is required
        and fails (e.g. the service account was revoked). Never logs or
        returns anything derived from `str(exc)` on the underlying
        google-auth exception - same discipline as
        `VertexAIValidator._auth_error_to_result`.
        """
        cache_key = self._cache_key(credential)
        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            cached = self._entries.get(cache_key)
            now = datetime.now(timezone.utc)
            if cached is not None and cached.expiry - now > self._refresh_margin:
                return cached.token

            token, expiry = await asyncio.to_thread(
                self._refresh_sync, credential.service_account_json
            )
            self._entries[cache_key] = _CachedVertexToken(token=token, expiry=expiry)
            return token

    @staticmethod
    def _cache_key(credential: ServiceAccountCredential) -> str:
        # `client_email` uniquely identifies a service account and is not
        # secret material (it's a public field of the service account JSON
        # and appears in IAM policies) - safe to use as a cache key.
        email = credential.service_account_json.get("client_email", "")
        return f"{credential.project_id}:{email}"

    @staticmethod
    def _refresh_sync(service_account_json: dict[str, Any]) -> tuple[str, datetime]:
        """Blocking: build credentials and refresh to obtain a bearer token
        plus its expiry. Run via `asyncio.to_thread` by the caller.

        Mirrors `VertexAIValidator._refresh_token`'s secret-hygiene rule:
        never raise/return anything containing `str(exc)` from the
        underlying google-auth exception.
        """
        try:
            credentials = Credentials.from_service_account_info(
                service_account_json, scopes=_VERTEX_SCOPES
            )
            request = google.auth.transport.requests.Request()
            credentials.refresh(request)
        except (google.auth.exceptions.MalformedError, google.auth.exceptions.GoogleAuthError):
            raise ProviderCallError("Failed to refresh Vertex AI OAuth token.") from None
        except Exception:
            raise ProviderCallError("Unexpected error refreshing Vertex AI OAuth token.") from None

        if not credentials.token:
            raise ProviderCallError("Vertex AI OAuth token refresh returned no token.")

        expiry = credentials.expiry
        if expiry is None:
            # google-auth guarantees a token but, in principle, could omit
            # expiry; fall back to a conservative assumed lifetime rather
            # than caching a token with unknown validity indefinitely.
            expiry = datetime.now(timezone.utc) + timedelta(minutes=55)
        elif expiry.tzinfo is None:
            # `google.oauth2.service_account.Credentials.expiry` is
            # documented as a naive UTC datetime.
            expiry = expiry.replace(tzinfo=timezone.utc)

        return credentials.token, expiry


def _map_finish_reason(finish_reason: str | None) -> str | None:
    if finish_reason is None:
        return None
    return _FINISH_REASON_MAP.get(finish_reason, "stop")


def _generate_content_url(
    project_id: str, location: str, native_model_id: str, *, stream: bool
) -> str:
    method = "streamGenerateContent" if stream else "generateContent"
    suffix = "?alt=sse" if stream else ""
    return (
        f"https://{location}-aiplatform.googleapis.com/v1/"
        f"projects/{project_id}/locations/{location}/publishers/google/"
        f"models/{native_model_id}:{method}{suffix}"
    )


def _predict_url(project_id: str, location: str, native_model_id: str) -> str:
    return (
        f"https://{location}-aiplatform.googleapis.com/v1/"
        f"projects/{project_id}/locations/{location}/publishers/google/"
        f"models/{native_model_id}:predict"
    )


def _translate_chat_request(request: ChatCompletionRequest) -> dict[str, Any]:
    """`ChatCompletionRequest` -> Gemini `GenerateContentRequest` body.

    Raises `providers.base.UnsupportedRequestError` for `n > 1` - see
    module docstring.
    """
    if request.n is not None and request.n > 1:
        raise UnsupportedRequestError(
            "Vertex AI (Gemini) does not support n > 1 in Phase 1.2's translation contract."
        )

    system_parts = [m.content for m in request.messages if m.role == "system"]
    contents = [
        {
            "role": "model" if m.role == "assistant" else "user",
            "parts": [{"text": m.content}],
        }
        for m in request.messages
        if m.role != "system"
    ]

    generation_config: dict[str, Any] = {}
    if request.temperature is not None:
        generation_config["temperature"] = request.temperature
    if request.top_p is not None:
        generation_config["topP"] = request.top_p
    if request.max_tokens is not None:
        generation_config["maxOutputTokens"] = request.max_tokens
    if request.stop is not None:
        generation_config["stopSequences"] = (
            [request.stop] if isinstance(request.stop, str) else list(request.stop)
        )

    body: dict[str, Any] = {"contents": contents}
    if system_parts:
        body["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}
    if generation_config:
        body["generationConfig"] = generation_config
    return body


def _translate_chat_response(native_model_id: str, data: dict[str, Any]) -> ChatCompletionResponse:
    """Gemini `GenerateContentResponse` body -> `ChatCompletionResponse`."""
    candidates = data.get("candidates") or []
    text = ""
    finish_reason: str | None = None
    if candidates:
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        text = "".join(part.get("text", "") for part in parts)
        finish_reason = _map_finish_reason(candidate.get("finishReason"))

    usage = data.get("usageMetadata") or {}
    prompt_tokens = int(usage.get("promptTokenCount", 0) or 0)
    completion_tokens = int(usage.get("candidatesTokenCount", 0) or 0)
    total_tokens = int(usage.get("totalTokenCount", prompt_tokens + completion_tokens) or 0)

    return ChatCompletionResponse(
        id=f"chatcmpl-{uuid.uuid4().hex}",
        created=int(time.time()),
        model=native_model_id,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatMessage(role="assistant", content=text),
                finish_reason=finish_reason,
            )
        ],
        usage=ChatCompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        ),
    )


async def create_chat_completion(
    client: httpx.AsyncClient,
    native_model_id: str,
    request: ChatCompletionRequest,
    credential: ServiceAccountCredential,
    token_cache: VertexAITokenCache,
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> ChatCompletionResponse:
    """Non-streaming chat completion via Gemini's `generateContent` API.

    `token_cache` must be a single long-lived `VertexAITokenCache` instance
    shared across requests (see that class's docstring) - not constructed
    per call.

    Raises `providers.base.UnsupportedRequestError` for `n > 1`, and
    `providers.base.ProviderCallError` on an OAuth refresh failure, a
    network failure, or a non-2xx response from Vertex AI.
    """
    body = _translate_chat_request(request)
    token = await token_cache.get_token(credential)
    url = _generate_content_url(
        credential.project_id, credential.location, native_model_id, stream=False
    )
    headers = {"Authorization": f"Bearer {token}", "content-type": "application/json"}
    try:
        response = await client.post(url, json=body, headers=headers, timeout=timeout_seconds)
    except Exception as exc:
        raise provider_call_error_from_exception(exc, _PROVIDER_NAME) from None
    if response.status_code >= 400:
        raise provider_call_error_from_response(response, _PROVIDER_NAME)
    return _translate_chat_response(native_model_id, response.json())


async def stream_chat_completion(
    client: httpx.AsyncClient,
    native_model_id: str,
    request: ChatCompletionRequest,
    credential: ServiceAccountCredential,
    token_cache: VertexAITokenCache,
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> AsyncGenerator[ChatCompletionChunk, None]:
    """Streaming chat completion via Gemini's `streamGenerateContent` API.

    True incremental relay - each upstream SSE frame is translated and
    yielded as it arrives (see module docstring, design doc section 6.7).
    `token_cache` has the same "single shared long-lived instance" contract
    as `create_chat_completion`.

    Raises `providers.base.UnsupportedRequestError` for `n > 1` (raised on
    first `__anext__`, since this is a generator function), and
    `providers.base.ProviderCallError` on an OAuth refresh failure, a
    network failure, or a non-2xx response, including mid-stream.
    """
    body = _translate_chat_request(request)
    token = await token_cache.get_token(credential)
    url = _generate_content_url(
        credential.project_id, credential.location, native_model_id, stream=True
    )
    headers = {"Authorization": f"Bearer {token}", "content-type": "application/json"}

    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    role_sent = False
    # Phase 1.4 (Budget - Basic): Gemini attaches `usageMetadata`
    # cumulatively to (per Google's documented behavior) every streamed
    # candidate frame - tracked here and, once the stream ends, relayed as
    # one extra terminal chunk (empty `choices`, populated `usage`),
    # mirroring OpenAI's/Anthropic's terminal-usage-chunk shape. This is the
    # least formally guaranteed of the three providers' streaming-usage
    # contracts - flagged for verification against a real/recorded Vertex
    # streaming response before relying on it in production.
    last_usage: ChatCompletionUsage | None = None

    try:
        async with client.stream(
            "POST", url, json=body, headers=headers, timeout=timeout_seconds
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise provider_call_error_from_response(response, _PROVIDER_NAME)

            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if not payload:
                    continue
                data = json.loads(payload)

                if not role_sent:
                    role_sent = True
                    yield ChatCompletionChunk(
                        id=chunk_id,
                        created=created,
                        model=native_model_id,
                        choices=[
                            ChatCompletionChunkChoice(
                                index=0,
                                delta=ChatCompletionChunkDelta(role="assistant", content=""),
                                finish_reason=None,
                            )
                        ],
                    )

                usage_metadata = data.get("usageMetadata")
                if usage_metadata:
                    prompt_tokens = int(usage_metadata.get("promptTokenCount", 0) or 0)
                    completion_tokens = int(usage_metadata.get("candidatesTokenCount", 0) or 0)
                    total_tokens = int(
                        usage_metadata.get("totalTokenCount", prompt_tokens + completion_tokens) or 0
                    )
                    last_usage = ChatCompletionUsage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                    )

                candidates = data.get("candidates") or []
                if not candidates:
                    continue
                candidate = candidates[0]
                parts = (candidate.get("content") or {}).get("parts") or []
                text = "".join(part.get("text", "") for part in parts)
                if text:
                    yield ChatCompletionChunk(
                        id=chunk_id,
                        created=created,
                        model=native_model_id,
                        choices=[
                            ChatCompletionChunkChoice(
                                index=0,
                                delta=ChatCompletionChunkDelta(content=text),
                                finish_reason=None,
                            )
                        ],
                    )

                finish_reason = candidate.get("finishReason")
                if finish_reason is not None:
                    yield ChatCompletionChunk(
                        id=chunk_id,
                        created=created,
                        model=native_model_id,
                        choices=[
                            ChatCompletionChunkChoice(
                                index=0,
                                delta=ChatCompletionChunkDelta(),
                                finish_reason=_map_finish_reason(finish_reason),
                            )
                        ],
                    )

            if last_usage is not None:
                yield ChatCompletionChunk(
                    id=chunk_id,
                    created=created,
                    model=native_model_id,
                    choices=[],
                    usage=last_usage,
                )
    except httpx.HTTPError as exc:
        raise provider_call_error_from_exception(exc, _PROVIDER_NAME) from None


async def create_embeddings(
    client: httpx.AsyncClient,
    native_model_id: str,
    request: EmbeddingsRequest,
    credential: ServiceAccountCredential,
    token_cache: VertexAITokenCache,
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> EmbeddingsResponse:
    """Embeddings via Vertex AI's `predict` API - see module docstring's
    "Embeddings translation contract" section.

    `token_cache` has the same "single shared long-lived instance" contract
    as `create_chat_completion`.

    Raises `providers.base.ProviderCallError` on an OAuth refresh failure,
    a network failure, or a non-2xx response from Vertex AI.
    """
    inputs = [request.input] if isinstance(request.input, str) else list(request.input)
    body = {"instances": [{"content": text} for text in inputs]}

    token = await token_cache.get_token(credential)
    url = _predict_url(credential.project_id, credential.location, native_model_id)
    headers = {"Authorization": f"Bearer {token}", "content-type": "application/json"}
    try:
        response = await client.post(url, json=body, headers=headers, timeout=timeout_seconds)
    except Exception as exc:
        raise provider_call_error_from_exception(exc, _PROVIDER_NAME) from None
    if response.status_code >= 400:
        raise provider_call_error_from_response(response, _PROVIDER_NAME)

    data = response.json()
    predictions = data.get("predictions") or []

    embeddings_data: list[EmbeddingItem] = []
    total_tokens = 0
    for index, prediction in enumerate(predictions):
        embeddings = prediction.get("embeddings") or {}
        values = embeddings.get("values") or []
        embeddings_data.append(EmbeddingItem(embedding=values, index=index))
        statistics = embeddings.get("statistics") or {}
        total_tokens += int(statistics.get("token_count", 0) or 0)

    return EmbeddingsResponse(
        data=embeddings_data,
        model=native_model_id,
        # Vertex has no distinct "completion tokens" concept for
        # embeddings - see module docstring's "Embeddings translation
        # contract" section for why prompt_tokens == total_tokens here.
        usage=EmbeddingsUsage(prompt_tokens=total_tokens, total_tokens=total_tokens),
    )
