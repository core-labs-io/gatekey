---
title: Gatekey — Tech Stack Decision
status: decided
last_updated: 2026-07-11
---

# Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Gateway core / backend | Python (FastAPI) | Async/streaming support for SSE pass-through; matches the ecosystem of comparable tools (LiteLLM, etc.); strong fit for future DLP/content-classification work in Phase 3/5. |
| Admin console frontend | Next.js / React | Common, well-supported default for the admin console UI. |
| Database | PostgreSQL | Fits the relational budget/audit/policy data model; supports Row-Level Security for multi-tenant isolation between orgs/teams. |
| Deployment | Docker Compose (Phase 1) | Per the overview's self-hosted-first, under-an-hour setup requirement. |

Backend and frontend are separate services (not a single full-stack framework) given the gateway core's latency/streaming requirements are distinct from the admin console's needs.

This decision is binding for all phases unless explicitly revisited — `architect`, `backend-developer`, `frontend-developer`, and `database-admin` are pre-loaded with it so it doesn't need to be re-specified per task.
