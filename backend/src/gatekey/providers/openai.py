"""OpenAI provider key validation (Phase 1.1) and inference calls (Phase 1.2, BD-7a).

`secret_payload` shape: `{"api_key": str}`.

Validates by calling `GET https://api.openai.com/v1/models` with the
submitted key as a Bearer token. Single attempt, bounded timeout, no
retries - this runs synchronously on the admin "save provider key" path.

BD-7a - inference calls
------------------------
OpenAI's native request/response shape *is* the gateway's OpenAI-compatible
shape (see `schemas/chat.py`), so translation here is a passthrough: build
the outbound JSON body directly from the request schema's fields, and parse
the provider's JSON response directly into the corresponding response
schema via `model_validate` - no field renaming/remapping is needed (unlike
`providers/anthropic.py` / `providers/vertex_ai.py`).

Every inference method takes an `httpx.AsyncClient` as its first argument
rather than constructing one itself - callers (ultimately app startup, a
later task) are expected to build one pooled client once and pass it into
every call, per the design doc's connection-reuse requirement. These
methods never construct their own client.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import httpx

from gatekey.providers.base import (
    ProviderValidator,
    ValidationResult,
    ValidationStatus,
    map_http_status,
    map_httpx_exception,
    provider_call_error_from_exception,
    provider_call_error_from_response,
)
from gatekey.schemas.chat import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    CompletionRequest,
    CompletionResponse,
    EmbeddingsRequest,
    EmbeddingsResponse,
)

if TYPE_CHECKING:
    # Imported only for type hints, not at runtime: `services.proxy_keys`
    # transitively imports `providers.registry`, which imports every
    # concrete provider module (including this one) to build its
    # provider -> validator map. A runtime import here would be a circular
    # import (`providers.openai` -> `services.proxy_keys` ->
    # `services.provider_keys` -> `providers.registry` ->
    # `providers.openai`). `from __future__ import annotations` (top of
    # this file) makes all annotations lazy strings, so this is safe -
    # `ApiKeyCredential` is never evaluated at import time, only by a type
    # checker.
    from gatekey.services.proxy_keys import ApiKeyCredential

OPENAI_MODELS_URL = "https://api.openai.com/v1/models"
OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
OPENAI_COMPLETIONS_URL = "https://api.openai.com/v1/completions"
OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
_PROVIDER_NAME = "OpenAI"
_DEFAULT_INFERENCE_TIMEOUT_SECONDS = 60.0


class OpenAIValidator(ProviderValidator):
    def __init__(self, timeout_seconds: float = 8.0) -> None:
        self._timeout_seconds = timeout_seconds

    async def validate(self, secret_payload: dict[str, Any]) -> ValidationResult:
        api_key = secret_payload.get("api_key")
        if not api_key or not isinstance(api_key, str):
            return ValidationResult(
                status=ValidationStatus.UNKNOWN_ERROR,
                detail="Malformed secret payload: expected non-empty 'api_key' string.",
            )

        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.get(OPENAI_MODELS_URL, headers=headers)
        except Exception as exc:
            return map_httpx_exception(exc, _PROVIDER_NAME)

        return map_http_status(response, _PROVIDER_NAME)


def _auth_headers(credential: ApiKeyCredential) -> dict[str, str]:
    return {"Authorization": f"Bearer {credential.api_key}"}


def _chat_request_body(native_model_id: str, request: ChatCompletionRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": native_model_id,
        "messages": [m.model_dump() for m in request.messages],
        "stream": request.stream,
    }
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.top_p is not None:
        body["top_p"] = request.top_p
    if request.max_tokens is not None:
        body["max_tokens"] = request.max_tokens
    if request.stop is not None:
        body["stop"] = request.stop
    if request.n is not None:
        body["n"] = request.n
    return body


def _completion_request_body(native_model_id: str, request: CompletionRequest) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": native_model_id,
        "prompt": request.prompt,
        "stream": request.stream,
    }
    if request.max_tokens is not None:
        body["max_tokens"] = request.max_tokens
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.stop is not None:
        body["stop"] = request.stop
    return body


async def create_chat_completion(
    client: httpx.AsyncClient,
    native_model_id: str,
    request: ChatCompletionRequest,
    credential: ApiKeyCredential,
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> ChatCompletionResponse:
    """Non-streaming `POST /v1/chat/completions` passthrough.

    Raises `providers.base.ProviderCallError` on a network failure or a
    non-2xx response from OpenAI.
    """
    body = _chat_request_body(native_model_id, request)
    body["stream"] = False
    try:
        response = await client.post(
            OPENAI_CHAT_COMPLETIONS_URL,
            json=body,
            headers=_auth_headers(credential),
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise provider_call_error_from_exception(exc, _PROVIDER_NAME) from None
    if response.status_code >= 400:
        raise provider_call_error_from_response(response, _PROVIDER_NAME)
    return ChatCompletionResponse.model_validate(response.json())


async def stream_chat_completion(
    client: httpx.AsyncClient,
    native_model_id: str,
    request: ChatCompletionRequest,
    credential: ApiKeyCredential,
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> AsyncGenerator[ChatCompletionChunk, None]:
    """Streaming `POST /v1/chat/completions` passthrough.

    Relays OpenAI's SSE `data: {...}` frames as `ChatCompletionChunk`
    objects. Stops cleanly (yielding nothing further) on OpenAI's
    `data: [DONE]` sentinel - the caller (route-handler layer) is
    responsible for emitting its own `[DONE]` frame to the client once
    this generator is exhausted; this function never yields a `[DONE]`
    marker itself.

    Raises `providers.base.ProviderCallError` on a network failure or a
    non-2xx response from OpenAI (raised even if it happens mid-stream,
    i.e. from inside the generator on a subsequent `__anext__`).
    """
    body = _chat_request_body(native_model_id, request)
    body["stream"] = True
    # Phase 1.4 (Budget - Basic): always request usage from upstream
    # internally, regardless of what the gateway caller asked for - see
    # `schemas.chat.ChatCompletionStreamOptions`'s docstring for why this is
    # purely internal billing plumbing, and `api/v1/gateway/chat.py` for
    # where the decision to forward-or-not to the caller is made.
    body["stream_options"] = {"include_usage": True}
    try:
        async with client.stream(
            "POST",
            OPENAI_CHAT_COMPLETIONS_URL,
            json=body,
            headers=_auth_headers(credential),
            timeout=timeout_seconds,
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise provider_call_error_from_response(response, _PROVIDER_NAME)
            async for line in response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if not payload or payload == "[DONE]":
                    continue
                yield ChatCompletionChunk.model_validate(json.loads(payload))
    except httpx.HTTPError as exc:
        raise provider_call_error_from_exception(exc, _PROVIDER_NAME) from None


async def create_completion(
    client: httpx.AsyncClient,
    native_model_id: str,
    request: CompletionRequest,
    credential: ApiKeyCredential,
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> CompletionResponse:
    """Legacy, non-streaming `POST /v1/completions` passthrough.

    The route-handler layer (BD-9) must reject `request.stream=True`
    against this legacy endpoint with HTTP 400 before calling this
    function (design doc Q4) - this function does not itself check
    `request.stream` and always calls OpenAI with `stream: false`.
    """
    body = _completion_request_body(native_model_id, request)
    body["stream"] = False
    try:
        response = await client.post(
            OPENAI_COMPLETIONS_URL,
            json=body,
            headers=_auth_headers(credential),
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise provider_call_error_from_exception(exc, _PROVIDER_NAME) from None
    if response.status_code >= 400:
        raise provider_call_error_from_response(response, _PROVIDER_NAME)
    return CompletionResponse.model_validate(response.json())


async def create_embeddings(
    client: httpx.AsyncClient,
    native_model_id: str,
    request: EmbeddingsRequest,
    credential: ApiKeyCredential,
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> EmbeddingsResponse:
    """`POST /v1/embeddings` passthrough."""
    body: dict[str, Any] = {"model": native_model_id, "input": request.input}
    try:
        response = await client.post(
            OPENAI_EMBEDDINGS_URL,
            json=body,
            headers=_auth_headers(credential),
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise provider_call_error_from_exception(exc, _PROVIDER_NAME) from None
    if response.status_code >= 400:
        raise provider_call_error_from_response(response, _PROVIDER_NAME)
    return EmbeddingsResponse.model_validate(response.json())
