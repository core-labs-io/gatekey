---
title: Gatekey — Product Requirements Overview
status: draft
last_updated: 2026-07-10
---

# Gatekey

**One-liner:** An open-source, self-hostable enterprise AI gateway. Companies bring their own provider API keys (Vertex AI, Azure OpenAI, AWS Bedrock, Anthropic, OpenAI, DeepSeek, etc.); Gatekey sits in the middle as a unified proxy and governance layer — controlling which models employees can use, enforcing budgets at user/team/company level, and providing full observability and audit over all AI traffic.

Gatekey is **not** a model host or a new AI provider. It never performs inference itself — it mediates access to existing provider keys under policy.

## Phase Index

| Phase | Name | Focus | File |
|---|---|---|---|
| 1 | Core Gateway (MVP) | Prove the proxy model works end-to-end | [phase-1-core-gateway.md](phase-1-core-gateway.md) |
| 2 | Multi-Tenant Governance | Org/team/user hierarchy, budgets, rollover | [phase-2-multi-tenant-governance.md](phase-2-multi-tenant-governance.md) |
| 3 | Security & Compliance Hardening | DLP, audit trail, residency, nested policy | [phase-3-security-compliance.md](phase-3-security-compliance.md) |
| 4 | Reliability & Cost Efficiency | Caching, rate limiting, failover, degradation | [phase-4-reliability-cost-efficiency.md](phase-4-reliability-cost-efficiency.md) |
| 5 | Differentiators | Shadow AI discovery, audit ledger, drift detection | [phase-5-differentiators.md](phase-5-differentiators.md) |
| 6 | Ecosystem & Scale | Plugin marketplace, budget marketplace, ROI attribution | [phase-6-ecosystem-scale.md](phase-6-ecosystem-scale.md) |

## Build Team

See [team-roster.md](team-roster.md) for the sub-agent structure (orchestrator, product owner, architect, developers, QA, security, devops, etc.) used to implement each phase.

## Sequencing Rationale

- Phases 1–4 are the "spine": each is a complete, independently usable product, so real pilot orgs can test at every stage rather than waiting for a finished build.
- Phase 3 (compliance) is deliberately placed before Phase 4 (polish/efficiency) because compliance sign-off is typically the actual blocker to enterprise adoption, not performance.
- Phase 5 (the unique differentiators) comes after the core platform is stable because those features build on logging, routing, and policy infrastructure established earlier — building them first would mean rebuilding them later.
- Phase 6 assumes a real user base exists to build ecosystem features for.

## Cross-Phase Non-Negotiables

These apply to every phase, not called out per-file:

- **Self-hosted first.** Docker-deployable; no mandatory phone-home telemetry. Enterprise buyers of a key-management product will not adopt a SaaS-only or telemetry-leaking tool.
- **No plaintext provider keys** at rest or in logs, from Phase 1 onward.
- **OpenAI-compatible API surface** maintained across phases so integrations built in Phase 1 keep working through Phase 6.
- **Every phase ships with docs** sufficient for a design partner to self-deploy and test without engineering support from the core team.
