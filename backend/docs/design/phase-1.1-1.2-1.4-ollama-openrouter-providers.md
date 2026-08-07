---
title: Phase 1.1/1.2/1.4 — Add Ollama & OpenRouter Providers — Product Spec
status: draft
last_updated: 2026-07-28
author: product-owner
depends_on: Phase 1.1 (provider/key mgmt), 1.2 (gateway core), 1.4 (budget/pricing) — all built, 277 tests passing
---

# Add Ollama & OpenRouter Providers — Product Spec

**This is not a new phase.** It is an addition within the already-shipped Phase 1
boundary — `gatekey/phase-1-core-gateway.md` §1.1 (Provider & Key Management),
§1.2 (Unified API / Gateway Core), §1.4 (Budget/Pricing). Phase 1's "at least 3
providers" language is a historical minimum already met (OpenAI, Anthropic,
Vertex AI) — this document does not touch that requirement text, it goes beyond
it.

Source of truth for every provider-specific technical fact below (base URLs,
auth shape, pricing, validation endpoints) is the already-researched brief
supplied by the orchestrator, not re-derived here. This document's job is
structure, acceptance criteria, and flagging tension against
`phase-1-core-gateway.md` and this codebase's existing patterns — not
second-guessing those facts.

Grounded against the actual shipped code: `providers/{base,openai,registry}.py`,
`providers/{model_registry,pricing}.py`, `db/models/provider_key.py`,
`schemas/provider_key.py`, `api/v1/admin/providers.py`,
`services/{provider_keys,proxy_keys,encryption}.py`,
`alembic/versions/0001_create_orgs_and_provider_keys.py`,
`frontend/src/lib/api.ts`, `gatekey/phase-1-admin-console-ui-requirements.md`
§7.2/§7.4.

---

## 1. Component A — Backend provider modules

### US-A1: Ollama chat completions work as an OpenAI-shape passthrough against an admin-configured base URL
- **AC-A1-1:** `providers/ollama.py` mirrors `providers/openai.py`'s structure exactly: `OllamaValidator(ProviderValidator)` + module-level `create_chat_completion()` / `stream_chat_completion()` functions taking `(client, native_model_id, request, credential, *, timeout_seconds)`.
- **AC-A1-2:** Unlike every existing provider module, there is **no** module-level `OLLAMA_CHAT_COMPLETIONS_URL` constant — the URL is built at call time as `f"{credential.base_url.rstrip('/')}/v1/chat/completions"`, read from the credential object, not a fixed string. This is the one structural deviation from `openai.py`'s pattern, called out explicitly so it isn't "fixed" back to match the other providers by habit during review.
- **AC-A1-3:** Outbound `Authorization: Bearer <token>` header is always sent (never omitted), where `<token>` is `credential.bearer_token` if non-empty, else a fixed placeholder literal (see §6, open item #4) — Ollama's OpenAI-compat layer requires *a* key be present even though it never validates it.
- **AC-A1-4:** `providers/ollama.py` defines **no** `create_completion()` and **no** `create_embeddings()` function — chat only, matching `MODEL_REGISTRY` having zero non-chat Ollama entries (§3). Not a placeholder stub, not built at all this pass.
- **AC-A1-5:** Errors (network failure, non-2xx response, mid-stream failure) raise `providers.base.ProviderCallError` via the existing shared `provider_call_error_from_response` / `provider_call_error_from_exception` helpers — no new error-mapping logic introduced.
- **AC-A1-6:** Streaming parses `data: {...}` SSE frames identically to `openai.py`, including injecting `stream_options: {"include_usage": true}` into the outbound body for Budget's (1.4) per-request cost/usage accounting — see §6 open item #5 for the one unverified behavioral assumption this depends on.
- **AC-A1-7:** A dedicated test (not present for any existing provider, since none of them vary base URL) asserts that two `OllamaCredential`s with different `base_url` values produce two different outbound request URLs for the same call.

### US-A2: OllamaValidator validates against the admin-entered base URL, mapping connection failures to PROVIDER_UNREACHABLE
- **AC-A2-1:** `validate(secret_payload)` reads `base_url` from `secret_payload` (not a constant) and issues `GET {base_url}/v1/models` with a Bearer header (using `bearer_token` if present, else the same placeholder as AC-A1-3).
- **AC-A2-2:** Reuses `map_httpx_exception` / `map_http_status` from `providers/base.py` unchanged — no Ollama-specific status mapping. A refused/unreachable connection (the dominant real-world failure mode for a self-hosted target) resolves to `ValidationStatus.PROVIDER_UNREACHABLE`, which the existing `PUT /v1/admin/providers/{provider}/key` route already maps to HTTP 502 `provider_unreachable` — no route-layer change needed.
- **AC-A2-3:** A malformed/missing `base_url` in `secret_payload` (defense-in-depth; schema validation at D2 should already prevent this reaching the validator) returns `ValidationStatus.UNKNOWN_ERROR`, matching `OpenAIValidator`'s existing malformed-payload branch.

### US-A3: OpenRouter chat completions work as a direct OpenAI-shape passthrough
- **AC-A3-1:** `providers/openrouter.py` mirrors `providers/openai.py` exactly, including a fixed `OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"` constant (OpenRouter's base URL is fixed, unlike Ollama's) and `Authorization: Bearer <api_key>` auth, identical shape to OpenAI's.
- **AC-A3-2:** Optional attribution headers (`HTTP-Referer`, `X-OpenRouter-Title`) are **not** implemented this pass — explicitly deferred per the brief's own "skip if it complicates the shared request-building helper" guidance; adding provider-specific optional headers now would branch the shared body-building helper for a nice-to-have, not a correctness requirement.
- **AC-A3-3:** `providers/openrouter.py` defines **no** `create_completion()` / `create_embeddings()` this pass, matching `MODEL_REGISTRY` having only `ModelCapability.CHAT` OpenRouter entries (§3) — same reasoning as Ollama (AC-A1-4), not because OpenRouter itself lacks those capabilities upstream.
- **AC-A3-4:** `OpenRouterValidator` calls `GET https://openrouter.ai/api/v1/models` with the submitted key as Bearer, reusing `map_httpx_exception`/`map_http_status` unchanged.

### US-A4: Registry wiring
- **AC-A4-1:** `providers/registry.py`'s `SUPPORTED_PROVIDERS` becomes `("openai", "anthropic", "vertex_ai", "ollama", "openrouter")`.
- **AC-A4-2:** `build_validator_registry()` gains `"ollama": OllamaValidator(...)`, `"openrouter": OpenRouterValidator(...)` entries.
- **AC-A4-3:** Any other hardcoded provider enumeration outside `registry.py` (e.g. in `api/deps.py`, if `get_validator_registry` doesn't delegate straight to `build_validator_registry`) must be checked and kept in sync — flag if found, don't assume `registry.py` is the only place this list lives.

---

## 2. Component B — Credential plumbing (services/provider_keys.py, services/proxy_keys.py, services/encryption.py)

**Scope-list correction:** the brief's file-scope list names `services/encryption.py` / `services/provider_keys.py` for this area. Grounding against the actual code shows the provider→credential-*shape* dispatch (which of `ApiKeyCredential` / `ServiceAccountCredential` / a new type a provider gets) lives in **`services/proxy_keys.py`**, not either of those two files. Ollama's `base_url + optional bearer_token` shape doesn't fit either existing credential dataclass, so `proxy_keys.py` requires a real code change (new dataclass + new dispatch branch), not just a "confirm this still works" check. Flagging this as an addition to the stated file-scope list, not a silent scope expansion — the underlying need was already implied by the credential-shape difference in the brief, I'm just naming the actual file it lands in.

### US-B1: OpenRouter reuses the existing bearer-key credential shape unchanged
- **AC-B1-1:** `services/proxy_keys.py`'s `_API_KEY_PROVIDERS` tuple gains `"openrouter"` — `get_decrypted_provider_credential("openrouter", ...)` returns a plain `ApiKeyCredential(provider="openrouter", api_key=...)`, identical code path to `"openai"`/`"anthropic"`.
- **AC-B1-2:** `services/provider_keys.py`'s `_serialize_secret_payload()` gains `"openrouter"` to the existing `if provider in ("openai", "anthropic")` branch (extracts `api_key`, JSON-serializes it).
- **AC-B1-3:** `_build_key_metadata()` returns `{}` for `"openrouter"`, matching `"openai"`/`"anthropic"` (no non-secret routing config needed — the base URL is fixed, see A3-1).

### US-B2: Ollama gets a new base-url-plus-optional-bearer credential shape
- **AC-B2-1:** New `OllamaCredential(ProviderCredential)` frozen dataclass in `services/proxy_keys.py`: `provider: str`, `base_url: str`, `bearer_token: str` (never `None` — empty string when not configured, see AC-B2-4). Inherits the base class's redacted `__repr__`/`__str__` unchanged (whole-object redaction, same convention as `ServiceAccountCredential`, even though `base_url` alone isn't secret — consistent, no special-casing).
- **AC-B2-2:** `get_decrypted_provider_credential()` gains a third dispatch branch (e.g. a new `_BASE_URL_BEARER_PROVIDERS = ("ollama",)` tuple) that: decrypts the stored ciphertext to a plaintext string (may be `""`), reads `base_url` from `row.key_metadata["base_url"]`, and returns `OllamaCredential(provider="ollama", base_url=..., bearer_token=<decrypted string, possibly empty>)`.
- **AC-B2-3:** `services/provider_keys.py`'s `_serialize_secret_payload()` gains an `"ollama"` branch: `json.dumps(secret_payload.get("bearer_token") or "").encode("utf-8")` — **always** produces a serializable value, never skips the encrypt step, directly satisfying `ProviderKey.ciphertext/nonce/auth_tag`'s `NOT NULL` schema constraint even when the admin left the bearer token blank.
- **AC-B2-4:** `_build_key_metadata()` gains an `"ollama"` branch returning `{"base_url": secret_payload["base_url"]}` — non-secret, stored in `key_metadata` exactly like Vertex AI's `project_id`/`location` pattern (module docstring precedent already documents this as the intended use of that column).
- **AC-B2-5:** A test confirms: creating an Ollama key with `bearer_token` omitted round-trips through encrypt → decrypt → `OllamaCredential.bearer_token == ""`, and the stored row's `ciphertext`/`nonce`/`auth_tag` are all non-empty bytes (never a NULL-equivalent placeholder) — this is the literal test of the brief's "something must always be encrypted" requirement, exercised honestly rather than special-cased around.

---

## 3. Component C — DB / migration

### US-C1: `ProviderName` enum and Postgres type gain two new values
- **AC-C1-1:** `db/models/provider_key.py`'s `ProviderName` enum gains `OLLAMA = "ollama"`, `OPENROUTER = "openrouter"` (lowercase snake_case, matching `vertex_ai`'s existing convention).
- **AC-C1-2:** New Alembic migration (next sequential id after `0005_create_usage_logs.py`, i.e. `0006_...py`), `down_revision = "0005"`, adds the two enum values via `op.execute("ALTER TYPE provider_name ADD VALUE IF NOT EXISTS 'ollama'")` and the same for `'openrouter'` — no table DDL, no data backfill (no existing row references these values).
- **AC-C1-3 (hold-the-line item, explicit per brief):** db-admin must run this migration against a real `postgres:16-alpine` instance (not sqlite/mocked) under Alembic's default transactional-DDL wrapping and confirm it applies cleanly. Postgres 12+ permits `ALTER TYPE ... ADD VALUE` inside a transaction as long as the new value isn't *used* (compared/inserted) within that same transaction — this migration doesn't use the values it adds, so it should be safe, but "should be" must become "verified" before this is called done.
- **AC-C1-4:** `downgrade()` is documented as a hard limitation, not a silent no-op or a broken attempt at reversal — Postgres has no native "drop enum value" primitive. Mirror `0001`'s discipline of an honest, working `downgrade()` where one exists; where a true reversal is genuinely not possible, the migration's docstring says so explicitly rather than pretending.
- **AC-C1-5:** `providers/registry.py`'s `SUPPORTED_PROVIDERS` (AC-A4-1) and the DB enum values must match exactly, same discipline as the existing 3 (module docstring: "Provider identifiers match the `ProviderKey.provider` enum values agreed with database-admin").

---

## 4. Component D — Schemas / Admin API

### US-D1: `OpenRouterKeyRequest`
- **AC-D1-1:** Identical shape to `OpenAIKeyRequest` — `api_key: str` with the same length bounds and non-blank validator, `model_config = ConfigDict(extra="forbid")`.
- **AC-D1-2:** `api/v1/admin/providers.py`'s `_REQUEST_SCHEMAS` dict gains `ProviderName.OPENROUTER: OpenRouterKeyRequest`.

### US-D2: `OllamaKeyRequest`
- **AC-D2-1:** Fields: `base_url: str` (required, non-blank, reasonable max length, extra="forbid"), `bearer_token: str | None = None` (optional; if provided, must be non-blank — an empty string submitted for `bearer_token` should be normalized to `None` rather than persisted as a distinct "empty but present" state, so there is exactly one representation of "no bearer token configured").
- **AC-D2-2:** `base_url` gets a minimal scheme sanity check (must start with `http://` or `https://`) — consistent with this schema module's stated philosophy of "minimal sanity bounds, not provider-specific format validation" (this check is generic to *any* admin-configured endpoint URL, not Ollama-specific). Flagged as a small judgment call, not a hard requirement from the brief — low risk either way.
- **AC-D2-3:** `api/v1/admin/providers.py`'s `_REQUEST_SCHEMAS` dict gains `ProviderName.OLLAMA: OllamaKeyRequest`.

### US-D3: Existing generic router absorbs both new providers with zero new route code
- **AC-D3-1:** `PUT/GET/DELETE /v1/admin/providers/{provider}` and `GET /v1/admin/providers` work for `ollama`/`openrouter` through the existing `ProviderName`-keyed router unchanged — this is the payoff of that design being generic; no new endpoint functions are added.
- **AC-D3-2:** Existing error-code mapping (`InvalidProviderKeyError`→422 `invalid_key`, `ProviderUnreachableError`→502 `provider_unreachable`, `ProviderValidationUnknownError`→500 `unknown_error`) applies unchanged — e.g. an Ollama base URL that refuses connections surfaces as 502, matching the brief's stated dominant failure mode.
- **AC-D3-3:** `ProviderKeyResponse.metadata` for a saved Ollama key returns exactly `{"base_url": "..."}"` — no `bearer_token` field anywhere in the response (it's ciphertext-only, never in `key_metadata`), confirming no accidental secret leakage through the metadata column.

---

## 5. Component E — Model registry & pricing

### US-E1: Ollama model registry entries (examples, not a live catalog)
- **AC-E1-1:** 3 example `ModelCapability.CHAT` entries added to `MODEL_REGISTRY` for tags `llama3.1`, `mistral`, `qwen2.5`, `provider="ollama"`, `native_model_id` = the bare tag string.
- **AC-E1-2:** A code comment states explicitly these are examples only, functional only if the admin's Ollama instance has actually pulled that exact model tag; a request for an unpulled model fails at Ollama with a provider error surfaced as `ProviderCallError` (acceptable, not a Gatekey bug, not silently wrong).
- **AC-E1-3:** A code comment flags dynamic per-org model discovery (`GET {base_url}/api/tags`) as a deliberate, explicit follow-up — **not built** this pass, comment only, per the brief's instruction.
- **AC-E1-4:** Zero `ModelCapability.EMBEDDINGS` entries for `ollama` (matches Ollama's OpenAI-compat layer not supporting embeddings, and A1-4's "chat only" scope).
- **AC-E1-5 (flagged, see §6 item #2):** gateway-facing key naming — recommend prefixing (`ollama/llama3.1`, `ollama/mistral`, `ollama/qwen2.5`) rather than bare tags, even though no bare-tag collision exists today against the current registry. See §6 for the full rationale and the explicit ask for architect confirmation.

### US-E2: OpenRouter model registry entries (small curated allowlist)
- **AC-E2-1:** At minimum one entry: gateway-facing key → `native_model_id="openai/gpt-4o-mini"`, `provider="openrouter"`, `ModelCapability.CHAT` — the only entry with confirmed, cited pricing in the brief.
- **AC-E2-2:** Up to two additional entries **may** be added only if backend-developer/architect sources and cites real, current OpenRouter pricing for them, matching `pricing.py`'s existing per-entry `source`/`as_of` citation discipline. **Do not invent slugs or prices to hit a "2-3 entries" target** — if no further citations are found before ship, one entry is a correct, acceptable deliverable. See §6 item #3.
- **AC-E2-3:** A code comment documents OpenRouter's `vendor/model` slug convention for `native_model_id`, and restates the registry's existing stated philosophy (small hand-curated allowlist, not a mirror of OpenRouter's full multi-hundred-model catalog).

### US-E3: Pricing table — Ollama entries priced at $0 with an explicit non-cost-basis disclaimer
- **AC-E3-1:** Every Ollama `MODEL_REGISTRY` key gets a `PRICING_TABLE` entry: `input_price_per_million_usd=Decimal("0.00")`, `output_price_per_million_usd=Decimal("0.00")` (non-`None`, satisfying the existing `CHAT`-capability completeness invariant).
- **AC-E3-2:** `source` field for these entries is an explanatory string (not a provider price-page URL, since none exists), e.g. `"Self-hosted: no per-token provider charge; $0.00 does not represent real infrastructure/GPU cost."`
- **AC-E3-3:** The module-level sourcing comment for the Ollama block states explicitly: (a) `$0` does not capture real infra/GPU operating cost; (b) a full self-hosted cost-basis model (e.g. GPU-hour-rate-based estimation) is a known, already-anticipated future gap tracked in `phase-5-differentiators.md` §5.5 ("Unified Governance for BYOK + Self-Hosted OSS Models"); (c) this Phase 1 addition is intentionally simpler and is **not** a preview or partial implementation of that eventual design. Confirmed against the actual §5.5 text (self-hosted normalization/cost-basis explicitly listed as future scope there).
- **AC-E3-4:** The existing `PRICING_TABLE.keys() == MODEL_REGISTRY.keys()` completeness test (and the CHAT-entry non-null-output-price check) continues to hold for the new entries — no exemption carved out for `$0`-priced models; `$0.00` is a legitimate, present value, not a missing one.

### US-E4: Pricing table — OpenRouter entries, no-markup pass-through
- **AC-E4-1:** `openrouter/...gpt-4o-mini` entry (exact gateway-facing key per E1-5/E2-1 naming decision): `input_price_per_million_usd=Decimal("0.15")`, `output_price_per_million_usd=Decimal("0.60")`, `as_of` and `source` populated with a real citation (not a placeholder/fabricated URL) — matches direct OpenAI pricing, corroborating the "no markup on tokens" claim.
- **AC-E4-2:** The module-level sourcing comment for the OpenRouter block states explicitly: OpenRouter passes through the underlying model's own per-token price with **no markup on token costs** (confirmed); a separate ~5.5% fee applies only to **credit purchases** at the account level and is out of scope for per-request cost accounting (Gatekey has no visibility into or role in an org's OpenRouter credit-purchase transactions) — stated so a future reader doesn't "fix" this table by adding a markup that doesn't apply to per-request costs.
- **AC-E4-3:** Any additional OpenRouter entries beyond the one confirmed above carry their own independent citation — no inferring/copying pricing across entries.

---

## 6. Component F — Frontend (Providers screen, admin console)

### US-F1: Providers screen renders 5 fixed cards, matching the existing closed-set pattern
- **AC-F1-1:** `frontend/src/lib/api.ts`: `ProviderName` type extended to `"openai" | "anthropic" | "vertex_ai" | "ollama" | "openrouter"`; `PROVIDER_LABELS` gains `"Ollama"` / `"OpenRouter"` display labels; `MODELS_BY_PROVIDER` gains entries listing exactly the gateway-facing model keys added in §5 — kept in sync manually with the backend `MODEL_REGISTRY`, same as the existing 3 providers (this is a pre-existing manual-sync risk in the codebase, not something newly introduced by this feature; not this slice's job to fix).
- **AC-F1-2:** Providers page (`phase-1-admin-console-ui-requirements.md` §7.4 pattern) renders 5 provider cards instead of 3, reusing the exact same card component/state machine (Connected / Not configured, `validated_at` timestamp, Edit/Remove vs. Add actions) — no new UI states introduced.
- **AC-F1-3:** Ollama's "Add/Edit key" modal form: `base_url` (required, text input, placeholder e.g. `http://localhost:11434`) + `bearer_token` (optional, masked/password-style input, helper text explicitly framed as "only needed if your Ollama instance sits behind an authenticating reverse proxy" — matches the brief's own framing verbatim).
- **AC-F1-4:** OpenRouter's "Add/Edit key" modal form is identical in shape to OpenAI's — single masked API-key field, no other fields.
- **AC-F1-5 (flagged, see §7 item #6):** the first-run setup wizard's (§7.2) provider-tab selector is **not** expanded to 5 — it stays scoped to the original 3 (OpenAI/Anthropic/Vertex AI). Ollama/OpenRouter are configured from the post-setup Providers page only. Rationale: protects the Phase 1 NFR of setup, from `git clone` to first successful proxied request, under 60 minutes — expanding wizard scope adds decision surface to the fastest/most time-sensitive path for zero required benefit (both new providers are additive, optional choices, not needed to reach "first successful proxied request"). Flagged for explicit confirmation since the brief doesn't state this either way.

---

## 7. Component G — Tests

- **US-G1:** Unit tests for `providers/ollama.py` — validator (any-key-accepted-since-Ollama-doesn't-validate-it / unreachable→`PROVIDER_UNREACHABLE` / timeout / malformed payload), `create_chat_completion` success/4xx/5xx/network-error, `stream_chat_completion` success/mid-stream-error, plus the base-URL-varies-by-credential regression test (AC-A1-7).
- **US-G2:** Unit tests for `providers/openrouter.py` mirroring `providers/openai.py`'s existing test coverage shape (validator + chat completion, non-streaming and streaming).
- **US-G3:** `providers/registry.py` tests — any existing assertion enumerating `SUPPORTED_PROVIDERS` or its length (previously 3) must be updated to 5, not silently left stale/skipped.
- **US-G4:** `schemas/provider_key.py` tests — `OllamaKeyRequest`/`OpenRouterKeyRequest` validation: blank/oversized/malformed fields → `422`; well-formed → passes; `bearer_token=""` normalizes to `None` (AC-D2-1).
- **US-G5:** `services/provider_keys.py` tests — new `"ollama"`/`"openrouter"` branches in `_serialize_secret_payload`/`_build_key_metadata`; confirm an Ollama key saved with no `bearer_token` still produces non-empty `ciphertext`/`nonce`/`auth_tag` (AC-B2-5).
- **US-G6:** `services/proxy_keys.py` tests — new `OllamaCredential` decode path with and without a stored bearer token; confirm redacted `__repr__`/`__str__` never leaks `bearer_token`.
- **US-G7:** `providers/pricing.py` / `providers/model_registry.py` tests — existing completeness assertion extended automatically by the new entries; add an explicit assertion that every Ollama entry prices at exactly `Decimal("0.00")`/`Decimal("0.00")` (present, not `None`, not missing), and that OpenRouter entries carry a non-empty, URL-shaped `source` string (Ollama's `source` is an explanatory string instead, per AC-E3-2 — don't apply the same URL-shape assertion there).
- **US-G8:** Integration test: a full gateway chat-completion request routed through an Ollama gateway-facing model key against a stubbed/mocked Ollama server (not a live Ollama instance in CI) returning a canned OpenAI-shaped response — confirms routing, `$0` charge, and usage logging behave identically to the existing 3-provider integration tests. Same pattern for one OpenRouter-routed request against a stubbed OpenRouter response.
- **US-G9:** Migration test for `0006_...` — run `alembic upgrade head` against a real `postgres:16-alpine` test container (matching whatever existing migration-test precedent this codebase already uses for `0001`-`0005`) and assert both new enum values are usable in a subsequent `INSERT` within a new transaction (AC-C1-3).

---

## 8. Component H — Docs

- **US-H1:** `backend/examples/README.md` gains Ollama and OpenRouter to the provider list / example client snippets — Ollama's setup note explains the admin must set `base_url` (and optionally a bearer token) since there's no fixed endpoint; OpenRouter's is a drop-in like OpenAI's (base URL + key only).
- **US-H2:** Wherever the provider list is otherwise enumerated in docs (README, provider-list doc) is updated to 5 — explicitly **not** touching `phase-1-core-gateway.md`'s original "at least 3 providers" language, per the brief's instruction (this is an addition beyond a historical minimum, not a correction to it).
- **US-H3 (flagged action item, not in this doc's write scope):** `gatekey/phase-1-admin-console-ui-requirements.md` §7.4 currently states "Exactly 3 provider slots in Phase 1... since the set is closed (`ProviderName` enum: `openai`, `anthropic`, `vertex_ai`)." That line is now stale and directly contradicted by this feature (the set is still closed, but at 5, not 3) and should be updated by whoever owns that requirements doc. Not a build blocker, but leaving it unedited would leave a requirements artifact actively wrong.

---

## 9. Non-Goals — explicit, do not let scope creep here

- **No dynamic Ollama model discovery** (`GET {base_url}/api/tags`) — comment-only follow-up (AC-E1-3).
- **No multi-key-per-provider** for any provider, including the two new ones — Phase 1's "one key per provider per org" constraint (`UNIQUE(org_id, provider)`) is unchanged and applies identically to `ollama`/`openrouter`.
- **No real self-hosted cost-basis model** (GPU-hour-rate estimation, compute-based pricing) — `$0` is the deliberate, disclaimed Phase 1 answer; the real version is `phase-5-differentiators.md` §5.5's job, not previewed here (AC-E3-3).
- **No hosted evaluation sandbox** — unaffected by this feature; still self-hosted-only per Phase 1's already-resolved decision.
- **No SSO/SCIM, RBAC tiers, caching, rate limiting, failover, DLP/PII redaction, or audit trail beyond request logs** — none of Phase 1's existing Out-of-Scope list is touched by this feature.
- **No OpenRouter attribution headers** (`HTTP-Referer`, `X-OpenRouter-Title`) this pass (AC-A3-2).
- **No legacy `/v1/completions` or `/v1/embeddings` support for either new provider** this pass (AC-A1-4, AC-A3-3) — both new provider modules implement chat completions only, matching the curated registry entries actually being added.
- **No admin-editable pricing table** — static in-code, same precedent already established (and already flagged once) in the Phase 1.4 budget spec; not re-litigated here.
- **No expansion of the first-run setup wizard's provider selection** beyond the original 3 (AC-F1-5, flagged for confirmation but the default recommendation is "no").
- **No changes to `services/budget.py`'s charge/cost-computation logic** — it already looks up pricing/registry generically; adding rows to those two tables requires zero changes to the charging pipeline itself.

---

## 10. Resolved ambiguities (inferred from the overview / prior-phase precedent, not guessed)

1. **Chat-only scope for both new providers, no legacy-completions/embeddings functions.** Directly stated for Ollama in the brief; inferred for OpenRouter by symmetry with the brief's explicit "both mirror openai.py's structure (validator + create_chat_completion/stream_chat_completion)" function list and the curated registry containing no non-chat entries for either.
2. **`$0` Ollama pricing with an explicit non-cost-basis disclaimer, cross-referenced to `phase-5-differentiators.md` §5.5.** Directly per the brief; confirmed the §5.5 text actually says what the brief claims (self-hosted cost normalization is explicitly future scope there).
3. **`proxy_keys.py` needs a new credential dataclass + dispatch branch for Ollama.** Not explicitly named in the brief's file-scope list, but structurally required by the base_url+optional-bearer shape not fitting either existing credential type — resolved by inference from the existing two-shape dispatch pattern already in that file, not guessed as a design choice (see §2 intro).
4. **No admin-editable pricing.** Matches the precedent already set (and flagged once) in the Phase 1.4 budget spec — not re-opened here.

---

## 11. Flagged — needs orchestrator/user confirmation, not decided silently

1. **Gateway-facing model key naming convention for the new providers.** `MODEL_REGISTRY` is a single flat dict keyed by gateway-facing name across *all* providers (confirmed in code — no per-provider namespacing exists today). OpenRouter's own native model IDs use a `vendor/model` slug format (e.g. `openai/gpt-4o-mini`), which — if used directly as the gateway-facing key — would sit right next to the *existing* `gpt-4o-mini` key that already routes to OpenAI directly, inviting confusion even though it wouldn't technically collide. Recommend prefixing both new providers' gateway-facing keys (`ollama/llama3.1`, `openrouter/openai/gpt-4o-mini`) to disambiguate and establish a forward-compatible convention, even though no literal string collision exists today for Ollama's example tags. This changes the public `model` field string API callers must pass — a genuine judgment call on API surface, not mine to make silently. Needs architect/orchestrator confirmation before `model_registry.py` entries are written.
2. **OpenRouter's curated list may ship with only 1 entry, not 2-3, unless additional cited pricing is sourced** (AC-E2-2). Recommend accepting 1 rather than inventing slugs/prices to hit a round number — flagging in case "2-3 entries" was actually a hard requirement rather than a rough target.
3. **Placeholder bearer-token literal sent to Ollama when the admin configured none** (AC-A1-3/AC-A2-1). Needs one canonical value (e.g. `"ollama"`, a common convention in Ollama's own community docs, vs. something more explicit like `"not-required"`). Cosmetic, but needs a single agreed constant rather than each call site inventing its own.
4. **Unverified: does Ollama's OpenAI-compat streaming layer honor `stream_options: {"include_usage": true}` the same way OpenAI does?** The brief doesn't confirm this specific behavior. If it doesn't, streaming Ollama chat requests never receive a terminal usage-bearing chunk, and — per 1.4's already-established "fail toward not charging" semantics for aborted/incomplete streams — those requests would be charged `$0` (harmless, since Ollama pricing is `$0` anyway) but would also silently lose accurate *token-count* usage logging (1.5), which is a real, if minor, observability gap distinct from the cost question. Recommend backend-developer verify against a real local Ollama instance before this is marked done; not a design blocker, but a blocker on a "done and correct" claim.
5. **Should the first-run setup wizard expand from 3 to 5 provider tabs?** Recommend no (AC-F1-5) to protect the under-60-minutes NFR; flagging for explicit confirmation since the brief is silent either way.
6. **`gatekey/phase-1-admin-console-ui-requirements.md` §7.4's "exactly 3 provider slots" line is now stale** and should be updated by whoever owns that doc — not a build blocker, flagged as a required follow-up edit (US-H3).

---

## 12. Cross-check against Phase 1 boundaries (`phase-1-core-gateway.md`)

- **§1.1 (one key per provider per org):** unchanged, applies identically to the 2 new providers (UNIQUE constraint untouched).
- **§1.1 (basic key validation on entry):** both new providers get a live validation call before save, same as the existing 3 — no weakening.
- **§1.2 (OpenAI-compatible routing):** both new providers slot into the existing `resolve_model()` → provider-dispatch pattern with zero changes to the dispatch mechanism itself.
- **§1.4 (cost computed from published token pricing, normalized to a common currency):** OpenRouter entries use real, cited, USD-per-million-token pricing exactly like the existing 3 providers; Ollama entries are `$0`, explicitly disclaimed as not representing real cost (§5, US-E3) — this is a deliberate, flagged simplification consistent with self-hosted inference having no per-token provider charge to normalize, not a loosening of the pricing requirement for providers that *do* have one.
- **Out of Scope (SSO/SCIM, RBAC, budget rollover, caching/rate limiting/failover, DLP, audit trail beyond request logs, Phase 5 features):** none touched by this feature.
- **NFRs:** p99 <150ms overhead — unaffected; this feature adds dict entries and two new provider modules, no new hot-path DB calls or synchronous work beyond what the existing per-provider dispatch already does. Idempotent cost accounting — unaffected; `services/budget.py`'s charge logic is untouched, it already looks up registry/pricing generically. Under-60-minutes setup — protected explicitly by keeping the setup wizard at 3 providers (AC-F1-5, flagged item #5).
- **Success criteria ("accurate cost/usage data"):** Ollama's `$0` pricing is *accurate to what Gatekey can charge* (there is no provider invoice to reconcile against) but is explicitly *not* accurate to real infra cost — this distinction is documented in-code (AC-E3-3) and in this spec (§9) so it's never later mistaken for a completed cost-governance feature.
