# Gatekey SDK drop-in-replacement examples (BD-11)

Phase 1.2 deliverable: proof that switching an existing internal app from a
direct provider SDK call to Gatekey requires changing **only the base URL and
the API key** — nothing else about the calling code changes. Every example
below uses the official `openai` SDK (Python and JavaScript/TypeScript)
completely unmodified. Gatekey is not a special client library; it's a
drop-in HTTP replacement for `https://api.openai.com`.

This also demonstrates that routing is provider-agnostic to the caller: the
same official `openai` client, with the same two changed constructor
arguments, is used to reach OpenAI, Anthropic, and Vertex AI models — the
caller's code has no idea (and doesn't need to know) which provider actually
served a given `model`. The same is true of the two self-hosted/aggregator
providers added alongside the original three — Ollama and OpenRouter (see
"Additional providers" below) — nothing about the calling code changes for
them either, only the `model` string and which admin-configured key backs
it.

## What changes, exactly

| | Direct provider SDK call | Via Gatekey |
|---|---|---|
| `base_url` / `baseURL` | unset (SDK default, `https://api.openai.com/v1`) | `http://<gatekey-host>:8000/v1` |
| `api_key` / `apiKey` | the provider's own secret key (`sk-...`, etc.) | a Gatekey **service-account key** (`gk_sk_...`) — never a provider key |
| everything else (`.chat.completions.create(...)`, `.embeddings.create(...)`, streaming iteration, request/response fields) | unchanged | unchanged |

Each script in `python/` and `js/` shows both the "before" (direct provider)
and "after" (Gatekey) call side by side so the diff is visible without
flipping between files.

## Prerequisites

1. A running Gatekey instance (see the repo root / `backend/` for how to run
   the API locally; defaults below assume it's reachable at
   `http://localhost:8000`).
2. At least one provider key configured in Gatekey, via the *admin* trust
   boundary (`Authorization: Bearer <GATEKEY_ADMIN_TOKEN>`, **not** a
   service-account key — see `backend/src/gatekey/api/deps.py`):

   ```bash
   curl -X PUT http://localhost:8000/v1/admin/providers/openai/key \
     -H "Authorization: Bearer $GATEKEY_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"api_key": "sk-..."}'

   curl -X PUT http://localhost:8000/v1/admin/providers/anthropic/key \
     -H "Authorization: Bearer $GATEKEY_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"api_key": "sk-ant-..."}'

   curl -X PUT http://localhost:8000/v1/admin/providers/vertex_ai/key \
     -H "Authorization: Bearer $GATEKEY_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"service_account_json": {...}}'

   curl -X PUT http://localhost:8000/v1/admin/providers/ollama/key \
     -H "Authorization: Bearer $GATEKEY_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"base_url": "http://localhost:11434", "bearer_token": null}'

   curl -X PUT http://localhost:8000/v1/admin/providers/openrouter/key \
     -H "Authorization: Bearer $GATEKEY_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"api_key": "sk-or-..."}'
   ```

   This validates the key against the provider before saving it — see
   `backend/src/gatekey/api/v1/admin/providers.py`. Only configure the
   providers whose examples you intend to run. See "Additional providers:
   Ollama and OpenRouter" below for what `base_url` should point at and why
   `bearer_token` is usually left `null`.

3. A **service-account key** — the credential these examples actually
   authenticate with (`Authorization: Bearer gk_sk_...`), minted via the
   admin-gated endpoint (again the human admin token, not a provider key).
   Every new key must be attributed to a `user_id` (Phase 1.4, budget
   owner) and a `team_id` the user is already a member of (Phase 2) — the
   easiest path is the repo-root README's Quick Start step 4 (Users →
   Teams → Service Accounts screens), or via curl:

   ```bash
   curl -X POST http://localhost:8000/v1/admin/service-accounts \
     -H "Authorization: Bearer $GATEKEY_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"name": "sdk-examples", "user_id": "<user-uuid>", "team_id": "<team-uuid>"}'
   ```

   The response's `secret` field (`gk_sk_...`) is shown **exactly once** —
   copy it immediately, it cannot be retrieved again
   (`backend/src/gatekey/api/v1/admin/service_accounts.py`).

   A Phase 2 **personal key** (`gk_pk_...`, minted self-service from the
   console's My API Keys screen) works identically everywhere a
   `gk_sk_...` key appears in these examples — same bearer header, same
   endpoints.

4. Export the environment variables the scripts read:

   ```bash
   export GATEKEY_BASE_URL="http://localhost:8000/v1"
   export GATEKEY_SERVICE_ACCOUNT_KEY="gk_sk_..."      # from step 3

   # only needed to run the "before" (direct provider) snippets for comparison:
   export OPENAI_API_KEY="sk-..."
   ```

5. Python: `pip install openai`. JavaScript: `npm install openai` (run from
   inside `examples/js/`, or anywhere and point `NODE_PATH` at it — the
   scripts are plain ES modules runnable with `node`, no build step).

## Running the examples

```bash
# Python
cd backend/examples/python
python chat_completion.py             # non-streaming chat, model=gpt-4o (OpenAI)
python chat_completion_streaming.py   # streaming chat, model=claude-sonnet-5 (Anthropic)
python embeddings.py                  # embeddings, model=gemini-embedding-001 (Vertex AI)

# JavaScript
cd backend/examples/js
npm install openai
node chat-completion.mjs
node chat-completion-streaming.mjs
node embeddings.mjs
```

Each script runs its "before" snippet first (direct provider, requires
`OPENAI_API_KEY`) and then its "after" snippet (via Gatekey, requires
`GATEKEY_BASE_URL` + `GATEKEY_SERVICE_ACCOUNT_KEY`). Comment out the
`call_direct_*` / `callDirect*` call if you don't have a real OpenAI key
handy and only want to exercise the Gatekey path.

## Coverage

| Script | Endpoint | Streaming | Model | Provider Gatekey routes to |
|---|---|---|---|---|
| `chat_completion.py` / `chat-completion.mjs` | `POST /v1/chat/completions` | no | `gpt-4o` | OpenAI |
| `chat_completion_streaming.py` / `chat-completion-streaming.mjs` | `POST /v1/chat/completions` (`stream: true`, SSE) | yes | `claude-sonnet-5` | Anthropic |
| `embeddings.py` / `embeddings.mjs` | `POST /v1/embeddings` | no | `gemini-embedding-001` | Vertex AI |

The caller-facing code is the official `openai` SDK in every case, regardless
of which provider actually serves the request — that's the point of Gatekey's
unified API (Phase 1 Story 1).

There are no dedicated `python/`/`js/` scripts for Ollama or OpenRouter — the
same three scripts above work unchanged against them, since Gatekey's API
surface doesn't vary by provider. Just point `model` at an Ollama- or
OpenRouter-routed model key instead (see below) and make sure the
corresponding admin key is configured.

## Additional providers: Ollama and OpenRouter

Added alongside the original three providers, on the same closed
`ProviderName` set (`openai`, `anthropic`, `vertex_ai`, `ollama`,
`openrouter`) — configured and called exactly like the three above, through
the same generic `PUT /v1/admin/providers/{provider}/key` admin endpoint and
the same `/v1/chat/completions` gateway route. The differences are provider-
specific: what goes in the key request body, and which gateway-facing
`model` strings route to each.

### Ollama (self-hosted, chat only)

Ollama has no fixed public endpoint — it's whatever host you're running it
on — so instead of an `api_key`, the admin configures a `base_url`:

```bash
curl -X PUT http://localhost:8000/v1/admin/providers/ollama/key \
  -H "Authorization: Bearer $GATEKEY_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"base_url": "http://localhost:11434", "bearer_token": null}'
```

- `base_url` — required. Point it at your Ollama instance: `http://localhost:11434`
  if Ollama runs on the same machine as Gatekey (outside Docker), or an
  internal network hostname/IP reachable from wherever the Gatekey backend
  container actually runs (e.g. `http://host.docker.internal:11434` or a
  service name on the same docker network — `localhost` from *inside* the
  Gatekey container does not reach a host-machine Ollama process).
- `bearer_token` — optional, `null` by default. Only set this if your Ollama
  instance sits behind an authenticating reverse proxy; Ollama itself doesn't
  check the value.
- This key is validated on save the same way the other four are (`GET
  {base_url}/v1/models`) — an unreachable `base_url` fails the `PUT` with a
  502 `provider_unreachable` error, same shape as any other provider's
  validation failure.

Gateway-facing model names are `ollama/`-prefixed:

```json
{"model": "ollama/llama3.1", "messages": [{"role": "user", "content": "hello"}]}
```

Available out of the box: `ollama/llama3.1`, `ollama/mistral`,
`ollama/qwen2.5`. **These only work if you've actually pulled that exact
model tag on your Ollama instance first** (`ollama pull llama3.1`, etc.) —
Gatekey doesn't pull models for you. Requesting a model you haven't pulled
fails with an upstream provider error (`provider_upstream_error`), not a
Gatekey bug; check `ollama list` on your instance if a request fails
unexpectedly.

Ollama is **chat-completions only** in this addition — there is no
`ollama/...` model for `/v1/embeddings` or the legacy `/v1/completions`
route (Ollama's OpenAI-compatible layer doesn't expose an embeddings
endpoint). Ollama-routed requests are priced at `$0.00` in the usage
dashboard — this reflects that Gatekey has no per-token provider invoice to
charge against for a self-hosted target, not that the model runs for free
(it still costs you real GPU/compute).

### OpenRouter (hosted, drop-in like OpenAI)

OpenRouter has a fixed hosted endpoint and a plain bearer API key, so its
admin key request is identical in shape to OpenAI's:

```bash
curl -X PUT http://localhost:8000/v1/admin/providers/openrouter/key \
  -H "Authorization: Bearer $GATEKEY_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"api_key": "sk-or-..."}'
```

Gateway-facing model names are `openrouter/`-prefixed, followed by
OpenRouter's own `vendor/model` slug:

```json
{"model": "openrouter/openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]}
```

`openrouter/openai/gpt-4o-mini` is the one model available out of the box.
Note the double vendor segment is expected — `openrouter/` is Gatekey's
routing prefix, `openai/gpt-4o-mini` is OpenRouter's own slug for the
underlying model. This is a different key from the pre-existing bare
`gpt-4o-mini`, which still routes straight to OpenAI directly — the two are
not interchangeable (different provider, different key configured, and
potentially different pricing/rate-limit behavior).

OpenRouter is also chat-completions only in this addition (no
`openrouter/...` embeddings model).

## Error shape caveat — read this before writing error-handling code

**Gatekey's error response body is not byte-for-byte identical to OpenAI's.**
This is a deliberate, documented design decision (see
`backend/docs/design/phase-1.2-gateway-core.md` and
`backend/src/gatekey/errors.py`), not an oversight, and it has a concrete
effect on the official SDK's typed exceptions:

- Gatekey always returns `{"error": {"code": "<gatekey_code>", "message":
  "<text>"}}`. OpenAI's own shape is `{"error": {"message", "type", "param",
  "code"}}` — a superset with an extra `type` field and, for validation
  errors, a `param` field.
- **The SDK's exception *class* selection still works correctly** —
  `openai.AuthenticationError`, `openai.NotFoundError`,
  `openai.APIStatusError`, etc. (Python) and their JS equivalents are chosen
  purely from the HTTP status code, and Gatekey returns conventional status
  codes (401/404/400/502/429 — see `errors.py`'s
  `_PASSTHROUGH_UPSTREAM_STATUS_CODES`). `except openai.NotFoundError:` /
  `catch (err) { if (err instanceof OpenAI.NotFoundError) ... }` behave the
  same as against real OpenAI.
- **`exc.code` / `err.code` is populated**, but from *Gatekey's* code
  vocabulary (`"unauthorized"`, `"model_not_found"`,
  `"provider_not_configured"`, `"unsupported_request"`,
  `"provider_upstream_error"`, `"validation_error"`, `"internal_error"` — see
  `errors.py`), not OpenAI's (`"invalid_api_key"`, `"insufficient_quota"`,
  etc.). Application code that pattern-matches on a specific *OpenAI* code
  string will not match against Gatekey.
- **`exc.type` / `err.type` is always `None`/`undefined`** against Gatekey —
  Gatekey's envelope has no `type` field at all, whereas real OpenAI always
  populates it (`"invalid_request_error"`, `"authentication_error"`, etc.).
  Code that branches on `.type` will silently see `None`/`undefined`, not an
  exception.
- **`exc.param` / `err.param` is always `None`/`undefined`** against
  Gatekey — it never identifies a single offending request field this way.
- **`str(exc)` / `exc.message` differs by SDK.** The Python SDK builds a
  generic `"Error code: <status> - <full parsed body>"` string regardless of
  backend, so it ends up embedding Gatekey's whole `{"error": {...}}` dict as
  text. The JS SDK's message builder prefers `error.message` when present as
  a string, and Gatekey's `message` field happens to satisfy that, so
  `err.message` on the JS SDK actually surfaces Gatekey's human-readable text
  fairly cleanly — this is a coincidence of both envelopes using the field
  name `message`, not a designed compatibility guarantee.

**Bottom line:** branch on HTTP status / exception class, not on `.type` or
specific `.code` string literals, if your error-handling logic needs to work
identically against both a real OpenAI endpoint and Gatekey. See the inline
comments in `chat_completion.py` / `chat-completion.mjs` for a concrete
example.
