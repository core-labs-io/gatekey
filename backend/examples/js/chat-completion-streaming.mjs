// Streaming chat completion: direct OpenAI SDK vs. Gatekey (BD-11).
//
// Endpoint demonstrated: POST /v1/chat/completions with stream: true (SSE)
// Model: claude-sonnet-5 -> routed by Gatekey to the Anthropic
// provider. Note the caller still uses the *official OpenAI SDK* end to
// end - it has no idea it's actually talking to Anthropic underneath.
// That's the provider-agnostic-routing point of Story 1: the same client
// class, same `.chat.completions.create({..., stream: true})` call, same
// chunk-iteration loop, works for OpenAI, Anthropic, and Vertex AI models
// alike.
//
// Run:
//   npm install openai
//   export OPENAI_API_KEY=sk-...                  # only needed for the "before" call
//   export GATEKEY_BASE_URL=http://localhost:8000/v1
//   export GATEKEY_SERVICE_ACCOUNT_KEY=gk_sk_...
//   node chat-completion-streaming.mjs
//
// See ../README.md for prerequisites and the full "error shape" explanation.

import OpenAI from "openai";

const MESSAGES = [{ role: "user", content: "Count from 1 to 5, one number per line." }];

// ---------------------------------------------------------------------------
// BEFORE: streaming directly from OpenAI. No Gatekey involved.
// ---------------------------------------------------------------------------
async function streamDirectOpenAI() {
  const client = new OpenAI({
    apiKey: process.env.OPENAI_API_KEY,
    // baseURL unset -> SDK default "https://api.openai.com/v1".
  });
  process.stdout.write("[direct openai] ");
  const stream = await client.chat.completions.create({
    model: "gpt-4o",
    messages: MESSAGES,
    stream: true,
  });
  // `for await...of` over the SDK's Stream consumes the SSE frames as they
  // arrive over the wire and yields already-parsed chunk objects - the same
  // mechanics used against Gatekey below.
  for await (const chunk of stream) {
    const delta = chunk.choices[0]?.delta?.content;
    if (delta) process.stdout.write(delta);
  }
  process.stdout.write("\n");
}

// ---------------------------------------------------------------------------
// AFTER: the identical streaming call, pointed at Gatekey, routed to
// Anthropic's claude-sonnet-5 under the hood. Again, only
// `baseURL` + `apiKey` differ from streamDirectOpenAI() above.
// ---------------------------------------------------------------------------
async function streamViaGatekey() {
  const client = new OpenAI({
    apiKey: process.env.GATEKEY_SERVICE_ACCOUNT_KEY, // gk_sk_... - NOT a provider key
    baseURL: process.env.GATEKEY_BASE_URL ?? "http://localhost:8000/v1",
  });
  process.stdout.write("[gatekey -> anthropic] ");
  // `model` is the only request-body value that changed from the "before"
  // snippet - the streaming mechanics (stream: true, `for await` over the
  // returned Stream, reading chunk.choices[0].delta.content) are identical.
  // Gatekey translates Anthropic's native SSE event shape into this
  // OpenAI-compatible chunk shape server-side (see
  // backend/src/gatekey/providers/anthropic.py:stream_chat_completion), so
  // the SDK's own OpenAI-format chunk parsing works unmodified.
  const stream = await client.chat.completions.create({
    model: "claude-sonnet-5",
    messages: MESSAGES,
    stream: true,
  });
  for await (const chunk of stream) {
    const delta = chunk.choices[0]?.delta?.content;
    if (delta) process.stdout.write(delta);
  }
  process.stdout.write("\n");
  // Note: a mid-stream provider failure on Gatekey's side cannot change the
  // HTTP status (headers/the 200 are already flushed once streaming
  // starts) - the SSE stream just ends early with no [DONE] sentinel, the
  // same observable behavior as the upstream connection dropping. A
  // request-shape or connectivity error that happens *before* the first
  // chunk is still surfaced as a normal thrown APIError - see
  // chat-completion.mjs's error-handling comment for the shape caveat that
  // applies to that case.
}

await streamDirectOpenAI();
await streamViaGatekey();
