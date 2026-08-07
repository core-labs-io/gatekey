"""Streaming chat completion: direct OpenAI SDK vs. Gatekey (BD-11).

Endpoint demonstrated: POST /v1/chat/completions with stream=true (SSE)
Model: claude-sonnet-5 -> routed by Gatekey to the Anthropic
provider. Note the caller still uses the *official OpenAI SDK* end to end -
it has no idea it's actually talking to Anthropic underneath. That's the
provider-agnostic-routing point of Story 1: the same client class, same
`.chat.completions.create(..., stream=True)` call, same chunk-iteration
loop, works for OpenAI, Anthropic, and Vertex AI models alike.

Run:
    pip install openai
    export OPENAI_API_KEY=sk-...                  # only needed for the "before" call
    export GATEKEY_BASE_URL=http://localhost:8000/v1
    export GATEKEY_SERVICE_ACCOUNT_KEY=gk_sk_...
    python chat_completion_streaming.py

See ../README.md for prerequisites and the full "error shape" explanation.
"""

from __future__ import annotations

import os

from openai import OpenAI

MESSAGES = [{"role": "user", "content": "Count from 1 to 5, one number per line."}]


# ---------------------------------------------------------------------------
# BEFORE: streaming directly from OpenAI. No Gatekey involved.
# ---------------------------------------------------------------------------
def stream_direct_openai() -> None:
    client = OpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        # base_url unset -> SDK default "https://api.openai.com/v1".
    )
    print("[direct openai] ", end="", flush=True)
    stream = client.chat.completions.create(model="gpt-4o", messages=MESSAGES, stream=True)
    # Iterating the SDK's Stream object consumes the SSE frames as they
    # arrive over the wire and yields already-parsed ChatCompletionChunk
    # objects - the same mechanics used against Gatekey below.
    for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            print(delta, end="", flush=True)
    print()


# ---------------------------------------------------------------------------
# AFTER: the identical streaming call, pointed at Gatekey, routed to
# Anthropic's claude-sonnet-5 under the hood. Again, only
# `base_url` + `api_key` differ from stream_direct_openai() above.
# ---------------------------------------------------------------------------
def stream_via_gatekey() -> None:
    client = OpenAI(
        api_key=os.environ["GATEKEY_SERVICE_ACCOUNT_KEY"],  # gk_sk_... - NOT a provider key
        base_url=os.environ.get("GATEKEY_BASE_URL", "http://localhost:8000/v1"),
    )
    print("[gatekey -> anthropic] ", end="", flush=True)
    # `model` is the only request-body value that changed from the "before"
    # snippet - the streaming mechanics (stream=True, iterating the
    # returned Stream object, reading chunk.choices[0].delta.content) are
    # identical. Gatekey translates Anthropic's native SSE event shape into
    # this OpenAI-compatible chunk shape server-side
    # (see backend/src/gatekey/providers/anthropic.py:stream_chat_completion),
    # so the SDK's own OpenAI-format chunk parsing works unmodified.
    stream = client.chat.completions.create(
        model="claude-sonnet-5",
        messages=MESSAGES,
        stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content if chunk.choices else None
        if delta:
            print(delta, end="", flush=True)
    print()
    # Note: a mid-stream provider failure on Gatekey's side cannot change
    # the HTTP status (headers/the 200 are already flushed once streaming
    # starts) - the SSE stream just ends early with no [DONE] sentinel, the
    # same observable behavior as the upstream connection dropping. A
    # request-shape or connectivity error that happens *before* the first
    # chunk is still surfaced as a normal APIStatusError - see
    # chat_completion.py's error-handling comment for the shape caveat that
    # applies to that case.


if __name__ == "__main__":
    stream_direct_openai()
    stream_via_gatekey()
