"""OpenRouter provider key validation and inference calls - direct
OpenAI-shape passthrough against a fixed hosted endpoint (Phase 1
addition, US-A3).

`secret_payload` shape: `{"api_key": str}` - identical to openai.py. Two
additional, OPTIONAL, non-secret fields (`trusted_provider_slugs`/
`trusted_provider_region`) can be set alongside the key - see `schemas.
provider_key.OpenRouterKeyRequest`'s docstring and `_chat_request_body()`
below for the residency-enforcement feature they exist for.

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

Model Catalog (Part A, `services/model_catalog.py`) - `list_models()`
-----------------------------------------------------------------------
This is the "future capability" `OPENROUTER_MODELS_URL`'s own comment
above anticipated: `GET /api/v1/models`, OpenRouter's public/unauthenticated
catalog listing (the exact endpoint that made it unsafe for key
VALIDATION, above, is exactly right for catalog LISTING - a listing has no
need to prove a key is valid at all). No `Authorization` header is sent
for this specific call. `services.model_catalog.list_available_models()`
still gates this behind a "provider key configured for this org" check of
its own before ever calling this function - see that module's docstring
for why, since this module's own call needs no auth. Unlike OpenAI/
Anthropic, OpenRouter's response carries real per-model pricing
(`pricing.prompt`/`pricing.completion`, per-token USD strings) - parsed
into `input_price_per_million_usd`/`output_price_per_million_usd` here
(scaled `* 1_000_000`, rounded to 6 decimal places), with a defensive,
all-or-nothing fallback to `None`/`None` on a `Decimal` parse failure or
OpenRouter's `"-1"` variable/negotiated-pricing sentinel (a negative
value) affecting EITHER field - see `_parse_openrouter_pricing()` below.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from decimal import Decimal, InvalidOperation
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
from gatekey.schemas.custom_model import AvailableModelEntry

if TYPE_CHECKING:
    from gatekey.services.proxy_keys import ApiKeyCredential

# `list_models()`'s per-token-USD-string -> per-million-USD scaling factor -
# see module docstring "Model Catalog" section / Model Catalog technical
# design doc section 1.2.
_USD_PER_MILLION_SCALE = Decimal(1_000_000)
# Rounding precision matching `custom_models.input_price_per_million_usd`'s
# `NUMERIC(12,6)` column - see module docstring.
_PRICE_DECIMAL_PLACES = Decimal("0.000001")

# NOT used for validation - public/unauthenticated, returns 200 regardless
# of the Authorization header (see this module's docstring "Post-ship fix"
# section before using this for anything auth-related). Used by
# `list_models()` below (module docstring "Model Catalog" section) - a
# catalog LISTING has no need to prove a key is valid, so the same
# unauthenticated behavior that made this endpoint wrong for VALIDATION is
# exactly right here.
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


def _chat_request_body(
    native_model_id: str, request: ChatCompletionRequest, credential: "ApiKeyCredential"
) -> dict[str, Any]:
    # Copied verbatim from providers.openai._chat_request_body, plus the
    # `provider.only` restriction below.
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
    if credential.trusted_provider_slugs:
        # Residency enforcement (see `services.residency.resolve_model_region`'s
        # openrouter branch and `schemas.provider_key.OpenRouterKeyRequest`'s
        # docstring): OpenRouter's own `provider.only` request field
        # restricts which underlying provider(s) it's allowed to route this
        # call to. Applied UNCONDITIONALLY whenever an admin has configured
        # a trusted list - not only when a residency rule happens to be
        # active for this specific request - because `resolve_model_region()`
        # trusts `trusted_provider_region` for EVERY OpenRouter request once
        # configured; the two must never drift apart, or the region claim
        # would stop being true. If none of the trusted providers can serve
        # `native_model_id`, OpenRouter itself returns an error - surfaced
        # as a normal `ProviderCallError` below, exactly as any other
        # provider-side rejection is.
        body["provider"] = {"only": list(credential.trusted_provider_slugs)}
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
    body = _chat_request_body(native_model_id, request, credential)
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
    body = _chat_request_body(native_model_id, request, credential)
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


def _parse_one_openrouter_price(raw: Any) -> Decimal | None:
    """Parse a single OpenRouter per-token USD price string
    (`pricing.prompt` or `pricing.completion`) into a per-million-USD
    `Decimal`, rounded to 6 decimal places
    (`custom_models.input_price_per_million_usd`'s `NUMERIC(12,6)`
    precision).

    Returns `None` (never a nonsense price) on:
      - a `Decimal()` parse failure (missing/non-numeric value), or
      - a negative value - OpenRouter uses `"-1"` as a sentinel for
        variable/negotiated pricing it has no fixed per-token rate for.
    """
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError):
        return None
    if value < 0:
        return None
    return (value * _USD_PER_MILLION_SCALE).quantize(_PRICE_DECIMAL_PLACES)


def _parse_openrouter_pricing(pricing: dict[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    """Parse an OpenRouter listing's `pricing` object into
    `(input_price_per_million_usd, output_price_per_million_usd)`.

    Deliberately all-or-nothing, not independent per field: if EITHER
    `pricing.prompt` or `pricing.completion` fails to parse to a valid,
    non-negative price (`_parse_one_openrouter_price()` returns `None`),
    BOTH returned price fields are `None` for this entry - a half-priced
    entry (e.g. a real input rate paired with a blanked-out output rate)
    would be a more confusing/misleading partial signal to the frontend's
    "prefill both, or leave both blank" contract than treating the whole
    entry as unpriced. See the Model Catalog technical design doc section
    1.2.
    """
    input_price = _parse_one_openrouter_price(pricing.get("prompt"))
    output_price = _parse_one_openrouter_price(pricing.get("completion"))
    if input_price is None or output_price is None:
        return None, None
    return input_price, output_price


async def list_models(
    client: httpx.AsyncClient,
    *,
    timeout_seconds: float = _DEFAULT_INFERENCE_TIMEOUT_SECONDS,
) -> list[AvailableModelEntry]:
    """`GET /api/v1/models` - see module docstring "Model Catalog" section.

    Deliberately takes NO `credential` parameter (unlike every other
    `list_models()`/inference method in this file) - this specific
    OpenRouter endpoint is public/unauthenticated, so there is no
    `Authorization` header to build. See module docstring for why
    `services.model_catalog.list_available_models()` still requires a
    configured `provider_keys` row before ever calling this function.

    Raises `providers.base.ProviderCallError` on a network failure or a
    non-2xx response. Unlike OpenAI/Anthropic's `list_models()`, pricing
    IS filled in here from the live response (`_parse_openrouter_price()`
    above) - `services.model_catalog.list_available_models()` does not
    additionally look these entries up in its `MODEL_REGISTRY`/
    `PRICING_TABLE` reverse index.
    """
    try:
        response = await client.get(OPENROUTER_MODELS_URL, timeout=timeout_seconds)
    except Exception as exc:
        raise provider_call_error_from_exception(exc, _PROVIDER_NAME) from None
    if response.status_code >= 400:
        raise provider_call_error_from_response(response, _PROVIDER_NAME)
    data = response.json()
    entries: list[AvailableModelEntry] = []
    for entry in data.get("data", []):
        input_price, output_price = _parse_openrouter_pricing(entry.get("pricing") or {})
        entries.append(
            AvailableModelEntry(
                native_model_id=entry["id"],
                display_name=entry.get("name") or entry["id"],
                input_price_per_million_usd=input_price,
                output_price_per_million_usd=output_price,
            )
        )
    return entries
