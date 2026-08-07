// Non-streaming chat completion: direct OpenAI SDK vs. Gatekey (BD-11).
//
// Endpoint demonstrated: POST /v1/chat/completions (non-streaming)
// Model: gpt-4o -> routed by Gatekey to the OpenAI provider.
//
// Run:
//   npm install openai
//   export OPENAI_API_KEY=sk-...                  # only needed for the "before" call
//   export GATEKEY_BASE_URL=http://localhost:8000/v1
//   export GATEKEY_SERVICE_ACCOUNT_KEY=gk_sk_...
//   node chat-completion.mjs
//
// See ../README.md for prerequisites and the full "error shape" explanation
// summarized in the comment above callViaGatekey() below.

import OpenAI from "openai";

const MESSAGES = [{ role: "user", content: "Say hello in one short sentence." }];

// ---------------------------------------------------------------------------
// BEFORE: calling OpenAI directly. No Gatekey involved.
// ---------------------------------------------------------------------------
async function callDirectOpenAI() {
  const client = new OpenAI({
    apiKey: process.env.OPENAI_API_KEY, // OpenAI's own secret key
    // baseURL left unset -> SDK default "https://api.openai.com/v1".
  });
  const response = await client.chat.completions.create({
    model: "gpt-4o",
    messages: MESSAGES,
  });
  console.log("[direct openai]", response.choices[0].message.content);
}

// ---------------------------------------------------------------------------
// AFTER: the identical call, now pointed at Gatekey.
//
// The ONLY two options that differ from callDirectOpenAI() above are
// `baseURL` and `apiKey`. The call to `.chat.completions.create(...)` -
// method name, arguments, response shape - is completely unchanged. This is
// the whole point of BD-11: an internal app switches over by touching only
// its client construction, not its call sites.
// ---------------------------------------------------------------------------
async function callViaGatekey() {
  const client = new OpenAI({
    apiKey: process.env.GATEKEY_SERVICE_ACCOUNT_KEY, // gk_sk_... - NOT a provider key
    baseURL: process.env.GATEKEY_BASE_URL ?? "http://localhost:8000/v1",
  });
  try {
    const response = await client.chat.completions.create({
      model: "gpt-4o",
      messages: MESSAGES,
    });
    console.log("[gatekey -> openai]", response.choices[0].message.content);
  } catch (err) {
    if (err instanceof OpenAI.APIError) {
      // ERROR SHAPE CAVEAT (full detail in ../README.md "Error shape
      // caveat"): Gatekey's JSON error body is {"error": {"code",
      // "message"}} - not OpenAI's {"error": {"message", "type", "param",
      // "code"}}. Concretely against Gatekey:
      //   - err.status and the exception *class* (here, APIError; would be
      //     OpenAI.AuthenticationError for a 401, OpenAI.NotFoundError for
      //     a 404, etc.) are still correct, since the SDK picks the class
      //     from the HTTP status code, not the body.
      //   - err.code IS populated, but holds Gatekey's own code string
      //     (e.g. "provider_upstream_error", "model_not_found") rather
      //     than an OpenAI code like "insufficient_quota" - don't
      //     pattern-match on OpenAI-specific code values.
      //   - err.type is always undefined from Gatekey (no "type" key in
      //     the envelope at all); real OpenAI always sets this.
      //   - err.param is always undefined from Gatekey.
      // So: branch on `err instanceof <SpecificErrorClass>` / err.status,
      // not on err.type or a specific err.code literal, if this handler
      // also needs to work against real OpenAI.
      console.error(`[gatekey] request failed: status=${err.status} code=${err.code} message=${err.message}`);
    }
    throw err;
  }
}

await callDirectOpenAI();
await callViaGatekey();
