// Embeddings: direct OpenAI SDK vs. Gatekey (BD-11).
//
// Endpoint demonstrated: POST /v1/embeddings
// Model: gemini-embedding-001 -> routed by Gatekey to the Vertex AI provider.
// Again, the caller uses the official OpenAI SDK's `.embeddings.create(...)`
// unmodified - Gatekey translates the OpenAI-shaped embeddings request/
// response into Vertex AI's native shape server-side (see
// backend/src/gatekey/providers/vertex_ai.py:create_embeddings).
//
// Run:
//   npm install openai
//   export OPENAI_API_KEY=sk-...                  # only needed for the "before" call
//   export GATEKEY_BASE_URL=http://localhost:8000/v1
//   export GATEKEY_SERVICE_ACCOUNT_KEY=gk_sk_...
//   node embeddings.mjs
//
// See ../README.md for prerequisites and the full "error shape" explanation.

import OpenAI from "openai";

const INPUT_TEXT = "Gatekey is an AI gateway.";

// ---------------------------------------------------------------------------
// BEFORE: calling OpenAI's embeddings API directly. No Gatekey involved.
// ---------------------------------------------------------------------------
async function embedDirectOpenAI() {
  const client = new OpenAI({
    apiKey: process.env.OPENAI_API_KEY,
    // baseURL unset -> SDK default "https://api.openai.com/v1".
  });
  const response = await client.embeddings.create({
    model: "text-embedding-3-small",
    input: INPUT_TEXT,
  });
  console.log(`[direct openai] embedding length=${response.data[0].embedding.length}`);
}

// ---------------------------------------------------------------------------
// AFTER: the identical shape of call, pointed at Gatekey, using a Vertex AI
// embeddings model. Only `baseURL` + `apiKey` differ from
// embedDirectOpenAI() above (plus `model`, to pick a Vertex-routed model
// instead of an OpenAI one - demonstrating that model choice, not client
// configuration, is what selects the provider).
// ---------------------------------------------------------------------------
async function embedViaGatekey() {
  const client = new OpenAI({
    apiKey: process.env.GATEKEY_SERVICE_ACCOUNT_KEY, // gk_sk_... - NOT a provider key
    baseURL: process.env.GATEKEY_BASE_URL ?? "http://localhost:8000/v1",
  });
  const response = await client.embeddings.create({
    model: "gemini-embedding-001",
    input: INPUT_TEXT,
  });
  console.log(`[gatekey -> vertex_ai] embedding length=${response.data[0].embedding.length}`);
}

await embedDirectOpenAI();
await embedViaGatekey();
