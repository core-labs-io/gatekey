---
title: Phase 4 — Reliability & Cost Efficiency
status: draft
last_updated: 2026-07-10
---

# Phase 4 — Reliability & Cost Efficiency

## Goal
Make Gatekey cheap and resilient enough to be the default path for AI traffic, not just the compliant one. This phase produces the concrete cost-savings and uptime numbers needed to justify it to a CFO/CTO.

## Depends On
Phase 2 (multi-key-per-provider groundwork) and Phase 3 (stable policy engine that caching/routing must respect — e.g., don't cache across a DLP-redaction boundary).

## In Scope

### 4.1 Multi-Key & Failover
- Support multiple keys per provider (deferred from Phase 1), enabling routing across them for quota/rate-limit spreading.
- Automatic failover: if a provider/key is down or erroring, automatically retry against a configured backup key or provider, per policy (some orgs may want failover disabled for compliance reasons — must be opt-in per team, not global-only). **Resolved (default):** off by default per team, confirming the bias already stated above — a compliance-sensitive team should have to explicitly opt in to traffic being rerouted, not discover after the fact that it was happening. This matches the admin console's design (`ui-requirements-admin.md` §6), which already ships the failover toggle off by default.
- Health checks per provider/key with visible status in the admin console.

### 4.2 Rate Limiting
- Per-user and per-team request-rate limits (requests/min, tokens/min) to protect shared provider quotas from being exhausted by one heavy user.
- Configurable behavior on limit hit: queue-and-retry vs. immediate reject, per team policy.

### 4.3 Caching
- Exact-match caching (identical prompt + params → cached response) with configurable TTL, opt-in per team (some data sensitivity policies may disallow caching).
- Semantic caching (near-duplicate prompt detection) as a stretch goal within this phase — flag as such rather than a hard commitment, since it requires an embedding model and adds complexity. **Resolved:** stretch goal only, not a Phase 4 commitment. Ship exact-match caching first and let its real-world hit-rate data from pilots determine whether semantic caching is worth the added complexity — matches the "validate demand before building" principle Phase 5 states explicitly, applied here too rather than building speculatively.
- Cache must respect DLP/residency policy from Phase 3 — a cached response must not be served across a policy boundary it wouldn't otherwise be allowed to cross.

### 4.4 Graceful Cost Degradation
- When a user or team is approaching (not yet at) their budget limit, optionally auto-downgrade requests to a cheaper configured model instead of waiting for a hard block, per team-configurable policy.
- User is notified (response header or log entry) when a request was downgraded due to budget proximity, so behavior isn't silently surprising. **Resolved (programmatic detection):** yes, via a response header (e.g., `X-Gatekey-Degraded: true` plus which model was substituted). This is additive — it doesn't touch the OpenAI-compatible response body shape, so it doesn't conflict with the cross-phase API-compatibility non-negotiable — and it's what actually lets a calling app *act* on a downgrade (retry differently, surface a notice to its own user) rather than only finding out after the fact in a log.

### 4.5 Performance & Cost Dashboards
- Extend Phase 1/2 usage dashboards with: cache hit rate, failover event count, cost saved via caching/degradation, rate-limit rejection counts.
- These numbers are the deliverable for the ROI conversation with finance/leadership.

## Out of Scope for Phase 4
- Budget marketplace / cross-team bidding (Phase 6)
- Shadow AI discovery, drift detection (Phase 5)

## Non-Functional Requirements
- Failover switch time: under 2 seconds from detecting a provider failure to routing the next request to a healthy backup.
- Cache lookup must not meaningfully add latency for a cache miss (target: under 10ms overhead).
- Rate limiter must be accurate under distributed/multi-instance deployment (no naive in-process counters if Gatekey is horizontally scaled).

## Success Criteria
- At least one pilot org measures and reports a concrete cost reduction (from caching and/or degradation) over a 30-day window.
- A simulated provider outage is handled by automatic failover without a pilot user noticing (validated in a game-day/chaos test with the pilot team's consent).
- Rate limiting is exercised under real burst traffic from at least one pilot app without incorrectly throttling normal usage.

## Open Questions to Resolve Before Building
All three questions originally listed here are resolved inline above (4.1, 4.3, 4.4) — see those sections for the decision and rationale. None remain open for this phase.
