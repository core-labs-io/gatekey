"""OpenAI-compatible Pydantic v2 request/response models (Phase 1.2, BD-8).

Scope for 1.2 is intentionally text-only: `ChatMessage.content` is a plain
string. Tool/function calls, multi-part content (e.g. `[{"type": "text",
...}]`), and vision/image content are all explicitly out of scope for this
phase (see the architect's design doc, section 3) and are not modeled here.

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

from typing import Literal

from pydantic import BaseModel, ConfigDict


class ChatMessage(BaseModel):
    """A single chat message. Text-only in 1.2 - no tool calls, no
    multi-part/vision content."""

    model_config = ConfigDict(extra="ignore")

    role: Literal["system", "user", "assistant"]
    content: str


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


class ChatCompletionChoice(BaseModel):
    index: int
    message: ChatMessage
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
