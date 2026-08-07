"""Ollama provider key validation and inference calls - self-hosted,
admin-configured base_url (Phase 1 addition, US-A1/US-A2).

`secret_payload` shape: `{"base_url": str, "bearer_token": str | None}`.

Structural deviation from every other provider module (openai/anthropic/
vertex_ai): there is deliberately no module-level `*_CHAT_COMPLETIONS_URL`
constant (AC-A1-2). The URL is built at call time from
`credential.base_url`, since Ollama's endpoint is admin-configured and
varies per deployment, unlike every other provider's fixed endpoint. Do
not "fix" this back to a module constant during review.

Chat only - no `create_completion()`/`create_embeddings()` this pass
(AC-A1-4), matching MODEL_REGISTRY having zero non-chat Ollama entries.
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
    from gatekey.services.proxy_keys import OllamaCredential

_PROVIDER_NAME = "Ollama"
_DEFAULT_INFERENCE_TIMEOUT_SECONDS = 60.0

# Fixed placeholder Bearer token sent when the admin has configured no
# bearer_token (AC-A1-3/AC-A2-1 - orchestrator-confirmed literal, settled
# item #3). Ollama's OpenAI-compat layer requires *a* Authorization header
# be present but never validates its value; "ollama" matches Ollama's own
# community convention (commonly documented as OPENAI_API_KEY=ollama in
# their own examples). Single named constant - referenced by both
# OllamaValidator and the inference functions below, never re-typed.
_OLLAMA_PLACEHOLDER_BEARER_TOKEN = "ollama"


def _resolve_bearer_token(bearer_token: str) -> str:
    return bearer_token or _OLLAMA_PLACEHOLDER_BEARER_TOKEN


class OllamaValidator(ProviderValidator):
    def __init__(self, timeout_seconds: float = 8.0) -> None:
        self._timeout_seconds = timeout_seconds

    async def validate(self, secret_payload: dict[str, Any]) -> ValidationResult:
        base_url = secret_payload.get("base_url")
        if not base_url or not isinstance(base_url, str):
            return ValidationResult(              # AC-A2-3
                status=ValidationStatus.UNKNOWN_ERROR,
                detail="Malformed secret payload: expected non-empty 'base_url' string.",
            )
        bearer_token = secret_payload.get("bearer_token") or ""
        headers = {"Authorization": f"Bearer {_resolve_bearer_token(bearer_token)}"}
        url = f"{base_url.rstrip('/')}/v1/models"
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.get(url, headers=headers)
        except Exception as exc:
            return map_httpx_exception(exc, _PROVIDER_NAME)   # AC-A2-2: unreachable -> PROVIDER_UNREACHABLE
        return map_http_status(response, _PROVIDER_NAME)


def _auth_headers(credential: "OllamaCredential") -> dict[str, str]:
    return {"Authorization": f"Bearer {_resolve_bearer_token(credential.bearer_token)}"}


def _chat_completions_url(credential: "OllamaCredential") -> str:
    # AC-A1-2 - built at call time, never a module constant.
    return f"{credential.base_url.rstrip('/')}/v1/chat/completions"


def _chat_request_body(native_model_id: str, request: ChatCompletionRequest) -> dict[str, Any]:
    # Copied verbatim from providers.openai._chat_request_body (not
    # imported) - keeps this module independently readable/editable, same
    # convention as anthropic.py/vertex_ai.py not sharing this helper either.
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
    credential: "OllamaCredential",
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> ChatCompletionResponse:
    """Non-streaming passthrough against `{base_url}/v1/chat/completions`.

    Mirrors `providers.openai.create_chat_completion` exactly except URL
    construction (AC-A1-2) and credential type. Raises
    `providers.base.ProviderCallError` on network failure or non-2xx
    (AC-A1-5).
    """
    body = _chat_request_body(native_model_id, request)
    body["stream"] = False
    try:
        response = await client.post(
            _chat_completions_url(credential),
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
    credential: "OllamaCredential",
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> AsyncGenerator[ChatCompletionChunk, None]:
    """Streaming counterpart - mirrors `providers.openai.stream_chat_completion`'s
    SSE `data: {...}` parsing loop exactly (AC-A1-6).

    FLAG (orchestrator item #4, settled as "flag, don't block"): whether
    Ollama's OpenAI-compat streaming layer actually honors
    `stream_options.include_usage=true` the way real OpenAI does (i.e.
    emits a terminal usage-bearing chunk) is UNVERIFIED against a live
    Ollama instance as of this writing. If it does not, streaming Ollama
    requests silently lose token-count usage logging (cost accounting is
    unaffected either way - Ollama pricing is $0, see providers/pricing.py).
    backend-developer: confirm against a real local Ollama instance before
    treating this as done; do not remove this comment without that
    confirmation.
    """
    body = _chat_request_body(native_model_id, request)
    body["stream"] = True
    body["stream_options"] = {"include_usage": True}
    try:
        async with client.stream(
            "POST",
            _chat_completions_url(credential),
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
