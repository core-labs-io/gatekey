"""Anthropic provider key validation (Phase 1.1) and inference calls (Phase 1.2, BD-7b).

`secret_payload` shape: `{"api_key": str}`.

Validates by calling `GET https://api.anthropic.com/v1/models` with the
submitted key in the `x-api-key` header plus the required
`anthropic-version` header. Single attempt, bounded timeout, no retries.

BD-7b - inference calls / translation contract
------------------------------------------------
Anthropic's Messages API (`POST /v1/messages`) has a different shape from
the OpenAI-compatible gateway surface, so (unlike `providers/openai.py`)
this module does real field-by-field translation both ways:

Request (`ChatCompletionRequest` -> Anthropic Messages body):
  - Every `system`-role message's `content` is concatenated (joined with
    `"\\n"`) into the top-level `system` string field; system messages are
    removed from the `messages` array Anthropic receives.
  - `max_tokens`: Anthropic requires this field. If the gateway request
    omits it, default to 1024.
  - `temperature`: passed through, but clamped to Anthropic's max of
    `1.0` (`min(value, 1.0)`) - OpenAI's range is 0-2, Anthropic's is 0-1.
  - `stop` -> `stop_sequences`: a bare string is wrapped in a one-element
    list; a list is passed through as-is; `None` is omitted entirely.
  - `n > 1` is rejected with `providers.base.UnsupportedRequestError`
    before any HTTP call is made (both streaming and non-streaming) -
    Anthropic's API has no concept of multiple choices per request.

Response (Anthropic Messages response -> `ChatCompletionResponse`):
  - `content` (array of content blocks) -> concatenation of every
    `{"type": "text", ...}` block's `text`, into a single assistant
    `ChatMessage`.
  - `stop_reason` -> `finish_reason` via `_STOP_REASON_MAP` below.
  - `usage.input_tokens`/`usage.output_tokens` -> `prompt_tokens`/
    `completion_tokens`; `total_tokens` is their sum (Anthropic doesn't
    return a total itself).

Streaming: translates Anthropic's SSE event sequence
(`message_start` -> `content_block_delta`* -> `message_delta` ->
`message_stop`) into `ChatCompletionChunk`s: one role-first empty-content
chunk on `message_start`, one content chunk per `text_delta`, and a final
finish_reason-only chunk on `message_delta` (once its `stop_reason` is
present). The generator returns (stops cleanly, yielding nothing further)
on `message_stop` - it never yields a `[DONE]` sentinel; that's the
route-handler layer's job, same as `providers/openai.py`.

Model Catalog (Part A, `services/model_catalog.py`) - `list_models()`
-----------------------------------------------------------------------
`GET /v1/models?limit=1000` (the documented max, single call, no
`after_id`/`before_id` cursor-following - see the Model Catalog technical
design doc section 1.5 for the deliberate, justified bound; Anthropic's
real model count is nowhere near 1000). Maps `{data: [{id, display_name,
...}]}` with no pricing filled in - `services/model_catalog.py` fills that
in afterward via its `MODEL_REGISTRY`/`PRICING_TABLE` reverse index, not
this module.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

import httpx

from gatekey.providers.base import (
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
    ChatCompletionResponseMessage,
    ChatCompletionUsage,
)
from gatekey.schemas.custom_model import AvailableModelEntry

if TYPE_CHECKING:
    # See providers/openai.py's identical block for why this is
    # TYPE_CHECKING-only (avoids a circular import through
    # services.provider_keys -> providers.registry -> providers.anthropic).
    from gatekey.services.proxy_keys import ApiKeyCredential

ANTHROPIC_MODELS_URL = "https://api.anthropic.com/v1/models"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
_PROVIDER_NAME = "Anthropic"
_DEFAULT_INFERENCE_TIMEOUT_SECONDS = 60.0
_ANTHROPIC_MAX_TEMPERATURE = 1.0
_ANTHROPIC_DEFAULT_MAX_TOKENS = 1024

# Anthropic `stop_reason` -> OpenAI-compatible `finish_reason`. Any
# stop_reason not in this table (should not happen against a documented
# Anthropic API version) falls back to `"stop"` rather than raising, so an
# unrecognized-but-benign new stop_reason value never turns into a hard
# failure for the caller.
_STOP_REASON_MAP: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}

# NOTE: `2023-06-01` has been Anthropic's stable, generally-applicable API
# version string for a long time as of this writing (this validation call
# doesn't depend on any version-gated feature). Confirm against
# https://docs.anthropic.com/en/api/versioning before shipping, in case a
# newer version has since become the recommended default.
ANTHROPIC_API_VERSION = "2023-06-01"

# `list_models()`'s single-call page size - the documented max, deliberately
# not followed by cursor-pagination (`after_id`/`before_id`) - see this
# module's docstring "Model Catalog" section / Model Catalog technical
# design doc section 1.5.
_ANTHROPIC_MODELS_LIST_LIMIT = 1000


class AnthropicValidator(ProviderValidator):
    def __init__(self, timeout_seconds: float = 8.0) -> None:
        self._timeout_seconds = timeout_seconds

    async def validate(self, secret_payload: dict[str, Any]) -> ValidationResult:
        api_key = secret_payload.get("api_key")
        if not api_key or not isinstance(api_key, str):
            return ValidationResult(
                status=ValidationStatus.UNKNOWN_ERROR,
                detail="Malformed secret payload: expected non-empty 'api_key' string.",
            )

        headers = {
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.get(ANTHROPIC_MODELS_URL, headers=headers)
        except Exception as exc:
            return map_httpx_exception(exc, _PROVIDER_NAME)

        return map_http_status(response, _PROVIDER_NAME)


def _map_stop_reason(stop_reason: str | None) -> str | None:
    if stop_reason is None:
        return None
    return _STOP_REASON_MAP.get(stop_reason, "stop")


def _auth_headers(credential: ApiKeyCredential) -> dict[str, str]:
    return {
        "x-api-key": credential.api_key,
        "anthropic-version": ANTHROPIC_API_VERSION,
        "content-type": "application/json",
    }


def _translate_chat_request(native_model_id: str, request: ChatCompletionRequest) -> dict[str, Any]:
    """`ChatCompletionRequest` -> Anthropic Messages API request body.

    Raises `providers.base.UnsupportedRequestError` for `n > 1` - see
    module docstring.
    """
    if request.n is not None and request.n > 1:
        raise UnsupportedRequestError(
            "Anthropic does not support n > 1 in Phase 1.2's translation contract."
        )

    system_parts = [m.content for m in request.messages if m.role == "system"]
    other_messages = [
        {"role": m.role, "content": m.content} for m in request.messages if m.role != "system"
    ]

    body: dict[str, Any] = {
        "model": native_model_id,
        "messages": other_messages,
        "max_tokens": (
            request.max_tokens if request.max_tokens is not None else _ANTHROPIC_DEFAULT_MAX_TOKENS
        ),
    }
    if system_parts:
        body["system"] = "\n".join(system_parts)
    if request.temperature is not None:
        body["temperature"] = min(request.temperature, _ANTHROPIC_MAX_TEMPERATURE)
    if request.top_p is not None:
        body["top_p"] = request.top_p
    if request.stop is not None:
        body["stop_sequences"] = (
            [request.stop] if isinstance(request.stop, str) else list(request.stop)
        )
    return body


def _translate_chat_response(native_model_id: str, data: dict[str, Any]) -> ChatCompletionResponse:
    """Anthropic Messages API response body -> `ChatCompletionResponse`."""
    content_blocks = data.get("content") or []
    text = "".join(
        block.get("text", "") for block in content_blocks if block.get("type") == "text"
    )
    usage = data.get("usage") or {}
    prompt_tokens = int(usage.get("input_tokens", 0) or 0)
    completion_tokens = int(usage.get("output_tokens", 0) or 0)

    return ChatCompletionResponse(
        id=data.get("id") or f"chatcmpl-{uuid.uuid4().hex}",
        created=int(time.time()),
        model=data.get("model") or native_model_id,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatCompletionResponseMessage(role="assistant", content=text),
                finish_reason=_map_stop_reason(data.get("stop_reason")),
            )
        ],
        usage=ChatCompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )


async def create_chat_completion(
    client: httpx.AsyncClient,
    native_model_id: str,
    request: ChatCompletionRequest,
    credential: ApiKeyCredential,
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> ChatCompletionResponse:
    """Non-streaming chat completion via Anthropic's Messages API.

    Raises `providers.base.UnsupportedRequestError` for `n > 1`, and
    `providers.base.ProviderCallError` on a network failure or a non-2xx
    response from Anthropic.
    """
    body = _translate_chat_request(native_model_id, request)
    body["stream"] = False
    try:
        response = await client.post(
            ANTHROPIC_MESSAGES_URL,
            json=body,
            headers=_auth_headers(credential),
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise provider_call_error_from_exception(exc, _PROVIDER_NAME) from None
    if response.status_code >= 400:
        raise provider_call_error_from_response(response, _PROVIDER_NAME)
    return _translate_chat_response(native_model_id, response.json())


async def stream_chat_completion(
    client: httpx.AsyncClient,
    native_model_id: str,
    request: ChatCompletionRequest,
    credential: ApiKeyCredential,
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> AsyncGenerator[ChatCompletionChunk, None]:
    """Streaming chat completion via Anthropic's Messages API.

    Translates Anthropic's SSE event sequence into `ChatCompletionChunk`s -
    see module docstring for the exact sequence. Raises
    `providers.base.UnsupportedRequestError` for `n > 1` (raised eagerly,
    before any HTTP call, even though this is a generator function - the
    check runs on the first `__anext__`), and
    `providers.base.ProviderCallError` on a network failure or a non-2xx
    response, including mid-stream.
    """
    body = _translate_chat_request(native_model_id, request)
    body["stream"] = True

    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    role_sent = False
    # Phase 1.4 (Budget - Basic): Anthropic always reports usage in-band
    # (no request-side opt-in needed, unlike OpenAI) - captured here and
    # yielded as one extra terminal chunk (empty `choices`, populated
    # `usage`) once both halves are known, mirroring the shape OpenAI's own
    # `stream_options.include_usage` frame uses.
    captured_input_tokens: int | None = None

    try:
        async with client.stream(
            "POST",
            ANTHROPIC_MESSAGES_URL,
            json=body,
            headers=_auth_headers(credential),
            timeout=timeout_seconds,
        ) as response:
            if response.status_code >= 400:
                await response.aread()
                raise provider_call_error_from_response(response, _PROVIDER_NAME)

            event_type: str | None = None
            async for line in response.aiter_lines():
                if not line:
                    continue
                if line.startswith("event:"):
                    event_type = line[len("event:") :].strip()
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if not payload:
                    continue
                data = json.loads(payload)

                if event_type == "message_start":
                    message_usage = (data.get("message") or {}).get("usage") or {}
                    if "input_tokens" in message_usage:
                        captured_input_tokens = int(message_usage["input_tokens"] or 0)
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
                elif event_type == "content_block_delta":
                    delta = data.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
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
                elif event_type == "message_delta":
                    delta = data.get("delta") or {}
                    stop_reason = delta.get("stop_reason")
                    if stop_reason is not None:
                        yield ChatCompletionChunk(
                            id=chunk_id,
                            created=created,
                            model=native_model_id,
                            choices=[
                                ChatCompletionChunkChoice(
                                    index=0,
                                    delta=ChatCompletionChunkDelta(),
                                    finish_reason=_map_stop_reason(stop_reason),
                                )
                            ],
                        )
                    output_usage = data.get("usage") or {}
                    if captured_input_tokens is not None and "output_tokens" in output_usage:
                        output_tokens = int(output_usage["output_tokens"] or 0)
                        yield ChatCompletionChunk(
                            id=chunk_id,
                            created=created,
                            model=native_model_id,
                            choices=[],
                            usage=ChatCompletionUsage(
                                prompt_tokens=captured_input_tokens,
                                completion_tokens=output_tokens,
                                total_tokens=captured_input_tokens + output_tokens,
                            ),
                        )
                elif event_type == "message_stop":
                    return
    except httpx.HTTPError as exc:
        raise provider_call_error_from_exception(exc, _PROVIDER_NAME) from None


async def list_models(
    client: httpx.AsyncClient,
    credential: ApiKeyCredential,
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> list[AvailableModelEntry]:
    """`GET /v1/models?limit=1000` - see module docstring "Model Catalog"
    section for why this is a single call, not cursor-following.

    Raises `providers.base.ProviderCallError` on a network failure or a
    non-2xx response. No pricing is filled in here (both price fields
    always `None`) - `services.model_catalog.list_available_models()` fills
    them in afterward via its own `MODEL_REGISTRY`/`PRICING_TABLE` reverse
    index.
    """
    try:
        response = await client.get(
            ANTHROPIC_MODELS_URL,
            params={"limit": _ANTHROPIC_MODELS_LIST_LIMIT},
            headers=_auth_headers(credential),
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise provider_call_error_from_exception(exc, _PROVIDER_NAME) from None
    if response.status_code >= 400:
        raise provider_call_error_from_response(response, _PROVIDER_NAME)
    data = response.json()
    return [
        AvailableModelEntry(
            native_model_id=entry["id"],
            display_name=entry.get("display_name") or entry["id"],
            input_price_per_million_usd=None,
            output_price_per_million_usd=None,
        )
        for entry in data.get("data", [])
    ]
