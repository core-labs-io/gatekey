---
title: Custom Model Registry (Admin-Managed BYOK Models)
description: Technical Design Document
status: draft
last_updated: 2026-08-06
authors: architect
---

# Custom Model Registry (Admin-Managed BYOK Models)
## Technical Design Document

Source: `gatekey/custom-model-registry-product-spec.md` (product-owner spec,
all three §9 open questions resolved by the user in §12 — treated as
settled, not open, throughout this document). Verified against real code in
`backend/src/gatekey/` before writing a single line of this design — every
claim in the product spec's §3/§5/§8 was checked against the actual current
implementation of the direct precedent this feature reuses (Phase 5.5
Unified Governance: `self_hosted_providers` table,
`SelfHostedModelRouteCache`, `resolve_route()`'s fallback, the
`_validate_model_ids()` collision guard). The spec's summaries of that code
are accurate. This design also found and resolves **two real gaps the spec
did not surface** — see §2.2 (the `ModelRoute` discriminator problem) and
§2.5 (the embeddings-provider gap) — both are load-bearing for a correct
implementation, not stylistic nits.

Current alembic head is `0043_encrypt_shadow_ai_webhook_url.py` (confirmed
by directory listing). This feature's migration is `0044`.

---

## 1. Overview

This feature lets an Org Admin register a DB-backed "custom model" — a
gateway-facing name mapped to a native model id at one of four existing BYOK
providers (`openai`/`anthropic`/`vertex_ai`/`openrouter`; **not** `ollama`,
which has its own mechanism per Phase 5.5), with admin-entered real
per-token pricing, gated behind a one-time live verification call before it
becomes routable. It reuses the Phase 5.5 precedent's shape (DB table +
process-local whole-snapshot cache + `resolve_route()` fallback + `verified`
gate + collision guard + Org-Admin-only console card) everywhere the two
features are alike, and diverges only where BYOK-specific requirements
demand it — no new credential type (rides the existing `provider_keys` row
for its `provider`), real per-token pricing instead of a GPU-hour estimate,
and — the single most consequential divergence, worked out in §2.2 below —
`ModelRoute.provider` carries the **real** BYOK provider string, not a
sentinel, which means routing code needs a different way to tell "this is a
custom-model request" than it needed for self-hosted.

### 1.1 Key Constraints Carried Forward

| Constraint | Implication |
|------------|-------------|
| Self-hosted first | No new external service; reuses the existing per-provider HTTP clients (`providers/openai.py` etc.) |
| No plaintext keys at rest | No new credential of any kind is stored by this feature — it rides the already-encrypted `provider_keys` row for its `provider` |
| OpenAI-compatible API | No request/response body shape changes anywhere; a custom model is indistinguishable on the wire from a static one |
| Phase 1 pricing completeness invariant | `PRICING_TABLE`'s `_validate_completeness()` guard is for the **static** registry only and is untouched; this feature builds a parallel, DB-backed completeness invariant (§4.1) for `custom_models`, enforced at every write, not just at import time |
| Phase 5.5 chat-only self-hosted precedent | **Deliberately NOT reused as-is** — self-hosted's `resolve_route()` fallback is wired into `chat.py` only (AC5.5.4). This feature's capability is per-row (`chat` **or** `embeddings`), so its fallback must be wired into both `chat.py` and `embeddings.py` (never `completions.py`, per spec §7's non-goal) — see §2.2/§5. |

### 1.2 Non-Functional Requirements (from spec §10, made explicit and testable)

| NFR | Target | Enforcement |
|-----|--------|-------------|
| Static registry always wins, provably | Registering a name colliding with a static `MODEL_REGISTRY` key is rejected (422) before any DB write | `services/custom_models.py`'s write-time validation, guard #1 (§4.1) |
| Unverified custom models are never routable | A gateway request for an unverified custom model 404s, identically to an unknown model | `CustomModelRouteCache` only ever contains `verified=true` rows — enforced at the query level (§2.2), not a second runtime flag check |
| Verification uses the real, existing BYOK key — never a new one | Verifying with no `provider_keys` row configured fails with the same `ProviderNotConfiguredError` shape a normal gateway request produces | `verify_custom_model()` calls `services.proxy_keys.get_decrypted_provider_credential()` — the identical function every gateway request already uses (§2.3) |
| Cost is computed from admin-entered real per-token rates, never an estimate | `usage_logs.cost_usd` for a custom-model request equals `compute_custom_model_cost()`'s exact arithmetic | `record_usage_charge()`'s existing `precomputed_cost_usd` hook (§2.2), no "estimated" label anywhere |
| Shadowing is detectable without redeploy/restart | `GET /v1/admin/custom-models` reflects the *currently running* code's registry on every call | `shadowed_by_registry` computed at response-build time, zero I/O, never persisted (§2.4) |
| Collision guards are bidirectional across all three model-name sources | Custom-vs-self-hosted collisions rejected in both directions with a specific error | `services/self_hosted_providers.py::_validate_model_ids()` extended (§4.1/§5), plus `services/custom_models.py`'s own equivalent guard |
| `resolve_route()` stays zero-I/O on the hot path | No new DB query per gateway request | `CustomModelRouteCache` — warmed cache read only, same tier as every other `*Cache` |

---

## 2. System Architecture

### 2.1 Data Flow: Registration, Edit, Remove

```
Org Admin (Providers screen, new "Custom Models" card)
        │
        v
POST /v1/admin/custom-models   (require_role("org_admin"))
        │
        └─ services.custom_models.register_custom_model(session, ...)
              │
              ├─ 1. provider == "ollama"?                    -> 422 (guard #5)
              ├─ 2. capability == "embeddings" AND
              │      provider not in ("openai", "vertex_ai") -> 422 (guard #6, NEW —
              │      see §2.5; not in the product spec, found by re-grepping
              │      embeddings.py's actual provider dispatch table)
              ├─ 3. capability/output_price mismatch          -> 422 (guard #4)
              ├─ 4. name collides with a static MODEL_REGISTRY key -> 422 (guard #1)
              ├─ 5. name collides with a self_hosted_providers.models entry,
              │      this org (queries SelfHostedProvider ORM rows directly —
              │      see §5 for why not via the service module)   -> 422 (guard #2)
              ├─ 6. name collides with another custom_models row, this org
              │      -> 409, via the table's own UNIQUE(org_id, name)         (guard #3)
              ├─ pricing_as_of = today()  (server-set, never admin-entered)
              └─ INSERT, verified=false always (registration never auto-verifies)
```

Edit (`PUT /v1/admin/custom-models/{id}`) runs the identical guard set
(excluding the row being edited from guard #3/#6), and additionally:
- Editing `native_model_id` or `provider` resets `verified` to `false`
  (identical rationale to `edit_self_hosted_provider`'s `base_url` reset).
- Editing `provider` re-runs guard #6 (embeddings/provider compatibility)
  against the *new* provider value.
- Editing *only* pricing fields (`input_price_per_million_usd`,
  `output_price_per_million_usd`, `pricing_source`) does **not** reset
  `verified`, and re-sets `pricing_as_of = today()`.
- Editing `capability` re-runs guard #3/#4/#6 against the new value; changing
  `capability` on a model with real usage history is allowed (no versioning
  workflow, per spec §7) but is flagged in the admin UI's confirm dialog as
  a meaningful semantic change (product spec doesn't require this UI
  affordance; recommended, not mandatory — see §11).

Remove (`DELETE /v1/admin/custom-models/{id}`) is a hard delete; the row
disappears from `CustomModelRouteCache` on the next cache refresh (§5), and
new requests for that name 404 immediately. No FK from `usage_logs` to
`custom_models` exists in this design (§2.6 explains why), so historical
`usage_logs` rows are structurally unaffected by the delete — there is
nothing to `SET NULL`.

Every handler re-derives the full `CustomModelRouteCache` snapshot from a
fresh DB read and calls `cache.set_all(...)` **after** its own commit —
identical convention to `api/v1/admin/self_hosted_providers.py`'s four
handlers (§5).

### 2.2 Data Flow: `resolve_route()` Layering — the `ModelRoute` Discriminator Problem

**This is the single most important architectural decision in this design,
and it is a real correction to how the product spec's §3 pseudocode would
actually behave if implemented literally.**

The product spec's §3 recommends `ModelRoute.provider` carry the *real* BYOK
provider value (`"openai"`/etc.) for a custom-model route — correctly, this
is what makes `call_provider_with_failover()`, `fetch_credential()`, and
`chat.py`'s/`embeddings.py`'s existing provider-dispatch branches work
completely unmodified (verified directly: `chat.py::_create_non_streaming`/
`_create_streaming` already branch on `provider == "openai"`/`"anthropic"`/
`"vertex_ai"`/`"ollama"`/`"self_hosted"`/`"openrouter"`, and
`embeddings.py`'s `_call()` closure already branches on `"openai"`/
`"vertex_ai"` — a custom model routes through these **unchanged**, needing
zero new dispatch branches, unlike self-hosted's `"self_hosted"` sentinel
which needed a brand-new `call_self_hosted_provider()` sibling function).

**The gap**: self-hosted's cost/dispatch special-casing throughout
`chat.py` (`if effective_route.provider == "self_hosted": ...`) works
because `"self_hosted"` is a synthetic value no static route ever has. A
custom model's `route.provider` is `"openai"` — **indistinguishable, by
value alone, from a static `gpt-4o` route's `route.provider`**. Any code
that needs to know "is this specific request a custom-model request" (the
cost-computation branch, most importantly — it must call
`compute_custom_model_cost()` instead of letting `record_usage_charge()`
fall through to `pricing.compute_cost()`, which would raise
`PricingEntryMissingError` since a custom model's name is never a
`PRICING_TABLE` key) cannot branch on `route.provider` the way self-hosted's
code does.

**Decision**: `ModelRoute` (`providers/model_registry.py`) gains a new
field, structurally parallel to `self_hosted_provider_id`:

```python
@dataclass(frozen=True)
class ModelRoute:
    provider: str
    capability: ModelCapability
    native_model_id: str
    self_hosted_provider_id: uuid.UUID | None = None   # existing (5.5)
    custom_model_id: uuid.UUID | None = None            # NEW (this feature)
```

`custom_model_id` is populated **only** when `resolve_route()`'s
custom-model-cache fallback produces the route; it is `None` for every
static route and every self-hosted route. This field — not `route.provider`
— is the sole discriminator every downstream cost/audit branch must test.
A route can never have both `self_hosted_provider_id` and `custom_model_id`
set (the two caches' key sets are disjoint by construction, per the
collision guards in §2.1/§4.1).

```
api/v1/gateway/chat.py::create_chat_completion   (chat.py — capability=chat)
api/v1/gateway/embeddings.py::create_embeddings  (embeddings.py — capability=embeddings)
        │
        route = resolve_route(
            body.model,
            self_hosted_cache=self_hosted_cache,   # chat.py only, unchanged (5.5)
            custom_model_cache=custom_model_cache, # NEW — BOTH chat.py and embeddings.py
        )
                    │
                    ├─ 1. resolve_model(model)  (static — unconditionally first, unchanged)
                    │
                    ├─ 2. on UnknownModelError, if custom_model_cache is not None:
                    │       entry = custom_model_cache.get(model)   # O(1), zero I/O
                    │       if entry is not None:
                    │           return ModelRoute(
                    │               provider=entry.provider,             # REAL BYOK value
                    │               capability=entry.capability,         # row's own capability —
                    │                                                     # NOT hardcoded CHAT (this
                    │                                                     # is what lets a single
                    │                                                     # resolve_route() correctly
                    │                                                     # serve both chat.py AND
                    │                                                     # embeddings.py call sites)
                    │               native_model_id=entry.native_model_id,
                    │               custom_model_id=entry.id,
                    │           )
                    │
                    ├─ 3. on UnknownModelError, if self_hosted_cache is not None:
                    │       (unchanged 5.5 fallback — chat.py only)
                    │
                    └─ 4. raise ModelNotFoundError(...)
```

**Why the existing capability check needs zero changes.** `chat.py` already
has `if route.capability != ModelCapability.CHAT: raise
HttpUnsupportedRequestError(...)`; `embeddings.py` has the identical check
for `EMBEDDINGS`. Because the custom-model branch above sets
`capability=entry.capability` (the row's real, admin-declared capability,
not a hardcoded value), a `chat`-capability custom model requested via
`/v1/embeddings` is rejected by this **existing** check exactly like any
static chat-only model would be — no new capability-enforcement code
anywhere. This is what makes the "chat OR embeddings, admin's choice"
requirement (spec §2) implementable without a parallel enforcement
mechanism.

**Why `completions.py` is deliberately never touched** (mirrors 5.5's
`chat.py`-only wiring exactly, just for a different, spec-mandated reason):
spec §7 explicitly non-goals `/v1/completions` support beyond whatever the
static registry already does. `completions.py::resolve_route(body.model)`
keeps calling with **no** `custom_model_cache` argument at all — the same
structural (call-site-level, not a runtime check) enforcement mechanism
5.5 already established for "self-hosted is chat only."

**Call-site hazard, flagged explicitly for backend-developer**: `chat.py`'s
existing call site is `resolve_route(body.model, self_hosted_cache)` —
**positional**. Adding `custom_model_cache` as a new parameter *after*
`self_hosted_cache` is safe for that positional call (`self_hosted_cache`
still binds correctly, `custom_model_cache` defaults to `None`), but any
new/edited call site in this feature's implementation **must** use keyword
arguments (`resolve_route(body.model, self_hosted_cache=..., custom_model_cache=...)`)
to avoid an easy-to-miss positional-argument mixup — this is a mandatory
wiring-checklist item (§5), not a style preference: get the parameter order
wrong here and a request silently resolves against the wrong cache with no
type error.

### 2.3 Data Flow: Verification

```
Org Admin, custom model row (verified=false)
        │
        v
POST /v1/admin/custom-models/{id}/verify   (require_role("org_admin"))
        │
        └─ services.custom_models.verify_custom_model(session, id, *, key_provider,
                                                         http_client, vertex_token_cache)
              │
              ├─ 1. row = get_custom_model_by_id(...)  -> 404 if absent
              ├─ 2. credential = services.proxy_keys.get_decrypted_provider_credential(
              │        session, row.provider, key_provider=key_provider)
              │        # THE EXISTING BYOK FETCH — same function every gateway
              │        # request already calls. Raises ProviderKeyNotConfiguredError
              │        # if no provider_keys row exists yet for row.provider —
              │        # translated to errors.ProviderNotConfiguredError (404),
              │        # identical shape to a real gateway request's failure mode.
              ├─ 3. dispatch on (row.provider, row.capability):
              │        ("openai", chat)        -> providers.openai.create_chat_completion
              │        ("openai", embeddings)  -> providers.openai.create_embeddings
              │        ("anthropic", chat)     -> providers.anthropic.create_chat_completion
              │        ("anthropic", embeddings) -> UNREACHABLE — guard #6 (§2.1/§2.5)
              │                                      blocks this combination at write time
              │        ("vertex_ai", chat)     -> providers.vertex_ai.create_chat_completion
              │                                    (needs vertex_token_cache — §2.5)
              │        ("vertex_ai", embeddings) -> providers.vertex_ai.create_embeddings
              │                                    (needs vertex_token_cache — §2.5)
              │        ("openrouter", chat)    -> providers.openrouter.create_chat_completion
              │        ("openrouter", embeddings) -> UNREACHABLE — guard #6
              ├─ 4. one minimal call: chat = one fixed one-token-ish prompt,
              │      max_tokens capped small; embeddings = one fixed short string.
              │      NEVER calls check_model_policy/check_residency/run_dlp_scan/
              │      check_budget_available/record_usage_charge — synthetic,
              │      admin-triggered, non-user content, mirrors 5.4's canary
              │      calls' bypass of the same gateway-pipeline steps.
              ├─ 5. on success: row.verified = true, commit.
              │    on ProviderCallError: row.verified stays/reverts false, commit,
              │      and the SPECIFIC provider error message is returned to the
              │      admin verbatim (never swallowed) — HTTP 502-shaped response
              │      body carrying the real upstream error text.
              └─ 6. write_audit_entry(..., action="custom_model.test_call",
                     new_value={"success": ..., "latency_ms": ...}) — NO usage_logs
                     row, NO budget/spend touched (mirrors AC5.4.9's canary-cost
                     principle, scoped to one manual action).
```

**Per-row cooldown** (spec §5/§9 item 2, confirmed in scope, not cut):
`verify_custom_model()` rejects with 429 if the row's most recent
`custom_model.test_call` audit entry (or a lightweight in-memory
last-verified-at marker on the row/cache — see §5 for the exact mechanism
chosen) is less than 30 seconds old. This is a defensive cost/abuse guard,
not a product requirement — implemented as a simple timestamp check, not a
new table.

### 2.4 Data Flow: Shadowing Detection

Three complementary, independent mechanisms, per spec §4.2/§12 (approved as
designed, not open):

**(a) Startup log** (loud warning, never a `RuntimeError` — a code upgrade
must never brick a running gateway):

```
main.py::_lifespan, immediately after CustomModelRouteCache is warmed
        │
        └─ NEW _log_custom_model_shadowing(app) helper:
              snapshot_keys = app.state.custom_model_route_cache._snapshot.keys()
              # (or a dedicated CustomModelRouteCache.known_model_ids() call —
              # mirrors SelfHostedModelRouteCache's existing method)
              colliding = snapshot_keys & MODEL_REGISTRY.keys()
              for name in sorted(colliding):
                  entry = app.state.custom_model_route_cache.get(name)
                  logger.error(
                      "custom_model_shadowed_by_static_registry",
                      extra={"model": name, "custom_model_id": str(entry.id)},
                  )
```

**(b) Live per-row badge, computed at response-build time, zero I/O, never
persisted**:

```
GET /v1/admin/custom-models
        │
        └─ for each row: shadowed_by_registry = row.name in MODEL_REGISTRY
           # a plain dict `in` check against the CURRENTLY RUNNING process's
           # MODEL_REGISTRY — always reflects the running code, never a
           # stale flag a migration or admin action could leave wrong.
```

**(c) No auto-remediation.** `resolve_route()`'s existing "static always
wins, tried first, unconditionally" ordering (§2.2, unchanged from 5.5's
precedent) is the *entire* enforcement mechanism — a shadowed custom
model's row still exists, still shows `verified=true`, but every gateway
request for its name now resolves to the static entry. Gatekey never
renames or deletes the row automatically.

### 2.5 Two Corrections to the Product Spec, Found by Re-Grepping Real Code

**(a) Embeddings capability is only real for two of the four BYOK
providers — a write-time guard the spec's §2/§4.1 does not mention.**

`embeddings.py`'s actual provider dispatch (`_call()` closure) is:
```python
if route.provider == "openai": return await openai_provider.create_embeddings(...)
if route.provider == "vertex_ai": return await vertex_provider.create_embeddings(...)
raise HttpUnsupportedRequestError(f"Provider '{route.provider}' does not support embeddings in this phase.")
```
with a comment stating this fallthrough is "unreachable in practice ... no
Anthropic model is ever registered with EMBEDDINGS capability, and the
registry only knows these three providers." `providers/anthropic.py` and
`providers/openrouter.py` have **no `create_embeddings` function at all** —
confirmed by direct inspection, not inferred. Without a write-time guard,
an admin could register a `capability=embeddings` custom model with
`provider="anthropic"` or `provider="openrouter"` (the DB schema's
4-provider `CHECK` alone would permit it) — it would pass `resolve_route()`
and the capability check fine, then hit this `else` branch and 422 on
*every* request, a confusing, always-broken state discoverable only by
trying it. **New guard #6** (§2.1/§4.1): `capability == "embeddings"` is
only valid for `provider in ("openai", "vertex_ai")` — rejected 422 at
write time with a message naming which two providers support embeddings.
This keeps `embeddings.py`'s existing "unreachable in practice" comment
**actually true** once custom models exist, rather than silently becoming
false.

**(b) Vertex AI's chat/embeddings clients require a shared,
process-long-lived `VertexAITokenCache` the verify endpoint must thread
through.** `providers/vertex_ai.py::create_chat_completion`/
`create_embeddings` both take a mandatory `token_cache: VertexAITokenCache`
parameter (OAuth token exchange/caching) — not optional, not defaultable.
`api/v1/admin/custom_models.py`'s verify handler must depend on
`Depends(get_vertex_token_cache)` (the same dependency every gateway route
handler already uses) and thread it into `verify_custom_model()`, only
actually used when `row.provider == "vertex_ai"`. Missing this is a
`TypeError` at verify time for any Vertex AI custom model, not a subtle
bug — but easy to miss when mirroring `reverify_self_hosted_provider()`
(which never needed this, since `OllamaValidator` has no OAuth step).

### 2.6 Deliberate Non-Decision: No `usage_logs.custom_model_id` FK

Unlike self-hosted (which added `usage_logs.self_hosted_provider_id`
because `route.provider == "self_hosted"` is a synthetic value carrying no
information about *which* endpoint served the request), a custom model's
`usage_logs.provider` and `usage_logs.model` columns already fully capture
what happened — `provider` is the real BYOK value, `model` is the unique
gateway-facing custom-model name. **This design deliberately does not add
a `custom_models` FK to `usage_logs`** — the product spec's own §8
checklist doesn't ask for one, and no per-custom-model usage-breakdown
endpoint (the self-hosted precedent's `GET
/v1/admin/self-hosted-providers/{id}/usage`) is in scope for v1 (an admin
can already filter existing usage views by the `model` string, which is
unique per org). **Flagged, not silently dropped** (§11 Known Limitations):
if a custom model is removed and a *different* custom model is later
registered reusing the same `name`, historical `usage_logs` rows for that
name become ambiguous as to which pricing/native-id configuration produced
them — the charged `cost_usd` figure itself is still correct (it was
computed and stored at charge time), only the "which admin-entered config
was this" traceability is lost. Low severity, real, worth one sentence in
the admin docs.

---

## 3. API Contracts

### 3.1 New Endpoints (Admin Console)

| Endpoint | Method | Description | RBAC |
|----------|--------|-------------|------|
| `/v1/admin/custom-models` | GET | List, includes per-row `shadowed_by_registry` (§2.4) | `require_admin_or_auditor` |
| `/v1/admin/custom-models` | POST | Register (§2.1) | `require_role("org_admin")` |
| `/v1/admin/custom-models/{id}` | PUT | Edit (§2.1) | `require_role("org_admin")` |
| `/v1/admin/custom-models/{id}` | DELETE | Remove, hard delete (§2.1) | `require_role("org_admin")` |
| `/v1/admin/custom-models/{id}/verify` | POST | One live test call (§2.3), 30s per-row cooldown | `require_role("org_admin")` |

### 3.2 Extended Existing Endpoints

#### `/v1/admin/model-policy` (PUT) and `/v1/admin/teams/{id}/model-policy` (PUT)
No request-shape change — `models: list[str]` now additionally accepts any
verified custom-model name (validated against the widened
`CustomModelRouteCache.known_model_ids()` union, §2.2/§5), alongside the
existing static + self-hosted sources.

### 3.3 Request/Response Bodies (`schemas/custom_model.py`)

```python
class CustomModelCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    provider: Literal["openai", "anthropic", "vertex_ai", "openrouter"]
    native_model_id: str = Field(min_length=1, max_length=256)
    capability: Literal["chat", "embeddings"]
    input_price_per_million_usd: Decimal = Field(gt=0)
    output_price_per_million_usd: Decimal | None = Field(default=None, gt=0)
    pricing_source: str | None = Field(default=None, max_length=2048)

class CustomModelUpdateRequest(BaseModel):
    # every field optional — omitted means "leave unchanged", identical
    # discipline to SelfHostedProviderUpdateRequest
    name: str | None = ...
    provider: Literal["openai", "anthropic", "vertex_ai", "openrouter"] | None = None
    native_model_id: str | None = ...
    capability: Literal["chat", "embeddings"] | None = None
    input_price_per_million_usd: Decimal | None = Field(default=None, gt=0)
    output_price_per_million_usd: Decimal | None = Field(default=None, gt=0)
    pricing_source: str | None = None

class CustomModelResponse(BaseModel):
    id: uuid.UUID
    name: str
    provider: str
    native_model_id: str
    capability: str
    input_price_per_million_usd: Decimal
    output_price_per_million_usd: Decimal | None
    pricing_source: str | None
    pricing_as_of: date
    verified: bool
    shadowed_by_registry: bool   # computed, never a DB column (§2.4)
    created_at: datetime
    updated_at: datetime
```

No secret-bearing field exists anywhere on this model — a real
simplification versus `SelfHostedProviderResponse`, per spec §8. Bounds
mirror `schemas/self_hosted_provider.py`'s "generous sanity bounds only,
not format-specific validation" discipline exactly.

---

## 4. Data Model Changes

### 4.1 New Table: `custom_models` (migration `0044`)

```sql
CREATE TABLE custom_models (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    provider TEXT NOT NULL,
    native_model_id TEXT NOT NULL,
    capability TEXT NOT NULL,
    input_price_per_million_usd NUMERIC(12,6) NOT NULL,
    output_price_per_million_usd NUMERIC(12,6) NULL,
    pricing_source TEXT NULL,
    pricing_as_of DATE NOT NULL,
    verified BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_custom_models_org_id_name UNIQUE (org_id, name),
    CONSTRAINT chk_custom_models_provider
        CHECK (provider IN ('openai', 'anthropic', 'vertex_ai', 'openrouter')),
    CONSTRAINT chk_custom_models_capability
        CHECK (capability IN ('chat', 'embeddings')),
    CONSTRAINT chk_custom_models_input_price_positive
        CHECK (input_price_per_million_usd > 0),
    CONSTRAINT chk_custom_models_output_price_positive
        CHECK (output_price_per_million_usd IS NULL OR output_price_per_million_usd > 0),
    -- Defense-in-depth backstop mirroring the app-layer completeness guard
    -- (§2.1 guard #4) — same "pair a business rule with a DB-level sanity
    -- bound" convention Phase 5 established (chk_chain_purge_mutually_exclusive).
    CONSTRAINT chk_custom_models_capability_output_price CHECK (
        (capability = 'chat' AND output_price_per_million_usd IS NOT NULL) OR
        (capability = 'embeddings' AND output_price_per_million_usd IS NULL)
    )
);
CREATE INDEX ix_custom_models_org_id ON custom_models (org_id);
```

`provider` is a plain `TEXT` + `CHECK`, **not** `provider_name_enum`
(confirmed: that Postgres enum type includes `'ollama'`, which this table
must exclude — reusing it would require either a partial-values migration
or an app-layer-only exclusion of a value the column itself still permits;
a fresh `CHECK` is simpler and matches spec §8's own explicit guidance).
`capability` is plain `TEXT` + `CHECK` rather than importing
`model_registry.ModelCapability` as a Postgres enum — mirrors this table's
own "no dependency on a type owned by a different bounded module" posture;
the ORM model maps it to the existing `ModelCapability` Python enum at the
application layer only (`Mapped[ModelCapability]` with a string-backed
column), the same pattern `self_hosted_providers` avoided needing at all
(it stores raw model-id strings, no capability column).

`pricing_as_of DATE NOT NULL` has no application-supplied default in the
`INSERT`/`UPDATE` statements — `services/custom_models.py` always sets it
explicitly to `date.today()` server-side (spec §2: "server-set, not
admin-entered" — deliberately not a DB `server_default` either, since it
must be re-set on every pricing edit, not just at row creation).

### 4.2 ORM Model: `db/models/custom_model.py`

New `CustomModel` class, `__tablename__ = "custom_models"`, mirroring
`db/models/self_hosted_provider.py`'s docstring conventions (migration
ownership note, no plaintext-secret disclaimer since — unlike that
model — there genuinely is no secret column here at all).

### 4.3 Migration Sequencing

| # | Content | Depends on |
|---|---------|------------|
| `0044` | `custom_models` table (all constraints above) | `0043` (current head) |

Single migration, no cross-feature dependency — database-admin's task here
has no internal ordering to manage, unlike Phase 5's six-migration set.

---

## 5. Integration Points — Mandatory Wiring Checklist

**Per the project's Phase 4 post-mortem and Phase 5's fix**: every row below
names the exact existing file/function that must import or call new code.
A task is not "done" until its row is checked off — a service module that
exists but whose row here is unimplemented is the exact Phase 4 failure
mode this checklist exists to prevent.

| # | Wiring | Exact location |
|---|--------|-----------------|
| 1 | `ModelRoute` gains `custom_model_id` field | `providers/model_registry.py::ModelRoute` — new `custom_model_id: uuid.UUID \| None = None` field, parallel to the existing `self_hosted_provider_id` (§2.2) |
| 2 | `CustomModelRouteCache` + loader | New `services/custom_models.py::CustomModelRouteCache` (whole-snapshot-replace, `get()`/`known_model_ids()`/`set_all()` — identical shape to `SelfHostedModelRouteCache`) and `load_custom_model_route_snapshot(session)` (queries `WHERE org_id = DEFAULT_ORG_ID AND verified = true` — the sole place `verified` is checked; the cache-membership rule is the enforcement mechanism, not a second runtime flag read) |
| 3 | Cache constructed + warmed at startup | `main.py::_lifespan` — `app.state.custom_model_route_cache = CustomModelRouteCache()` constructed empty; new `_warm_custom_model_route_cache(app)` helper (identical fail-open/bounded-timeout contract to `_warm_self_hosted_model_route_cache`) called **immediately after** it, in the same block as line ~476's self-hosted warm call |
| 4 | Shadowing startup log | `main.py::_lifespan` — new `_log_custom_model_shadowing(app)` helper called **immediately after** step 3's warm call succeeds (§2.4a); never raises, logs at `ERROR` per colliding name |
| 5 | Cache fetched via a new `api/deps.py` dependency | `api/deps.py::get_custom_model_route_cache(request) -> CustomModelRouteCache` — one-line shape identical to `get_self_hosted_model_route_cache` |
| 6 | `resolve_route()` extended | `api/v1/gateway/common.py::resolve_route` — new `custom_model_cache: CustomModelRouteCache \| None = None` parameter; fallback logic per §2.2's pseudocode, checked after static, before/around self-hosted (order documented as immaterial per spec §3, implemented custom-then-self-hosted) |
| 7 | `chat.py` passes the cache — **keyword args, not positional** | `api/v1/gateway/chat.py::create_chat_completion` — add `custom_model_cache: CustomModelRouteCache = Depends(get_custom_model_route_cache)` to the handler signature; **update the existing call site** to `resolve_route(body.model, self_hosted_cache=self_hosted_cache, custom_model_cache=custom_model_cache)` (§2.2's call-site hazard — do not leave it positional) |
| 8 | `embeddings.py` passes the cache — **new wiring, self-hosted never needed this** | `api/v1/gateway/embeddings.py::create_embeddings` — add `custom_model_cache: CustomModelRouteCache = Depends(get_custom_model_route_cache)` to the handler signature; change `route = resolve_route(body.model)` to `route = resolve_route(body.model, custom_model_cache=custom_model_cache)`. **Do not** add a `self_hosted_cache` parameter here — self-hosted stays chat-only (AC5.5.4 untouched) |
| 9 | `completions.py` — explicitly NOT touched | `api/v1/gateway/completions.py::create_completion` — `resolve_route(body.model)` stays exactly as-is, no cache argument added, ever (structural enforcement of spec §7's non-goal) |
| 10 | Cost computation — chat path (both streaming and non-streaming) | `api/v1/gateway/chat.py` — mirror the existing `self_hosted_route_entry`/`precomputed_cost_usd` pattern (currently keyed on `effective_route.provider == "self_hosted"`) with a **new, parallel** branch keyed on `effective_route.custom_model_id is not None` (never `route.provider`, per §2.2) calling `compute_custom_model_cost()` instead of `compute_self_hosted_cost()`. Both branches can coexist in the same `if`/`elif` chain since the two are mutually exclusive by construction |
| 11 | Cost computation — embeddings path | `api/v1/gateway/embeddings.py::create_embeddings` — **new** wiring (embeddings never had a self-hosted precomputed-cost branch to mirror, since self-hosted is chat-only): compute `custom_model_route_entry = custom_model_cache.get(body.model) if route.custom_model_id is not None else None` before the `record_usage_charge(...)` call, and pass `precomputed_cost_usd=compute_custom_model_cost(custom_model_route_entry, prompt_tokens=..., completion_tokens=None)` when set |
| 12 | `compute_custom_model_cost()` | New `services/custom_models.py::compute_custom_model_cost(entry: CustomModelCacheEntry, *, prompt_tokens: int, completion_tokens: int \| None) -> Decimal` — same per-token formula `providers.pricing.compute_cost()` uses (`input_price * prompt_tokens / 1e6 [+ output_price * completion_tokens / 1e6]`), **not** the self-hosted GPU-hour proxy. Placed in `services/custom_models.py` (not `providers/pricing.py`) per spec §8's explicit instruction — operates on a services-layer `CustomModelCacheEntry`, not a `providers`-layer `PricingEntry` |
| 13 | Model-policy validation widened | `services/model_policy.py::set_policy`/`set_team_model_policy` — new `custom_model_cache: CustomModelRouteCache \| None = None` parameter (third source, alongside the existing `self_hosted_cache`), widened `known_models = MODEL_REGISTRY.keys() \| self_hosted_cache.known_model_ids() \| custom_model_cache.known_model_ids()` (each term conditional on its cache being non-`None`, preserving byte-for-byte pre-feature behavior when omitted) |
| 14 | **Wire** the widened validation into its two real call sites | `api/v1/admin/model_policy.py` (~line 75, `set_policy(session, payload.mode, payload.models, self_hosted_cache=self_hosted_cache, ...)`) and `api/v1/teams.py` (~line 580, `set_team_model_policy(session, team_id, payload.models, cache=cache, self_hosted_cache=self_hosted_cache, ...)`) — both handlers gain `custom_model_cache: CustomModelRouteCache = Depends(get_custom_model_route_cache)` and thread it through |
| 15 | Bidirectional collision guard — self-hosted side | `services/self_hosted_providers.py::_validate_model_ids()` — add a third check: query `db.models.custom_model.CustomModel.name` directly (import the **ORM model**, not `services/custom_models.py`, to avoid a service-module circular import — see note below) for this org; reject with a new/reused 422 if any requested self-hosted model id collides with a custom model's name |
| 16 | Bidirectional collision guard — custom-model side | `services/custom_models.py`'s own write-time validation — query `db.models.self_hosted_provider.SelfHostedProvider.models` directly (same ORM-import-only rule, mirrored) for this org; reject 422 if the requested `name` collides with any self-hosted provider's declared model id |
| 17 | Embeddings-provider write-time guard (§2.5a) | `services/custom_models.py`'s validation — reject 422 if `capability == "embeddings"` and `provider not in ("openai", "vertex_ai")` |
| 18 | Verification | New `services/custom_models.py::verify_custom_model(session, id, *, key_provider, http_client, vertex_token_cache) -> CustomModel` — dispatch table per §2.3, using `services.proxy_keys.get_decrypted_provider_credential()` (never a new credential path) and the **existing** per-provider client modules (`providers/openai.py`, `providers/anthropic.py`, `providers/vertex_ai.py`, `providers/openrouter.py`) unmodified |
| 19 | Admin router + verify handler threads `vertex_token_cache` | `api/v1/admin/custom_models.py`'s verify endpoint — `Depends(get_provider_http_client)` and `Depends(get_vertex_token_cache)` (§2.5b — required even though most rows won't be Vertex AI; the dependency itself is cheap, already process-shared) |
| 20 | Admin router registered | `main.py` — `from gatekey.api.v1.admin.custom_models import router as admin_custom_models_router`; `app.include_router(admin_custom_models_router)`, alongside `admin_self_hosted_providers_router` (~line 689) |
| 21 | Cache invalidated on every admin write | `api/v1/admin/custom_models.py` — each of the four mutating handlers (register/edit/remove/verify) re-derives the full mapping via `load_custom_model_route_snapshot()` and calls `cache.set_all(...)` **after** its own commit — identical convention to `api/v1/admin/self_hosted_providers.py`'s `_refresh_cache()` helper |
| 22 | `shadowed_by_registry` computed in the list/response builder | `api/v1/admin/custom_models.py::_to_response()` — `shadowed_by_registry = row.name in MODEL_REGISTRY` computed inline, never a DB column, never cached (§2.4b) |
| 23 | Audit action vocabulary extended | `services/audit.py` module docstring's fixed action list — add `custom_model.register`, `custom_model.update`, `custom_model.remove`, `custom_model.test_call` (mirrors the `self_hosted_provider.*` additions Phase 5.5 made) |
| 24 | Admin UI: Providers screen card | `frontend/app/providers/page.tsx` — new `CustomModelsCard`, sibling to and placed adjacent to the existing `SelfHostedModelsCard` (~line 817), same register/edit/remove/verify pattern; shadowed rows render the "Shadowed by an updated Gatekey model registry — rename or remove" badge, same visual language as the existing "Not verified" badge |
| 25 | Admin UI: Model Policy checklist | `frontend/app/model-policy/page.tsx` — new `"Custom"` group in the provider-grouped checklist, sourced from a new `listCustomModels()` call, reusing the existing self-hosted group's verified/unverified-disabled-checkbox rendering logic (~line 195-227) unchanged in shape |
| 26 | Frontend API client | `frontend/src/lib/api.ts` — `listCustomModels`, `registerCustomModel`, `editCustomModel`, `removeCustomModel`, `verifyCustomModel`, `CustomModelResponse` type — mirrors the existing `*SelfHostedProvider*` functions (~line 2220-2282) exactly in shape |

**Note on the ORM-import-only rule (rows 15/16)**: `services/self_hosted_providers.py`
and `services/custom_models.py` each need to query the *other* feature's
table for the bidirectional collision guard. Importing the other's
*service module* in both directions would create a circular import
(`self_hosted_providers.py` imports from `custom_models.py`, which imports
from `self_hosted_providers.py`). The existing `_validate_model_ids()`
already establishes the correct pattern: it queries the `SelfHostedProvider`
ORM class directly, not through a service function. Both new checks must
follow this same discipline — import `db.models.custom_model.CustomModel`
and `db.models.self_hosted_provider.SelfHostedProvider` (the ORM classes),
never each other's `services/*.py` module.

---

## 6. Deployment Considerations

### 6.1 No new infrastructure dependency

Zero new external services, zero new environment variables. This feature
runs entirely against the existing Postgres + in-process-cache stack and
the four already-integrated BYOK provider HTTP clients.

### 6.2 Verify-call cooldown constant

`_CUSTOM_MODEL_VERIFY_COOLDOWN_SECONDS = 30` — a module constant in
`services/custom_models.py`, mirroring the existing convention of
hardcoded, non-`Settings`-configurable scheduler/cooldown constants
elsewhere in this codebase (e.g. `PROVIDER_KEY_HEALTH_CHECK_INTERVAL_SECONDS`).

### 6.3 Startup shadowing log is a one-time, per-process check

Runs once at process startup (alongside the cache warm), not on a
recurring schedule — a new custom model registered *after* startup that
happens to collide with an already-loaded static registry key is instead
caught by the write-time guard #1 (§2.1), which prevents that collision
from ever being created in the first place. The startup log's only real
job is catching the *inverse* order: an already-registered custom model,
then a **new Gatekey release** (deployed via restart) that adds a
colliding static key — exactly the shadowing scenario spec §4.2 describes,
which by definition can only be detected at the moment the new code starts
running.

---

## 7. Error Handling and Edge Cases

| Scenario | Handling |
|----------|----------|
| Custom model name collides with a static `MODEL_REGISTRY` key at registration | 422, rejected before any DB write (guard #1) |
| Custom model name collides with a self-hosted model id (either direction) | 422, specific error naming the conflicting source (guards #2/#15/#16) |
| `capability="embeddings"` with `provider` = `anthropic`/`openrouter` | 422 at write time (guard #6/#17) — never reaches a runtime 422 loop on every request |
| `provider="ollama"` | 422, message pointing at Providers → Self-Hosted Models instead |
| Verify called with no `provider_keys` row for the target provider | `ProviderNotConfiguredError` (404), identical shape to a real gateway request |
| Verify called against a real but wrong `native_model_id` | `ProviderCallError` from the real provider, surfaced verbatim to the admin; `verified` stays/reverts `false` |
| Verify called twice within 30s for the same row | 429, cooldown message |
| Unverified custom model requested at the gateway | `ModelNotFoundError` (404) — cache membership rule, same as any unknown model |
| Custom model requested at `/v1/completions` | `ModelNotFoundError` (404) — structurally enforced, `completions.py` never passes the cache (row 9) |
| Chat-capability custom model requested at `/v1/embeddings` (or vice versa) | `UnsupportedRequestError` (422) — the existing capability check fires unchanged, because `resolve_route()`'s custom-model branch sets the row's real `capability`, not a hardcoded value (§2.2) |
| A future Gatekey release adds a static key colliding with an already-registered, already-verified custom model | Static wins at request time (unchanged ordering); `ERROR`-level startup log names the org + custom model id; `GET /v1/admin/custom-models` shows `shadowed_by_registry: true` on the next admin-console load; no auto-remediation (§2.4) |
| Custom model removed, then a different custom model later registered with the same `name` | Both allowed (no versioning workflow, per spec §7); historical `usage_logs` rows for that name are not mis-charged (cost was fixed at charge time) but are not distinguishably attributable to "which config produced this row" (§2.6, flagged, low severity) |

---

## 8. Security Considerations

| Concern | Mitigation |
|---------|------------|
| No new credential storage | This feature stores no secret of any kind — it rides the existing, already-encrypted `provider_keys` row via the identical `get_decrypted_provider_credential()` every gateway request already calls. No new AES-GCM envelope, no new AAD binding to get wrong. |
| $0 / near-$0 pricing bypassing budget enforcement | Hard-blocked at the DB level (`CHECK > 0` on both price columns, per §12's resolved decision) **and** at the app layer — defense in depth, matching this codebase's established convention |
| Verify-call cost/abuse | 30s per-row cooldown (§6.2); never writes a `usage_logs` row or touches budget (mirrors AC5.4.9's canary-cost isolation) |
| **Shadowing — flagged by the product spec as the single highest-severity new concern this feature introduces, and this design treats it as such** | See §2.4/§7. The mitigation is detection-and-disclosure (startup log + live badge), never automatic — reviewed explicitly below in §8.1 as its own subsection given the severity |
| Verification error messages | Provider errors are surfaced verbatim to the Org Admin (by design, for diagnosability) — confirm at review time that `ProviderCallError.message`'s existing "never echo a raw provider response body" discipline (already enforced by `provider_call_error_from_response()`) is sufficient here too; this is the *same* message shape a real gateway 502 already exposes to any caller, not a new disclosure surface |
| Bidirectional collision guard correctness | Both directions must be independently tested (spec §10's explicit NFR) — a bug in only one direction re-opens exactly the shadowing-adjacent "silent reroute to the wrong config" risk this whole feature exists to prevent |
| RBAC | No new primitive — `require_role("org_admin")` for all writes/verify, `require_admin_or_auditor` for reads, identical shape to every `self_hosted_providers` endpoint; Team Lead/Member get zero access to `/v1/admin/custom-models*`, and never see "custom model" as a concept anywhere (spec §6) |

### 8.1 Security-Reviewer Mandatory Flag List

The following must each get an explicit pass/fail line in the security
review, not be folded into a general "looks fine":

1. **Shadowing (highest severity, per spec §9 item 1 and this design's
   §2.4).** Verify: (a) the startup log actually fires and names the
   correct org + custom-model id for a deliberately-collided test fixture;
   (b) `shadowed_by_registry` in the `GET` response is computed fresh per
   request (not cached, not stale after a hot-reload-free process restart
   with a different `MODEL_REGISTRY`); (c) a live gateway request for a
   shadowed name actually routes to the static entry, not the custom one —
   this is the spec's own §10 acceptance test; (d) confirm no code path
   auto-renames or auto-deletes a shadowed row under any circumstance.
2. **Bidirectional collision guard, both directions, independently.**
   Verify a self-hosted registration is rejected when it collides with an
   existing custom model's name, AND a custom-model registration is
   rejected when it collides with an existing self-hosted model id — test
   both, not just one and assume symmetry.
3. **`resolve_route()`'s call-site keyword-argument hazard (§2.2/§5 row
   7).** Confirm `chat.py`'s updated call site uses keyword arguments, not
   a positional slot that could silently bind `custom_model_cache` to the
   wrong parameter.
4. **`ModelRoute.custom_model_id` is the sole cost-computation
   discriminator (§2.2), never `route.provider`.** Confirm every new
   cost-branch in `chat.py`/`embeddings.py` tests `custom_model_id is not
   None`, not a provider-string comparison — a provider-string-based check
   would silently misfire against a static route to the same provider.
5. **Embeddings-provider guard (§2.5a) is enforced at write time, not just
   assumed.** Register a `capability=embeddings` custom model with
   `provider=anthropic` and confirm it is rejected 422, never silently
   accepted and left permanently broken.
6. **Verify endpoint never charges budget or writes `usage_logs`.**
   Directly test: real (mocked-HTTP) verify call fires, `usage_logs` row
   count and `current_spend_usd` are unchanged before/after — same
   assertion shape Phase 5's canary-cost-isolation test already
   establishes as this codebase's standard for this class of claim.
7. **No plaintext credential anywhere in this feature's code path.**
   Confirm `verify_custom_model()`'s error handling never logs or returns
   the decrypted credential object itself (only `ProviderCredential`'s
   already-redacted `__repr__`/`__str__` should ever be at risk, but
   confirm no f-string interpolates the raw token directly).
8. **RBAC boundary.** Confirm a non-org-admin session cannot reach any
   `/v1/admin/custom-models*` write endpoint, and that a Team Lead/Member
   session gets no visibility into a `verified=false` or shadowed row via
   any indirect surface (e.g. Model Policy's checklist must never leak an
   unverified custom model's existence to a non-admin).

---

## 9. Testing Strategy

### 9.1 Integration Test Scenarios (P0 unless noted)

| Test Scenario | Type |
|---------------|------|
| Register a custom model reusing a static `MODEL_REGISTRY` key name → 422, no DB write | Integration |
| Register a custom model, do not verify, gateway request for its name → 404 | Integration |
| Register + verify a custom model, gateway chat request → real provider call, correct `usage_logs.cost_usd` matching `compute_custom_model_cost()` exactly | Integration |
| Register + verify a custom `capability=embeddings` model on `openai` or `vertex_ai`, gateway `/v1/embeddings` request → succeeds, correct cost | Integration |
| Register `capability=embeddings` on `anthropic`/`openrouter` → 422 at write time | Integration |
| Verify with no `provider_keys` row configured for the target provider → `ProviderNotConfiguredError` (404), same shape as a real gateway request | Integration |
| Custom model requested at `/v1/completions` → 404, never routes | Integration |
| Chat-capability custom model requested at `/v1/embeddings` (and vice versa) → 422 `UnsupportedRequestError` | Integration |
| Shadowing: register custom model `X`, simulate a static-registry update also defining `X` (test-only registry override), confirm (a) `GET /v1/admin/custom-models` reports `shadowed_by_registry: true`, (b) a live gateway request for `X` routes to the static entry | Integration — this is spec §10's explicit acceptance test |
| Collision bidirectionality: (a) custom-model name matching an existing self-hosted model id rejected; (b) self-hosted model id matching an existing custom-model name rejected; both surface a specific, source-naming error | Integration |
| `$0`/negative pricing rejected at both the app layer and the DB `CHECK` | Unit + Integration |
| Verify-call cooldown: second verify within 30s → 429 | Unit |
| Verify never writes `usage_logs`, never changes `current_spend_usd` | Integration |
| Model Policy PUT accepts a verified custom-model name; rejects an unverified one | Integration |
| `resolve_route()` unit: static > custom > self-hosted precedence, all three cache combinations | Unit |
| Full regression: every existing gateway integration test for Phases 1-5 passes unmodified for an org that never configures a custom model | Integration (regression gate) |

### 9.2 Mocking Strategy

| External Service | Mock Approach |
|-------------------|----------------|
| Verify-endpoint provider calls | Reuse the existing `respx`/`pytest-httpx` provider mocks from Phases 1/4/5 — one mock per (provider, capability) combination actually reachable per guard #6 |
| Vertex AI token exchange | Reuse the existing `VertexAITokenCache` test double already used by Phase 1/5 Vertex tests |

### 9.3 Regression Coverage

Every existing Phase 1-5 gateway integration test suite must be re-run
unmodified after this feature's `chat.py`/`embeddings.py`/`common.py`/
`model_policy.py`/`model_registry.py` changes and pass with byte-identical
behavior for any org that never registers a custom model — the explicit
regression gate for the `resolve_route()`/`ModelRoute`/`set_policy()`/
`set_team_model_policy()` signature changes documented in §2.2/§5.

---

## 10. Non-Compliance Risks

| Risk | Mitigation |
|------|------------|
| Shadowing silently reroutes traffic to a different provider/pricing config | §2.4/§8.1 item 1 — startup log + live badge + no auto-remediation; this is a detection, not prevention, mitigation, by design (spec §12 approved) |
| `resolve_route()`/`ModelRoute`/`record_usage_charge()` cost-branch changes silently break an existing call site | §9.3's explicit full-suite regression gate; every changed call site enumerated in §5 |
| Embeddings-provider guard forgotten, custom embeddings model registered on an unsupported provider | §2.5a/§8.1 item 5 — explicit write-time guard, explicit test |
| Verify endpoint becomes a real-money abuse vector via rapid repeated clicking | 30s per-row cooldown (§6.2) — a soft mitigation, not a hard rate limiter; flagged as informal per spec §9 item 2 |
| Circular import between `services/self_hosted_providers.py` and `services/custom_models.py` | Both query the *other* feature's ORM model class directly, never each other's service module (§5 note) |

---

## 11. Known Limitations

| Limitation | Reason | Future Phase |
|------------|--------|---------------|
| No `usage_logs.custom_model_id` FK — a removed-then-re-registered same-`name` custom model's historical usage rows are not distinguishably attributable | Not required by spec §8's checklist; no per-custom-model usage-breakdown endpoint requested for v1 | Fast-follow if a per-custom-model cost-audit view (self-hosted's `/usage` precedent) is requested |
| No auto-discovery from a provider's own list-models API | Explicit non-goal (spec §7), matches both the static registry's and 5.5's stated philosophy | Not planned — would be a different product direction |
| No org-vs-team scoping | Custom models are org-wide only, matches every other model-definition-shaped table | Not planned unless team-level model *definition* (not just access) is separately requested |
| No bulk import/CSV | One model at a time via the admin console form (spec §7) | Fast-follow if real demand |
| No tiered/threshold pricing | Flat input/output rate pair only, same simplification the static table already accepts for `gemini-2.5-pro` | Fast-follow |
| No scheduled re-verification | Manual, on-demand only — matches 5.5's identical choice for self-hosted endpoints | Fast-follow if requested |
| No price-staleness auto-detection | `pricing_source`/`pricing_as_of` make staleness human-auditable, not self-detecting | Not planned — matches `PRICING_TABLE`'s own documented caveat |
| No versioning/deprecation workflow | Hard delete only, matches self-hosted's identical remove behavior | Not planned |
| `validate_downgrade_target_model()` (Phase 4's graceful-degradation config-time validator) not extended | Not required by this feature's scope, same accepted gap self-hosted (5.5) left for the identical function | Natural fast-follow, could bundle with 5.5's own already-documented gap |

---

## 12. Implementation Tasks

### 12.1 Database Tasks (database-admin)

| Task | Priority | Dependencies |
|------|----------|--------------|
| Migration `0044`: `custom_models` table, all constraints (§4.1) | P0 | `0043` (current head) |
| ORM model `db/models/custom_model.py::CustomModel` | P0 | Migration `0044` |
| Test migration `upgrade()`/`downgrade()` against a real dev Postgres | P1 | Above |

### 12.2 Backend Tasks (backend-developer)

| Task | Priority | Dependencies |
|------|----------|--------------|
| `ModelRoute.custom_model_id` field (`providers/model_registry.py`) | P0 | — |
| `services/custom_models.py`: CRUD, write-time guards #1/#4/#6/#17, bidirectional guard (#16), `CustomModelRouteCache`, `load_custom_model_route_snapshot()`, `compute_custom_model_cost()`, `verify_custom_model()` | P0 | `0044`, ORM model |
| `services/self_hosted_providers.py::_validate_model_ids()` — add the third bidirectional guard against `custom_models.name` (§5 row 15) | P0 | `0044`, ORM model |
| Extend `resolve_route()` with `custom_model_cache` fallback (§2.2/§5 row 6) | P0 | `CustomModelRouteCache` |
| **Wire** `chat.py`'s call site to keyword args + new cache param (§5 rows 7/10) | P0 | Above |
| **Wire** `embeddings.py`'s call site + new cache param + cost branch (§5 rows 8/11) | P0 | Above |
| Confirm `completions.py` is genuinely untouched (§5 row 9) — explicit negative-check task, not a no-op to skip | P1 | — |
| Widen `set_policy`/`set_team_model_policy` (§5 row 13) | P0 | `CustomModelRouteCache` |
| **Wire** the two admin call sites (`api/v1/admin/model_policy.py`, `api/v1/teams.py`) (§5 row 14) | P0 | Above |
| `main.py`: construct + warm `CustomModelRouteCache`, startup shadowing log (§5 rows 3/4) | P0 | `load_custom_model_route_snapshot()` |
| `api/deps.py::get_custom_model_route_cache` (§5 row 5) | P0 | Cache construction |
| Admin router `api/v1/admin/custom_models.py`: all 5 endpoints, cache invalidation on every write, `shadowed_by_registry` computation (§5 rows 21/22), verify handler threading `vertex_token_cache`/`http_client` (§5 row 19) | P0 | All of the above |
| **Wire** admin router registration into `main.py` (§5 row 20) | P0 | Above |
| Schemas `schemas/custom_model.py` | P0 | — (can start in parallel with the service module) |
| Audit action vocabulary extension (§5 row 23) | P1 | — |
| Full regression suite pass (§9.3) before declaring this feature done | P0 | All above |

**Sequencing note (matches this project's established convention, per
memory: backend chunks proceed sequentially, not in parallel, when they
share files):** this feature touches `chat.py`, `embeddings.py`,
`common.py`, `model_policy.py`, `model_registry.py`, and `main.py` — the
same shared-file set Phase 5's own post-mortem flagged as a parallel-work
risk. Build in this order within backend-developer's single pass:
(1) `ModelRoute`/schema/ORM plumbing → (2) `services/custom_models.py`
(CRUD + cache + guards, no gateway wiring yet) → (3) `resolve_route()`
extension → (4) `chat.py`/`embeddings.py` wiring → (5) `model_policy.py`
widening + its two call sites → (6) `main.py` (cache warm, shadowing log,
router registration) → (7) admin router → (8) full regression run.

### 12.3 Frontend Tasks (frontend-developer)

| Task | Priority | Dependencies |
|------|----------|--------------|
| `frontend/src/lib/api.ts`: `listCustomModels`/`registerCustomModel`/`editCustomModel`/`removeCustomModel`/`verifyCustomModel`/`CustomModelResponse` (§5 row 26) | P0 | Backend API contract stable |
| Providers screen: `CustomModelsCard` (register/edit/remove/verify), shadowed-row badge (§5 row 24) | P0 | Above |
| Model Policy checklist: new "Custom" group (§5 row 25) | P0 | Above |

### 12.4 QA Tasks (qa-engineer)

| Task | Priority | Dependencies |
|------|----------|--------------|
| Independently re-verify every scenario in §9.1 against real tests (not agent self-report) | P0 | Backend + frontend complete |
| Write any missing tests for gaps found | P0 | Above |
| Specifically re-verify the two spec corrections found by this design (§2.5a embeddings-provider guard, §2.2 discriminator-field cost branching) with a dedicated test each, since both are easy to silently regress | P0 | Above |
| Confirm zero dead/unwired code — every new function/class in `services/custom_models.py` has a real caller (Phase 4 post-mortem discipline) | P0 | Above |

### 12.5 Security Review (security-reviewer, mandatory gate)

| Task | Priority | Dependencies |
|------|----------|--------------|
| Work through the §8.1 mandatory flag list item by item, pass/fail each | P0 | QA complete |
| Independently re-verify (not trust) QA's re-verification of the shadowing acceptance test and the bidirectional collision guard | P0 | Above |

---

## 13. Deployment Checklist

### Pre-Deployment
- [ ] Migration `0044` applied successfully
- [ ] No `custom_models` seed data required (empty table is the correct starting state)

### Post-Deployment
- [ ] Register a test custom model, confirm it appears `verified=false`
- [ ] Verify it (real provider call), confirm `verified=true`
- [ ] Send one gateway chat (or embeddings, per its capability) request, confirm `usage_logs.cost_usd` matches `compute_custom_model_cost()` exactly, `usage_logs.provider` is the real BYOK provider string
- [ ] Confirm the model appears in Model Policy's "Custom" group and is addable to the org baseline
- [ ] Confirm a Team Lead/Member session sees zero `/v1/admin/custom-models*` access and no unverified/shadowed row leaks into any surface they can see
- [ ] Register a custom model reusing a static registry name, confirm 422 rejection
- [ ] (If feasible in the deploy environment) simulate a shadowing collision and confirm the startup log line + live badge both fire correctly

---

## 14. Final Task List for Orchestrator (Sequential Handoff)

Matches this project's established sequential workflow (database-admin →
backend-developer → frontend-developer → qa-engineer → security-reviewer),
since backend work here touches the same shared-file set
(`chat.py`/`embeddings.py`/`common.py`/`model_policy.py`/`main.py`) that
made parallel dispatch risky in Phase 5.

| Task ID | Description | Agent | Dependencies |
|---------|-------------|-------|---------------|
| CMR-1 | Migration `0044`: create `custom_models` table with all constraints (§4.1); ORM model `db/models/custom_model.py`; verify upgrade/downgrade on real Postgres | database-admin | — (alembic head `0043`) |
| CMR-2 | `ModelRoute.custom_model_id` field; `schemas/custom_model.py`; `services/custom_models.py` CRUD + write-time guards (#1/#4/#6/#17) + bidirectional guard (#16) + `CustomModelRouteCache`/`load_custom_model_route_snapshot()`/`compute_custom_model_cost()`/`verify_custom_model()` | backend-developer | CMR-1 |
| CMR-3 | `services/self_hosted_providers.py::_validate_model_ids()` bidirectional guard extension (§5 row 15) | backend-developer | CMR-2 (needs `CustomModel` ORM class) |
| CMR-4 | `resolve_route()` extension in `common.py`; wire `chat.py` (keyword args) and `embeddings.py` (new wiring) call sites + cost branches (§5 rows 6-11) | backend-developer | CMR-2 |
| CMR-5 | Widen `set_policy`/`set_team_model_policy`; wire `api/v1/admin/model_policy.py` and `api/v1/teams.py` call sites (§5 rows 13-14) | backend-developer | CMR-2 |
| CMR-6 | `main.py`: cache construction/warm, startup shadowing log, admin router registration; `api/deps.py::get_custom_model_route_cache` (§5 rows 3-5, 20) | backend-developer | CMR-2 |
| CMR-7 | Admin router `api/v1/admin/custom_models.py`: all 5 endpoints, cache invalidation, `shadowed_by_registry`, verify handler (`vertex_token_cache`/`http_client` threading) | backend-developer | CMR-2, CMR-6 |
| CMR-8 | Audit action vocabulary extension; full backend regression suite run (§9.3) | backend-developer | CMR-3, CMR-4, CMR-5, CMR-7 |
| CMR-9 | `frontend/src/lib/api.ts` custom-model functions/types | frontend-developer | CMR-7 (API contract stable) |
| CMR-10 | Providers screen `CustomModelsCard` (register/edit/remove/verify, shadowed badge) | frontend-developer | CMR-9 |
| CMR-11 | Model Policy checklist "Custom" group | frontend-developer | CMR-9 |
| CMR-12 | QA: independently re-verify every §9.1 scenario, write tests for gaps, dedicated tests for the two spec corrections (§2.2, §2.5a), confirm zero dead code | qa-engineer | CMR-8, CMR-10, CMR-11 |
| CMR-13 | Security review: work the §8.1 mandatory flag list, independently re-verify shadowing + bidirectional-collision findings | security-reviewer | CMR-12 |
| CMR-14 | Fix any findings from CMR-12/CMR-13, re-run full regression suite, update docs/memory | backend-developer (+ relevant agent per finding) | CMR-13 |

---

*This design document is reference material for implementation. Questions
should be routed to the architect via the gatekey project repository.*
