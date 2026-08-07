---
title: Phase 5 — Differentiators
status: draft
last_updated: 2026-07-10
---

# Phase 5 — Differentiators

## Goal
Ship the features that no other AI gateway product currently offers, now that the core platform (routing, budgets, policy, logging) is stable. These are the features that make the strongest pitch to security- and compliance-driven buyers — but they're also the least proven in market, so validate demand before building all of them.

## Depends On
Phases 1–4 complete and stable — these features build directly on the request pipeline, policy engine, and logging infrastructure established earlier.

## In Scope

### 5.1 Shadow AI Discovery
- Detect employee usage of unsanctioned AI tools that bypass Gatekey entirely (e.g., direct calls to `api.openai.com`, `chat.deepseek.com`, consumer ChatGPT web usage).
- Initial approach options to evaluate: DNS/network log ingestion from existing enterprise security tooling (SASE/proxy logs), or a lightweight browser extension that flags and optionally blocks non-gateway AI traffic. **Default direction pending real input (see Open Questions):** SASE/proxy log ingestion as the v1 approach — it needs no client-side software deployment, no endpoint-agent IT approval process, and works with tooling most target enterprises already run. Treat the browser extension as a fallback for orgs without existing SASE/proxy logging, not the default build target — but this should be confirmed against what the first real design partner's security stack actually looks like before committing engineering time, since it's cheap to be wrong about in the wrong direction (building a browser extension nobody's IT team will approve).
- Findings surfaced in the admin console as a "shadow AI" report: which users/teams, which unsanctioned tools, frequency — not necessarily blocking by default, since this is a detection/awareness feature first.
- Optional enforcement mode (block/redirect to gateway) as a later increment within this phase, gated behind explicit org opt-in given its intrusiveness.

### 5.2 Cryptographically Hash-Chained Audit Ledger
- Extend the Phase 3 audit log into a tamper-evident chain: each log entry includes a hash of the previous entry, so any retroactive modification is detectable.
- Provide a verification tool/endpoint that lets an auditor confirm the chain's integrity from genesis to present.
- Target use case: legal/compliance teams needing to demonstrate "this AI-generated output was produced exactly as logged, unaltered" — position this explicitly for regulated industries (legal, finance, healthcare, government).
- Does not require an actual distributed blockchain — a simple hash-chain with periodic external anchoring (e.g., publishing a chain-head hash to an external timestamping service) is sufficient. **Resolved (is external anchoring required for v1):** no — ship the in-database hash chain first; treat external anchoring as a fast-follow gated on an actual regulated-industry design partner explicitly needing it for a specific compliance framework that requires third-party timestamping. Building the anchoring integration speculatively adds real complexity (choosing and integrating a timestamping service) with no validated need yet — exactly the kind of premature scope this phase's own framing warns against.

### 5.3 Content-Classification-Aware Dynamic Routing
- Build on the basic DLP-triggered rule from Phase 3 (3.4) into full dynamic classification: automatically categorize prompt content by sensitivity (PII, source code, financial data, legal, general) and route to models/providers pre-approved for that category.
- Support integration with sensitivity labels enterprises already maintain (e.g., Microsoft Purview labels, Google DLP classifications) so policy doesn't need to be reinvented from scratch.
- Admin defines category → allowed-model mappings; the classifier assigns each request to a category; the router enforces the mapping automatically, without a human maintaining per-team allow lists manually.

### 5.4 Provider Drift Detector
- Maintain a canary suite of test prompts run on a schedule (e.g., daily) against each actively-used model.
- Compare outputs, latency, and refusal-rate against a stored baseline; flag statistically significant drift to admins.
- Purpose: providers periodically change model weights behind a stable API/version name without notice — this gives enterprises an audit trail and early warning for "our AI vendor changed something," which currently no product in this space surfaces.
- Drift alerts should be exportable/logged alongside the audit trail from 5.2, since a drift event may be relevant to compliance review.

### 5.5 Unified Governance for BYOK + Self-Hosted OSS Models
- Allow a self-hosted inference endpoint (e.g., vLLM, Ollama) to be registered as a "provider" alongside BYOK provider keys, under the same policy/budget/audit plane.
- Normalize cost accounting across token-based provider pricing and compute-based self-hosted cost (e.g., estimated cost per request based on configured GPU-hour rate) so budgets remain meaningful across both.
- Positions Gatekey as the migration path from paid provider APIs to self-hosted models, not just a proxy in front of paid APIs — notable differentiator for cost-conscious enterprises and a natural fit for an open-source project's audience.

## Out of Scope for Phase 5
- Policy-as-code plugin marketplace, internal budget marketplace (Phase 6)

## Non-Functional Requirements
- Shadow AI discovery must be clearly scoped and documented regarding what data it collects, given its inherent privacy sensitivity — this feature needs its own mini data-handling policy, reviewed before building.
- Drift detection canary runs must not consume meaningful budget — use a small, fixed, cheap prompt set, and count its cost separately from user-attributable usage.

## Success Criteria
- Validate demand before building all five: run design-partner conversations or a lightweight survey to rank interest in 5.1–5.5, and prioritize accordingly rather than building in listed order by default.
- At least one pilot org uses the audit ledger verification tool to confirm chain integrity as part of a real (or simulated) compliance exercise.
- Drift detector catches or would have caught at least one real provider-side model change during the pilot window, demonstrated via the canary history.

## Open Questions to Resolve Before Building
The hash-chain-anchoring question is resolved inline above (5.2). The shadow-AI approach question has a default direction stated inline above (5.1) but is intentionally not fully closed — it depends on a specific enterprise's security stack, which is a fact only a real design partner can supply, not something this doc should guess at with confidence.

**Still genuinely open, needs real design-partner input, not a product-design call this doc can resolve alone:**
- Which of the five features has the strongest pull from actual design partners — this determines build order within the phase, per this phase's own success criteria (validate demand before building all five). **Interim default if forced to sequence before that signal exists:** lowest-integration-risk first — 5.2 (Hash-Chained Ledger, builds directly on the existing Phase 3 audit log with no external dependency) and 5.4 (Provider Drift Detector, self-contained canary suite against existing provider integrations) ahead of 5.5 (BYOK + self-hosted governance), 5.3 (content-classification routing, larger scope), and 5.1 (Shadow AI, highest lift/uncertainty per the note above). Real demand signal overrides this the moment it exists — this ordering exists only so the phase isn't fully blocked before that conversation happens.
- For shadow AI discovery specifically: does the target enterprise already have SASE/proxy logging Gatekey can integrate with — confirm before committing to the default direction stated in 5.1.
