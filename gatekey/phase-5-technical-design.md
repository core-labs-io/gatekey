---
title: Gatekey Phase 5 — Differentiators
description: Technical Design Document
status: draft
last_updated: 2026-08-06
authors: architect
---

# Gatekey Phase 5 — Differentiators
## Technical Design Document

Source: `gatekey/phase-5-product-spec.md` (49 ACs, build order 5.2→5.4→5.5→5.3→5.1),
`gatekey/phase-5-differentiators.md`. Verified against real code in
`backend/src/gatekey/` — see §5 for the file-by-file wiring checklist this
document treats as mandatory, not optional, per the Phase 4 post-mortem
(entire subsystems shipped with zero real callers).

**Ground-truth check performed before this design**: grepped the full
backend tree for any partial Phase 5 code beyond the two docstring hints the
product-owner already found. Confirmed: no `canary`, `drift`, `chain_hash`,
`self_hosted`, `shadow_ai`, `sensitivity_label` symbols exist anywhere in
`backend/src/gatekey/` except the two forward-compat docstring notes in
`db/models/audit_entry.py` and `db/models/content_aware_rule.py` the spec
already cites, and the `providers/pricing.py` Ollama-entry note pointing at
this phase. Nothing to build on beyond those three docstrings — every
sub-feature below is a genuine net-new build. Current alembic head is
`0036_add_threshold_bounds_check_to_degradation_policies.py`; Phase 5
migrations start at `0037`.

---

## 1. Overview

Phase 5 ships five differentiator features on top of a stable Phases 1–4
platform: a hash-chained audit ledger (5.2), a provider drift detector (5.4),
unified BYOK+self-hosted governance (5.5), content-classification-aware
routing (5.3), and shadow-AI discovery (5.1). Built in that order (lowest
integration risk first) per the product spec's §0 locked decision.

### 1.1 Key Constraints Carried Forward

| Constraint | Implication |
|------------|-------------|
| Self-hosted first | No external anchoring service (5.2), no embeddings-API calls (5.3/5.4), no vendor SASE adapters (5.1) |
| No plaintext keys at rest | Self-hosted `bearer_token` uses the identical AES-256-GCM envelope as `provider_keys`; shadow-AI ingest token is hash-only (never reversible) |
| OpenAI-compatible API | No sub-feature changes request/response body shape; drift canaries never appear in `/v1/chat/completions` traffic |
| Phase 3 DLP/residency boundaries | Content-classification generalization (5.3) runs at the exact same pipeline position DLP already occupies; self-hosted routing (5.5) flows through the identical DLP/residency/budget pipeline, never a bypass |
| Phase 2 atomic budget check-and-deduct | Self-hosted cost charging reuses the same atomic `UPDATE ... RETURNING` pattern (`services/budget.py`), never a read-modify-write |

### 1.2 Non-Functional Requirements

| NFR | Target | Enforcement |
|-----|--------|-------------|
| Hash-chain integrity is real | Tamper to one historical row is detectable, names the exact entry | `GET /v1/admin/audit/verify` full recompute walk |
| Hash-chain write concurrency | No forked chain under concurrent writes to the same org | `SELECT ... FOR UPDATE` on `compliance_settings` row inside `write_audit_entry` (see §2.1) |
| Canary cost never touches user-attributable budget | Zero new `usage_logs` rows, zero `current_spend_usd` change | `canary_runs.cost_usd` only, `record_usage_charge()` never called for canary traffic |
| Canary suite cost floor small and bounded | 5 fixed prompts, capped `max_tokens=50` | Code-seeded `canary_prompts`, no admin-editable prompt authoring in v1 |
| `resolve_route()` stays zero-I/O on the hot path | No new DB query per gateway request | `SelfHostedModelRouteCache` — warmed cache read only, same tier as `ModelPolicyCache` |
| Self-hosted cost is visibly labeled "estimated" | Every self-hosted cost figure in the admin UI/export | UI label requirement, `usage_logs.provider = "self_hosted"` is the query discriminator |
| Shadow-AI collects connection metadata only | Never full URLs, query strings, or bodies | `shadow_ai_ingest_events` schema has no body/URL column; policy doc is a shipped artifact |
| Shadow-AI ingestion is fail-closed until setup | Rejects all traffic pre-setup | `require_shadow_ai_ingest_token` rejects when `shadow_ai_ingest_config.ingest_token_hash IS NULL` |

---

## 2. System Architecture

### 2.1 Data Flow: Hash-Chained Audit Ledger (5.2)

```
Any mutation/gateway-block call site
        │
        v
services.audit.write_audit_entry(session, actor=..., action=..., ...)
        │
        ├─ 1. compliance = get_effective_compliance_settings(session)  (existing helper, cheap indexed read)
        │
        ├─ 2. if not compliance.chain_enabled:
        │        session.add(AuditEntry(..., chain_hash=None, prev_hash=None, chain_seq=None))
        │        await session.flush()          # byte-for-byte pre-Phase-5 behavior
        │        return
        │
        └─ 3. if compliance.chain_enabled:
                 # SERIALIZE: lock the org's compliance_settings row (guaranteed
                 # to exist — see below) for the rest of THIS transaction.
                 await session.execute(
                     select(ComplianceSettings.org_id)
                     .where(ComplianceSettings.org_id == org_id)
                     .with_for_update()
                 )
                 tail = SELECT chain_hash, chain_seq FROM audit_entries
                         WHERE org_id = :org_id ORDER BY chain_seq DESC LIMIT 1
                 prev_hash_for_hash   = tail.chain_hash if tail else ""
                 prev_hash_to_store   = tail.chain_hash if tail else None   # NULL at genesis
                 chain_seq            = (tail.chain_seq + 1) if tail else 1
                 chain_hash = SHA256(prev_hash_for_hash + canonical_json({id, org_id,
                                actor_label, action, target_type, target_id,
                                old_value, new_value, source_ip, created_at}))
                 session.add(AuditEntry(..., chain_hash=chain_hash,
                                         prev_hash=prev_hash_to_store, chain_seq=chain_seq))
                 await session.flush()
                 # lock released when the CALL SITE's session.commit()/rollback() runs
                 # (write_audit_entry never commits — same "flush, don't commit"
                 # contract it already has today)
```

**Key Decision: lock `compliance_settings`, not the `audit_entries` tail row, and not a raw `pg_advisory_xact_lock`.**

*Decision:* Serialize the read-tail + compute-hash + insert critical section
by taking `SELECT ... FOR UPDATE` on the org's `compliance_settings` row —
the exact same "lock the stable parent config row before writing dependent
children" pattern `services/team_budget.py`'s `_lock_team`/the org-settings
lock already establish in this codebase (ADR-5).

*Alternatives considered (the two the product spec flagged, §9 item 1):*
- **Row lock on the `audit_entries` tail row itself.** Rejected: at a true
  chain genesis (the first-ever chained write for an org, and at every
  point before `chain_enabled` is first turned on) there is no tail row to
  lock — `SELECT ... FOR UPDATE` cannot lock a row that doesn't exist yet,
  so this doesn't actually solve the bootstrap race.
- **A raw `pg_advisory_xact_lock` keyed on `org_id`.** Rejected in favor of
  the row lock: this codebase has zero existing uses of Postgres advisory
  locks anywhere (verified by grep); introducing a brand-new locking
  primitive for one feature adds a second locking idiom for
  database-admin/backend-developer to reason about, for no benefit over
  reusing the row-lock idiom that already has a security-reviewed precedent
  and already composes correctly with SQLAlchemy's session/transaction
  lifecycle.

*Why `compliance_settings` specifically, not a new dedicated lock row:*
`chain_enabled = true` can only ever be true if a `compliance_settings` row
was explicitly upserted for that org (default is `false`, absence-of-row
default is also `false` — see `services/compliance_settings.py`), so the
row **is guaranteed to exist** every time `write_audit_entry` takes this
lock. This also closes the enable/backfill race for free: the admin
endpoint that flips `chain_enabled` false→true (§2.1 below) takes the
identical `FOR UPDATE` lock on the same row, runs the full historical
backfill (AC5.2.6) inside that same transaction, and only then sets
`chain_enabled = true` and commits. Because every concurrent
`write_audit_entry` call also blocks on that same row lock, no writer can
observe `chain_enabled = true` with a not-yet-backfilled tail — the
backfill is guaranteed atomic relative to every other chain write.

*This is a refinement of the product spec's §9 judgment call #1, not a
silent deviation* — the spec offered "row lock on tail vs. advisory lock
keyed on org_id" as the two options for the architect to choose between;
this design proposes a **third, better-grounded option** (lock a
guaranteed-to-exist parent config row, not the moving-target tail row) and
is called out explicitly in this document's cover report.

*Trade-off:* every audit write, once chaining is enabled, pays one extra
`SELECT ... FOR UPDATE` + one extra `SELECT` (tail read) beyond today's
single `INSERT`. `write_audit_entry` is called from many admin mutation
call sites plus three gateway-path block cases (`dlp.block`,
`residency.hard_block`/`residency.warn`, `access_schedule.block`) — none of
these are the high-QPS success path (`/v1/chat/completions`'s happy path
never calls `write_audit_entry`), so this is the same "not zero-I/O, but
cheap and on a low-QPS path" tradeoff `check_budget_available()` already
makes, not a new NFR risk.

**Enable-toggle backfill (AC5.2.6) — the one real capacity/latency risk in
this section, flagged in §11.**

`services/compliance_settings.py` gains `set_chain_enabled(session, org_id,
enabled: bool)`, called from a new `PUT /v1/admin/compliance-settings/chain`
(or an extension of the existing compliance-settings PUT — see §3). When
transitioning `false → true`, this function, inside one transaction, under
the `FOR UPDATE` lock:
1. Walks every existing `audit_entries` row for the org ordered by
   `(created_at, id)` (deterministic tie-break, per AC5.2.6), batched
   (`_PURGE_BATCH_SIZE`-style batching, 5000 rows/round-trip, mirroring
   `services/scheduler.py::_purge_rows_older_than`'s existing batching
   idiom) but **all within the same transaction** — batching bounds
   per-round-trip payload size, it does not release the lock between
   batches (releasing early would let a concurrent write compute against a
   partially-backfilled tail).
2. Computes and `UPDATE`s `chain_seq`/`prev_hash`/`chain_hash` for every row.
3. Sets `chain_enabled = true` and commits.

For a very large pre-existing `audit_entries` table this is a genuinely
long-running, lock-holding operation — flagged explicitly in §11 (Known
Limitations) as a v1 trade-off, not silently accepted.

---

### 2.2 Data Flow: Provider Drift Detector (5.4)

```
services.scheduler.run_scheduler_loop  (existing tick, once per 60s)
        │
        └─ NEW 5th try/except block, after the existing 4 (rotation, audit
           purge, log/prompt purge, provider-key health check):
                 run_drift_canary_if_due(session, app)
                     │
                     ├─ gate: app.state.last_drift_canary_check_at
                     │        (in-memory marker, EXACT run_provider_key_
                     │        health_check_if_due pattern) — only fires once
                     │        per DRIFT_CANARY_CHECK_INTERVAL_SECONDS (24h)
                     │
                     └─ services.drift_detector.run_canary_suite_for_org(session, app)
                             │
                             ├─ 1. active_models = SELECT DISTINCT model FROM usage_logs
                             │       WHERE org_id=:org AND created_at > now()-7d
                             │       (real, non-canary traffic only — canary
                             │       runs never write usage_logs, so this
                             │       query is inherently canary-free)
                             ├─ 2. canary_model_settings filters out any
                             │       model an admin disabled (AC5.4.11)
                             ├─ 3. prompts = SELECT * FROM canary_prompts WHERE enabled
                             │       (the 5 code-seeded rows)
                             ├─ 4. FOR EACH active, enabled model, SEQUENTIALLY
                             │       (not asyncio.gather — see Key Decision below),
                             │       capped at _CANARY_MAX_MODELS_PER_TICK:
                             │         resolve route (MODEL_REGISTRY or
                             │         SelfHostedModelRouteCache — §2.3)
                             │         fetch+decrypt the real credential
                             │           (same fetch_credential() path gateway
                             │           requests use)
                             │         FOR EACH of the 5 prompts:
                             │           call provider's create_chat_completion
                             │           directly (bypassing check_model_policy/
                             │           check_residency/run_dlp_scan/
                             │           check_budget_available — synthetic,
                             │           non-user, org-controlled content)
                             │           measure latency_ms, run refusal regex,
                             │           compute similarity vs. canary_baselines
                             │           compute cost via pricing.compute_cost()/
                             │           compute_self_hosted_cost() — NEVER
                             │           record_usage_charge()
                             │           INSERT canary_runs row (cost_usd only
                             │           column touched for spend; no usage_logs
                             │           row, no budget UPDATE)
                             ├─ 5. establish_baseline_if_ready() — once 7 days
                             │       of runs exist for a (model, prompt) with no
                             │       baseline row yet, compute+insert canary_baselines
                             └─ 6. flag_drift() — rolling 7-run window vs.
                                     baseline, threshold rules (AC5.4.6);
                                     INSERT drift_alerts row per newly-flagged
                                     (model, metric) pair
```

**Key Decision: sequential per-model execution within one tick, capped batch
size, no multi-tick state machine.**

*Decision:* canary calls for every actively-used model run one at a time
(`await` in a loop, not `asyncio.gather`) inside a single scheduler tick,
capped at `_CANARY_MAX_MODELS_PER_TICK` (default 50) models per tick; any
remainder is picked up on a **later** tick, gated by the SAME
`last_drift_canary_check_at` marker being now-stale (i.e. it naturally
retries within the next few 60s ticks until the whole due batch is drained,
then goes quiet for the rest of the 24h window).

*Alternatives considered:*
- **All canaries concurrently via `asyncio.gather`.** Rejected: this
  codebase's existing pilot `MODEL_REGISTRY` is small (~15 entries across 5
  providers) so concurrency is rarely a real problem today, but a
  self-hosted org (5.5) can register an unbounded number of additional
  models — gathering all of them at once could spike concurrent outbound
  connections against several providers simultaneously at exactly midnight
  UTC (or whatever this org's first tick lands on), which is precisely the
  "spike" the orchestrator's brief asked to guard against. Sequential
  execution bounds concurrent outbound canary calls to 1 at all times.
- **A dedicated multi-tick state machine (explicit "in progress" cursor
  persisted to a table).** Rejected as over-engineering for v1: the simple
  cap-and-retry-next-tick approach converges within minutes even for a
  large org (batch cap 50, ticks every 60s), and failing to fully finish a
  day's canary sweep before the next day's due-check simply means a slightly
  incomplete day's worth of `canary_runs`, not a correctness bug.

*Cost floor claim (AC5.4.10) verification method:* 5 prompts ×
`max_tokens=50` × N actively-used models, computed via the real
`pricing.compute_cost()`/`compute_self_hosted_cost()` path — see §9 for the
acceptance test that measures the actual daily figure once implemented.

**Judgment call revision — per-model canary enable/disable needs a new
table the spec's §8 checklist didn't list.** AC5.4.11 grants Org Admin the
ability to "configure per-model canary enable/disable and thresholds," but
AC5.4.6 hardcodes the drift thresholds as fixed constants and the spec's
own §8 data-model checklist lists no config table for per-model settings.
This design resolves the tension by building **only the enable/disable
half** in v1 (new minimal table `canary_model_settings`, §4) and explicitly
deferring per-model configurable thresholds (AC5.4.6's fixed
50%/20pp/0.7 thresholds stay global for every model) — flagged prominently
in this document's cover report as a spec inconsistency the architect
resolved by narrowing scope, not a silent drop.

---

### 2.3 Data Flow: Self-Hosted Model Routing (5.5)

This is the "genuine architectural lift" the product spec flagged
(judgment call #11). It is **three** separate extensions to
previously-static code, not one:

**(a) Route resolution — `resolve_route()` gains a cache-backed fallback.**

```
api/v1/gateway/chat.py::create_chat_completion  (chat.py ONLY — AC5.5.4)
        │
        route = resolve_route(body.model, self_hosted_cache)
                    │
                    ├─ 1. try providers.model_registry.resolve_model(model)
                    │       (STATIC MODEL_REGISTRY dict lookup — UNCHANGED,
                    │       always tried FIRST, so a self-hosted model id
                    │       can never shadow an existing static route)
                    │
                    └─ 2. on UnknownModelError, if self_hosted_cache is not None:
                             entry = self_hosted_cache.get(model)   # O(1) dict lookup, zero I/O
                             if entry is not None and entry.verified:
                                 return ModelRoute(
                                     provider="self_hosted",
                                     capability=ModelCapability.CHAT,
                                     native_model_id=model,
                                     self_hosted_provider_id=entry.provider_id,
                                 )
                             raise ModelNotFoundError(...)   # unknown or not-yet-verified
```

`completions.py`/`embeddings.py` call `resolve_route(body.model)` with
**no** cache argument (default `None`) — **unchanged, zero-line-diff**
behavior there, which is what structurally enforces AC5.5.4's "chat only"
constraint at the call-site level (not just a downstream capability check).

**(b) Credential fetch + dispatch — a new, simpler sibling to
`call_provider_with_failover()`.**

`self_hosted_providers` is a completely separate table from `provider_keys`
— `call_provider_with_failover()`'s `provider_key_health.select_provider_key()`
call looks up `provider_keys` rows keyed on `route.provider`, which will
find **nothing** for `route.provider == "self_hosted"` (there is no
`provider_keys` row with that value — `provider_name_enum` doesn't even
have a `self_hosted` member). Multi-key/failover is explicitly out of scope
for self-hosted this phase (spec §3 deferred list), so `chat.py` branches:

```python
if effective_route.provider == "self_hosted":
    failover = await call_self_hosted_provider(   # NEW function, common.py
        session, route=effective_route, key_provider=key_provider, call_fn=...
    )
else:
    failover = await call_provider_with_failover(...)   # UNCHANGED
```

`call_self_hosted_provider()` (new, `api/v1/gateway/common.py`):
1. `services.self_hosted_providers.get_decrypted_self_hosted_credential(session, route.self_hosted_provider_id, key_provider)` — fetches the `SelfHostedProvider` row and decrypts `ciphertext`/`nonce`/`auth_tag` via the **identical** `services.encryption.decrypt_secret()` used for `provider_keys`, AAD = `f"{org_id}:self_hosted:{self_hosted_provider_id}"` (a distinct AAD binding from provider_keys' `f"{org_id}:{provider}"`, so a ciphertext can never be swapped between a `provider_keys` row and a `self_hosted_providers` row even if both belonged to the same org). Returns an `OllamaCredential(provider="self_hosted", base_url=row.base_url, bearer_token=decrypted or "")` — **reusing `services/proxy_keys.py`'s existing `OllamaCredential` dataclass as-is**, since AC5.5.2 already establishes it's decoupled from any specific row identity.
2. Calls `call_fn(credential)` — `call_fn` dispatches to `ollama_provider.create_chat_completion`/`stream_chat_completion` (AC5.5.2 — reused verbatim, no new provider-client module).
3. On `ProviderCallError`: no retry, no failover (single endpoint, deferred scope) — re-raise unchanged.
4. On success: returns `FailoverCallResult(result=..., attempt=0, used_key_id=None)` — the **same return shape** `call_provider_with_failover()` produces, so every downstream consumer of `failover.result`/`.attempt`/`.used_key_id` (header building, usage-log writes) needs zero further branching.

**`_create_non_streaming`/`_create_streaming` in `chat.py` gain a new
`if provider == "self_hosted":` branch** dispatching to
`ollama_provider.create_chat_completion`/`stream_chat_completion` with
`native_model_id = route.native_model_id` (the admin-declared model id
string itself — no separate native-id mapping table; the string an admin
types into `self_hosted_providers.models` is both the gateway-facing model
key and the literal `model` field value sent to the self-hosted endpoint).

**(c) Cost/budget — `record_usage_charge()` needs a self-hosted branch
that bypasses `pricing.compute_cost()` entirely.**

`pricing.get_pricing_entry()` is a static `PRICING_TABLE` dict lookup — a
self-hosted model id is never in it, so calling the existing
`budget_service.record_usage_charge`/`record_team_membership_usage_charge`
unmodified for a self-hosted request would raise `PricingEntryMissingError`
on every single self-hosted request. `services/budget.py`'s two charge
functions gain an optional `precomputed_cost_usd: Decimal | None = None`
parameter — when provided, the atomic `UPDATE ... RETURNING` uses it
directly instead of calling `compute_cost()`. `api/v1/gateway/common.py`'s
`record_usage_charge()` dispatcher gains the same optional parameter and
threads it through.

`chat.py`'s two call sites (`create_chat_completion`'s non-streaming branch,
`_sse_event_stream`'s streaming finally-block) compute
`precomputed_cost_usd = compute_self_hosted_cost(provider_cost_basis,
wall_clock_latency_seconds=elapsed)` **only** when
`effective_route.provider == "self_hosted"`, where `elapsed` is read off
the **existing** `LatencyTimer` (the delta between the `pre_dispatch` mark
and `provider_response_received`/`flush_complete` — the provider's own
round-trip time, not total request latency including DLP/budget-check
overhead) and `provider_cost_basis` is `cost_basis_per_gpu_hour`, carried as
a **non-secret** field on the `SelfHostedModelRouteCache` entry itself
(config data, safe to cache — same tier as every other `*Cache` class'
values, never secret material).

**(d) Model-policy validation must ALSO accept self-hosted model ids — a
required extension beyond `resolve_route()` the product spec's judgment
call #11 does not explicitly name, but which AC5.5.6 ("addable/removable
from org baseline... with no special-casing") cannot be satisfied without.**

`services/model_policy.py::set_policy()` and `set_team_model_policy()`
both currently validate `models` against `MODEL_REGISTRY.keys()` only
(`unknown = set(models) - MODEL_REGISTRY.keys()`) — an Org Admin cannot add
a self-hosted model id to the org allow/denylist today; the PUT would be
rejected with `UnknownModelInPolicyError`. Both functions gain an
additional `self_hosted_cache: SelfHostedModelRouteCache` parameter and
widen the validation to `unknown = set(models) - MODEL_REGISTRY.keys() -
self_hosted_cache.known_model_ids()`. `content_aware_rules`' own
`set_content_aware_rule()` needs **no change** — its docstring already
states `allowed_models` is never validated against `MODEL_REGISTRY` at all.

*Explicitly out of scope, flagged as a real gap, not silently absorbed:*
`services/model_policy.py::validate_downgrade_target_model()` (Phase 4's
graceful-degradation config-time validator) is **not** extended this phase
— a degradation policy cannot target a self-hosted model as its downgrade
target in v1. This is a natural, even desirable, fast-follow (degrading a
paid model to a free self-hosted one), but the product spec never asks for
it and extending it is not required by any Phase 5 AC — see §12.

**Key Decision: `SelfHostedModelRouteCache` follows the exact
whole-snapshot-replace convention, warmed and invalidated exactly like
`ModelPolicyCache`/`ResidencyRuleCache`/Phase 4's newly-warmed caches.**

*Decision:* new class `services/self_hosted_providers.py::SelfHostedModelRouteCache`
— `get(model) -> SelfHostedRouteEntry | None`, `set_all(mapping)` full
replace, GIL-atomic reference-swap, zero lock, one instance on
`app.state.self_hosted_model_route_cache`, constructed empty at lifespan
start and warmed via a new `_warm_self_hosted_model_route_cache(app)`
helper in `main.py`, called alongside `_warm_residency_and_content_aware_caches`
etc. **This is the precedent cited in the orchestrator's brief**: this is
literally the same pattern as `ModelPolicyCache`/`ContentAwareRuleCache`
(§main.py `_lifespan`) and the Phase-4-fixed `RateLimitCache`/
`CachingSettingsCache`/`DegradationPolicyCache` (Fix 6 NFR gap — "existed
but were never constructed/warmed" is the exact mistake this design must
not repeat).

*Invalidation on write:* every self-hosted-provider admin mutation
(register/edit/remove/re-verify) — `api/v1/admin/self_hosted_providers.py`'s
four handlers — re-derives the full mapping from a fresh DB read (`SELECT *
FROM self_hosted_providers WHERE verified = true`) and calls
`cache.set_all(fresh_mapping)` **after** its own commit, mirroring
`set_content_aware_rule()`'s "push into cache immediately after commit"
convention. A full re-derive (not an incremental single-entry update) is
chosen because `self_hosted_providers` is expected to be a small,
low-write-frequency admin-config table (same size class as
`backup_groups`/`content_aware_rules`), so re-querying the whole table on
every admin write is cheap and eliminates an entire class of
incremental-update bugs `TeamModelPolicyCache.set_team()`'s more surgical
approach would otherwise need to get right per-provider.

---

### 2.4 Data Flow: Content-Classification-Aware Routing (5.3)

```
api/v1/gateway/common.py::run_dlp_scan()   (existing call site, chat.py/
                                             wherever Phase 3 already wires it)
        │
        ├─ gating (NEW — see Key Decision below):
        │     dlp_scanning_enabled = has_any_scanning_enabled(policy, custom_patterns)  (existing)
        │     content_aware_needs_classification = any(
        │         cache.get(cat) is not None and cache.get(cat).enabled
        │         for cat in ("pii", "financial_data", "source_code", "legal")
        │     )                                                                          (NEW)
        │     if not dlp_scanning_enabled and not content_aware_needs_classification:
        │         return DlpPipelineResult(redacted_texts=None, category_findings=frozenset())
        │         # byte-for-byte the old fast no-op path for an org that has
        │         # configured NEITHER DLP detectors NOR any content-aware category
        │
        ├─ sensitivity-label short-circuit (AC5.3.5):
        │     label = request header X-Gatekey-Sensitivity-Label (optional)
        │     mappings = SELECT * FROM sensitivity_label_mappings WHERE org_id=:org
        │                 (fresh per-request read — NOT a new *Cache class, same
        │                 "cheap indexed read" tier as load_dlp_policy(), not the
        │                 zero-I/O ModelPolicyCache tier — see rationale below)
        │     pretrusted_categories = {m.gatekey_category for m in mappings
        │                               if m.external_label == label}   # 0 or 1 category
        │     # an unrecognized label -> pretrusted_categories = {} -> falls through
        │     # to Gatekey's own classifiers for every category, never a hard error
        │
        ├─ services.dlp.scan_texts() — EXTENDED:
        │     - runs Presidio (pii + NEW financial_data built-in patterns) — SKIPPED
        │       for "financial_data" if it's in pretrusted_categories
        │     - runs NEW source_code heuristic (services.content_classifiers) — SKIPPED
        │       if pretrusted, else run only if content_aware "source_code" enabled
        │     - runs NEW legal heuristic — same conditional-skip logic
        │     - returns DlpScanOutcome with a NEW field:
        │         category_findings: frozenset[str]  (pii/financial_data/source_code/legal)
        │       PLUS the EXISTING `pii_detected: bool` field, now DERIVED as
        │       `"pii" in category_findings` (backward-compat — see Key Decision)
        │     category_findings |= pretrusted_categories
        │
        └─ check_content_classification(model, cache, category_findings=...)  (SIGNATURE CHANGED)
              │
              └─ resolve_content_classification(model, cache=cache, category_findings=...)
                    │  (services/model_policy.py — GENERALIZED from single-category
                    │   'pii' check into an intersection-across-matched-categories loop)
                    effective_allowed = None
                    for category in category_findings:
                        rule = cache.get(category)
                        if rule is not None and rule.enabled:
                            effective_allowed = (rule.allowed_models if effective_allowed is None
                                                  else effective_allowed & rule.allowed_models)
                    if effective_allowed is None:
                        return ALLOWED   # no enabled matched category — unchanged
                    return ALLOWED if model in effective_allowed else
                           BLOCKED(blocking_layer="content_classification")
```

**Key Decision: `category_findings: frozenset[str]` subsumes
`pii_detected: bool` — no forked code path, existing callers/tests updated,
not left running two divergent DLP result shapes.**

*Decision:* `DlpScanOutcome`/`DlpPipelineResult` gain `category_findings`
as a new field; `pii_detected` **stays in the dataclass** but its value is
now `"pii" in category_findings` — a pure derivation, computed once at
construction, never independently maintained. `resolve_content_classification()`
and `check_content_classification()` **change their keyword parameter**
from `pii_detected: bool` to `category_findings: frozenset[str]` — this is
a breaking signature change for the two direct call sites (`chat.py`, and
any Phase 3 unit test that calls either function directly with
`pii_detected=True/False`).

*Why this is safe/backward-compatible in behavior, even though it's a
breaking signature change in code:* `resolve_content_classification()`'s
new intersection logic is a strict generalization — called with
`category_findings=frozenset({"pii"})` (what every pre-Phase-5 call site
effectively meant by `pii_detected=True`) it produces byte-identical
results to today's single-category check, since the intersection loop over
a one-element set with the `"pii"` rule degenerates exactly to the current
`if pii_detected: rule = cache.get("pii"); ...` body. **What does NOT stay
unchanged**: any existing test that imports and calls
`resolve_content_classification(model, cache=cache, pii_detected=True)`
directly (bypassing `run_dlp_scan`) will fail to compile against the new
signature — these must be updated as an explicit backend-developer task
(§10), not silently left broken. `chat.py`'s one call site is updated as
part of this same task, converting
`pii_detected=dlp_result.pii_detected` → `category_findings=dlp_result.category_findings`.

*Why source_code/legal detection is gated on the content-aware rule being
enabled, not run unconditionally on every request:* unlike `pii`, these two
categories have **no DLP action** (no redact/block concept — "redact
doesn't make sense for code," per AC5.3.1) — their only consumer is
content-aware routing. Running them on every request regardless of whether
any admin has enabled the corresponding `content_aware_rules` row would be
pure wasted CPU for the (expected-common, pre-Phase-5-adoption) case where
nobody has configured these categories yet — mirrors the existing
`content_aware_pii_enabled`-gated `requires_sync_scan()` precedent exactly,
generalized to all three new categories.

*Why `sensitivity_label_mappings` gets no dedicated `*Cache` class:* this
table is read only on the already-non-zero-I/O DLP-scan code path (which
already pays `load_dlp_policy`/`load_custom_patterns`/
`get_team_dlp_override` reads per request when scanning is required) — one
more cheap indexed `SELECT` is the same tier of cost `load_dlp_policy()`
already accepts, not a candidate for the zero-I/O `ModelPolicyCache` tier
reserved for checks that run on literally every gateway request regardless
of DLP config.

---

### 2.5 Data Flow: Shadow AI Discovery (5.1)

```
Org's SASE/proxy tool (external, not Gatekey)
        │  own webhook/transform to Gatekey's generic ingestion contract
        v
POST /v1/admin/shadow-ai/ingest
  Authorization: Bearer gk_sai_...
        │
        ├─ require_shadow_ai_ingest_token   (NEW dependency, api/deps.py —
        │     NOT require_admin, NOT require_gateway_credential, NOT
        │     require_scim_token — see Key Decision below)
        │
        └─ services.shadow_ai.ingest_events(session, org_id, events)
              for each event in the submitted batch:
                  if event.destination_host in known_ai_tool_hostnames (enabled=true):
                      resolve matched_user_id via User.email == event.user_identifier
                          (best-effort, NULL if no match — AC5.1.5)
                      INSERT shadow_ai_ingest_events row
                  else:
                      DROP — never persisted (AC5.1.1's privacy-by-minimization gate)
              if enforcement_mode in ("notification", "webhook"):
                  fire notification/webhook per newly-inserted event (BackgroundTasks,
                  mirrors services.notifiers' existing deferred-delivery shape)
```

**Key Decision: `require_shadow_ai_ingest_token` is a fourth, fully
non-overlapping trust boundary — verified, not assumed, to satisfy nothing
else.**

*Storage:* new singleton-per-org table `shadow_ai_ingest_config` (org_id
PK, `ingest_token_hash BYTEA NULL`, `token_created_at`) — **hash-only
storage** (SHA-256 digest via the same `hash_secret()`
`services/service_accounts.py` already exposes), mirroring
`ScimConfig.bearer_token_hash`/`ServiceAccountKey.secret_hash`, **not** the
AES-256-GCM envelope `provider_keys`/`self_hosted_providers` use.

*This is a deliberate revision of the orchestrator's brief, flagged
explicitly per that brief's own instruction.* The brief's design-decision
prompt suggested "stored how (encrypted, same envelope as other secrets)."
This design instead uses hash-only storage, because the AES-GCM envelope
exists specifically for secrets Gatekey must later **decrypt and use
outbound** (a provider API key, a self-hosted bearer token Gatekey sends to
the self-hosted server). A shadow-AI ingest token is **inbound-only** —
Gatekey only ever needs to verify a presented token equals what it issued,
exactly the shape `ScimConfig.bearer_token_hash` already solves in this
codebase with a security-reviewed precedent. Hash-only storage is strictly
**more** secure than an encrypted envelope for this shape of credential (a
hash cannot be reversed even if the master key were later compromised,
whereas an envelope is designed to be reversible) — this is the correct
mechanism, not a downgrade.

*Verification:* `require_shadow_ai_ingest_token(request, credentials,
session) -> ShadowAiIngestContext` — checks the bearer token's `gk_sai_`
prefix, hashes it, looks up `shadow_ai_ingest_config.ingest_token_hash` by
`org_id` (single-org today, `DEFAULT_ORG_ID`), `hmac`-safe comparison.
Rejects with the same generic 401 message discipline every other
`require_*` dependency in `api/deps.py` uses.

*Confirmed non-reuse, both directions:*
- A `gk_sai_...` token can never satisfy `require_admin` (checks
  `GATEKEY_ADMIN_TOKEN` via constant-time compare, or a session cookie —
  neither path even inspects a bearer token's prefix against `gk_sai_`),
  `require_gateway_credential` (dispatches only on `gk_sk_`/`gk_pk_`
  prefixes; falls through to the generic 401 for anything else, including
  `gk_sai_`), or `require_scim_token` (compares against a **different**
  column, `scim_config.bearer_token_hash` — even a hash collision attempt
  would need to match a different row's stored digest).
- Conversely, a real admin session, service-account key, personal key, or
  SCIM token can never satisfy `require_shadow_ai_ingest_token` — that
  dependency only accepts the `gk_sai_` prefix and only compares against
  `shadow_ai_ingest_config`'s own column.

*Router placement — an explicit warning for backend-developer.* Despite
its `/v1/admin/shadow-ai/ingest` URL path (per AC5.1.3's literal spec'd
path), this route **must not** be registered on a router that also declares
`dependencies=[Depends(require_admin)]` at the router level (the pattern
`api/v1/admin/providers.py`'s `router = APIRouter(..., dependencies=[Depends(require_admin)])`
uses) — doing so would make every admin session/break-glass token
ALSO able to call the ingestion endpoint, which is a real trust-boundary
violation the "distinct trust boundary" requirement explicitly forbids.
This design puts the ingest route on its **own** router
(`api/v1/shadow_ai_ingest.py`), with `Depends(require_shadow_ai_ingest_token)`
declared **only** on that one route, while every other Shadow AI admin
surface (report, config, hostname allowlist, token generation) lives on a
**separate** router (`api/v1/admin/shadow_ai.py`) using the standard
`require_admin_or_auditor`/`require_role`/`require_team_role` dependencies.

---

## 3. API Contracts

### 3.1 New Endpoints (Admin Console)

| Endpoint | Method | Description | RBAC |
|----------|--------|-------------|------|
| `/v1/admin/audit/verify` | GET | Walk the org's chain, recompute, compare | `require_admin_or_auditor` |
| `/v1/admin/compliance-settings` (extended) | PUT | Adds `chain_enabled: bool`; rejects if `audit_retention_days` is non-null and vice versa | `require_role("org_admin")` |
| `/v1/admin/drift-detector/canary-prompts` | GET | Read-only list of the 5 code-seeded prompts | `require_admin_or_auditor` |
| `/v1/admin/drift-detector/status` | GET | Per-model status/trend table | `require_admin_or_auditor` |
| `/v1/admin/drift-detector/alerts` | GET | List `drift_alerts` | `require_admin_or_auditor` |
| `/v1/admin/drift-detector/alerts/{id}/export` | POST | Writes `AuditEntry` (`drift.alert_exported`), sets `status=exported_to_audit` | `require_admin_or_auditor` |
| `/v1/admin/drift-detector/canary-history` | GET | Per-model `canary_runs` history, filterable | `require_admin_or_auditor` |
| `/v1/admin/drift-detector/models/{model}` | PUT | Per-model canary enable/disable only (thresholds stay global — see §2.2) | `require_role("org_admin")` |
| `/v1/admin/self-hosted-providers` | GET/POST | List / register a self-hosted endpoint | `require_role("org_admin")` (write), `require_admin_or_auditor` (read) |
| `/v1/admin/self-hosted-providers/{id}` | PUT/DELETE | Edit / remove | `require_role("org_admin")` |
| `/v1/admin/self-hosted-providers/{id}/verify` | POST | Manual re-verification (`OllamaValidator.validate()` reused) | `require_role("org_admin")` |
| `/v1/admin/self-hosted-providers/{id}/usage` | GET | Requests/estimated cost/avg latency for the cost-normalization audit view | `require_admin_or_auditor` |
| `/v1/admin/content-aware-rules/sensitivity-label-mappings` | GET/POST/PUT/DELETE | CRUD for `sensitivity_label_mappings` | `require_role("org_admin")` |
| `/v1/admin/shadow-ai/ingest-token` | POST | One-time-reveal token generation/rotation — the opt-in gate (AC5.1.4) | `require_role("org_admin")` |
| `/v1/admin/shadow-ai/config` | GET/PUT | Detection source, enforcement mode, `shadow_ai_retention_days` | `require_role("org_admin")` (write), `require_admin_or_auditor` (read) |
| `/v1/admin/shadow-ai/known-hostnames` | GET/POST/DELETE | Curated allowlist CRUD | `require_role("org_admin")` (write), `require_admin_or_auditor` (read) |
| `/v1/admin/shadow-ai/report` | GET | `(user, tool, frequency_per_week, last_seen)`, filterable by team/date | `require_admin_or_auditor`, team-scoped for Team Lead (§2.5) |
| `/v1/shadow-ai-ingest/events` (mounted path `/v1/admin/shadow-ai/ingest`) | POST | Batch event ingestion — **dedicated router, dedicated dependency** (§2.5) | `require_shadow_ai_ingest_token` only |

### 3.2 Extended Existing Endpoints

#### `/v1/admin/providers` — new "Self-Hosted Models" surface lives on its own router (§3.1), not an extension of `providers.py`'s existing per-provider CRUD (self-hosted is a separate table, not a `ProviderName` enum member).

#### `/v1/admin/model-policy` (PUT) and `/v1/admin/teams/{id}/model-policy` (PUT)
No request-shape change — `models: list[str]` now additionally accepts any
verified self-hosted model id (validated against the widened
`self_hosted_cache.known_model_ids()` union, §2.3(d)).

### 3.3 New Gateway Request Header

| Header | Values | Description |
|--------|--------|--------------|
| `X-Gatekey-Sensitivity-Label` | free text | Optional pre-set classification label (AC5.3.5); unrecognized values silently ignored, never a hard error |

No new response headers this phase (unlike Phase 4). Self-hosted routing,
drift canaries, and the audit chain are all invisible on the wire to a
caller — OpenAI-compatible surface unchanged.

---

## 4. Data Model Changes

### 4.1 Modified Tables

#### `audit_entries` (additive only — migration `0037`)
```sql
ALTER TABLE audit_entries ADD COLUMN chain_hash TEXT NULL;
ALTER TABLE audit_entries ADD COLUMN prev_hash TEXT NULL;   -- NULL only at true genesis
ALTER TABLE audit_entries ADD COLUMN chain_seq BIGINT NULL;

CREATE UNIQUE INDEX uq_audit_entries_org_id_chain_seq
    ON audit_entries (org_id, chain_seq) WHERE chain_seq IS NOT NULL;
CREATE INDEX ix_audit_entries_org_id_chain_seq_desc
    ON audit_entries (org_id, chain_seq DESC) WHERE chain_seq IS NOT NULL;
```

#### `compliance_settings` (additive — migration `0038`)
```sql
ALTER TABLE compliance_settings ADD COLUMN chain_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE compliance_settings ADD CONSTRAINT chk_chain_purge_mutually_exclusive
    CHECK (NOT (chain_enabled AND audit_retention_days IS NOT NULL));
```
DB-level `CHECK` as a defense-in-depth backstop on top of the app-layer
validation in `set_compliance_settings`/`set_chain_enabled` — mirrors this
codebase's existing convention of pairing an app-layer business rule with a
DB-level sanity bound (see `0036`'s degradation-threshold `CHECK`).

#### `usage_logs` (additive — migration `0040`, alongside `self_hosted_providers`)
```sql
ALTER TABLE usage_logs ADD COLUMN self_hosted_provider_id UUID NULL
    REFERENCES self_hosted_providers(id) ON DELETE SET NULL;
CREATE INDEX ix_usage_logs_self_hosted_provider_id ON usage_logs (self_hosted_provider_id);
```
`usage_logs.provider` (existing plain-string column) takes the literal
value `"self_hosted"` for these rows — no `provider_name_enum` migration
needed (that Postgres enum type is untouched this phase).

### 4.2 New Tables

#### `canary_prompts` (migration `0039`)
```sql
CREATE TABLE canary_prompts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_text TEXT NOT NULL,
    label TEXT NOT NULL,              -- 'factual' | 'creative' | 'refusal_probe'
    max_tokens INTEGER NOT NULL DEFAULT 50 CHECK (max_tokens > 0 AND max_tokens <= 200),
    enabled BOOLEAN NOT NULL DEFAULT true
);
-- Code-seeded via a data migration (5 fixed rows) — mirrors PRICING_TABLE's
-- "hand-curated, in-code" posture, but persisted (not a Python dict) since
-- canary_runs FK-references prompt_id.
```

#### `canary_model_settings` (migration `0039` — new, not in the spec's §8
checklist; resolves the AC5.4.6/AC5.4.11 tension, §2.2)
```sql
CREATE TABLE canary_model_settings (
    model TEXT PRIMARY KEY,
    enabled BOOLEAN NOT NULL DEFAULT true
);
-- Absence of a row = enabled (permissive default, same absence-of-row
-- convention every other config table in this codebase uses).
```

#### `canary_baselines` (migration `0039`)
```sql
CREATE TABLE canary_baselines (
    model TEXT NOT NULL,
    prompt_id UUID NOT NULL REFERENCES canary_prompts(id) ON DELETE CASCADE,
    baseline_latency_ms NUMERIC(10,2) NOT NULL,
    baseline_refusal_rate NUMERIC(5,4) NOT NULL,
    baseline_output_text TEXT NOT NULL,
    established_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (model, prompt_id)
);
```

#### `canary_runs` (migration `0039`)
```sql
CREATE TABLE canary_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model TEXT NOT NULL,
    prompt_id UUID NOT NULL REFERENCES canary_prompts(id) ON DELETE CASCADE,
    run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    output_text TEXT NOT NULL,          -- synthetic content — see AC5.4.3, not user traffic
    latency_ms INTEGER NOT NULL,
    refusal_detected BOOLEAN NOT NULL,
    similarity_score_vs_baseline NUMERIC(5,4) NULL,   -- NULL until a baseline exists
    cost_usd NUMERIC(20,10) NOT NULL,
    is_canary BOOLEAN NOT NULL DEFAULT true
);
CREATE INDEX ix_canary_runs_model_run_at ON canary_runs (model, run_at DESC);
```

#### `drift_alerts` (migration `0039`)
```sql
CREATE TABLE drift_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model TEXT NOT NULL,
    metric TEXT NOT NULL CHECK (metric IN ('latency', 'refusal_rate', 'output_similarity')),
    baseline_value NUMERIC(10,4) NOT NULL,
    observed_value NUMERIC(10,4) NOT NULL,
    delta_pct NUMERIC(6,2) NOT NULL,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'exported_to_audit'))
);
CREATE INDEX ix_drift_alerts_model_detected_at ON drift_alerts (model, detected_at DESC);
```

#### `self_hosted_providers` (migration `0040`)
```sql
CREATE TABLE self_hosted_providers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    base_url TEXT NOT NULL,
    ciphertext BYTEA NOT NULL,
    nonce BYTEA NOT NULL,
    auth_tag BYTEA NOT NULL,
    cost_basis_per_gpu_hour NUMERIC(10,4) NOT NULL CHECK (cost_basis_per_gpu_hour > 0),
    verified BOOLEAN NOT NULL DEFAULT false,
    models JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_self_hosted_providers_org_id_name UNIQUE (org_id, name)
);
```
`ciphertext`/`nonce`/`auth_tag` — the identical three-column AES-256-GCM
envelope shape `provider_keys` uses (§2.3(b) for the distinct AAD binding).
No plaintext `bearer_token` column, ever.

#### `content_aware_rules` — data-only seed migration (`0041`, no schema change)
```sql
INSERT INTO content_aware_rules (org_id, category, enabled, allowed_models)
VALUES
  (:default_org_id, 'source_code', false, '[]'::jsonb),
  (:default_org_id, 'financial_data', false, '[]'::jsonb),
  (:default_org_id, 'legal', false, '[]'::jsonb)
ON CONFLICT (org_id, category) DO NOTHING;
```
`ON CONFLICT DO NOTHING` — idempotent, and defensive against an admin
already having created a `source_code`/`financial_data` row manually via
the existing generic `PUT /v1/admin/content-aware-rules/{category}` route
(the category string was never restricted, per that table's own
docstring), which would otherwise conflict.

#### `sensitivity_label_mappings` (migration `0041`)
```sql
CREATE TABLE sensitivity_label_mappings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    external_label TEXT NOT NULL,
    gatekey_category TEXT NOT NULL,
    CONSTRAINT uq_sensitivity_label_mappings_org_label UNIQUE (org_id, external_label)
);
```

#### `shadow_ai_ingest_events` (migration `0042`)
```sql
CREATE TABLE shadow_ai_ingest_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    user_identifier TEXT NOT NULL,
    matched_user_id UUID NULL REFERENCES users(id) ON DELETE SET NULL,
    destination_host TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('sase_log', 'proxy_log')),
    raw_metadata JSONB NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_shadow_ai_ingest_events_org_created ON shadow_ai_ingest_events (org_id, created_at);
CREATE INDEX ix_shadow_ai_ingest_events_matched_user ON shadow_ai_ingest_events (matched_user_id);
```

#### `known_ai_tool_hostnames` (migration `0042`, data-seeded)
```sql
CREATE TABLE known_ai_tool_hostnames (
    hostname TEXT PRIMARY KEY,
    tool_label TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true
);
-- Seed data: api.openai.com, chat.openai.com, chatgpt.com, claude.ai,
-- chat.deepseek.com, gemini.google.com, api.anthropic.com (org's own
-- sanctioned Anthropic traffic already goes through Gatekey, but a direct
-- call to this host is still an unsanctioned bypass by definition).
```

#### `shadow_ai_ingest_config` (migration `0042`)
```sql
CREATE TABLE shadow_ai_ingest_config (
    org_id UUID PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
    ingest_token_hash BYTEA NULL,       -- NULL = ingestion not yet set up (AC5.1.4)
    token_created_at TIMESTAMPTZ NULL,
    detection_source TEXT NOT NULL DEFAULT 'sase_log' CHECK (detection_source IN ('sase_log', 'proxy_log')),
    enforcement_mode TEXT NOT NULL DEFAULT 'detect_only'
        CHECK (enforcement_mode IN ('detect_only', 'notification', 'webhook')),
    webhook_url TEXT NULL,
    shadow_ai_retention_days INTEGER NOT NULL DEFAULT 90 CHECK (shadow_ai_retention_days > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```
`webhook_url` follows the existing `team.webhook_url`-style
encrypted-at-rest-if-secret convention noted in `errors.py`'s redaction
list — added to `_REDACTED_FIELD_NAMES` if not already covered by the
generic `webhook_url` key (it already is, per `errors.py`'s existing list).

### 4.3 Migration Sequencing Summary

| # | Content | Depends on |
|---|---------|------------|
| `0037` | `audit_entries` chain columns | — |
| `0038` | `compliance_settings.chain_enabled` + CHECK | `0037` (same feature, ordered for readability) |
| `0039` | `canary_prompts` (+ data seed), `canary_model_settings`, `canary_baselines`, `canary_runs`, `drift_alerts` | — |
| `0040` | `self_hosted_providers`, `usage_logs.self_hosted_provider_id` | — |
| `0041` | `content_aware_rules` data seed, `sensitivity_label_mappings` | — |
| `0042` | `shadow_ai_ingest_events`, `known_ai_tool_hostnames` (+ data seed), `shadow_ai_ingest_config` | — |

All six are independent of each other (no cross-migration FK dependencies)
— database-admin can build/test them in parallel; only the within-feature
ordering (`0037`→`0038`) matters.

---

## 5. Integration Points — Mandatory Wiring Checklist

**This section is the direct answer to the Phase 4 post-mortem.** Every row
below names the exact existing file that must import/call new Phase 5 code,
and the exact registration step in `main.py`. A sub-feature is not "done"
until every row for it is checked off — a service module that exists but
whose row below is unimplemented is exactly the Phase 4 failure mode.

### 5.1 (Ledger, 5.2)

| # | Wiring | Exact location |
|---|--------|-----------------|
| 1 | `write_audit_entry` computes chain fields | `services/audit.py::write_audit_entry` — extend the function body itself (the sole INSERT path, per its own docstring — no new call site needed, every existing caller gets chaining for free) |
| 2 | Purge job skips chained orgs | `services/scheduler.py::run_audit_purge_if_due` — add `if compliance.chain_enabled: return 0` immediately after the existing `compliance = await get_effective_compliance_settings(session)` line, before the `audit_retention_days is None` check |
| 3 | Mutual-exclusivity validation | `services/compliance_settings.py::set_compliance_settings`/new `set_chain_enabled` — reject with a structured 422 if the incoming state would violate the CHECK |
| 4 | Verify endpoint registered | `main.py` — new `from gatekey.api.v1.admin.audit_chain import router as admin_audit_chain_router`; `app.include_router(admin_audit_chain_router)` in the `app.include_router(...)` block, alongside `admin_audit_entries_router` (~line 614) |
| 5 | Export includes chain columns | `api/v1/admin/audit_entries.py::_export_row_dict`/`_CSV_COLUMNS` — extend conditionally on `compliance.chain_enabled`, fetched once at the top of `list_audit_entries_endpoint` |
| 6 | Admin UI badge + Verify now | `frontend/src/components/audit-entries.tsx` — extend to call `GET /v1/admin/audit/verify` and render the badge/empty-state |

### 5.2 (Drift Detector, 5.4)

| # | Wiring | Exact location |
|---|--------|-----------------|
| 1 | New tick added to the scheduler loop | `services/scheduler.py::run_scheduler_loop` — new 5th `try/except` block calling `await run_drift_canary_if_due(session, app)`, appended **after** the existing `run_provider_key_health_check_if_due` block |
| 2 | In-memory due-check marker | `services/scheduler.py::run_drift_canary_if_due` — `app.state.last_drift_canary_check_at`, `DRIFT_CANARY_CHECK_INTERVAL_SECONDS = 24*60*60` module constant, identical shape to `PROVIDER_KEY_HEALTH_CHECK_INTERVAL_SECONDS`'s marker |
| 3 | Canary suite service module | New `services/drift_detector.py` — imported **only** from `services/scheduler.py` (the tick) and `api/v1/admin/drift_detector.py` (read endpoints) |
| 4 | Admin router registered | `main.py` — `from gatekey.api.v1.admin.drift_detector import router as admin_drift_detector_router`; `app.include_router(admin_drift_detector_router)` |
| 5 | Export-to-audit-log writes a real `AuditEntry` | `api/v1/admin/drift_detector.py`'s export handler calls `services.audit.write_audit_entry(..., action="drift.alert_exported", ...)` then commits — reuses the existing audit action vocabulary extension point (`services/audit.py`'s module docstring list gains this one new action string) |
| 6 | Admin UI tab | `frontend/src/components/` — new `drift-detector.tsx`, wired into the Differentiators nav section of `ConsoleShell.tsx` |

### 5.3 (Self-Hosted Governance, 5.5)

| # | Wiring | Exact location |
|---|--------|-----------------|
| 1 | `SelfHostedModelRouteCache` constructed + warmed | `main.py::_lifespan` — `app.state.self_hosted_model_route_cache = SelfHostedModelRouteCache()`; new `_warm_self_hosted_model_route_cache(app)` helper called alongside `_warm_residency_and_content_aware_caches` |
| 2 | Cache fetched via a new `api/deps.py` dependency | `api/deps.py::get_self_hosted_model_route_cache(request) -> SelfHostedModelRouteCache` — same one-line shape as `get_model_policy_cache` |
| 3 | `resolve_route()` extended | `api/v1/gateway/common.py::resolve_route` — new optional `self_hosted_cache` parameter, fallback logic added (§2.3(a)) |
| 4 | **Only** `chat.py` passes the cache | `api/v1/gateway/chat.py::create_chat_completion` — add `self_hosted_cache: SelfHostedModelRouteCache = Depends(get_self_hosted_model_route_cache)` to the handler signature, pass to `resolve_route(body.model, self_hosted_cache)`. **`completions.py`/`embeddings.py` are explicitly NOT touched** — this is the AC5.5.4 enforcement point |
| 5 | New credential-fetch + dispatch path | `services/self_hosted_providers.py::get_decrypted_self_hosted_credential`; `api/v1/gateway/common.py::call_self_hosted_provider` (new function, §2.3(b)) |
| 6 | `chat.py`'s dispatch functions gain a branch | `api/v1/gateway/chat.py::_create_non_streaming`/`_create_streaming` — add `if provider == "self_hosted": return await ollama_provider.create_chat_completion(...)` branch |
| 7 | `chat.py`'s call sites branch on failover-vs-self-hosted | `api/v1/gateway/chat.py::create_chat_completion` — both the streaming and non-streaming try-blocks: `if effective_route.provider == "self_hosted": call_self_hosted_provider(...) else: call_provider_with_failover(...)` |
| 8 | Self-hosted cost computed and charged | `services/budget.py::record_usage_charge`/`record_team_membership_usage_charge` — new `precomputed_cost_usd` parameter; `api/v1/gateway/common.py::record_usage_charge` dispatcher threads it through; `chat.py`'s two charge call sites (non-streaming + `_sse_event_stream`'s finally block) compute `compute_self_hosted_cost(...)` and pass it when `effective_route.provider == "self_hosted"` |
| 9 | `usage_logs.self_hosted_provider_id` populated | `services/usage_logs.py::record_usage_log` — new parameter; `chat.py`'s `record_usage_log(...)` calls pass `self_hosted_provider_id=effective_route.self_hosted_provider_id` |
| 10 | Model-policy validation widened | `services/model_policy.py::set_policy`/`set_team_model_policy` — new `self_hosted_cache` parameter, widened `unknown =` computation (§2.3(d)) |
| 11 | Cache invalidated on every admin write | `api/v1/admin/self_hosted_providers.py` — each of the four handlers (register/edit/remove/re-verify) re-derives the full mapping and calls `cache.set_all(...)` after its own commit |
| 12 | Admin router registered | `main.py` — `from gatekey.api.v1.admin.self_hosted_providers import router as admin_self_hosted_providers_router`; `app.include_router(admin_self_hosted_providers_router)` |
| 13 | Admin UI | `frontend/src/components/ProviderKeyForm.tsx`-adjacent new "Self-Hosted Models" card; new `differentiators-self-hosted.tsx` cross-link tab |

### 5.4 (Content-Classification Routing, 5.3)

| # | Wiring | Exact location |
|---|--------|-----------------|
| 1 | `DlpScanOutcome`/`scan_texts()` extended | `services/dlp.py` — new `category_findings` field/computation, new source_code/legal heuristic functions in a new `services/content_classifiers.py`, imported by `services/dlp.py` |
| 2 | Financial-data Presidio patterns added | `services/dlp.py::build_analyzer_engine` — extend the `RecognizerRegistry` with new pattern recognizers for bank account/IBAN/SWIFT-BIC; extend `_DETECTOR_ENTITY_MAP`/`_ENTITY_CATEGORY_MAP` |
| 3 | `run_dlp_scan()` gating extended | `api/v1/gateway/common.py::run_dlp_scan` — new `content_aware_needs_classification` computation, sensitivity-label lookup, threaded into `scan_texts()`'s call |
| 4 | `check_content_classification`/`resolve_content_classification` signature change | `api/v1/gateway/common.py` and `services/model_policy.py` — both change `pii_detected: bool` → `category_findings: frozenset[str]` (§2.4) |
| 5 | `chat.py`'s call site updated | `api/v1/gateway/chat.py::create_chat_completion` — `check_content_classification(body.model, content_aware_cache, category_findings=dlp_result.category_findings)` |
| 6 | Existing Phase 3 unit tests updated | `tests/unit/` — every direct caller of `resolve_content_classification(..., pii_detected=...)` updated to the new keyword; explicit backend-developer task, not silently deferred |
| 7 | Sensitivity-label mappings CRUD registered | `main.py` — new admin router for `sensitivity_label_mappings`, included alongside `admin_content_aware_rules_router` |
| 8 | Admin UI reframing | `frontend/src/components/` — Content-Aware Routing tab's "Classification source" radio group replaced with an additive mapping-table component (per AC5.3.6's explicit UI reframing) |

### 5.5 (Shadow AI, 5.1)

| # | Wiring | Exact location |
|---|--------|-----------------|
| 1 | New auth dependency | `api/deps.py::require_shadow_ai_ingest_token` — new function, parallel to `require_scim_token` |
| 2 | Ingest router on its own router, own dependency | New `api/v1/shadow_ai_ingest.py` — `router = APIRouter(prefix="/v1/admin/shadow-ai")`, the one POST route declares `Depends(require_shadow_ai_ingest_token)` **on the route itself**, not the router |
| 3 | Ingest router registered | `main.py` — `from gatekey.api.v1.shadow_ai_ingest import router as shadow_ai_ingest_router`; `app.include_router(shadow_ai_ingest_router)` |
| 4 | Admin config/report/token-gen router registered separately | `main.py` — `from gatekey.api.v1.admin.shadow_ai import router as admin_shadow_ai_router`; `app.include_router(admin_shadow_ai_router)` |
| 5 | Purge job added to scheduler tick | `services/scheduler.py::run_scheduler_loop` — new 6th (or 7th, after drift canary) try/except block calling `await run_shadow_ai_purge_if_due(session)`; new function mirrors `run_log_prompt_purge_if_due`'s "always fires against a finite cutoff, no interval-gating" shape (not the health-check/drift-canary interval-gated shape), since `shadow_ai_retention_days` is never NULL |
| 6 | Team Lead scoping on the report endpoint | `api/v1/admin/shadow_ai.py::get_shadow_ai_report` — resolves the caller's led team(s) via existing `services/teams.py` membership helpers, forces/validates the `team_id` filter |
| 7 | Data-handling policy artifact | `docs/policy/shadow-ai-data-handling.md` — new file, linked from the admin UI's "View policy" |
| 8 | Admin UI tab | `frontend/src/components/` — new `shadow-ai.tsx`, wired into `ConsoleShell.tsx`'s Differentiators nav |

---

## 6. Deployment Considerations

### 6.1 No new infrastructure dependency

Unlike Phase 4 (which introduced Redis as an optional profile), Phase 5
adds **zero** new external services — no timestamping/anchoring service
(5.2, explicitly deferred), no embeddings API (5.3/5.4), no vendor SASE
adapter (5.1). Every sub-feature runs against the existing Postgres +
in-process-cache + scheduler-loop stack. Self-hosted providers (5.5) are
themselves the "new infrastructure," but they are admin-registered targets,
not a Gatekey-managed dependency.

### 6.2 Scheduler tick budget

`run_scheduler_loop` now runs **7** sequential try/except blocks per 60s
tick (rotation, audit purge, log/prompt purge, provider-key health check,
drift canary, shadow-AI purge — 5.2's chain has no tick). Every job besides
drift-canary is a cheap no-op in the common case (interval-gated or
naturally-idempotent DELETE); the drift-canary job is the one that can do
real, potentially slow work (sequential outbound HTTP calls) — bounded by
`_CANARY_MAX_MODELS_PER_TICK` (§2.2) so a single tick cannot block the loop
indefinitely even for a large self-hosted deployment.

### 6.3 Hash-chain enable/backfill operational guidance

Document in the Phase 5 admin docs (docs-writer task): recommend enabling
the hash chain during a low-traffic window for orgs with a large existing
`audit_entries` table, since the backfill (§2.1) holds a row lock for its
full duration.

### 6.4 Environment/config additions

No new required environment variables. `DRIFT_CANARY_CHECK_INTERVAL_SECONDS`,
`_CANARY_MAX_MODELS_PER_TICK` are module constants (mirroring
`PROVIDER_KEY_HEALTH_CHECK_INTERVAL_SECONDS`'s precedent — hardcoded, not
`Settings`-configurable, consistent with this codebase's existing scheduler
constants).

---

## 7. Error Handling and Edge Cases

### 7.1 Hash Chain

| Scenario | Handling |
|----------|----------|
| Chain-enable requested while `audit_retention_days` is finite | 422, structured error naming which of the two settings must change first |
| Verify called before chain ever enabled | `{"status": "not_enabled"}` (or the UI's own empty-state copy) — never a false "intact" |
| A row's `old_value`/`new_value` mutated directly via raw SQL (tamper simulation) | `GET /v1/admin/audit/verify` returns `"status": "broken"` naming the exact `id`/`chain_seq` |
| Backfill interrupted mid-transaction (process crash) | Entire transaction rolls back — `chain_enabled` stays `false`, no partially-chained state ever visible; admin retries the enable action |

### 7.2 Drift Detector

| Scenario | Handling |
|----------|----------|
| One model's provider call fails during a canary sweep | Caught per-model, logged, sweep continues with the remaining models — a single bad provider never drops the whole day's batch |
| No key configured for an actively-used model's provider | Skip that model for this sweep, log a warning — never crash the tick |
| Fewer than 7 days of runs exist for a (model, prompt) pair | No baseline yet, `similarity_score_vs_baseline = NULL`, no drift flagging possible for that pair until day 7 |
| Self-hosted model canary-tested | Cost computed via `compute_self_hosted_cost()`, same `canary_runs.cost_usd`-only rule |

### 7.3 Self-Hosted Routing

| Scenario | Handling |
|----------|----------|
| Self-hosted model id collides with a static `MODEL_REGISTRY` key | Static registry always wins (checked first in `resolve_route`) — admin console should validate at registration time and reject the collision with a clear message |
| Self-hosted provider not yet verified, model requested | `resolve_route` treats it as unknown (`entry.verified` check) → `ModelNotFoundError`, same 404 shape as any unknown model |
| Self-hosted endpoint unreachable at request time | `ollama_provider.create_chat_completion` raises `ProviderCallError` → `call_self_hosted_provider` re-raises unchanged (no retry) → `ProviderUpstreamError` (502), same shape a BYOK provider outage produces |
| `/v1/completions` or `/v1/embeddings` request for a self-hosted model id | `resolve_route(body.model)` (no cache arg) never finds it → `ModelNotFoundError` (404), structurally enforced, not a runtime check that could be forgotten |

### 7.4 Content Classification

| Scenario | Handling |
|----------|----------|
| Request triggers `financial_data` (allowed={A,B}) and `legal` (allowed={B,C}), both enabled | Intersection = {B} — only B is allowed |
| Same, but allowed sets are disjoint | Intersection = {} — request blocked, `blocking_layer="content_classification"` |
| `X-Gatekey-Sensitivity-Label` value matches no configured mapping | Silently ignored — falls through to Gatekey's own classifiers for every category, never a hard error |
| Sensitivity label pre-trusts `financial_data`, request also independently contains real PII | `pii` still runs Gatekey's own classifier (the label only short-circuits the ONE category it maps to) — `category_findings` ends up `{"financial_data", "pii"}` |

### 7.5 Shadow AI

| Scenario | Handling |
|----------|----------|
| Ingestion request before setup (no token generated) | 401 — `shadow_ai_ingest_config` row absent or `ingest_token_hash IS NULL` |
| Batch contains hosts both on and off the curated allowlist | Only matched-hostname rows persist; the rest silently dropped, not logged with any identifying content |
| `user_identifier` doesn't match any known Gatekey user email | Row still persists, `matched_user_id = NULL`, surfaced in the report as "not linked to a Gatekey user" |
| Webhook enforcement configured but the org's webhook endpoint is down | Best-effort delivery, logged failure, never blocks the ingestion request's own 2xx response |

---

## 8. Security Considerations

| Concern | Mitigation |
|---------|------------|
| Self-hosted `bearer_token` at rest | Identical AES-256-GCM envelope to `provider_keys`, distinct AAD binding (`org_id:self_hosted:{id}`) preventing cross-row ciphertext swap |
| Shadow-AI ingest token | Hash-only storage (never reversible), own trust boundary, verified non-overlapping with every other credential type (§2.5) |
| Hash-chain tamper detection | Recomputation is deterministic and includes every mutable field (`old_value`/`new_value`/`source_ip`/`created_at`) — a raw-SQL `UPDATE` to any field breaks verification |
| Canary prompts | Fixed, code-seeded, non-user content — no injection surface from admin-supplied prompt text in v1 (prompt authoring deferred) |
| Sensitivity-label header | Purely a routing hint, never trusted for anything security-critical beyond which classifier to skip — an attacker presenting a fabricated label can at worst cause Gatekey to skip re-deriving a category its own classifier would have found anyway for ONE category; it can never suppress DLP redaction/block actions (which are driven by the org's own DLP policy, not by content-classification routing) |
| Self-hosted cost figure feeds real budget enforcement | Visibly labeled "estimated" everywhere it's surfaced (UI + export), per AC5.5.7's explicit requirement — never presented as invoice-grade |

---

## 9. Testing Strategy

### 9.1 Integration Test Scenarios

| Test Scenario | Priority | Test Type |
|---------------|----------|-----------|
| Two concurrent `write_audit_entry` calls for the same org never fork the chain | P0 | Integration (concurrency) |
| Tamper one historical row via raw SQL → `verify` returns `"broken"` naming the entry | P0 | Integration |
| Enable chain while `audit_retention_days` is finite → rejected | P0 | Integration |
| Full historical backfill produces a verifiable chain from true genesis | P0 | Integration |
| Canary tick: zero new `usage_logs` rows, zero `current_spend_usd` change, nonzero bounded `canary_runs.cost_usd` sum | P0 | Integration |
| Daily canary cost for a representative multi-model org stays under a documented ceiling | P1 | Load/cost measurement |
| Self-hosted chat request flows through DLP/residency/budget identically to a BYOK request | P0 | Integration |
| Self-hosted model id collision with a static `MODEL_REGISTRY` key resolves to the static route | P0 | Unit |
| Self-hosted model rejected at `/v1/completions`/`/v1/embeddings` | P0 | Integration |
| Multi-category content-classification intersection blocking (disjoint sets) | P0 | Integration |
| Sensitivity-label short-circuit skips the mapped category's classifier only | P1 | Unit |
| Shadow-AI ingest drops unmatched-hostname rows, persists matched ones | P0 | Integration |
| Shadow-AI ingestion token cannot authenticate any other endpoint, and vice versa | P0 | Security/integration |
| Shadow-AI report Team Lead scoping returns only that team's members | P0 | Integration |

### 9.2 Mocking Strategy

| External Service | Mock Approach |
|-------------------|----------------|
| Canary provider calls | Reuse the existing `pytest-httpx`/`respx` provider mocks from Phases 1/4 |
| Self-hosted endpoint | `respx` mock against a fake `base_url`, exercising `OllamaValidator`/`ollama_provider` unmodified |
| Shadow-AI ingestion batches | Direct FastAPI `TestClient` POST with fixture batches, no external mock needed |

### 9.3 Regression Coverage (explicit, per the anti-Phase-4-mistake instruction)

Every existing Phase 1–4 gateway integration test suite must be re-run
unmodified after this phase's `chat.py`/`common.py`/`dlp.py`/`model_policy.py`/
`budget.py` changes and pass with byte-identical behavior for any org that
never configures a Phase 5 feature — this is the explicit regression gate
for the `resolve_route()`, `check_content_classification()`, and
`record_usage_charge()` signature changes documented in §2.3/§2.4.

---

## 10. Implementation Tasks

### 10.1 Database Tasks (Database Admin)

| Task | Priority | Dependencies |
|------|----------|--------------|
| Migration `0037`: `audit_entries` chain columns + unique partial index | P0 | — |
| Migration `0038`: `compliance_settings.chain_enabled` + CHECK | P0 | `0037` |
| Migration `0039`: `canary_prompts` (+ data seed), `canary_model_settings`, `canary_baselines`, `canary_runs`, `drift_alerts` | P0 | — |
| Migration `0040`: `self_hosted_providers`, `usage_logs.self_hosted_provider_id` | P0 | — |
| Migration `0041`: `content_aware_rules` data seed, `sensitivity_label_mappings` | P0 | — |
| Migration `0042`: `shadow_ai_ingest_events`, `known_ai_tool_hostnames` (+ data seed), `shadow_ai_ingest_config` | P0 | — |
| ORM models for every new table | P0 | Matching migration |
| Test every migration's `upgrade()`/`downgrade()` on dev DB | P1 | All above |

**Parallelism:** all six migrations are schema-independent of each other
(no cross-FKs) and can be built/tested fully in parallel; only
`0037`→`0038`'s ordering matters within the ledger feature.

### 10.2 Backend Tasks (Backend Developer)

| Task | Priority | Dependencies |
|------|----------|--------------|
| **5.2 Ledger** | | |
| Extend `write_audit_entry` with chain computation + `FOR UPDATE` lock | P0 | `0037`/`0038` |
| `set_chain_enabled` with atomic backfill | P0 | `0037`/`0038` |
| Mutual-exclusivity validation in `set_compliance_settings` | P0 | `0038` |
| `run_audit_purge_if_due` chain-aware guard | P0 | `0038` |
| **Wire** purge-guard edit into the existing scheduler tick (§5.1 row 2) | P0 | Above |
| `GET /v1/admin/audit/verify` endpoint + router | P0 | Chain computation |
| **Wire** verify router into `main.py` (§5.1 row 4) | P0 | Above |
| Extend audit export with chain columns | P1 | Chain computation |
| **5.4 Drift Detector** (can start in parallel with 5.2) | | |
| `services/drift_detector.py`: canary suite runner, refusal heuristic, similarity heuristic, threshold flagging, baseline establishment | P0 | `0039` |
| `run_drift_canary_if_due` | P0 | Above |
| **Wire** the new tick into `run_scheduler_loop` (§5.1 row 1) | P0 | Above |
| Admin endpoints (`api/v1/admin/drift_detector.py`) | P0 | Above |
| **Wire** admin router into `main.py` (§5.1 row 4) | P0 | Above |
| **5.5 Self-Hosted Governance** (depends on 5.4 completing first per build order, but is architecturally independent — can overlap with 5.4's tail if capacity allows) | | |
| `services/self_hosted_providers.py`: CRUD, `SelfHostedModelRouteCache`, credential decrypt | P0 | `0040` |
| Extend `resolve_route()` with cache fallback | P0 | Above |
| **Wire** `chat.py` to pass the cache (ONLY chat.py) | P0 | Above |
| `call_self_hosted_provider()` + `chat.py` dispatch branch (both streaming/non-streaming) | P0 | Above |
| Extend `budget_service.record_usage_charge`/team variant with `precomputed_cost_usd` | P0 | — (independent of self-hosted specifically, but only exercised by it this phase) |
| **Wire** `chat.py`'s two charge call sites to compute self-hosted cost | P0 | Above two |
| Extend `usage_logs.record_usage_log` with `self_hosted_provider_id` | P0 | `0040` |
| Widen `set_policy`/`set_team_model_policy` validation | P0 | `SelfHostedModelRouteCache` |
| **Wire** cache warm into `main.py` lifespan + `api/deps.py` dependency | P0 | Above |
| **Wire** cache invalidation into all 4 admin CRUD handlers | P0 | Above |
| Admin endpoints + router registration | P0 | Above |
| **5.3 Content-Classification Routing** (depends on 5.5 per build order) | | |
| `services/content_classifiers.py`: source_code/legal heuristics | P0 | — |
| Extend `services/dlp.py`: financial_data Presidio patterns, `category_findings` | P0 | Above |
| Generalize `resolve_content_classification`/`check_content_classification` | P0 | Above |
| **Wire** `chat.py`'s call site to the new signature | P0 | Above |
| **Update** every existing Phase 3 unit test calling the old `pii_detected=` signature | P0 | Above |
| `sensitivity_label_mappings` CRUD + request-time lookup wiring | P0 | `0041` |
| **5.1 Shadow AI** (highest lift, build last) | | |
| `require_shadow_ai_ingest_token` in `api/deps.py` | P0 | `0042` |
| `services/shadow_ai.py`: ingestion, matching, report aggregation | P0 | `0042` |
| Ingest router (own file, own dependency) — **not** on the admin-RBAC router | P0 | Above |
| **Wire** ingest router registration into `main.py` | P0 | Above |
| Admin config/report/token-gen router | P0 | Above |
| **Wire** admin router registration into `main.py` | P0 | Above |
| `run_shadow_ai_purge_if_due` | P0 | `0042` |
| **Wire** the new tick into `run_scheduler_loop` | P0 | Above |
| Team Lead report scoping | P0 | `services/teams.py` membership helpers |
| Notification/webhook enforcement delivery (`BackgroundTasks`) | P1 | Above |
| `docs/policy/shadow-ai-data-handling.md` artifact | P0 | — (docs-writer, but blocks AC5.1.9's NFR — coordinate) |

**Parallelism:** 5.2 and 5.4 can be developed fully in parallel (both are
self-contained, no shared code touched). 5.5 touches shared gateway-pipeline
code (`chat.py`, `common.py`, `budget.py`, `model_policy.py`) and should not
be developed concurrently with 5.3 (which touches an overlapping set —
`common.py`, `model_policy.py`, `dlp.py`) by two different engineers without
tight coordination, since both modify `check_content_classification`'s
neighborhood and `chat.py`'s pipeline — the spec's own build order
(5.5 before 5.3) exists partly for this reason. 5.1 is fully independent of
every other sub-feature (new files only, no shared-code edits) and can be
developed in parallel with any of the above once its migration lands.

### 10.3 Frontend Tasks (Frontend Developer)

| Task | Priority | Dependencies |
|------|----------|--------------|
| Audit Log tab: hash-chain badge + "Verify now" | P0 | Backend API |
| Drift Detector tab (status table, alert detail, export button) | P0 | Backend API |
| Providers screen: Self-Hosted Models card (register/edit/remove) | P0 | Backend API |
| Differentiators → Self-Hosted Governance cross-link tab | P1 | Backend API |
| Model Policy → Static tab: Self-Hosted provider group in the checklist | P1 | Backend API |
| Model Policy → Content-Aware Routing tab: 4-category table, sensitivity-label mapping table (reframed, not exclusive radio) | P0 | Backend API |
| Shadow AI tab: detection source, enforcement mode (+ confirm dialog), report table, policy-doc link | P0 | Backend API |

**Parallelism:** all seven frontend surfaces can be developed in parallel
with each other and with backend work once each endpoint's contract is
stable (per Phase 4's established convention of frontend building against
documented contracts before backend fully lands).

---

## 11. Non-Compliance Risks

| Risk | Mitigation |
|------|------------|
| Hash-chain backfill locks `compliance_settings` for a long time on a large org | Documented operational guidance (§6.3); flagged as a known limitation (§12), not silently accepted |
| Drift canary tick spikes concurrent outbound requests | Sequential execution + `_CANARY_MAX_MODELS_PER_TICK` cap (§2.2) |
| Self-hosted cost formula under/over-estimates real spend | Visibly labeled "estimated" everywhere (AC5.5.7); documented as a v1 interim choice, not invoice-grade |
| `resolve_route()`/`record_usage_charge()`/`check_content_classification()` signature changes silently break an existing call site | §9.3's explicit full-suite regression gate; every changed call site enumerated in §5's wiring checklist |
| Shadow-AI ingestion endpoint accidentally inherits the admin trust boundary | Router-placement warning (§2.5) — own router, own dependency, never nested under `dependencies=[Depends(require_admin)]` |
| Content-classification category explosion causes false positives (legal/source_code heuristics) | Flagged for extra QA/security scrutiny per the product spec's own §9 items 4/6/12 |

---

## 12. Known Limitations

| Limitation | Reason | Future Phase |
|------------|--------|---------------|
| Hash-chain backfill is synchronous and lock-holding | No background-job infra for a resumable backfill in this phase | Fast-follow if a real design partner's audit table proves too large |
| No external hash-chain anchoring | Explicitly deferred (phase doc's own resolved decision) | Fast-follow, gated on regulated-industry demand |
| Chain and purge are mutually exclusive, not co-existing | Simpler/safer v1 choice over purge-aware re-genesis bookkeeping | Revisit once a real design partner needs both simultaneously |
| Drift thresholds are fixed/global, not per-model-admin-configurable | AC5.4.6/AC5.4.11 tension resolved by narrowing scope (§2.2) | Fast-follow if per-model tuning is requested |
| No admin-editable canary prompts | Fixed 5-prompt code-seeded set only | Fast-follow |
| Self-hosted models cannot be a graceful-degradation downgrade target | Not required by any Phase 5 AC; `validate_downgrade_target_model` not extended (§2.3(d)) | Natural fast-follow |
| No multi-key/failover for self-hosted endpoints | Explicitly deferred (spec §3) | Phase 4's backup-group mechanism could extend here later |
| No served-model auto-discovery for self-hosted endpoints | Admin types the model list manually | Fast-follow |
| No true ML/embeddings-based content classification or drift comparison | Cost/dependency/determinism constraints (spec §9 items 5/7) | Fast-follow if false-positive rates prove too high |
| No vendor-specific SASE/proxy adapters | Generic ingestion contract only | Each design partner's own integration work |
| No true inline network blocking for Shadow AI | Architecturally impossible from passive log ingestion | Deferred to a future browser-extension increment |

---

## 13. Success Criteria Verification

| Success Criterion | Verification Method |
|--------------------|----------------------|
| Pilot org uses the ledger verification tool to confirm chain integrity | `GET /v1/admin/audit/verify` called by a real pilot Auditor/Org Admin account, returns `"intact"` over a non-trivial entry count |
| Drift detector catches (or would have caught) a real provider-side model change | At least one `drift_alerts` row with a real `detected_at` exists, exported to the audit log and reviewed |
| Canary cost never touches user-attributable budget | `SELECT count(*) FROM usage_logs` referencing canary traffic is always zero; `current_spend_usd` figures unchanged after a canary tick |
| Self-hosted cost is visibly labeled an estimate | Manual UI/export review against AC5.5.7's requirement |
| Shadow-AI data-handling policy exists and is enforced pre-setup | `docs/policy/shadow-ai-data-handling.md` exists and is linked; ingestion endpoint rejects all traffic until setup is complete |

---

## 14. Deployment Checklist

### Pre-Deployment
- [ ] All six migrations (`0037`–`0042`) applied successfully
- [ ] `canary_prompts` seed data present (5 rows)
- [ ] `content_aware_rules` seed data present (source_code/financial_data/legal rows)
- [ ] `known_ai_tool_hostnames` seed data present
- [ ] `docs/policy/shadow-ai-data-handling.md` exists and is reachable from the admin UI

### Post-Deployment
- [ ] Scheduler loop running with all 7 ticks (verify in logs: no new `scheduler_*_tick_failed` entries)
- [ ] Hash chain: enable for a test org, verify `GET /v1/admin/audit/verify` returns `"intact"`
- [ ] Drift canary: confirm a tick fires and `canary_runs` rows appear, `usage_logs` unaffected
- [ ] Self-hosted: register a test vLLM/Ollama endpoint, verify, send one chat request, confirm `usage_logs.provider = "self_hosted"` and cost is nonzero
- [ ] Content-classification: configure two categories with disjoint `allowed_models`, confirm a triggering request is blocked
- [ ] Shadow AI: generate an ingest token, POST a test batch, confirm only matched-hostname rows persist; confirm the token cannot call any other endpoint

---

*This design document is reference material for implementation. Questions should be routed to the architect via the gatekey project repository.*
