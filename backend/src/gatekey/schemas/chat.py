"""OpenAI-compatible Pydantic v2 request/response models (Phase 1.2, BD-8).

Scope for 1.2 is intentionally text-only: `ChatMessage.content` is a plain
string internally, and everywhere downstream (DLP scan, token counting,
every provider adapter) still only ever sees `str`. Tool/function calls and
vision/image content remain explicitly out of scope (see the architect's
design doc, section 3) and are not modeled here.

Post-ship fix: `ChatMessage._normalize_content_parts` (below) accepts the
OpenAI "content parts" array wire shape (`[{"type": "text", "text": "..."}]`)
in addition to a bare string. Real-world OpenAI-compatible clients (Kilo
Code, Cursor, the OpenAI SDKs' own type defs) send that array shape
unconditionally for plain text, not just for vision - a strict `content:
str` schema 422s on every message from those clients, which breaks this
module's own stated "drop-in replacement for a direct provider SDK call"
goal. The parts array is flattened to a plain string at the request
boundary; a non-text part (e.g. `image_url`) is still rejected with a clear
422, since vision genuinely isn't implemented past this point - this is a
wire-format accommodation, not an expansion of what content this phase
actually understands.

Request schemas (`ChatCompletionRequest`, `CompletionRequest`,
`EmbeddingsRequest`, and the `ChatMessage` they nest) deliberately use
`model_config = ConfigDict(extra="ignore")`, NOT `"forbid"` - this is a
documented deviation from the admin schemas' `extra="forbid"` posture (see
`schemas/provider_key.py`). Real OpenAI SDKs send additional fields by
default (`user`, `stream_options`, `presence_penalty`, etc.) that this
phase's translation layer doesn't act on; rejecting requests that include
them would break the "drop-in replacement for a direct provider SDK call"
deliverable that's explicit in-scope for 1.2. Unknown fields are silently
dropped, not validated or forwarded.

Response schemas are shapes Gatekey itself constructs (or, for OpenAI,
validates directly against the passthrough upstream response - see
`providers/openai.py`), so they don't need the same "extra fields from an
uncontrolled caller" allowance; pydantic's own default (`extra="ignore"`)
is left as-is for them rather than being made explicit, since nothing here
depends on strict/forbid behavior.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator


class ChatMessage(BaseModel):
    """A single chat message. Text-only in 1.2 - no tool calls, no
    vision content. `content` accepts either a bare string or the OpenAI
    "content parts" array shape (flattened to a string) - see module
    docstring."""

    model_config = ConfigDict(extra="ignore")

    role: Literal["system", "user", "assistant"]
    content: str

    @field_validator("content", mode="before")
    @classmethod
    def _normalize_content_parts(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        texts: list[str] = []
        for part in value:
            part_type = part.get("type") if isinstance(part, dict) else None
            if part_type != "text":
                raise ValueError(
                    "Only text content parts are supported (got part type "
                    f"{part_type!r}) - vision/image content is not implemented."
                )
            text = part.get("text")
            if not isinstance(text, str):
                raise ValueError("Content part missing a string 'text' field.")
            texts.append(text)
        return "".join(texts)


class ChatCompletionStreamOptions(BaseModel):
    """OpenAI-compatible `stream_options` (Phase 1.4 - Budget Basic).

    `include_usage`: if the caller sets this `true`, the gateway relays one
    extra terminal SSE frame (empty `choices`, populated `usage`) - the same
    shape OpenAI's own real API uses. Internally, Gatekey always requests
    usage from upstream regardless of this flag (needed for billing on
    every streaming request); this field controls only whether that data is
    also *forwarded* to this particular caller - see
    `api/v1/gateway/chat.py`'s `_sse_event_stream` for where that decision
    is made.
    """

    model_config = ConfigDict(extra="ignore")

    include_usage: bool = False


class ChatCompletionRequest(BaseModel):
    """Request body for `POST /v1/chat/completions`."""

    model_config = ConfigDict(extra="ignore")

    model: str
    messages: list[ChatMessage]
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    stop: str | list[str] | None = None
    stream: bool = False
    n: int | None = None
    stream_options: ChatCompletionStreamOptions | None = None


class ChatCompletionUsage(BaseModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class ChatCompletionResponseMessage(BaseModel):
    """A completion's response message - deliberately NOT `ChatMessage`
    (added post-ship, alongside a live crash fix). `ChatCompletionResponse`
    is validated directly against the raw upstream provider response
    (`ChatCompletionResponse.model_validate(response.json())` in
    `providers/openai.py`/`openrouter.py`/`ollama.py`) - real providers,
    especially reasoning models, legitimately return `content: null` (all
    the response budget spent on hidden reasoning tokens, a tool-call-only
    turn, etc.) per OpenAI's own actual API contract. `ChatMessage.content`
    is `str` (never `None`) by design for INBOUND request messages (Phase
    1.2 is text-only, no tool calls) - reusing it here would have silently
    also loosened request validation, not just fixed the response side.
    """

    model_config = ConfigDict(extra="ignore")

    role: Literal["system", "user", "assistant"]
    content: str | None


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatCompletionResponseMessage
    finish_reason: str | None = None


class ChatCompletionResponse(BaseModel):
    """Non-streaming response body for `POST /v1/chat/completions`."""

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int
    model: str
    choices: list[ChatCompletionChoice]
    usage: ChatCompletionUsage


class ChatCompletionChunkDelta(BaseModel):
    role: Literal["assistant"] | None = None
    content: str | None = None


class ChatCompletionChunkChoice(BaseModel):
    index: int
    delta: ChatCompletionChunkDelta
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    """A single SSE `data:` frame for streaming `/v1/chat/completions`.

    The `[DONE]` sentinel that terminates an OpenAI-compatible SSE stream
    is not modeled as an instance of this class - it's a literal
    `data: [DONE]` frame emitted by the route-handler layer after the
    provider-translation generator (see `providers/*.py`) is exhausted.

    `usage` (Phase 1.4): populated only on the one terminal, empty-`choices`
    frame each provider's translation layer yields once usage reporting is
    available for the stream - see `providers/*.py` `stream_chat_completion`
    and `ChatCompletionStreamOptions`'s docstring. `None` on every ordinary
    content/role/finish_reason chunk.
    """

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    created: int
    model: str
    choices: list[ChatCompletionChunkChoice]
    usage: ChatCompletionUsage | None = None


class CompletionRequest(BaseModel):
    """Request body for the legacy `POST /v1/completions` endpoint.

    `stream: bool = False` is defined here for shape completeness only.
    Per the design doc (Q4), a request with `stream: true` against this
    legacy endpoint must be rejected with HTTP 400 at the route-handler
    layer (BD-9) - this schema does not enforce that itself.
    """

    model_config = ConfigDict(extra="ignore")

    model: str
    prompt: str
    max_tokens: int | None = None
    temperature: float | None = None
    stop: str | list[str] | None = None
    stream: bool = False


class CompletionChoice(BaseModel):
    index: int
    text: str
    finish_reason: str | None = None


class CompletionResponse(BaseModel):
    """Response body for the legacy `POST /v1/completions` endpoint."""

    id: str
    object: Literal["text_completion"] = "text_completion"
    created: int
    model: str
    choices: list[CompletionChoice]
    usage: ChatCompletionUsage


class EmbeddingsRequest(BaseModel):
    """Request body for `POST /v1/embeddings`."""

    model_config = ConfigDict(extra="ignore")

    model: str
    input: str | list[str]


class EmbeddingItem(BaseModel):
    object: Literal["embedding"] = "embedding"
    embedding: list[float]
    index: int


class EmbeddingsUsage(BaseModel):
    prompt_tokens: int
    total_tokens: int


class EmbeddingsResponse(BaseModel):
    """Response body for `POST /v1/embeddings`."""

    object: Literal["list"] = "list"
    data: list[EmbeddingItem]
    model: str
    usage: EmbeddingsUsage
