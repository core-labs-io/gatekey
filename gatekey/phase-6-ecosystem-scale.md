---
title: Phase 6 — Ecosystem & Scale
status: draft
last_updated: 2026-07-10
---

# Phase 6 — Ecosystem & Scale

## Goal
Build platform/community-effect features once there's a real user base to build for. This phase assumes Gatekey has active design partners and/or open-source community contributors — it should not be started speculatively.

## Depends On
Phases 1–5 in production use with a real (even if small) community/user base providing feedback on what ecosystem features would actually get used.

## In Scope

### 6.1 Policy-as-Code Plugin Marketplace
- Expose the policy engine (model routing, budget rules, DLP actions, content classification from Phase 5) as pluggable policy-as-code, using an engine like OPA/Rego or an equivalent embeddable rules language.
- Support community-authored and shared policy packs (e.g., "HIPAA-oriented baseline policy," "EU residency + GDPR baseline") that orgs can adopt and customize rather than writing from scratch.
- Marketplace/registry for discovering and installing community policy packs, with a review/vetting process to avoid malicious or broken policies being widely adopted — this is a trust-and-safety surface, not just a technical feature. **Resolved (vetting model):** start with core-maintainer-run manual review before a pack is listed; defer a formal community-governance model (Terraform/Helm-registry-style third-party review) until submission volume actually exceeds what manual review can sustain. Building that governance structure before there's a real submission pipeline to govern would be process built ahead of the demand this phase is supposed to be driven by.
- This leans directly into being open source: a closed-source competitor cannot easily replicate a community policy ecosystem.

### 6.2 Internal Budget Marketplace
- Extend Phase 2's within-team budget rollover to a cross-team mechanism: a Team Lead can list surplus budget as available, other Team Leads can request/bid for temporary access to it.
- Org Admin approval thresholds configurable (e.g., auto-approve under $X, require sign-off above that).
- Full transaction ledger of budget transfers between teams, feeding the audit trail established in Phase 3/5. **Resolved (transactional guarantee level):** a best-effort ledger reconciled by an admin is sufficient for v1 — this is internal bookkeeping within one company, not settlement between separate legal entities. The enforcement-critical path (a team can never spend past its ceiling) is already covered by Phase 2's atomic spend-check-and-deduct guarantee; the marketplace ledger on top of it needs to be accurate and auditable, not "real-money-grade" with cross-team two-phase commit.

### 6.3 ROI / Impact Attribution
- Integrations (e.g., Jira, GitHub, ticketing systems) to correlate AI usage/cost with downstream business outcomes — tickets closed, PRs merged, docs produced — so finance sees cost-per-outcome, not just raw spend.
- This is inherently integration-heavy and should be scoped to 1–2 integrations first based on pilot org tooling, not built as a generic framework upfront.

### 6.4 Cost Forecasting
- Historical-trend-based forecasting per team/org (simple trend extrapolation to start; ML-based forecasting only if trend-based proves insufficient) to flag likely budget overage before it happens, rather than only alerting at the 80%/100% thresholds from Phase 2.

### 6.5 Self-Serve Model Evaluation Sandbox
- Allow an admin to trial a new model against synthetic/masked replicas of company data before broadly allowlisting it for a team — reduces the "how do we know this model is good/safe enough" friction that currently blocks new model adoption.

## Out of Scope for Phase 6
- Anything not already validated as wanted by real users of Phases 1–5 — this phase should be the most demand-driven of all of them, and features here are candidates, not commitments.

## Non-Functional Requirements
- Policy marketplace submissions need a vetting/sandboxing process before execution against real org traffic — a malicious or buggy community policy must not be able to exfiltrate data or bypass budget enforcement.
- Cross-team budget transfers must maintain the same atomicity/consistency guarantees as the Phase 2 within-team reassignment.

## Success Criteria
- At least one community-contributed policy pack is published, installed by a different org than the one that authored it, and used in production.
- Cross-team budget marketplace activity is exercised by real Team Leads, with the transaction ledger correctly reconciling against each team's ceiling.
- Ship only the sub-features (6.1–6.5) that pilot/community feedback from earlier phases actually asked for — treat this file as a menu, not a fixed scope.

## Open Questions to Resolve Before Building
The vetting-process and budget-marketplace-guarantee questions are resolved inline above (6.1, 6.2).

**Still genuinely open, needs real design-partner input:**
- Which 1–2 outcome-attribution integrations (6.3) do actual pilot orgs care about most. The doc's own instruction here is explicit — don't guess, ask — and that still holds: this is a fact about specific pilot orgs' tooling (Jira vs. GitHub vs. something else entirely) that no amount of internal reasoning substitutes for. No default direction is stated for this one; asking real design partners is the resolution path, not a fallback to pick if asking isn't possible.
