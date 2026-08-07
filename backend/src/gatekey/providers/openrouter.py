"""OpenRouter provider key validation and inference calls - direct
OpenAI-shape passthrough against a fixed hosted endpoint (Phase 1
addition, US-A3).

`secret_payload` shape: `{"api_key": str}` - identical to openai.py.

Mirrors `providers/openai.py` exactly except: fixed OpenRouter URLs
(base URL is fixed, unlike Ollama's - AC-A3-1), and no
`create_completion()`/`create_embeddings()` this pass (AC-A3-3), matching
MODEL_REGISTRY having only ModelCapability.CHAT OpenRouter entries.

Not implemented this pass (AC-A3-2): optional attribution headers
(`HTTP-Referer`, `X-OpenRouter-Title`) - deferred, not a correctness gap.

Post-ship fix: validation endpoint (found by live smoke test, not caught by
mocked unit tests - see git history)
------------------------------------------------------------------------
The first version of this module validated a submitted key against
`GET /api/v1/models`, copying `providers/openai.py`'s pattern exactly.
Unlike OpenAI's `/v1/models`, **OpenRouter's `/api/v1/models` is a public,
unauthenticated catalog listing** - it returns `200` for any request,
including one with no `Authorization` header at all, or a completely fake
key. Using it as a validation check meant Gatekey would accept and save
*any* string as a "validated" OpenRouter key, defeating Phase 1.1's "test
call to provider to confirm the key works before saving" requirement
entirely. This was confirmed live (real HTTP calls, not mocks) against
OpenRouter's actual API. The fix: validate against `GET /api/v1/auth/key`
instead, which requires a genuinely valid key and returns `401` otherwise
(also confirmed live). Do not change this back to `/api/v1/models` - it
will silently reintroduce the bug, and the existing mocked unit tests will
not catch it, because they mock whatever URL this module calls and assert
against that same URL rather than against OpenRouter's real behavior.
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
from gatekey.schemas.chat import ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse

if TYPE_CHECKING:
    from gatekey.services.proxy_keys import ApiKeyCredential

# NOT used for validation - public/unauthenticated, returns 200 regardless
# of the Authorization header. Kept only in case a future capability
# (e.g. dynamic model discovery) legitimately needs OpenRouter's catalog.
# See this module's docstring "Post-ship fix" section before using this for
# anything auth-related.
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
# Used for validation instead - requires a genuinely valid key, returns 401
# otherwise. See this module's docstring.
OPENROUTER_AUTH_KEY_URL = "https://openrouter.ai/api/v1/auth/key"
OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"
_PROVIDER_NAME = "OpenRouter"
_DEFAULT_INFERENCE_TIMEOUT_SECONDS = 60.0


class OpenRouterValidator(ProviderValidator):
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
                response = await client.get(OPENROUTER_AUTH_KEY_URL, headers=headers)
        except Exception as exc:
            return map_httpx_exception(exc, _PROVIDER_NAME)
        return map_http_status(response, _PROVIDER_NAME)


def _auth_headers(credential: "ApiKeyCredential") -> dict[str, str]:
    return {"Authorization": f"Bearer {credential.api_key}"}


def _chat_request_body(native_model_id: str, request: ChatCompletionRequest) -> dict[str, Any]:
    # Copied verbatim from providers.openai._chat_request_body.
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


async def create_chat_completion(
    client: httpx.AsyncClient,
    native_model_id: str,
    request: ChatCompletionRequest,
    credential: "ApiKeyCredential",
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> ChatCompletionResponse:
    """Non-streaming passthrough - identical structure to
    `providers.openai.create_chat_completion`, `OPENROUTER_CHAT_COMPLETIONS_URL`
    substituted for OpenAI's fixed URL."""
    body = _chat_request_body(native_model_id, request)
    body["stream"] = False
    try:
        response = await client.post(
            OPENROUTER_CHAT_COMPLETIONS_URL,
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
    credential: "ApiKeyCredential",
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> AsyncGenerator[ChatCompletionChunk, None]:
    """Streaming passthrough - identical structure to
    `providers.openai.stream_chat_completion`, including the unconditional
    outbound `stream_options: {"include_usage": true}` for Budget's (1.4)
    accounting. Not flagged as unverified (unlike Ollama, item #4) -
    OpenRouter is a direct OpenAI-shape proxy over a hosted, stable
    endpoint, the same class of target `providers/openai.py`'s already-
    proven implementation targets."""
    body = _chat_request_body(native_model_id, request)
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    try:
        async with client.stream(
            "POST",
            OPENROUTER_CHAT_COMPLETIONS_URL,
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
