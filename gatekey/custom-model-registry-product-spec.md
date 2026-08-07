---
title: Custom Model Registry (Admin-Managed BYOK Models) — Buildable Spec
status: draft
last_updated: 2026-08-06
source_docs:
  - 00-overview.md
  - phase-5-product-spec.md (structural template, and the §3/AC5.5.x
    self-hosted-governance precedent this spec deliberately mirrors)
  - backend/src/gatekey/providers/model_registry.py
  - backend/src/gatekey/providers/pricing.py
  - backend/src/gatekey/providers/registry.py
  - backend/src/gatekey/db/models/self_hosted_provider.py
  - backend/src/gatekey/services/self_hosted_providers.py
  - backend/src/gatekey/schemas/self_hosted_provider.py
  - backend/src/gatekey/api/v1/admin/self_hosted_providers.py
  - backend/src/gatekey/api/v1/gateway/common.py (resolve_route,
    record_usage_charge, call_self_hosted_provider)
  - backend/src/gatekey/services/budget.py (compute_cost, precomputed_cost_usd)
  - frontend/app/model-policy/page.tsx
  - frontend/app/providers/page.tsx
  - frontend/src/lib/api.ts
author: product-owner (sub-agent)
consumed_by: architect
---

# Custom Model Registry (Admin-Managed BYOK Models) — Buildable Spec

## 0. Problem Statement and Grounding

Today `providers/model_registry.py`'s `MODEL_REGISTRY` is a pure, in-memory,
hand-curated Python `dict` built at import time — 2 OpenAI chat models, 3
Anthropic, 2 Vertex AI chat + 1 embeddings, 3 Ollama, 1 OpenRouter. Its own
module docstring calls this "intentionally a small, hand-curated allowlist...
not a mirror of every model each provider exposes," and states `resolve_model()`
is "the *only* sanctioned way to look up a model" — no other call site may
read `MODEL_REGISTRY` directly. **This constraint is preserved unchanged by
this spec** — see §3.

`providers/pricing.py`'s `PRICING_TABLE` has a matching hard completeness
invariant (`_validate_completeness()`, enforced at import time): every
`MODEL_REGISTRY` key must have a `PricingEntry`, and every `CHAT` entry must
have a non-`None` output price. `services.budget.compute_cost()` raises
`PricingEntryMissingError` rather than ever charging `$0` for an unpriced
model. This is Gatekey's core value prop (accurate budget enforcement) and
this spec's design is built to extend that invariant into a new DB-backed
source, never to weaken it.

There is already a real, shipped precedent for "admin adds a model without a
code change": Phase 5.5's Unified Governance feature
(`self_hosted_providers` table, `services/self_hosted_providers.py`,
`SelfHostedModelRouteCache`, `resolve_route()`'s fallback). This spec
deliberately reuses that precedent's shape (DB table + process-local
whole-snapshot cache + `resolve_route()` fallback + `verified` gate +
collision guard + Org-Admin-only admin console card) everywhere the two
features are actually alike, and diverges from it only where BYOK-specific
requirements demand it (real per-token pricing instead of a GPU-hour
estimate; reusing an *existing* BYOK provider key instead of a new
credential; no new provider-client module needed at all).

**Naming**: new DB table `custom_models`, new cache `CustomModelRouteCache`,
new service module `services/custom_models.py`, new admin router
`api/v1/admin/custom_models.py` — parallel names to
`self_hosted_providers`/`SelfHostedModelRouteCache`/
`services/self_hosted_providers.py`/`api/v1/admin/self_hosted_providers.py`
throughout, so a developer already familiar with 5.5 can transfer that
knowledge directly.

---

## 1. In Scope — What an Org Admin Can Do

**User stories**

- As an Org Admin, I can register a custom model entry for a BYOK provider
  (OpenAI, Anthropic, Vertex AI, or OpenRouter) — giving it a gateway-facing
  name, its native provider model id, its capability, and real per-token
  input/output USD pricing — so my org can use a model the day the provider
  ships it, without waiting for a Gatekey code release.
- As an Org Admin, I can edit an existing custom model's native id, pricing,
  or name (subject to the same collision rules as registration).
- As an Org Admin, I can remove a custom model; requests referencing it start
  failing (404) immediately, and historical usage records referencing it are
  unaffected.
- As an Org Admin, before a custom model becomes usable, I can fire a real,
  minimal test call against it (using the BYOK key already on file for that
  provider) to confirm the native model id is actually valid and reachable,
  rather than discovering a typo only when a real user's request fails.
- As an Org Admin, I can see at a glance which custom models are verified,
  which are not yet, and which have become **shadowed** by a static registry
  update in a newer Gatekey release (see §4) and therefore need renaming.
- As a Team Lead, Member, or Auditor, I never see a "manage custom models"
  action anywhere — I only ever see a verified custom model appear
  indistinguishably alongside every static-registry model, wherever models
  are already surfaced to me today (Model Policy's checklist for an Org
  Admin/Auditor; the end-user Model Access view; the OpenAI-compatible
  gateway itself).

---

## 2. Fields an Admin Provides

New table `custom_models` (org-scoped, mirrors `self_hosted_providers`'
column shape where the concepts overlap):

| Field | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `org_id` | UUID FK, `CASCADE` | single-org today, table is multi-org-ready like every other Phase 2+ table |
| `name` | text, unique per `(org_id, name)` | the gateway-facing key accepted in the `model` request field. Freeform admin-typed string — no mandatory prefix (unlike the static registry's `ollama/`/`openrouter/` ADR-1 convention, which exists to disambiguate within one hand-curated dict; here uniqueness is a DB constraint, not a naming convention). Admin is free to use a prefix convention of their own choosing (e.g. `custom/gpt-5-preview`) but it is not enforced. |
| `provider` | text, one of `"openai" \| "anthropic" \| "vertex_ai" \| "openrouter"` | **`"ollama"` is deliberately excluded** — see §7 non-goal. These four values are a strict subset of `providers.registry.SUPPORTED_PROVIDERS`. |
| `native_model_id` | text | the literal string sent to the provider's own API. |
| `capability` | enum, reuses `ModelCapability` (`chat \| embeddings`) | one row = one capability, matching `ModelRoute`'s existing 1:1 discipline. A provider model that supports both needs two rows with two different `name`s. |
| `input_price_per_million_usd` | `Numeric(12,6)`, `CHECK > 0` | USD per million input/prompt tokens. **Resolved (§12): hard-blocked at `> 0`, matching self-hosted's `cost_basis_per_gpu_hour` constraint exactly** — no $0 pricing in v1. A typo'd/blank $0 would silently bypass budget enforcement; a genuine free-tier model is rare enough not to special-case in v1. |
| `output_price_per_million_usd` | `Numeric(12,6)`, nullable, `CHECK > 0` when present | **required (non-null, `> 0`) when `capability = chat`**, forbidden (must be `NULL`) when `capability = embeddings` — mirrors `PricingEntry`'s exact invariant, enforced at write time in the service layer (the DB-backed equivalent of `_validate_completeness()`, which only runs at import time over the static dict and cannot see this table). |
| `pricing_source` | text, nullable, admin free-text | optional citation, e.g. a URL to the provider's pricing page — mirrors `PricingEntry.source`'s intent but is optional here (not every admin will bother), unlike the static table where every entry has one. |
| `pricing_as_of` | date, **server-set, not admin-entered** | set to "today" automatically every time pricing fields are created or edited — removes a whole class of admin-typo risk (mistyped/backdated date) the static table's manually-maintained `as_of` field is exposed to. |
| `verified` | bool, default `false` | gates routing eligibility exactly like `self_hosted_providers.verified` — see §5. |
| `created_at` / `updated_at` | timestamptz | |

**Explicitly not asked for at registration** (see §7): a bearer token / new
credential of any kind. A custom model routes through the **existing** BYOK
`provider_keys` row for its `provider` — there is nothing new to encrypt or
store here, which is the single biggest scope-reducer versus the self-hosted
precedent (which needed its own encrypted-credential envelope because a
self-hosted endpoint is a wholly new trust boundary; a custom model is not —
it's a new *name* pointing at an *existing*, already-governed provider key).

---

## 3. `resolve_model()` / `resolve_route()` Layering — Recommendation

**Correction to the framing this spec was commissioned with**: the question
"how should `resolve_model()` layer DB-backed custom models with the static
registry" has a precise answer grounded in what actually shipped for 5.5,
and it is *not* to touch `resolve_model()` itself.

- `resolve_model()` (`providers/model_registry.py`) **stays exactly as it
  is** — a pure, zero-I/O, synchronous function over the static
  `MODEL_REGISTRY` dict only, and remains the sole sanctioned reader of that
  dict, per its own module docstring's explicit constraint. This constraint
  is a Cross-Phase non-negotiable-adjacent decision this spec does not
  loosen.
- The actual layering point — already established by 5.5 and reused
  unchanged in shape — is `api.v1.gateway.common.resolve_route()`, which
  today tries `resolve_model()` first, unconditionally, and only on
  `UnknownModelError` falls back to an optional, pre-warmed
  `SelfHostedModelRouteCache` lookup.
- **Recommendation**: add a second, structurally identical optional
  fallback parameter, `custom_model_cache: CustomModelRouteCache | None =
  None`, checked **after** the static registry and **before** (or after —
  order is immaterial, see below) the self-hosted cache. Concretely:

  ```
  resolve_route(model, self_hosted_cache=None, custom_model_cache=None):
      try: return resolve_model(model)          # static — always tried first, unconditionally
      except UnknownModelError:
          if custom_model_cache is not None:
              entry = custom_model_cache.get(model)
              if entry is not None: return <ModelRoute built from entry>
          if self_hosted_cache is not None:
              entry = self_hosted_cache.get(model)
              if entry is not None: return <ModelRoute built from entry>
          raise ModelNotFoundError(...)
  ```

- **Why static always wins, unconditionally, first**: identical rationale
  to 5.5's own documented choice — a DB-registered name is rejected *at
  write time* (§4) if it collides with a static key, so under normal
  operation this ordering is never actually exercised as a tie-break; it
  only matters in the shadowing scenario (§4) where a *later* Gatekey
  release adds a static key that collides with an *already-registered*
  custom model. Static-wins is the only choice consistent with 5.5's
  existing precedent and with the static registry being the thing under
  Gatekey-maintainer, not org-admin, control.
- **Why custom-model vs. self-hosted order is immaterial**: §4's collision
  guard is extended to check **both** tables bidirectionally — a custom
  model `name` can never collide with an existing `self_hosted_providers`
  entry's `models` list, and vice versa — so in correctly-operating steady
  state the two caches' key sets are always disjoint from each other (and
  from the static registry). The order above is written custom-first
  purely for determinism/documentation, not because it's ever load-bearing.
- `ModelRoute.provider` for a custom-model route is the row's real
  `provider` value (`"openai"`/`"anthropic"`/`"vertex_ai"`/`"openrouter"`)
  — **not** a new sentinel like `self_hosted`'s `"self_hosted"` literal.
  This is a deliberate, important difference from 5.5: a custom model's
  credential fetch (`fetch_credential()`) and provider dispatch
  (`call_provider_with_failover()`) are **completely unmodified** — they
  already work off `route.provider`, so a custom OpenAI model transparently
  gets Phase 4 failover/backup-group support for free, the same as any
  static OpenAI model. Only `native_model_id` differs from what a static
  route would carry, and the provider-call code already threads
  `route.native_model_id` through, not `model`, so no call-site change is
  needed there either.
- `check_model_policy()`/`resolve_model_access()`/`set_policy()`/
  `set_team_model_policy()`'s existing widened "is this a known model id"
  validation (already extended once, for 5.5, to also accept
  `SelfHostedModelRouteCache.known_model_ids()`) is extended a second time
  to also accept `CustomModelRouteCache.known_model_ids()` — no
  special-casing, identical mechanism, identical rationale (AC5.5.6's
  precedent).
- **Cost computation**: `record_usage_charge()` already has exactly the
  extension point this needs — `precomputed_cost_usd`, added for 5.5's
  self-hosted GPU-hour estimate. A custom model's cost is computed by a new
  `compute_custom_model_cost(pricing_entry, prompt_tokens,
  completion_tokens) -> Decimal` using the **exact same per-token formula**
  `services.budget.compute_cost()` already uses for static models (`input
  price * prompt_tokens / 1e6 [+ output price * completion_tokens / 1e6]`)
  — **not** the self-hosted GPU-hour-proxy formula, because custom-model
  pricing is real admin-entered $/token, not a compute-time estimate. The
  route handler passes this through `precomputed_cost_usd` the same way the
  self-hosted path already does. This is a genuinely different cost model
  from 5.5's, despite reusing the same plumbing hook.

---

## 4. Validation and the Collision/Shadowing Problem

### 4.1 At write time (register/edit) — hard rejects, no DB write

Mirrors `services/self_hosted_providers.py`'s `_validate_model_ids()`
two-guard pattern exactly, extended to a third table:

1. **Collision with a static `MODEL_REGISTRY` key** — rejected (422). The
   static registry always wins at request time (§3), so registering a name
   that shadows a real static key would make that custom-model row
   permanently unreachable the moment it's created — reject at write time
   with a clear message, same as 5.5's identical guard.
2. **Collision with a name already claimed by a different `self_hosted_providers`
   row's `models` list, for this org** — rejected (422). Bidirectional: the
   self-hosted registration path (`_validate_model_ids`) is *also* widened
   to check against `custom_models.name` for this org, so a self-hosted
   admin can't retroactively claim a name a custom-model admin already
   owns, or vice versa, regardless of which table gets written first.
3. **Collision with another `custom_models` row's `name`, for this org**
   (excluding the row being edited) — rejected (409, `Conflict`), matching
   `SelfHostedProviderNameConflictError`'s shape via the table's own
   `UNIQUE(org_id, name)` constraint.
4. **`capability`/`output_price_per_million_usd` mismatch** — `chat` with a
   `NULL` output price, or `embeddings` with a non-`NULL` output price —
   rejected (422). The DB-backed equivalent of `pricing.py`'s
   `_validate_completeness()` invariant, enforced in
   `services/custom_models.py` at every create/edit, not just at process
   start.
5. **`provider = "ollama"`** — rejected (422) with an explicit message
   pointing the admin at Providers → Self-Hosted Models instead (§7).

### 4.2 The shadowing problem (an already-registered custom model, then a
future Gatekey release adds the same static key)

This is the one genuinely hard, novel case this feature introduces, and the
answer must be explicit rather than assumed:

- **What happens mechanically**: per §3's static-always-wins ordering, from
  the moment the new Gatekey release (containing the new
  `MODEL_REGISTRY` entry) is deployed, every request for that name routes to
  the **static** entry — a different `native_model_id`/provider mapping/
  pricing than whatever the org admin's custom-model row pointed at. This is
  worse than the self-hosted precedent's "shadowed entry becomes
  unreachable" framing (5.5, judgment call in that phase's own service
  docstring) — here it is a **silent reroute to a different provider
  configuration and different pricing**, not a clean 404. That is a real
  cost-governance and possibly a data-residency/DLP-classification risk
  (traffic the admin believed was going to their own hand-verified mapping
  now goes to whatever the static registry says), not just an availability
  nuisance.
- **Recommendation** (three complementary mitigations, none of which is a
  hard crash — a Gatekey code upgrade must never brick a running gateway
  over something the *operator* didn't cause):
  1. **Startup check** (mirrors `pricing.py`'s `_validate_completeness()`
     pattern, but as a loud warning, not a `RuntimeError`): at app startup,
     after `CustomModelRouteCache` is warmed, cross-reference its key set
     against `MODEL_REGISTRY.keys()`. Any intersection is logged at
     `ERROR` level, one line per colliding name, naming the org and the
     custom model's `id` — discoverable in ops logs even before any admin
     opens the console.
  2. **Live, cheap re-check on every admin console load**: the `GET
     /v1/admin/custom-models` response includes a computed
     `shadowed_by_registry: bool` field per row (a simple
     `name in MODEL_REGISTRY` in-memory check, zero I/O, computed at
     response-build time — never persisted, since it must always reflect
     the *currently running* code's registry, not a stale flag). The admin
     console shows a prominent warning badge ("Shadowed by an updated
     Gatekey model registry — rename or remove") on any such row, same
     visual language as the "Not verified" badge.
  3. **No automatic remediation** — Gatekey never auto-renames or
     auto-deletes a shadowed row. The admin must act (rename the custom
     model to a non-colliding name, or remove it and adopt the new static
     entry) — an explicit, deliberate choice: auto-remediation on a
     cost-governance-relevant table is exactly the kind of "helpful"
     surprise this codebase's existing conventions (e.g. AC5.2's
     purge/chain mutual-exclusivity being blocked rather than
     auto-resolved) consistently avoid.
- This is flagged for the architect as a genuinely new cross-cutting
  concern (an application-code upgrade can invalidate admin-entered DB
  config) that no prior Gatekey feature has had to handle — 5.5's
  self-hosted `models` list can never collide with a *future* static entry
  in the same silent-reroute way, because self-hosted routes carry their
  own `self_hosted_provider_id`/dedicated dispatch path (`call_self_hosted_provider`),
  never `route.provider` set to a real BYOK provider string. A custom
  model's route, by this spec's own design (§3), deliberately **does**
  carry a real BYOK `provider` value — which is what makes the reroute
  risk real. The architect should treat this as the single highest-risk
  design point in this whole spec.

---

## 5. Verification Before Availability

**Precedent reused**: `self_hosted_providers.verified` gates routing
eligibility — `SelfHostedModelRouteCache` only ever contains entries from
rows with `verified = true` (enforced at the query level in
`load_self_hosted_model_route_snapshot`, not as a second runtime check).
This spec applies the **identical gate** to `custom_models.verified`, for a
BYOK-specific reason that makes it, if anything, more important here than
for self-hosted:

- A wrong `native_model_id` against a self-hosted endpoint fails loudly and
  cheaply (a local/owned server, `ProviderCallError`, no real money lost
  beyond compute already spent).
- A wrong `native_model_id` against a **paid BYOK provider** is a real
  cost-governance risk in two distinct failure modes: (a) it errors, and
  now every real user's request through that custom model fails at the
  worst possible time — after policy/budget/DLP checks already passed,
  right at provider dispatch; or worse, (b) the typo happens to alias to a
  *different but real* model at that provider (e.g. an off-by-one version
  string), which succeeds, bills correctly to *that* model's real cost, but
  silently produces the wrong pricing outcome against Gatekey's own
  `custom_models.input_price_per_million_usd`/`output_price_per_million_usd`
  entered for the *intended* model — a genuine budget-accuracy bug, exactly
  the failure class `pricing.py`'s entire design exists to prevent.

**Mechanism** (deliberately reuses the *already-configured BYOK key* — this
is the scope-reducing difference from 5.5 noted in §2, not a new credential
flow):

- New endpoint `POST /v1/admin/custom-models/{id}/verify` (Org Admin only).
  Fires exactly **one** minimal live call against `native_model_id` using
  the org's existing, already-decrypted `provider_keys` credential for
  `provider` (the same `get_decrypted_provider_credential()` every gateway
  request already uses) — a `chat` capability model gets a minimal chat
  completion (e.g. a fixed one-token prompt, `max_tokens`/equivalent capped
  small); an `embeddings` capability model gets a minimal embeddings call
  on a fixed short string. Both use each provider's **existing** client
  module (`providers/openai.py`, `providers/anthropic.py`, etc.) — no new
  provider-client code, mirroring 5.5's "reuse the existing client" choice
  exactly, just against a different, already-existing client set.
- On success: `verified = true`. On any `ProviderCallError` (including "no
  key configured for this provider yet" — surfaced as
  `ProviderNotConfiguredError`, same 404 shape every gateway request
  already produces): `verified` stays/reverts to `false`, and the specific
  provider error is returned to the admin verbatim (not swallowed) so a
  typo'd `native_model_id` is diagnosable immediately, not just "failed."
- **Registration never auto-verifies** — identical to 5.5's
  `register_self_hosted_provider()` always starting `verified = false`. The
  admin must explicitly click "Test model."
- **Editing `native_model_id` or `provider` resets `verified` to `false`** —
  identical rationale to 5.5's "changed endpoint/credential must be
  re-verified" rule (`edit_self_hosted_provider`'s `base_url`/`bearer_token`
  reset behavior). Editing *only* pricing fields does **not** reset
  `verified` (pricing has no bearing on whether the model is actually
  reachable).
- **Cost of the verification call itself**: real, tiny, one-time (not a
  recurring scheduled job like Phase 5.4's canaries) — **recommendation**:
  do **not** write a `usage_logs` row and do **not** charge any team/user/
  org budget for it, mirroring AC5.4.9's "canary cost never touches
  user-attributable spend" principle, scoped down to a single manual
  action rather than a scheduled suite. Do write an audit entry
  (`custom_model.test_call`, recording success/failure and latency) so the
  action is traceable, matching every other admin mutation's audit-logging
  discipline in this codebase. **Flagged for architect/security review**:
  unlike the canary suite (bounded, scheduled, fixed-prompt), a
  verify-on-demand action has no rate limit of its own beyond normal
  API-abuse protections — an Org Admin repeatedly clicking "Test model"
  incurs real, if small, uncharged provider cost each time; recommend a
  simple per-row cooldown (e.g. no more than once per 30 seconds) purely as
  a cost/abuse guard, not a product requirement from the phase docs.

**Selectability gate**: a custom model with `verified = false` is excluded
from `CustomModelRouteCache`'s snapshot (same "only verified rows land in
the cache at all" mechanism as 5.5, not a second runtime flag check) —
requests for it produce the same `ModelNotFoundError` (404) as any unknown
model, and it is not offered as a selectable checkbox in Model Policy
(mirrors `model-policy/page.tsx`'s existing "only verified self-hosted
models are selectable" pattern exactly — see that file's disabled-checkbox
logic for unverified self-hosted rows, reused verbatim for the new "Custom"
group).

---

## 6. RBAC

Matches the task's explicit instruction and 5.5's precedent exactly — no
new role, no new RBAC primitive:

- **Org Admin** — full CRUD (register/edit/remove) + trigger verification,
  via `require_role("org_admin")`, identical shape to every
  `self_hosted_providers` write endpoint.
- **Org Admin + Auditor** — list/read (`GET /v1/admin/custom-models`, and
  the per-row `shadowed_by_registry` computed field from §4.2), via
  `require_admin_or_auditor` — matches 5.5's read-endpoint RBAC exactly,
  and matches Model Policy's own existing Org-Admin-only admin-console
  convention (an Auditor can see the resolved model list and its
  provenance for compliance-review purposes, never edit it).
- **Team Lead / Member** — **no access** to any `/v1/admin/custom-models*`
  endpoint. They never see a "custom model" concept as such anywhere — a
  verified custom model is indistinguishable from any static-registry model
  everywhere it's surfaced to them (Model Access view, the gateway's model
  list, any usage/cost breakdown they can already see). This is the
  concrete meaning of the task's "only view the resolved model list": there
  is no separate "resolved list" view being built — the existing
  model-surfacing code paths (Model Policy checklist, end-user Model Access
  page, gateway routing) already present a unified view by construction,
  because `resolve_route()`/`CustomModelRouteCache.known_model_ids()` merge
  the sources before anything downstream ever sees them. No new endpoint or
  screen is needed for "just viewing" beyond what already exists.
- Admin console surface: a new **"Custom Models" card on the Providers
  screen** (`frontend/app/providers/page.tsx`), directly analogous to and
  visually consistent with the existing "Self-Hosted Models" card — same
  page, same `+ Register` / row-with-badges / Edit / Remove / verify-action
  pattern, placed adjacent to it (not on Model Policy's page, since Model
  Policy is about *allow/deny*, not *definition*, of models — registering a
  custom model is a Providers-screen concern, exactly like BYOK keys and
  self-hosted endpoints already are; it then shows up as a new "Custom"
  group in Model Policy's checklist alongside "Self-Hosted," reusing that
  page's existing per-group verified/unverified rendering logic unchanged
  in shape).

---

## 7. Explicit Non-Goals for v1

- **No auto-discovery from a provider's own list-models API** (e.g.
  `GET /v1/models` against OpenAI/Anthropic/Vertex/OpenRouter to suggest or
  validate `native_model_id` against a live catalog). Deliberate — matches
  both the static registry's own stated "not a mirror" philosophy and 5.5's
  identical explicit deferral of self-hosted model auto-discovery. The
  entire point of this feature is admin-typed, admin-attested control, not
  an auto-syncing mirror; auto-discovery is a fundamentally different
  (arguably contradictory) product direction, not a "v2 of this."
- **`provider = "ollama"` is out of scope for this feature.** Ollama
  already has its own, fully-separate, already-shipped admin-editable model
  mechanism (the Self-Hosted Governance feature, 5.5) — Ollama used as a
  *paid-tier-adjacent BYOK-style key* is not a real-world pattern this
  codebase or its providers support today, and folding it into this feature
  would create two competing ways to register an Ollama model. An admin who
  wants a new Ollama model adds it to an existing `self_hosted_providers`
  row's `models` list — unchanged, not touched by this spec.
- **No org-vs-team scoping.** Custom models are org-wide only, exactly like
  `self_hosted_providers`, `content_aware_rules`, and every other
  model-definition-shaped table in this codebase — team-level model
  *access* narrowing already exists (Team Model Restrictions) and continues
  to apply to a custom model's name exactly like any other, unmodified.
- **No bulk import/CSV upload.** One model at a time via the admin console
  form, matching the self-hosted registration UX exactly (a multi-line
  textarea for self-hosted's `models` list is the closest existing
  precedent to "many at once," and even that is a single endpoint's whole
  list, not a bulk-create-many-rows operation — this feature doesn't need
  that shape at all, since each custom model needs its own distinct
  pricing/native-id, unlike self-hosted's flat model-id list).
- **No tiered/threshold pricing** (e.g. a different rate above 200k input
  tokens, the way the static `gemini-2.5-pro` entry's own code comment
  flags as a known simplification it already accepts). One flat
  input/output rate pair per custom model — the same simplification the
  static table already lives with for that exact real-world case.
- **No scheduled re-verification / no continuous health polling** of a
  custom model, mirroring 5.5's identical choice for self-hosted endpoints
  (AC5.5.3: manual re-verification only, not wired into
  `run_provider_key_health_check_if_due`). A custom model's *provider key*
  health is already covered by Phase 4's existing per-key health checks
  (unchanged — a custom model just rides `route.provider`'s existing key);
  it is only the `native_model_id` itself whose validity this feature adds
  a check for, and that check stays manual/on-demand.
- **No automatic price-staleness detection.** Gatekey never checks whether
  an admin-entered rate still matches the provider's live pricing page —
  the admin is fully responsible for keeping it current, exactly matching
  the static `PRICING_TABLE`'s own documented caveat ("An operator
  deploying Gatekey should still confirm these against the live pricing
  pages before relying on them"). `pricing_source`/`pricing_as_of` (§2)
  exist to make staleness *auditable by a human*, not to make it
  *self-detecting*.
- **No versioning/deprecation workflow.** Removing a custom model is a hard
  delete; there is no "mark deprecated, migrate traffic gradually" flow.
  Matches self-hosted's identical remove behavior (immediate 404 for new
  requests; historical `usage_logs` rows are unaffected, FK `SET NULL`
  where applicable).
- **No `/v1/completions` (legacy completions endpoint) support** beyond
  whatever the static registry itself already does — capability stays the
  existing two-value `ModelCapability` enum (`chat`/`embeddings`), unchanged.

---

## 8. Data Model Touchpoints (for architect — a checklist, not schema design)

- New table `custom_models`: `id`, `org_id` (FK, `CASCADE`), `name`
  (`UNIQUE(org_id, name)`), `provider` (text, constrained to the 4-value
  BYOK subset — a `CHECK` constraint or reuse of a narrowed enum, not
  `provider_name_enum` itself unless that type already excludes `ollama`/
  `self_hosted` cleanly), `native_model_id`, `capability` (reuse
  `ModelCapability`), `input_price_per_million_usd` (`Numeric(12,6)`,
  `>= 0`), `output_price_per_million_usd` (`Numeric(12,6)`, nullable,
  `>= 0`), `pricing_source` (text, nullable), `pricing_as_of` (date,
  server-set), `verified` (bool, default `false`), `created_at`,
  `updated_at`.
- `services/self_hosted_providers.py`'s `_validate_model_ids()` gains a
  third check against `custom_models.name` for this org (§4.1, guard #2) —
  a real, small change to already-shipped code, not purely additive.
- New `services/custom_models.py`: CRUD (register/edit/remove/get/list),
  `CustomModelRouteCache` (whole-snapshot-replace, same convention as
  `ModelPolicyCache`/`SelfHostedModelRouteCache`), verification
  (`verify_custom_model()`, reusing existing per-provider client modules),
  and `compute_custom_model_cost()` (§3).
- `api.v1.gateway.common.resolve_route()`: new optional
  `custom_model_cache` parameter (§3) — a real, small change to an
  already-shipped, heavily-documented hot-path function.
- `services/model_policy.py`'s `set_policy()`/`set_team_model_policy()`
  widened "known model id" validation: add
  `CustomModelRouteCache.known_model_ids()` as a third accepted source,
  alongside `MODEL_REGISTRY` and `SelfHostedModelRouteCache` (§3).
- `services/budget.py`/`api.v1.gateway.common.record_usage_charge()`: no
  signature change needed — `precomputed_cost_usd` already exists (5.5);
  the new caller is a custom-model gateway request computing it via
  `compute_custom_model_cost()` instead of `compute_self_hosted_cost()`.
- New admin router `api/v1/admin/custom_models.py`: `GET`/`POST`/`PUT
  {id}`/`DELETE {id}`/`POST {id}/verify`, RBAC per §6, audit-logged per the
  existing `write_audit_entry()` convention (mirrors
  `api/v1/admin/self_hosted_providers.py`'s handlers almost line for line).
- New Pydantic schemas `schemas/custom_model.py`
  (`CustomModelCreateRequest`/`UpdateRequest`/`Response`), mirroring
  `schemas/self_hosted_provider.py`'s bounds-only-validation discipline —
  `Response` never needs a "never echo a secret" rule the way
  `SelfHostedProviderResponse` does (there is no secret on this table at
  all), which is itself worth calling out as a real simplification.
- `frontend/app/providers/page.tsx`: new `CustomModelsCard`, sibling to
  `SelfHostedModelsCard`, same component shape.
- `frontend/app/model-policy/page.tsx`: new `"Custom"` group in the
  provider-grouped checklist, sourced from `listCustomModels()`, reusing
  the existing self-hosted group's verified/unverified rendering logic.
- `frontend/src/lib/api.ts`: `listCustomModels`, `registerCustomModel`,
  `editCustomModel`, `removeCustomModel`, `verifyCustomModel`,
  `CustomModelResponse` type — mirrors the existing
  `*SelfHostedProvider*` functions' shapes exactly.

---

## 9. Flagged Ambiguities / Judgment Calls (for architect + security review)

1. **Shadowing risk (§4.2) is the highest-severity new concern this feature
   introduces** — a future static-registry release can silently reroute an
   admin's already-governed custom model traffic to a different
   provider/pricing config. The three-part mitigation (startup log,
   live per-row badge, no auto-remediation) is this spec's answer, but it
   is a genuinely new class of risk (app-code-upgrade invalidating
   admin-entered DB config) with no prior precedent in this codebase to
   fall back on — treat as the top review item.
2. **Verification-call cost/rate-limiting** (§5) — recommending an
   informal per-row cooldown as a defensive measure, not something either
   phase doc or the task brief specified; confirm this is acceptable scope
   or explicitly cut it.
3. **`$0.00` pricing permitted** (§2) — unlike self-hosted's `cost_basis_per_gpu_hour`
   (`CHECK > 0`, hard-blocked), this spec allows `$0` for custom BYOK
   pricing (a real promotional-tier scenario) with only a soft UI nudge,
   not a hard block. This is a deliberate divergence from 5.5's stricter
   `> 0` constraint — worth confirming with product/security before
   treating as final, since it reopens exactly the "silent free request"
   risk `pricing.py`'s own docstring warns against, just via an admin's own
   explicit (if possibly mistaken) input rather than a missing entry.
4. **No mandatory naming prefix/namespace** (§2) — self-hosted and the
   static registry both use prefix conventions (`ollama/`, `openrouter/`)
   to keep names self-describing; this spec deliberately does not require
   one for custom models, trusting the collision guard (§4.1) alone for
   correctness. Worth a UX gut-check: an unprefixed custom model name sits
   in the exact same flat namespace as every static model, which could
   read as more "official" than it is (e.g. `gpt-5.5-preview` looks
   identical whether it's a static entry or an admin's own guess at a
   provider's not-yet-code-registered model). Consider recommending (not
   requiring) a prefix convention in the admin UI's placeholder/help text
   only.
5. **Whether `provider_keys` for the target provider must already exist at
   *registration* time** — this spec allows registering a custom model
   before its BYOK key is configured (verification will simply fail with
   the existing `ProviderNotConfiguredError` 404 until the key is added),
   rather than hard-blocking creation. Matches this codebase's general
   "let the natural downstream check fail with a clear error" style over
   adding a redundant precondition — flagged in case product wants the
   stricter block instead.

---

## 10. Non-Functional Requirements (testable)

- **Static registry always wins, provably.** Acceptance test: register a
  custom model reusing an existing static `MODEL_REGISTRY` key name — the
  registration call must be rejected (422) before any DB write, not
  silently accepted and shadowed.
- **Unverified custom models are never routable.** Acceptance test:
  register a custom model, do not verify it, send a gateway request for
  its name — must 404 (`ModelNotFoundError`), identically to an unknown
  model, never routed to the provider.
- **Verification uses the real, existing BYOK key — never a new one.**
  Acceptance test: verifying a custom model with no `provider_keys` row
  configured for its `provider` fails with the same
  `ProviderNotConfiguredError` shape a normal gateway request would
  produce; no credential input field exists anywhere in the custom-model
  registration/edit form.
- **Cost is computed from admin-entered real per-token rates, not an
  estimate formula.** Acceptance test: a verified custom model's
  `usage_logs.cost_usd` for a request with known prompt/completion token
  counts equals the exact `compute_custom_model_cost()` arithmetic — no
  "estimated" language anywhere in its UI/API surface (distinguishing it
  from self-hosted's mandatory "estimated" labeling).
- **Shadowing is detectable without redeploying or restarting the admin
  console.** Acceptance test: with a custom model already registered under
  name `X`, simulate a static-registry update that also defines `X`
  (test-only registry override) — the next `GET
  /v1/admin/custom-models` call must report `shadowed_by_registry: true`
  for that row, and a live gateway request for `X` must route to the
  static entry, not the custom one.
- **Collision guards are bidirectional across all three model-name
  sources.** Acceptance test: (a) registering a custom model whose name
  matches an existing self-hosted model's id is rejected; (b) registering
  a self-hosted model whose id matches an existing custom model's name is
  rejected; (c) both directions surface a clear, specific error naming the
  conflicting source.

---

## 11. Dependencies on Prior Phases

- **Phase 1** — `MODEL_REGISTRY`/`resolve_model()`, `PRICING_TABLE`/
  `PricingEntry`/`get_pricing_entry()`/`compute_cost()`, provider-key
  encryption envelope, `ProviderCredential`/`fetch_credential()`.
- **Phase 2** — RBAC role set and `require_role`/`require_admin_or_auditor`
  dependency primitives used unchanged throughout §6.
- **Phase 4** — per-provider-key health checks and failover
  (`call_provider_with_failover`) — a custom model rides these completely
  unmodified, since its `ModelRoute.provider` is a real BYOK provider
  string, not a new sentinel (§3).
- **Phase 5.5** — the direct structural precedent for nearly this entire
  feature: `self_hosted_providers` table shape, `SelfHostedModelRouteCache`'s
  whole-snapshot-replace convention, `resolve_route()`'s fallback pattern,
  the `verified`-gates-cache-membership rule, the model-id collision guard,
  `record_usage_charge()`'s `precomputed_cost_usd` hook, and the
  Providers-screen admin-console card pattern. This spec's single largest
  design decision is *which* parts of that precedent to reuse verbatim
  (cache/fallback/verified-gate/collision-guard/RBAC/console-card shape)
  versus deliberately diverge from (no new credential type, real per-token
  pricing instead of a GPU-hour estimate, `route.provider` stays the real
  BYOK provider instead of a new sentinel) — see §3 and §5 for the
  reasoning behind each divergence.

---

## 12. Decisions (resolved by the user, 2026-08-06)

- **$0 pricing: hard-blocked (`> 0` required)**, matching self-hosted's
  stricter constraint. §2's `input_price_per_million_usd`/
  `output_price_per_million_usd` fields updated accordingly.
- **Registration allowed before the BYOK key exists** — confirmed as
  designed; verification fails naturally with the existing
  `ProviderNotConfiguredError` shape until the key is added. No precondition
  added at registration time.
- **§4.2's shadowing mitigation (startup error log + live "shadowed" badge +
  no auto-remediation) — approved as designed.** Architect should treat this
  as settled, not open.
