---
title: Phase 1 — Core Gateway (MVP)
status: draft
last_updated: 2026-07-10
---

# Phase 1 — Core Gateway (MVP)

## Goal
Prove the basic proxy model works end-to-end with real traffic from a small pilot team, with low enough setup friction that someone will actually try it.

## In Scope

### 1.1 Provider & Key Management
- Store and manage API keys for at least 3 providers at launch: OpenAI, Anthropic, Google Vertex AI. **Resolved:** these three ship together, not sequenced by demand — the scope above already names all three, and splitting engineering effort to sequence them by "pilot demand" only makes sense once a specific design partner's provider mix is known, which isn't the case yet. If a real design partner's needs later force a different order, that's a scheduling change, not a scope change.
- Keys encrypted at rest (AES-256 or via KMS); never returned in plaintext through any API or UI after initial entry.
- One key per provider per org to start (multi-key per provider deferred to Phase 2).
- Basic key validation on entry (test call to provider to confirm the key works before saving).

### 1.2 Unified API / Gateway Core
- Single OpenAI-compatible REST endpoint (`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` at minimum) that routes to the correct provider based on the model requested. **Resolved (embeddings vs. vision):** chat/completions + embeddings, as scoped above, is sufficient for v1 — both are already listed as "at minimum." Vision/multimodal endpoints are explicitly deferred: they add real complexity (multipart uploads, per-image cost normalization that doesn't fit the token-based pricing model 1.4 assumes) that isn't needed to prove the core proxy model per this phase's Goal, and no in-scope requirement here depends on it. Revisit once a concrete pilot app needs it.
- Streaming response support (SSE) — many internal apps depend on this from day one.
- Official SDK/client examples for at least Python and JavaScript/TypeScript showing drop-in replacement of a direct provider SDK call.
- Per-app service-account keys (distinct from human user identity) so internal applications can authenticate without a human login.

### 1.3 Model Access Governance (Basic)
- Org-wide static allowlist/denylist of models (flat list, no team nesting yet).
- Request to a denied model returns a clear, structured error (not a silent failure or generic 403).

### 1.4 Budget (Basic)
- One flat spend budget per user, defined in currency (e.g., USD).
- Hard cutoff: requests blocked once a user's budget is exhausted, with a clear error message.
- Cost computed per request using provider's published token pricing, normalized to a common currency.

### 1.5 Logging & Observability (Basic)
- Per-request log: user, model, provider, input/output token counts, cost, latency, timestamp, success/failure.
- Simple usage view: totals by user, by model, over a selectable time range.
- No prompt/response body logging required yet (defer to Phase 3 with redaction controls) — but the schema should anticipate adding it.

### 1.6 Admin Console (Minimal)
- Add/edit/remove provider keys.
- Add/remove users, set per-user budget.
- Set org-wide model allowlist/denylist.
- View usage dashboard (from 1.5).
- Single "org admin" role only — no RBAC tiers yet (full RBAC is Phase 2).

### 1.7 Deployment
- `docker-compose up` gets a working instance running locally in under an hour, including a seed/setup wizard for first admin account and first provider key.
- Config via environment variables / a single config file — no external dependencies beyond a database (e.g., Postgres) and the container runtime.
- **Resolved (self-hosted only vs. hosted sandbox):** self-hosted only for Phase 1 — no hosted evaluation sandbox. This matches the cross-phase "self-hosted first" non-negotiable (`00-overview.md`) directly, and standing up a hosted sandbox is a distinct ops/infra investment that would split early engineering effort across two deployment models before either is proven. The under-60-minutes `docker-compose up` success criterion already exists specifically to make local evaluation frictionless without needing a hosted option. If self-hosted setup friction turns out to still be a real adoption blocker despite hitting that target, a hosted sandbox becomes a marketing/evaluation initiative to revisit later — not a Phase 1 build item.

## Out of Scope for Phase 1
- SSO/SCIM
- Team hierarchy, RBAC beyond a single admin role
- Budget rollover
- Caching, rate limiting, failover
- DLP/PII redaction
- Audit trail beyond request logs
- Any of the Phase 5 differentiator features

## Non-Functional Requirements
- p99 added latency overhead from the gateway itself: target under 150ms (excluding provider inference time).
- Must not lose or double-charge requests on provider timeout/retry — idempotent cost accounting.
- Basic uptime target for pilot use: no formal SLA yet, but no single point of failure that isn't documented as a known limitation.

## Success Criteria
- A pilot team can deploy Gatekey, route real production or near-production traffic for at least one internal app through it, and see accurate cost/usage data.
- Setup, from `git clone` to first successful proxied request, takes under 60 minutes for someone unfamiliar with the codebase.
- Switching an existing internal app from a direct provider SDK call to Gatekey requires changing only the base URL and API key.

## Open Questions to Resolve Before Building
All three original questions here are resolved inline above (1.1, 1.2, 1.7) — see those sections for the decision and rationale. None remain open for this phase.
