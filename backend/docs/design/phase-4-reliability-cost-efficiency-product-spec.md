---
title: Phase 4 — Reliability & Cost Efficiency — Buildable Product Spec
status: draft
last_updated: 2026-08-04
source_docs:
  - phase-4-reliability-cost-efficiency.md
  - ui-requirements-admin.md (§5 Dashboard, §6 Providers, §8 Teams & Users,
    §11 Reliability & Cost, §16 Data Reference)
  - 00-overview.md
  - phase-2-multi-tenant-governance-design.md (§12, forward-looking rework
    flags — Phase 4's own shared-state obligation)
  - phase-3-security-compliance-product-spec.md (precedent format)
  - backend/src/gatekey/providers/registry.py, providers/model_registry.py,
    db/models/provider_key.py, services/budget.py, api/v1/gateway/common.py,
    db/models/team.py, errors.py, docker-compose.yml (current-state grounding)
author: product-owner (sub-agent)
consumed_by: architect
---

# Phase 4 — Reliability & Cost Efficiency — Buildable Spec

This translates `phase-4-reliability-cost-efficiency.md` §4.1–§4.5 into user
stories and testable acceptance criteria. The source phase doc states its own
"Open Questions" section is fully resolved inline — this doc does not
re-litigate those resolutions (failover off-by-default, semantic caching
stretch-only, degradation notified via response header). It operationalizes
each into buildable criteria and separately flags the real implementation-
detail gaps that surfaced only when cross-referencing the phase doc against
the UI docs and the current codebase (most notably a scoping conflict on
failover opt-in and a genuinely new architecture question this phase
introduces) — these are listed in §8, not silently decided.

**Scope framing carried through every section below:** Phase 4 is the first
phase that requires infrastructure beyond Postgres. Every prior phase
(1 through 3) ships on `docker-compose up` with Postgres as the only
required service. This phase's rate-limiter NFR ("no naive in-process
counters if Gatekey is horizontally scaled") and its caching requirement both
need a shared-state mechanism that survives multiple worker processes — this
is new, not an extension of anything already built, and §0 flags it
prominently rather than deciding it.

---

## 0. Non-Negotiable Architecture Decisions

1. **A shared-state mechanism is required, and its choice is an
   architect-level call, not decided here.** Both the rate limiter (§2,
   distributed-accuracy NFR) and the response cache (§3) need state that is
   correct across multiple Gatekey worker processes — this codebase has been
   Postgres-only through Phase 3 (`docker-compose.yml` starts only Postgres +
   backend + frontend, with Keycloak as an explicitly profile-gated,
   never-on-by-default optional service). Two realistic forks, with real
   tradeoffs, neither pre-selected:
   - **Introduce Redis (or equivalent) as new required infra.** Pro:
     purpose-built primitives for exactly this problem (atomic `INCR` +
     `EXPIRE` for rate windows, native TTL for cache entries, pub/sub for
     cache invalidation) — well-trodden, low custom-code risk. Con: breaks
     the "zero extra services beyond Postgres" setup story that has held
     through Phase 3; adds a new REQUIRED (not optional/profile-gated)
     service to `docker-compose.yml` for every deployment upgrading past
     Phase 3, and a new failure mode/ops burden for the self-hosted,
     support-yourself audience this product targets (`00-overview.md`'s
     "every phase ships with docs sufficient to self-deploy without
     engineering support").
   - **Do it in Postgres, with careful locking.** Pro: zero new required
     infra, keeps the self-hosted-simplicity story intact. Con: rate-limit
     counters are a high-contention, high-frequency write workload — exactly
     the wrong shape for `SELECT ... FOR UPDATE` row-lock contention against
     the same connection pool serving every other request; the §2/§3 latency
     NFRs (cache-miss overhead under ~10ms) are materially harder to hit
     against a full RDBMS round trip under load; TTL/expiry (rate windows,
     cache TTL) doesn't come free the way Redis `EXPIRE` does — needs a
     purge job or lazy-expiry check on every read.

   Whichever is chosen must satisfy the rate limiter's cross-worker accuracy
   NFR and should, per `phase-2-multi-tenant-governance-design.md` §12's own
   forward-looking flag, be the single mechanism that ALSO finally resolves
   `ModelPolicyCache`/`TeamModelPolicyCache`'s already-documented
   in-process-singleton/no-cross-worker-convergence limitation — "not solved
   three times independently." The cache store (§3) may reasonably live in a
   different place than the rate-limit counters (e.g. counters in Redis,
   cache entries in Postgres with a purge job, since a cache miss already
   tolerates a DB round trip the same way `check_budget_available()` does) —
   that split is itself part of what the architect resolves, not pre-decided
   here. See Ambiguity A2.

2. **Multi-key-per-provider is a schema change to the existing `ProviderKey`
   table, not a new table or a redesigned encryption envelope.**
   `db/models/provider_key.py`'s `UNIQUE(org_id, provider)` constraint —
   documented in that file's own module docstring as "Phase 1.1 constraint...
   deferred to Phase 2" (now Phase 4) — is relaxed to
   `UNIQUE(org_id, provider, label)`; a required `label` column is added.
   The AES-256-GCM envelope (`ciphertext`/`nonce`/`auth_tag`,
   associated-data binding to `org_id:provider`) and the Phase 3 rotation
   overlap columns are unchanged — do not touch them.

3. **Model routing stays static and in-process; key SELECTION among
   multiple keys happens at credential-fetch time, not at route-resolution
   time.** `providers/model_registry.py`'s `resolve_model()` remains the one
   sanctioned, zero-I/O lookup point (its own docstring: "the *only*
   sanctioned way to look up a model") — multi-key routing and failover do
   not touch it. Failover retries happen inside/around
   `api/v1/gateway/common.py`'s `fetch_credential()` step and the provider
   call that follows it, never by re-resolving to a different model.

4. **Pipeline insertion points, in this exact order**, extending
   `api/v1/gateway/common.py`'s established sequence (`check_access_schedule
   -> resolve_route -> check_model_policy -> check_residency -> run_dlp_scan
   -> check_content_classification -> check_budget_available ->
   fetch_credential -> provider call -> record_usage_charge`):

   ```
   check_access_schedule -> resolve_route -> check_model_policy ->
   check_residency -> run_dlp_scan -> check_content_classification ->
   check_rate_limit (NEW, §2)          -- after policy denial, before spend
   -> check_cache (NEW, §3)            -- hit short-circuits everything below
   -> check_budget_available -> check_degradation (NEW, §4)
   -> fetch_credential (failover-aware, §1) -> provider call
   -> record_usage_charge -> cache_store (NEW, §3, on miss only)
   ```

   `check_rate_limit` sits after `check_model_policy` for the same reason
   the DLP scan does (a request already going to be denied shouldn't consume
   a rate-limit slot). `check_cache` sits before `check_budget_available`
   deliberately (§3, AC3.6/AC3.8) — a cache hit needs no provider call, so it
   needs no rate-limit slot, no budget charge, and nothing to degrade.
   `check_degradation` sits after `check_budget_available` succeeds (the
   user is confirmed under budget) and before `fetch_credential`, since it
   may substitute which model `fetch_credential`/the provider call actually
   targets.

---

## 1. §4.1 Multi-Key & Failover

**User stories**

- As an Org Admin, I add more than one key for a provider, each with a
  required unique label, so traffic can spread across keys to avoid a single
  key's rate/quota ceiling.
- As an Org Admin, I see a live Healthy/Degraded/Down status per key, with a
  one-line reason when it's not Healthy, so I know whether to intervene
  before failover would even trigger.
- As an Org Admin, I opt a key into automatic failover to a designated
  backup key, off by default, so a compliance-sensitive setup never has
  traffic silently rerouted without an explicit decision.
- As any user, when my request's primary key errors and failover is
  enabled, my request still succeeds via the backup key, transparently — I
  never see the primary's failure.

**Acceptance criteria**

- AC1.1 — `ProviderKey` migration (additive, per §0.2): relax
  `UNIQUE(org_id, provider)` to `UNIQUE(org_id, provider, label)`; add
  `label` (`NOT NULL`, unique per provider). No change to the encryption
  envelope columns or the Phase 3 rotation-overlap columns.
- AC1.2 — Admin adds an additional key to an existing provider via the
  existing add-key modal (Phase 1 §7.4 pattern), now requiring a unique
  label — matches `ui-requirements-admin.md` §6's "+ Add another key" /
  required-key-name behavior. Each key is independently live-validated at
  creation (same three structured error states as Phase 1: invalid format,
  auth rejected, provider unreachable) and independently editable/removable.
- AC1.3 — Health status (Healthy / Degraded / Down, per the UI's Health-dot
  component, §2 of that doc) is computed **passively, from real traffic
  outcomes** over a rolling window (recommend 15 minutes, matching the UI
  copy's own "elevated error rate in the last 15 min" wording) — not an
  active/synthetic ping. This is deliberate: an active health-check call to
  a provider costs real provider API spend, which would directly undercut
  this phase's own cost-efficiency goal. Degraded = error rate above a
  configurable threshold within the window; Down = a higher threshold /
  consecutive-failure count exceeded, or the endpoint is provably
  unreachable. No source doc gives concrete threshold numbers — see
  Ambiguity A5.
- AC1.4 (NFR-supporting) — Health status is readable by every Gatekey worker
  process, not cached per-process — a status only known to the worker that
  observed the failing request is insufficient in a multi-instance
  deployment; it must be visible to whichever worker serves the *next*
  request. This depends directly on §0's shared-state mechanism.
- AC1.5 — Failover toggle: **per provider-key**, off by default, matching
  the concrete wireframe in `ui-requirements-admin.md` §6 ("Failover: ☐
  Enabled — retry against \[backup key\]") and the `ProviderKeyMulti` data
  shape in that doc's §16 (`failover_enabled`, `failover_target_id`). See
  Ambiguity A1 — the phase doc's own prose calls this "opt-in per **team**,"
  but the only concrete UI control that exists anywhere in the reviewed
  admin doc is scoped to the provider key, not to a team. Build the
  per-provider-key toggle now (it is the only buildable, concrete control),
  flagged for explicit sign-off on whether a team-dimension override is
  also required.
- AC1.6 — Committed scope: **same-provider, multi-key failover only** — a
  request that errors against its primary key retries against a
  backup key of the *same provider* (same underlying model, same
  `native_model_id`). Cross-provider failover (a different provider,
  necessarily a different underlying model) is **not** committed scope this
  phase, despite the phase doc's prose technically allowing "backup key or
  provider" — see Ambiguity A3.
- AC1.7 — Failover retry is bounded to exactly one retry against the
  configured backup — never an unbounded retry loop across every configured
  key for that provider. If the backup also errors, the original provider
  error is surfaced to the caller unchanged (never a silent second failure
  that just hangs or times out).
- AC1.8 — Retry is transparent to the caller: same OpenAI-compatible
  response shape regardless of which key ultimately served the request
  (cross-phase API-compatibility non-negotiable, `00-overview.md`) — no
  error surfaced for the failed primary attempt on a successful failover.
- AC1.9 (NFR, operationalized) — In a game-day/chaos test (matches the phase
  doc's own Success Criteria), measure wall-clock time from the moment a
  provider/key is forced to fail to the moment the **next** request —
  potentially served by a *different* worker process — is routed to and
  successfully served by the healthy backup. Must be under 2 seconds,
  measured deployment-wide (all worker instances), not per-process — this
  is a direct, testable consequence of §0's shared-state requirement, not
  achievable with per-process health caching.
- AC1.10 — Every failover event (from-key, to-key, timestamp,
  detection-to-switch duration) is recorded for the Failover & Health tab's
  timeline view (`ui-requirements-admin.md` §11) and feeds the §5 dashboard
  "Failovers" tile.

**Deferred / explicitly out of scope for this section**

- Cross-provider (cross-model) failover (Ambiguity A3).
- Active/synthetic provider health-check polling (AC1.3's resolved
  passive-only approach).
- A dedicated per-team failover UI control beyond the concrete
  per-provider-key toggle already wireframed (Ambiguity A1).

---

## 2. §4.2 Rate Limiting

**User stories**

- As an Org Admin, I set an org-wide default per-user request/token rate
  limit, so one heavy individual user can't exhaust the org's shared
  provider quota.
- As an Org Admin, I set a per-team aggregate rate limit, so one team's
  combined traffic can't starve other teams sharing the same provider keys.
- As an Org Admin, I choose per scope whether a limit-hit rejects
  immediately or queues briefly and retries, matching how tolerant that
  team's calling app is of latency vs. hard failure.
- As any user, when I'm rate-limited, I get a clear structured error with a
  concrete retry hint, never a silent drop or a generic failure.

**Acceptance criteria**

- AC2.1 — `RateLimitRule: { id, scope: "org_default_per_user"|team_id,
  requests_per_min: int|null, tokens_per_min: int|null, on_limit:
  "reject"|"queue_retry" }`, matching the Rate Limits tab table shape in
  `ui-requirements-admin.md` §11 (`Scope | Limit | Behavior on hit`).
- AC2.2 — Two independently enforced axes, both may apply simultaneously to
  the same request (most-restrictive-wins when both trigger): (a) a
  per-**individual-user** limit, org-wide default, enforced against each
  user's own traffic regardless of team; (b) a per-**team-aggregate** limit,
  enforced against the SUM of requests/tokens from every member of that
  team combined — protects a team's shared provider-quota allocation as a
  pool. This resolves the phase doc's "per-user and per-team" wording as two
  different aggregation levels, not a narrowing override of one by the
  other (unlike the org→team precedence pattern used for DLP/residency/
  model-policy) — flagged for confirmation, see Ambiguity A4, since the UI
  table alone doesn't fully disambiguate this from a simpler override
  reading.
- AC2.3 — Request-count (`requests_per_min`) enforcement is pre-emptive: the
  counter increments atomically at request start and can reject before the
  provider is ever called.
- AC2.4 — Token-count (`tokens_per_min`) enforcement is necessarily
  retrospective, mirroring `services/budget.py`'s own accepted semantics
  ("can only check whether already over budget from previous requests,
  never whether this request will push them over"): a tokens/min limit can
  only ever block based on ALREADY-consumed tokens from prior requests
  within the current rolling window. This is a stated build requirement, not
  a gap — never estimate/pre-charge tokens against the limit.
- AC2.5 — "Reject immediately": a request that would exceed a configured
  limit is rejected synchronously with a structured error
  (`rate_limit_exceeded`, 429 — matches this codebase's `errors.py`
  `GatekeyError` subclass pattern) including a `Retry-After` header with a
  concrete wait hint.
- AC2.6 — "Queue and retry": the request is held server-side and the limit
  re-checked on a short interval until either it clears (proceeds normally)
  or a max queue wait is exceeded, at which point it finally rejects with
  the same `rate_limit_exceeded` 429. Neither source doc specifies the max
  wait bound — see Ambiguity A8; recommend a short, configurable default
  (seconds, not minutes) rather than an unbounded hold, to avoid exhausting
  server-side connection/worker capacity under sustained burst traffic.
- AC2.7 — Pipeline placement: immediately after `check_content_classification`
  and before `check_cache`/`check_budget_available` (§0.4) — a request
  already denied by policy shouldn't consume a rate-limit slot, matching the
  existing DLP-scan-placement precedent.
- AC2.8 (NFR, hard architectural constraint) — Rate-limit counters MUST be
  accurate under a horizontally-scaled, multi-instance deployment. An
  in-process counter (a plain dict/`Counter` living in one worker's memory)
  is explicitly disallowed by the phase doc's own NFR — this is precisely
  the shared-state question §0.1 hands to the architect; this AC exists so
  that requirement is never quietly bypassed with an "easy" in-process
  implementation later.
- AC2.9 — Rate-limit rejection counts (both immediate-reject and
  timed-out-from-queue outcomes) feed the §5 dashboard metric, scoped per
  rule the same way the Rate Limits tab table itself is scoped — see
  Ambiguity A6 on exact UI placement.

**Deferred / explicitly out of scope for this section**

- Pre-call token estimation/reservation against the tokens/min limit
  (AC2.4).
- Per-named-individual-user configuration UI (only the org-wide default per-
  user limit is buildable from the reviewed UI; see Ambiguity A4).

---

## 3. §4.3 Caching

**User stories**

- As an Org Admin, I enable exact-match response caching with a configurable
  TTL, so identical repeated prompts don't re-incur provider cost.
- As a Team Lead/Org Admin, I opt my team out of caching if our data
  sensitivity policy requires every request to actually reach the provider
  fresh.
- As any user, a cached response is never served to me if it was produced
  under a different DLP/residency/model-policy state than what applies to
  my own request right now.

**Acceptance criteria**

- AC3.1 — **Cache key composition** (the concrete design answer this
  section requires, not left to the architect to invent from scratch): a
  deterministic hash of `{ org_id, team_id, resolved provider +
  native_model_id (post-`resolve_route`, not the raw caller-supplied model
  alias, so two gateway-facing names that resolve to the same underlying
  model share a cache entry), the exact POST-DLP-redaction prompt/message
  content, and every request parameter that affects the response
  (temperature, max_tokens, etc.) }`. Using the POST-redaction text (what
  was actually sent to the provider, per `run_dlp_scan()`'s `redacted_texts`)
  rather than the raw pre-redaction input is deliberate: it's what actually
  determines the response, and it means two requests whose raw PII differs
  but which redact to identical text legitimately share an entry instead of
  needlessly missing.
- AC3.2 — Including `team_id` in the key is what enforces the phase doc's
  "must not be served across a policy boundary it wouldn't otherwise be
  allowed to cross" requirement for team-scoped policy variation (team DLP
  action override, team residency rule, team model restriction): a cache
  entry written under one team's policy state can structurally never be
  looked up by a request from a different team, since the key itself
  differs. This is the "incorporate policy-relevant context into the key"
  approach (chosen over "scope/invalidate entries by policy state") because
  it fails safe by construction for the per-request case — a policy
  difference always produces a miss, never an accidental hit — with no
  separate invalidation bookkeeping needed for that case.
- AC3.3 — AC3.1/3.2 do **not**, by themselves, handle a policy **config
  change over time** (e.g. an org tightens DLP from redact to block, or
  newly restricts a residency region, after an entry was already cached
  under the prior policy) — a stale entry could still be served within its
  TTL under a since-changed policy. See Ambiguity A9 — recommend reusing
  this codebase's existing invalidate-on-write instinct
  (the same pattern behind `ModelPolicyCache`/`TeamModelPolicyCache`'s
  invalidation on a policy write) so any DLP/residency/model-policy/
  content-classification mutation also flushes the response cache, rather
  than relying on TTL alone to bound staleness risk.
- AC3.4 — Exact-match only: byte-identical post-redaction prompt + params
  (hash comparison). No fuzzy/semantic matching in this phase — carried
  forward from the phase doc's own resolution that semantic caching is
  stretch-only, not a Phase 4 commitment; do not build embedding-based
  similarity matching now.
- AC3.5 — Configurable TTL; enabled **org-wide by default with per-team
  opt-**out** available** — per `ui-requirements-admin.md` §11's explicit
  behavior note ("caching is 'opt-in per team,' so org-wide-enabled-with-
  team-opt-out, as wireframed, is the correct default framing"). This
  supersedes a literal reading of the phase doc's "opt-in per team" phrase
  in favor of the UI doc's own explicit clarification of what that phrase
  means in practice.
- AC3.6 — Pipeline placement: cache lookup runs before `check_rate_limit`'s
  effect matters and before `check_budget_available`/`check_degradation`
  (§0.4) — logically forced, not a judgment call: a cache hit needs no
  provider call, so there is nothing to protect a provider quota against and
  nothing to downgrade.
- AC3.7 — A cache **hit** does not call `record_usage_charge` — it costs the
  org nothing (no provider call made), so it must not charge budget. This is
  also what makes the "cost saved via caching" dashboard figure (§5)
  meaningful: `(cache hits in window) × (what each would have cost the
  provider)`.
- AC3.8 — Because a cache hit bypasses `check_budget_available` entirely
  (AC3.6), it is available even to a budget-**exhausted** user. See
  Ambiguity A7 — flagged for explicit product sign-off, since this is a
  genuine behavior choice (a hard budget cutoff reads most naturally as
  "nothing more, period," but a free/already-computed response arguably
  needn't be blocked on principle). Recommend allowing it — the budget gate
  exists to control real spend, and a cache hit is not spend.
- AC3.9 (NFR) — Cache-miss overhead adds no more than ~10ms to request
  latency (phase doc's explicit target) — needs a load-test acceptance
  check, matching this doc's general posture toward latency NFRs (Phase 3's
  DLP-scan NFR treatment).
- AC3.10 — On a cache miss + successful provider response, a new entry is
  written post-response, keyed per AC3.1, with the org/team's configured
  TTL. A degraded (§4) response naturally caches under the downgrade
  target's own model in the key — never under the originally-requested
  model's cache namespace — with no special-casing required, since the
  actually-used model is already part of the key.
- AC3.11 — Semantic caching: **not built this phase** (carried-forward
  resolution). The UI's "\[Beta\]" near-duplicate-detection toggle either
  ships visible-but-clearly-inert (documented as a current limitation) or is
  omitted entirely — either is acceptable, but it must never silently do
  nothing when checked. See Ambiguity A10.

**Deferred / explicitly out of scope for this section**

- Semantic/near-duplicate caching (Ambiguity A10; stretch-only per the
  phase doc, revisit only after real exact-match hit-rate data from a
  pilot, per the phase doc's own "validate demand before building"
  framing).
- Active invalidation triggered by anything other than a policy config
  mutation (e.g. no manual "clear cache" admin action is specified by
  either source doc — not built unless later requested).

---

## 4. §4.4 Graceful Cost Degradation

**User stories**

- As an Org Admin/Team Lead, I configure an auto-downgrade: when a user is
  within a configurable percentage of their budget (but not yet over it),
  requests route to a cheaper configured model instead of waiting for the
  hard block.
- As any user, when my request was silently downgraded, I can detect it
  programmatically via a response header, so my own app can act on it
  (retry differently, surface a notice) rather than only discovering it
  later in a log.

**Acceptance criteria**

- AC4.1 — `DegradationPolicy: { id, scope: "org"|team_id, enabled: boolean,
  threshold_pct_of_budget: number, downgrade_target_model: string }` — per
  `ui-requirements-admin.md` §11's "Auto-downgrade when a user is within
  \[10%\] of budget" + "Downgrade target model" fields. Reuses the exact
  budget-state read `check_budget_available()` already performs
  (per-user or per-(team, user) `current_spend_usd`/`budget_usd`) — no
  second query.
- AC4.2 — Threshold condition: `current_spend_usd >= budget_usd * (1 -
  threshold_pct/100)` AND `current_spend_usd < budget_usd` — i.e.
  approaching but not yet at/over. A request already over budget is blocked
  outright by the existing hard cutoff; degradation never overrides that —
  it only intervenes in the window strictly before it.
- AC4.3 — Pipeline placement: evaluated immediately after
  `check_budget_available` succeeds and before `fetch_credential` (§0.4).
  If degradation triggers, `resolve_route` is re-run for
  `downgrade_target_model` (not the originally-requested model) before
  `fetch_credential`. The original model's earlier policy/residency/DLP
  checks are **not** re-run against the substituted model — see Ambiguity
  A9 on whether they should be.
- AC4.4 — If `downgrade_target_model` is not currently allowed under the
  org/team static model-policy baseline (misconfiguration, or a later
  policy change denied it), degradation is **skipped** and the original
  request proceeds normally under the standard budget check — never
  hard-fail a request because its cheaper fallback became invalid, which
  would defeat the entire "graceful" premise. Inferred default, not stated
  in either source doc — see Ambiguity A9.
- AC4.5 — Response header contract (additive; does not touch the
  OpenAI-compatible response body — the phase doc's own explicit resolution
  and non-negotiable): on a downgraded response, exactly two headers are
  set — `X-Gatekey-Degraded: true` and `X-Gatekey-Degraded-Model:
  <downgrade_target_model>` — both entirely absent on a non-degraded
  response (never `X-Gatekey-Degraded: false`).
- AC4.6 — Usage is charged against the actual model used (the downgrade
  target) via the existing `record_usage_charge`/`compute_cost` path,
  unchanged (§0.3) — the charge reflects the cheaper model's real cost. This
  also produces the "cost saved via degradation" figure (§5):
  `(original model's price for this request's token counts) - (downgrade
  target's actual charged cost)`.
- AC4.7 — A downgraded request's request-log entry records both the
  originally-requested model and the actually-used model, so an admin
  auditing usage history sees degradation events after the fact, not only a
  calling app that inspects the live header.
- AC4.8 — Streaming responses: `X-Gatekey-Degraded`/`X-Gatekey-Degraded-Model`
  are set on the initial response, before the stream body begins — no
  special-casing needed since headers are already sent before a streaming
  body under normal HTTP semantics.

**Deferred / explicitly out of scope for this section**

- Any degradation-body-shape change (e.g. an OpenAI-response-body field
  signaling the downgrade) — headers only, per the phase doc's explicit,
  non-negotiable resolution (§0/AC4.5).
- Multi-step degradation ladders (progressively cheaper models as budget
  proximity worsens) — the phase doc specifies exactly one configured
  downgrade target, not a ladder.

---

## 5. §4.5 Performance & Cost Dashboards

**User stories**

- As an Org Admin, I see cache hit rate and failover event counts on the
  main Dashboard, so reliability/efficiency posture is visible at a glance
  alongside spend/requests/latency/errors.
- As an Org Admin, I can hand a concrete "cost saved" figure to
  finance/leadership as the ROI case for adopting Gatekey more broadly.
- As an Org Admin, I can see how often rate limiting is actually rejecting
  traffic, so I can tell a too-tight limit from a genuinely protective one.

**Acceptance criteria**

- AC5.1 — Dashboard (`ui-requirements-admin.md` §5) gains two new stat
  tiles — Cache (hit rate %) and Failovers (event count) — exactly as
  wireframed, each gated behind the existing empty-state rule (don't render
  with permanent zeros if the relevant Reliability feature was never
  enabled — reuse Phase 1's stated convention, don't invent a new one).
- AC5.2 — "Cost saved via caching/degradation" and "rate-limit rejection
  counts" have **no corresponding stat tile or panel anywhere in the
  reviewed UI doc's Dashboard (§5) or Reliability & Cost (§11) wireframes**,
  despite being explicitly required by the phase doc's §4.5 text — a real
  gap, see Ambiguity A6. Recommend: (a) "Cost saved" as a new Dashboard
  stat tile (or a detail line on the existing Spend tile) given the phase
  doc's own framing of it as "the deliverable for the ROI conversation with
  finance/leadership" — it deserves Spend-tile-level prominence, not a
  buried sub-metric; (b) "rate-limit rejection count" as a new column on
  the existing Rate Limits tab table (§11), since it's naturally a
  per-rule/per-scope figure, not a single org-wide number.
- AC5.3 — All four new metrics (cache hit rate, failover count, cost saved,
  rate-limit rejections) respect the existing Dashboard time-range selector
  (§5's "Time range: \[7 days ▾\]") — computed over the selected window,
  never a fixed lifetime total.
- AC5.4 — "Cost saved" is a single combined figure: AC3.7's cache-hit
  savings plus AC4.6's degradation savings over the selected window, per the
  phase doc's own "cost saved via caching/degradation" phrasing — not two
  separate numbers, unless Ambiguity A6's sign-off decides otherwise.
- AC5.5 — Success criterion, operationalized as a QA acceptance test
  (matches the phase doc's own Success Criteria): over a real 30-day
  window with caching and/or degradation actually enabled and exercised by
  a pilot org, the Dashboard produces a non-zero, defensible "cost saved"
  figure an admin could hand to a CFO/CTO as-is — not a number requiring
  manual reconciliation against raw usage logs to trust.

**Deferred / explicitly out of scope for this section**

- Any new chart type beyond stat tiles / existing table extensions (no
  trend/forecast chart is specified for these metrics in Phase 4 — that
  pattern is Phase 6's Forecasting tab).

---

## 6. Explicit Scope Boundary Summary

**In scope for Phase 4 (build now):**
- Multi-key-per-provider (relaxed unique constraint + required label),
  same-provider-only automatic failover, per-provider-key opt-in toggle off
  by default, passively-derived (traffic-based, not synthetic-ping) health
  status, deployment-wide <2s failover-switch NFR.
- Per-user (org-default individual cap) + per-team (aggregate pool) rate
  limiting on requests/min and tokens/min, configurable reject-vs-queue
  behavior per team, distributed-accurate counters (explicit hard
  constraint against in-process counters).
- Exact-match response caching, org-wide-on with per-team opt-out, TTL
  configurable, cache key incorporating team/policy-relevant context so a
  cached response can never cross a policy boundary the live request itself
  couldn't cross, cache-hit bypass of rate-limit/budget/degradation checks.
- Graceful cost degradation with team-configurable proximity threshold and
  a single configured downgrade target model, `X-Gatekey-Degraded` +
  `X-Gatekey-Degraded-Model` response headers, correct cost-saved
  accounting against the actually-charged model.
- Dashboard and Reliability & Cost tab extensions for all four required
  metrics (cache hit rate, failover count, cost saved, rate-limit
  rejections).
- Resolving the new shared-state infrastructure question (Redis vs.
  Postgres-with-locking) — explicitly an architect deliverable, flagged
  prominently in §0, not decided by this spec.

**Explicitly deferred / out of scope (matches the phase doc's own
Out-of-Scope list, plus items surfaced only by operationalizing it):**
- Budget marketplace / cross-team bidding (Phase 6).
- Shadow AI discovery, drift detection (Phase 5).
- Semantic/near-duplicate caching (stretch-only per the phase doc's own
  resolution; revisit post-pilot).
- Cross-provider (cross-model) failover (Ambiguity A3) — the phase doc's
  prose technically allows it, but no concrete UI or data shape supports it;
  do not build it speculatively.
- Active/synthetic provider health-check polling that spends real provider
  API budget (AC1.3).
- A per-team failover override UI control beyond the concrete
  per-provider-key toggle already wireframed (Ambiguity A1).
- Multi-step degradation ladders; any response-body-shape change to signal
  degradation (headers only, non-negotiable).

---

## 7. Data Model Touchpoints (for architect — not a schema design, a checklist)

- `ProviderKey`: relax `UNIQUE(org_id, provider)` → `UNIQUE(org_id,
  provider, label)`; add `label` (`NOT NULL`); add `failover_enabled`
  (boolean, default false) + `failover_target_id` (self-referencing FK,
  nullable) per the `ProviderKeyMulti` shape (`ui-requirements-admin.md`
  §16) — additive migration, encryption envelope columns untouched.
- New shared-state store (§0.1, architect decision) backing: rate-limit
  counters (per-user individual + per-team aggregate, req/min and
  tokens/min), live provider/key health status (rolling error-rate window),
  and — depending on the architect's split decision — the response cache
  itself (native TTL if Redis; an indexed `expires_at` + purge job if
  Postgres).
- `RateLimitRule: { id, scope: "org_default_per_user"|team_id,
  requests_per_min, tokens_per_min, on_limit: "reject"|"queue_retry" }` —
  Postgres config table (not the hot-path counter store itself).
- `DegradationPolicy: { id, scope: "org"|team_id, enabled,
  threshold_pct_of_budget, downgrade_target_model }` — Postgres config
  table, mirrors `Team`'s existing per-team toggle-column style
  (`alert_threshold_80_enabled`, `webhook_alert_enabled` precedent) rather
  than introducing a new configuration pattern.
- `Team`: gains a caching opt-out boolean (per AC3.5's resolved
  org-on/team-opt-out framing), same per-team-toggle style as the existing
  alert-threshold/webhook columns.
- Cache entries: keyed per AC3.1 (`org_id`, `team_id`, resolved provider +
  `native_model_id`, post-redaction prompt + params hash) — a new
  `CacheEntry` table with an indexed `expires_at` + purge job if Postgres is
  the chosen store, or native Redis keys/TTL otherwise.
- Failover-event and rate-limit-rejection event logs: new append-style
  records (or new event-type columns on the existing usage-log write path,
  architect's call) feeding §5's dashboard tiles — need enough structure to
  compute "detection-to-switch duration" per failover event for the
  Failover & Health tab's timeline.

---

## 8. Flagged Ambiguities (genuinely open — not re-litigating resolved items)

The phase doc's own open questions are all resolved inline and used as-is
(failover off-by-default, semantic caching stretch-only, degradation via
response header). The following surfaced only by cross-referencing the
phase doc against the UI docs, the design doc's forward-looking flags, and
the current codebase — building against a guess risks rework.

- **A1 (high priority) — Failover's scoping conflict.** The phase doc's own
  prose resolution says failover must be "opt-in per **team**," but the
  only concrete UI control anywhere in the reviewed admin doc (§6's
  Providers screen, and the `ProviderKeyMulti` shape in §16) is a toggle
  scoped to the **provider key**, org-wide in effect — there is no
  team-level failover control anywhere in the Teams & Users (§8) or
  Reliability & Cost (§11) sections. Building AC1.5's per-provider-key
  toggle satisfies the "off by default" half of the resolution but not the
  "per team" half — an org with one compliance-sensitive team and one
  cost-sensitive team literally cannot get different failover behavior
  through the same provider key under this design. **Recommend** shipping
  the per-provider-key toggle now (the only buildable, concrete control)
  and flagging the team-dimension gap as a near-term follow-up (a
  narrowing-only per-team override, added once its UI exists) rather than
  building a UI surface neither source doc actually specifies. Needs
  explicit product sign-off, not a silent pick.

- **A2 (high priority, architecture decision, not a build ambiguity) —
  Shared-state mechanism fork.** See §0.1: Redis (new required infra,
  purpose-built primitives, breaks the "zero extra services" story) vs.
  Postgres-with-locking (zero new infra, but real contention/latency/TTL
  engineering cost). Genuinely a self-hosted-simplicity-vs-engineering-
  tradeoff, not a guess — the architect should decide and record the
  choice, parallel treatment to Phase 3's A11 (CLI helper language).

- **A3 — Cross-provider (cross-model) failover.** The phase doc's prose
  ("retry against a configured backup key or **provider**") technically
  allows failing over to an entirely different provider/model, but the
  concrete `ProviderKeyMulti` UI wireframe only shows a backup key picker
  nested inside one provider's own card — no cross-provider picker exists
  anywhere in the reviewed doc. **Recommend** committing same-provider-only
  for this phase's build (AC1.6); cross-provider failover is a materially
  bigger feature (capability/format compatibility, re-validating
  policy/DLP/residency for a different model, different pricing) that
  deserves its own explicit scoping if wanted — flag for confirmation, not
  blocking, parallel to Phase 3's A12.

- **A4 — Rate limiting's "per-user and per-team" scoping.** Is the
  per-team number in the UI's Rate Limits table an independent AGGREGATE
  pool across the whole team (my recommended reading, AC2.2), or simply a
  narrowing override of the org-default per-user number (the same
  org→team precedence pattern used everywhere else in this system)? The
  table shape alone doesn't disambiguate. **Recommend** the two-axis
  reading — it best matches "protect shared provider quotas... from being
  exhausted by one heavy user" (implying both an individual guard and a
  pool guard) — but this changes what's actually enforced, so it needs
  explicit confirmation before building.

- **A5 — Health-status derivation and thresholds.** Passive (from real
  traffic error rates, matching the UI's "elevated error rate in the last
  15 min" copy) vs. active synthetic polling, and no source doc gives
  concrete Degraded/Down threshold numbers. **Recommend** passive-only
  (AC1.3) — avoids spending real provider API budget on health checks,
  which would undercut this phase's own cost-efficiency goal — with
  threshold numbers left to the architect/QA to pick and document
  explicitly (no number in either source doc to inherit).

- **A6 — "Cost saved" and "rate-limit rejection count" have no UI
  representation.** Neither the Dashboard (§5) nor the Reliability & Cost
  tab (§11) wireframes show either metric, despite the phase doc explicitly
  requiring both. **Recommend** per AC5.2: cost-saved as a new Dashboard
  stat tile (deserves Spend-tile-level prominence per the phase doc's own
  ROI framing), rate-limit-rejection-count as a new column on the existing
  Rate Limits tab table. Needs product sign-off on exact placement before
  frontend work starts.

- **A7 — Cache hit vs. budget-exhausted interaction.** Should a
  budget-exhausted user still receive a free cache hit (AC3.6 structurally
  bypasses the budget check for a cache hit), or should the hard budget
  cutoff block ALL responses including cached ones on principle? **Recommend**
  allowing it (AC3.8) — the budget gate exists to control real spend, and a
  cache hit is not spend — but this is a genuine product judgment call, not
  an engineering inevitability, and should be confirmed rather than assumed.

- **A8 — "Queue and retry" max wait bound.** Neither source doc gives a
  number for how long a queued, rate-limited request may be held before
  finally rejecting. **Recommend** a short, configurable default measured
  in seconds (not minutes) to avoid exhausting server-side connection/worker
  capacity under sustained burst traffic — needs an explicit number pinned
  before build, same class of gap as Phase 3's A1 (no numeric default for
  audit retention).

- **A9 — Cache staleness across a policy config change, and degradation's
  target-model trust.** Two related, smaller gaps: (a) AC3.1/3.2's
  key design handles the per-request policy-boundary case but not a policy
  **change over time** — recommend actively flushing the cache on any
  DLP/residency/model-policy mutation (reusing the existing invalidate-on-
  write instinct behind `ModelPolicyCache`), rather than relying on TTL
  alone; (b) whether a configured `downgrade_target_model` needs its own
  live residency/DLP/content-classification re-validation at request time,
  or whether a single upfront "admin configured it, trust it" assumption is
  acceptable — recommend trusting it (AC4.4's skip-if-currently-denied
  already provides the safety net for a *model-policy* mismatch), but this
  is a live-path decision worth explicit confirmation since it affects
  whether degraded traffic could ever slip past a residency/DLP boundary
  the original request itself was checked against.

- **A10 (minor, not blocking) — Semantic-caching UI presence.** Ship the
  "\[Beta\]" toggle as visible-but-clearly-inert (documented as a current
  limitation), or omit it entirely from Phase 4's build? **Recommend** the
  same "ship visible-but-inert, cheap and forward-compatible" pattern
  Phase 3's A6 used for the Content-Aware Routing tab's inert rows — low
  risk either way, flagging only so it's a deliberate choice, not a
  silently-built dead checkbox.
