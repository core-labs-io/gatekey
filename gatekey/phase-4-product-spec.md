---
title: Phase 4 — Reliability & Cost Efficiency — Buildable Spec
status: draft
last_updated: 2026-08-05
source_docs:
  - phase-4-reliability-cost-efficiency.md
  - ui-requirements-admin.md (§10, §11, §12)
  - 00-overview.md
author: product-owner (sub-agent)
consumed_by: architect
---

# Phase 4 — Reliability & Cost Efficiency — Buildable Spec

This translates `phase-4-reliability-cost-efficiency.md` §4.1–§4.5 into user
stories and testable acceptance criteria. All "Open Questions" in the
source phase file are already resolved inline there (4.1, 4.3, 4.4);
this doc does not re-litigate them, only operationalizes them.

---

## 0. Non-Negotiable Architecture Decisions (carried in, not re-decided here)

1. **Database layer.** Use the existing Postgres schema from Phases 1–3. New
   tables/fields will be: `ProviderKeyHealth` (multi-key support, health
   tracking), `RateLimitConfig`, `RateLimitState`, `CacheEntry`,
   `BudgetDegradationPolicy`. No new database vendor introduced.

2. **Caching strategy.** Redis cache for exact-match entries (token counts
   and response bodies). Key format: `cache:v1:{team_id}:{user_id}:{model}:{prompt_hash}`
   to respect DLP/residency boundaries (team/user context baked into key).
   Cache miss behavior: proceed to provider, write-through on success.
   Cache must never serve a response across a residency boundary it wouldn't
   otherwise cross (enforced at lookup time, not just key design).

3. **Failover routing.** Each `ServiceAccountKey` or `ProviderKey` may have
   zero or more backup keys configured (provider-internal failover) or
   cross-provider failover (same model, different provider). Failover policy
   per team/org: `disabled` (default) or `enabled`. When enabled, the
   gateway retries against the next configured backup key on provider
   response errors (5xx, network timeout). The retry must complete within
   the failover switch time target (under 2 seconds total from failure
   detection to healthy response or final error).

4. **Rate limiter implementation.** Distributed rate limiting using Redis
   sliding-window counters (per `team_id`, `user_id`, `model`, `provider`).
   Naive in-process counters are rejected (does not survive instance
   restart, inconsistent across horizontally scaled Gateways). API:
   `team_id:rate_limit:{period}:{counter_type}` with Redis `INCRBY` +
   `EXPIREAT` for sliding windows.

5. **Graceful degradation model selection.** Delegates model selection
   logic to an additional policy layer that runs *after* the standard
   model-policy resolution. When a team's budget hits its configured
   degradation threshold (e.g., 90% of remaining budget), requests to
   expensive models are rerouted to a cheaper configured "fallback model"
   for that user/team. The original model request is logged, the
   substituted model is logged separately. The `X-Gatekey-Degraded: true`
   header indicates a downgrade occurred, plus a `X-Gatekey-Degraded-From`
   and `X-Gatekey-Degraded-To` header for the model names.

---

## 1. §4.1 Multi-Key & Failover

**User stories**

- As an Org Admin, I can configure multiple API keys per provider for a
  service account, so traffic can be distributed across keys to avoid
  quota exhaustion.
- As an Org Admin or Team Lead, I can enable/disable automatic failover
  per team (default: disabled), so compliance-sensitive teams are not
  surprised by traffic rerouting.
- As an Org Admin, I can see the health status of each provider key in
  the admin console (last check timestamp, last error, availability %).
- As a user, when failover is enabled and a provider/key fails, my
  request is automatically retried against a backup key without manual
  intervention.

**Acceptance criteria**

- AC4.1.1 — `ProviderKey` table extended with `is_primary` (boolean) and
  `primary_key_id` (nullable foreign key to another `ProviderKey` in the
  same backup group). Keys with the same `backup_group_id` form a failover
  group. A group must have at least one `is_primary=true` key.
- AC4.1.2 — A service account's `team_id` and `provider_id` determine
  which backup group it draws from (service account → team → provider →
  backup group). The backup group may contain multiple keys for the same
  provider or different providers offering the same model set.
- AC4.1.3 — **Failover policy defaults to `disabled` per team** per the
  phase doc's resolved default. A team's `failover_enabled` boolean field
  defaults to `false`; org-wide default is read from team config, not
  inherited from org level.
- AC4.1.4 — When failover is disabled, requests to a failing provider key
  return the error immediately (no retry). When enabled, the gateway
  retries against the next key in the backup group on network errors,
  5xx errors, or provider-specific errors indicating key exhaustion
  (e.g., OpenAI's rate_limit error code).
- AC4.1.5 — **Failover switch time target: under 2 seconds total** from
  first request failure to successful response (or final error after all
  retries exhausted). This includes network round-trips to all backup keys.
  Acceptance test: inject a failure into key #1, verify the gateway
  completes the request via key #2 within 2 seconds.
- AC4.1.6 — Health check endpoint per provider key: a scheduled job runs
  every 5 minutes, makes a test request (minimal token count, no cost if
  cache hit), records `last_health_check`, `last_error`, `availability_24h`
  (calculated from success/failure ratio), and updates the `ProviderKey`
  row. A key with `availability_24h < 0.9` is marked `degraded` (yellow)
  or `unavailable` (red) in the UI.
- AC4.1.7 — Admin console "Provider Keys" screen shows each key's health
  status (green/yellow/red), last check timestamp, and error message (if
  any). A key with multiple health issues shows the most recent error.
- AC4.1.8 — When failover retries, each attempt logs the key used, the
  error (if any), and whether the final response succeeded. This data
  appears in the request log detail view (Phase 1 schema extended with
  `failover_attempt` and `failover_key_id` fields).
- AC4.1.9 — The fallback key must support the same model(s) as the primary
  key; if the backup provider does not support the requested model,
  return an immediate error (not continue searching). No model mismatch
  during failover — either the backup can handle it, or fail.

**Deferred / explicitly out of scope for this section**

- Automatic key rotation triggered by health checks (Phase 3's scheduled
  rotation is explicit, this is health-based manual recommendation only).
- Cross-org backup groups (backup keys must belong to the same org).
- Smart routing based on real-time latency or cost — only health status
  (availability %) drives failover decisions in this phase.

---

## 2. §4.2 Rate Limiting

**User stories**

- As an Org Admin or Team Lead, I can configure per-team and per-user rate
  limits (requests per minute, tokens per minute) per provider/model, so
  one heavy user doesn't exhaust shared quotas.
- As an Org Admin or Team Lead, I can configure the behavior when a rate
  limit is hit: queue-and-retry or immediate reject, per team policy.
- As a user, when I exceed my rate limit, my request is either queued for
  later retry or rejected immediately with a clear error, depending on my
  team's configuration.

**Acceptance criteria**

- AC4.2.1 — Rate limit config stored per `(team_id, provider_id, model)`
  (or per `(team_id, provider_id)` with default per model), with:
  `requests_per_minute` (integer, nullable), `tokens_per_minute` (integer,
  nullable). At least one must be configured for the feature to be active.
- AC4.2.2 — **Team-level and user-level limits both enforced**. A user's
  request counts toward both the team's pool and their personal limit.
  Exceeding either triggers the configured behavior.
- AC4.2.3 — **Behavior on limit hit is configurable per team**:
  `immediate_reject` (default) or `queue_and_retry`. The configuration is
  stored on the team record.
- AC4.2.4 — `immediate_reject` returns HTTP 429 with a structured error:
  `rate_limit_exceeded`, plus `retry_after_seconds` (if queued) or
  `hard_limit` (if immediate reject). No waiting, no automatic retry.
- AC4.2.5 — `queue_and_retry` places the request in a Redis-backed queue
  with a TTL (default 60 seconds). A background worker polls the queue,
  checks rate limits again, and either forwards the request or returns a
  timeout error if the queue TTL expires before the limit clears. Queue
  depth is visible in the admin console (per team).
- AC4.2.6 — **Rate limiter is distributed/multi-instance safe**. Uses
  Redis sliding-window counters, not in-process memory. For a
  requests/minute limit of 100 on a 3-instance Gateway cluster, the Redis
  key `rate_limit:team:123:provider:openai:window:1m` holds a global count.
  Acceptance test: simulate 3 parallel Gateway instances, fire 150 requests
  at once, verify exactly 100 succeed and 50 are rejected (or queued).
- AC4.2.7 — Each rate limit check adds a header to the response:
  `X-RateLimit-Remaining: {count}`, `X-RateLimit-Limit: {limit}`,
  `X-RateLimit-Reset: {timestamp}` (ISO 8601). These are real (sliding
  window) values, not estimates.
- AC4.2.8 — Admin console "Rate Limits" screen shows per-team limits,
  current utilization (requests/minute tokens/minute in last 60 seconds),
  and queue depth (if `queue_and_retry` is enabled). Filters by team,
  provider, and model.
- AC4.2.9 — A user's personal rate limit is additive to their team's limit.
  A team with limit 100 rpm and a user with limit 50 rpm can send up to
  150 rpm total (50 to personal pool, 100 shared team pool). Exceeding
  either pool triggers rejection/queue.

**Deferred / explicitly out of scope for this section**

- Per-provider rate limit aggregation (separate limits for OpenAI vs
  Anthropic used by the same user) — each provider's quota is independent,
  not combined.
- Dynamic rate limit adjustment based on budget — limits are fixed per
  config, not adjusted as budget runs low (that's graceful degradation
  territory, §4.4).

---

## 3. §4.3 Caching

**User stories**

- As an Org Admin or Team Lead, I can enable caching per team, so requests
  to identical prompts can return cached responses and save cost.
- As a user, when caching is enabled and my prompt matches a cached entry,
  I receive the cached response with a cache header indicating it was a hit.
- As an Org Admin, I can see cache hit rate, memory usage, and cost saved
  via caching in the admin console.
- As an Org Admin, I can configure TTL per team and per cache type.

**Acceptance criteria**

- AC4.3.1 — **Exact-match caching only** in this phase. Cache key is a
  hash of: `(provider_id, model, temperature, max_tokens, messages)` —
  the exact OpenAI chat completion request body (normalized, e.g. sorted
  keys, canonical JSON). No semantic caching (near-duplicate detection)
  in this phase — that is explicitly a stretch goal flagged in the phase
  doc, not a commitment.
- AC4.3.2 — Cache is opt-in per team. A team's `cache_enabled` boolean
  defaults to `false`. When disabled, no cache read/write occurs for that
  team's requests.
- AC4.3.3 — Cache TTL configurable per team (default: 5 minutes, min 1 min,
  max 24 hours). Stored as `cache_ttl_minutes` on the team record.
- AC4.3.4 — Cache lookup must add under 10ms overhead for a cache miss
  (the NFR requirement). Acceptance test: measure gateway latency with
  cache disabled vs. cache enabled with a miss (no Redis network call
  optimized out). Difference must be under 10ms p99.
- AC4.3.5 — Cache hit response includes `X-Cache: HIT` header; cache miss
  includes `X-Cache: MISS`. Response body is unchanged (still the provider's
  JSON response, not wrapped).
- AC4.3.6 — **Cache respects DLP/residency policy from Phase 3**. A cached
  response is never served across a policy boundary. Specifically:
  - The cache key includes `team_id` and `user_id` (already required per
    AC4.3.1's key format), so a response cached for team A cannot be served
    to team B.
  - If a request's DLP scan would block/redact the prompt (Phase 3.2),
    the request is not cached at all. Cache writes only occur on unblocked
    requests.
  - Residency routing: if a response was fetched from an EU endpoint due
    to residency policy, that same cache entry is only served to requests
    from the same residency zone. (Cache key includes `residency_zone`
    derived from team/org residency config.)
- AC4.3.7 — Cache write is write-through: on cache miss, the gateway
  forwards to the provider, receives the response, writes it to cache
  (with TTL), then returns it. No cache-only paths (e.g., write-behind)
  in this phase — simplicity over optimization.
- AC4.3.8 — Cache invalidation: manual "Clear cache" button in admin
  console per team (soft clear — sets a sentinel value, not data deletion)
  and automatic TTL-based expiry (Redis `EXPIRE` command on write).
  Bulk invalidation (all teams, org-wide) available to Org Admin.
- AC4.3.9 — Admin console "Caching" screen shows per-team stats:
  - Hit rate (hits / (hits + misses) over time range)
  - Memory used (approximate, from Redis `INFO memory`)
  - Cost saved (sum of (cached response cost) for hits)
  - Top 10 most-cached prompts (teaser; not the full prompt for security)
- AC4.3.10 — Cache is implemented using Redis (not in-memory, not
  filesystem). A `docker-compose --profile redis` starts a Redis instance
  alongside the gateway (similar to the SSO profile). Plain
  `docker-compose up` does not start Redis.

**Deferred / explicitly out of scope for this section**

- Semantic caching (near-duplicate detection via embedding similarity)
  — explicitly flagged as stretch goal only, not a Phase 4 commitment.
- Cache warm-up / pre-fetching based on patterns.
- Cache hit precedence over budget checks (a cached response may bypass
  budget entirely — this is acceptable, per the phase doc's intent to
  "save cost," and budget enforcement is per request, not per unique
  prompt).

---

## 4. §4.4 Graceful Cost Degradation

**User stories**

- As an Org Admin or Team Lead, I can configure automatic model
  downgrades when a user is approaching their budget limit, so they
  continue getting service instead of hitting a hard block.
- As a user, when a request is downgraded due to budget proximity, I
  receive a clear header indicating a cheaper model was substituted.
- As an Org Admin, I can see how many requests were downgraded and how
  much cost was saved via graceful degradation in the admin console.

**Acceptance criteria**

- AC4.4.1 — Graceful degradation is per-team configurable. A team's
  `degradation_enabled` boolean defaults to `false`. When disabled, no
  automatic model substitution occurs (hard block at budget limit only).
- AC4.4.2 — Degradation threshold configurable per team (percentage of
  remaining budget that triggers downgrade, default 90%). Also configurable:
  the "fallback model" to use (must be from the team's allowed model list,
  not just any model). Stored as `degradation_threshold_pct` (e.g., 90)
  and `degradation_fallback_model` on the team record.
- AC4.4.3 — **Budget proximity detection** runs *after* model-policy
  resolution but *before* provider routing. If degradation is enabled,
  the team's current budget (current spend vs. ceiling) is evaluated.
  If remaining budget < `budget_ceiling * (degradation_threshold_pct / 100)`,
  the request's model is substituted with the configured fallback model.
- AC4.4.4 — When a downgrade occurs, the response includes headers:
  - `X-Gatekey-Degraded: true`
  - `X-Gatekey-Degraded-From: {original_model}`
  - `X-Gatekey-Degraded-To: {fallback_model}`
  The OpenAI-compatible response body is unchanged (still the fallback
  model's response), preserving the cross-phase API compatibility non-negotiable.
- AC4.4.5 — The original model and substituted model are both logged in
  the request log for audit purposes (new fields: `degraded_from_model`,
  `degraded_to_model`, nullable). A request that was downgraded appears
  with both model names visible in the log detail view.
- AC4.4.6 — The fallback model is charged at its own rate (the cheaper
  model's pricing), so the cost savings are real, not nominal. The
  logged cost is the actual provider charge for the substituted model.
- AC4.4.7 — Degradation does not apply to embeddings or completions that
  are not chat completions — this is a chat-completion model substitution
  feature only. Embeddings and non-chat completions still hit the hard
  budget block at limit.
- AC4.4.8 — Admin console "Cost Efficiency" screen shows per-team:
  - Number of requests that were downgraded (count)
  - Cost saved (sum of (original_model_cost - fallback_model_cost))
  - Total budget preserved by avoiding hard blocks (estimated)
  - Top downgraded prompts (teaser; not the full prompt)

**Deferred / explicitly out of scope for this section**

- Per-user degradation policies (only team-level config in this phase).
- Automatic fallback model selection based on cost (must be explicitly
  configured by an admin per team, not auto-discovered).
- User-facing UI notification beyond response headers (e.g., in-app alert)
  — headers are the only required signal to the calling application.

---

## 5. §4.5 Performance & Cost Dashboards

**User stories**

- As an Org Admin, I can see cache hit rate, failover event count, and
  cost saved via caching/degradation on the main usage dashboard.
- As an Org Admin, I can filter the usage dashboard by time range,
  team, and provider to see these metrics breakdown.
- As an Org Admin, I can export the dashboard data (CSV/JSON) for
  ROI reporting to finance/leadership.

**Acceptance criteria**

- AC4.5.1 — Phase 1's usage dashboard (per-user, per-model totals) is
  extended with three new metric cards:
  - Cache hit rate (percentage, last N days)
  - Failover events (count, last N days)
  - Cost saved via caching + graceful degradation (currency amount, last N days)
- AC4.5.2 — Each metric card supports filtering by:
  - Time range (last 24h, 7d, 30d, 90d, custom date range)
  - Team (single team or "All teams")
  - Provider (single provider or "All providers")
  Filters apply to all metrics simultaneously.
- AC4.5.3 — Export functionality produces CSV and JSON files with the
  same filterable dimensions (time range, team, provider) and all metrics
  (requests, cost, cache hits, failovers, downgrades, cost saved). The
  CSV includes a header row with descriptive column names.
- AC4.5.4 — The dashboard shows real-time data (not stale cache) with
  a configurable refresh interval (15s, 30s, 60s, manual only) via the
  UI (saved per-user preference).
- AC4.5.5 — Cost saved calculation:
  - Caching: `cache_hits * average_request_cost` (average from last 30 days
    of non-cache-hit requests for the same team/provider combination)
  - Graceful degradation: `sum(degraded_requests * (original_model_cost - fallback_model_cost))`
  Both are shown separately and as a combined "Total cost saved" figure.
- AC4.5.6 — A "Cost Efficiency Report" button on the dashboard generates
  a pre-filtered, org-wide export ready for finance review (all teams,
  last 30 days, combined totals). This is a one-click export to CSV/JSON.
- AC4.5.7 — Failover events are counted as any request that required
  a backup key retry (per AC4.1.7's logging). A single request that
  retries across 3 keys counts as 1 failover event (not 3).

**Deferred / explicitly out of scope for this section**

- Custom dashboard widgets (drag-and-drop, save custom layouts) — this
  phase extends the existing dashboard with the three metrics above only.
- Real-time alerts based on dashboard metrics (e.g., "failover spike
  detected") — alerting is out of scope; dashboards are read-only.
- Per-provider cache hit rate breakdown — team/provider filters exist,
  but no "cache hit rate by provider" aggregation beyond the filter.

---

## 6. Explicit Scope Boundary Summary

**In scope for Phase 4 (build now):**
- Multi-key support per provider (backup groups), team-level failover
  toggle (disabled by default), health checks per key, admin visibility
  into key health status.
- Distributed rate limiting per team/user/provider/model (Redis-based,
  sliding window), per-team configurable behavior on limit hit
  (immediate_reject vs. queue_and_retry), rate limit headers on responses.
- Exact-match caching only (no semantic caching), opt-in per team,
  TTL configurable, cache keys include team/user/context to respect
  DLP/residency boundaries, write-through strategy, header indicating
  cache hit/miss.
- Graceful degradation per team (enabled by default: false), threshold
  configurable (default 90%), explicit fallback model selection, response
  headers indicating degradation occurred, logged cost savings.
- Dashboard metrics: cache hit rate, failover events, cost saved via
  caching/degradation, with filtering and export for ROI reporting.

**Explicitly deferred / out of scope (do not build, even where UI docs
show controls on shared screens because those docs describe the full
roadmap end state):**
- Semantic caching (near-duplicate via embedding similarity) — stretch
  goal flagged in phase doc, not a commitment.
- Smart routing based on real-time latency or cost for failover — only
  health status drives decisions.
- Per-user rate limits beyond additive personal limit (no team-only mode).
- Per-user degradation policies (team-level only in this phase).
- Automatic fallback model discovery (must be explicitly configured).
- Custom dashboard layouts or real-time alerting.
- Budget marketplace / cross-team bidding (Phase 6).
- Shadow AI discovery, drift detection (Phase 5).

---

## 7. Dependencies on Prior Phases

This phase builds on the following established infrastructure:

- **Phase 1** — Core gateway architecture, provider key storage, request
  logging schema, admin console patterns, budget enforcement logic.
- **Phase 2** — Multi-tenant hierarchy (org/team/user), RBAC (Org Admin,
  Team Lead, Member roles), service account keys with team attribution,
  team-level configuration model, audit trail table.
- **Phase 3** — DLP/residency policy engine (caching must respect these
  boundaries), audit trail schema, team-level policy configuration.

Specific inter-phase requirements:

- Caching key design must include `team_id`, `user_id`, and `residency_zone`
  to enforce the Phase 3 residency boundaries (cache must never serve a
  response across a policy boundary).
- Rate limiting must reference `team_id` and `user_id` from the Phase 2
  hierarchy (users belong to teams, budgets are per-team).
- Graceful degradation uses the existing budget tracking schema from
  Phase 1/2; it is an additional policy layer on top of budget enforcement.
- All new admin console screens reuse Phase 1/2 UI patterns and RBAC
  (Org Admin and Team Lead roles have access).

---

## 8. Data Model Touchpoints (for architect — not a schema design, a checklist)

- `Team`: add `failover_enabled` (boolean, default false),
  `rate_limit_requests_per_minute` (integer, nullable),
  `rate_limit_tokens_per_minute` (integer, nullable),
  `rate_limit_behavior` (`immediate_reject`|`queue_and_retry`, default `immediate_reject`),
  `cache_enabled` (boolean, default false), `cache_ttl_minutes` (integer,
  default 5), `degradation_enabled` (boolean, default false),
  `degradation_threshold_pct` (integer, default 90),
  `degradation_fallback_model` (string, nullable).
- `ProviderKey`: add `backup_group_id` (string, nullable), `is_primary` (boolean,
  default true), `health_status` (`unknown`|`healthy`|`degraded`|`unavailable`,
  calculated from health checks), `last_health_check` (timestamp),
  `last_error` (text, nullable), `availability_24h` (decimal, nullable).
- `ServiceAccountKey`: add `backup_key_id` (nullable foreign key to
  `ProviderKey` in same backup group) — not a direct field, resolved via
  `provider_id → team_id → backup_group_id` join.
- `RequestLog` (extended from Phase 1): add `failover_attempt` (integer,
  0 = primary, >0 = retry count), `failover_key_id` (nullable foreign key),
  `cache_hit` (boolean), `degraded_from_model` (nullable string),
  `degraded_to_model` (nullable string).
- New table `CacheEntry`: `id`, `team_id`, `user_id`, `provider_id`,
  `model`, `residency_zone`, `prompt_hash` (SHA-256), `response_body`
  (JSONB), `input_tokens`, `output_tokens`, `created_at`, `expires_at`.
- New table `RateLimitCounter` (Redis-backed, but for observability/monitoring):
  `team_id`, `user_id`, `provider_id`, `model`, `counter_type`
  (`requests`|`tokens`), `window_start`, `current_count`.
- New table `DegradationEvent`: `request_id`, `team_id`, `user_id`,
  `original_model`, `degraded_model`, `original_cost`, `degraded_cost`,
  `timestamp` — for cost savings calculation in dashboard.

---

## 9. Flagged Ambiguities (genuinely open — not re-litigating resolved items)

The phase doc's own open questions are all resolved inline (4.1, 4.3, 4.4)
and used as-is per the orchestrator's instruction. The following are gaps
I found only by cross-referencing the phase doc against the UI docs:

- **A1 — Cache eviction policy for Redis is not specified.** The phase
  doc requires TTL-based expiry (already covered by AC4.3.6), but does
  not specify what happens when Redis memory fills: LRU, LFU, or error?
  **Recommend:** use Redis' default eviction policy (`maxmemory-policy
  allkeys-lru`) since it's simple and matches "cache hit rate" goal
  (keep frequently-accessed entries). Document as a deployment
  configuration detail, not a code behavior.

- **A2 — Rate limit queue TTL is not specified.** AC4.2.5 mentions a TTL
  for `queue_and_retry` but the phase doc doesn't state what it should be.
  **Recommend:** 60 seconds default, configurable via `rate_limit_queue_ttl_seconds`
  on team config (min 10s, max 300s). This balances "try to get through"
  against "don't wait forever."

- **A3 — Failover behavior on partial success is ambiguous.** If a failover
  retry returns a partial response (e.g., SSE stream cut short), should
  the gateway attempt yet another backup key or return the partial response?
  **Recommend:** partial responses (HTTP 206, stream中断) are treated as
  failures for failover purposes only if the error code indicates key
  exhaustion or rate limiting. Other partial success (network partial
  response) should return the partial data with a warning header,
  not retry automatically — the caller may have already consumed part
  of the response.

- **A4 — Degradation budget check frequency is not specified.** Should the
  budget check run on every request (expensive) or cached (stale)? **Recommend:**
  Budget state is read from the existing team budget cache (Phase 2) with
  a short TTL (1 minute) — not a fresh DB query per request, but fresh
  enough to avoid prolonged overages. This matches Phase 2's NFR for
  budget enforcement latency.

- **A5 — Cost saved calculation for caching assumes all cached responses
  have the same cost as non-cached ones, but token counts may differ if
  the provider re-encodes the prompt.** **Recommend:** when a cache hit
  occurs, log `input_tokens=0, output_tokens=0` for the cached request
  (since no actual inference happened), and calculate "cost saved" as
  the full normal cost of that model's response. The `CacheEntry` stores
  the actual `input_tokens` and `output_tokens` from the original request
  for accurate historical tracking.

---

## 10. Non-Functional Requirements (testable)

- **Failover switch time:** Under 2 seconds from detecting a provider
  failure to routing the next request to a healthy backup. Acceptance
  test: inject failure into key #1, measure time to successful response
  via key #2.

- **Cache lookup overhead:** Under 10ms added latency for a cache miss.
  Acceptance test: measure p99 gateway latency with cache enabled vs.
  disabled on identical request patterns (no cache warm-up).

- **Rate limiter accuracy under distributed deployment:** Must be accurate
  when Gatekey is horizontally scaled. Acceptance test: deploy 3 Gateway
  instances behind a load balancer, fire 150 rapid requests at a 100
  requests/minute limit, verify exactly 100 succeed (or 50 queued, if
  queue_and_retry is enabled).

- **DLP/residency boundary respect in caching:** A cached response must
  never be served across a policy boundary. Acceptance test: configure
  team A as "US-only" residency, team B as "EU-only"; team A makes a
  request, caches it; team B's identical request must NOT get the cache
  hit (must proceed to provider and respect EU residency).

- **Degradation header accuracy:** `X-Gatekey-Degraded: true` must be
  present when and only when a model substitution occurred. Acceptance
  test: verify header absence on non-degraded requests, presence on
  degraded ones; verify `X-Gatekey-Degraded-From` and
  `X-Gatekey-Degraded-To` contain valid model names.

---

## 11. Success Criteria (from phase doc)

- **At least one pilot org measures and reports a concrete cost reduction
  (from caching and/or degradation) over a 30-day window.** Acceptance:
  Export from Phase 4's dashboard shows "Cost saved via caching" or
  "Cost saved via graceful degradation" > $0 for a 30-day window.

- **A simulated provider outage is handled by automatic failover without
  a pilot user noticing (validated in a game-day/chaos test with the
  pilot team's consent).** Acceptance: Orchestrated outage of primary
  provider key, requests automatically succeed via backup key, user
  receives successful response with `X-Cache: MISS` and no visible error.

- **Rate limiting is exercised under real burst traffic from at least one
  pilot app without incorrectly throttling normal usage.** Acceptance: A
  pilot app sends a burst of traffic exceeding the configured rate limit,
  exactly the expected number of requests are rejected/queued (per AC4.2.6),
  and normal (non-burst) traffic continues to succeed.

---

## 12. Open Questions to Resolve Before Building

All questions in the phase doc are resolved inline (4.1, 4.3, 4.4). The
following remain open due to new ambiguities uncovered during spec
translation — they are not re-litigating phase doc content:

- **Q1 — What Redis eviction policy to use for caching?** As flagged in
  A1 above, the phase doc only specifies TTL expiry, not what happens
  when Redis is full. Default `allkeys-lru` is recommended, but this
  needs explicit confirmation before Redis deployment.

- **Q2 — What is the default queue TTL for `queue_and_retry` rate limiting?**
  As flagged in A2 above, the phase doc does not specify. 60 seconds
  is recommended, but may need adjustment based on pilot usage patterns.

- **Q3 — How should partial success (stream中断, HTTP 206) be handled
  during failover?** As flagged in A3 above, partial responses may or
  may not indicate a failure. The gateway must distinguish between
  "key exhausted" (retry) and "partial but valid response" (return to
  caller, no retry).

**Recommendation:** Address Q1 and Q2 during technical design (they are
implementation decisions with minimal risk). Q3 may require a brief
design sync with pilot teams to understand their expectations for
streaming response behavior.
