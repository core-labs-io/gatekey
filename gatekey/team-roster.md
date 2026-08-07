---
title: Gatekey — Sub-Agent Team Roster
status: draft
last_updated: 2026-07-10
---

# Sub-Agent Team Roster

How Gatekey's build process is organized across specialized agents, once implementation starts. Every role below is implemented as a project-scoped custom agent in `.claude/agents/`, each pre-loaded with Gatekey's non-negotiables (self-hosted first, no plaintext keys, OpenAI-compatible API surface, per-phase docs) so that context doesn't need to be re-supplied on every delegation.

## Orchestration Model

`orchestrator` is the lead agent for any Gatekey build task: reads the relevant phase requirement file, breaks it into tasks, delegates to the right specialist agent (in parallel where independent, sequential where dependent), and reviews each agent's output before handoff to the next role.

`scrum-master` tracks status underneath the orchestrator on anything spanning more than a couple of steps: what's done vs. outstanding per phase requirement, and flags dependency conflicts or scope creep before they cause rework.

For a phase where the full pipeline should run with minimal manual steering, a scripted **Workflow** can be authored to run the same team as a background pipeline instead of step-by-step delegation via the orchestrator agent. Which mode to use is decided per phase.

## Roles

| Role | Agent file | Responsibility |
|---|---|---|
| Orchestrator | `.claude/agents/orchestrator.md` | Breaks down phase work, sequences and delegates to specialists, reviews outputs before handoff. Entry point for any build task. |
| Scrum Master | `.claude/agents/scrum-master.md` | Tracks task/phase status, flags blockers and dependency conflicts, catches scope creep against each phase's "Out of Scope" section. |
| Product Owner | `.claude/agents/product-owner.md` | Turns each phase's requirements file into buildable specs: user stories, acceptance criteria, scope boundaries. First specialist engaged per phase. |
| Architect | `.claude/agents/architect.md` | System design and ADRs for the phase; breaks the spec into small buildable tasks; guards architectural integrity across phases (e.g., Phase 4 caching must respect Phase 3 policy boundaries). |
| Database Admin | `.claude/agents/database-admin.md` | Schema design, migrations, indexing — owns the budget ledger, audit log, and policy data models; enforces atomic budget accounting under concurrency. |
| Backend Developer | `.claude/agents/backend-developer.md` | Gateway core, provider routing, budget enforcement, policy engine, service-layer logic. |
| Frontend Developer | `.claude/agents/frontend-developer.md` | Admin console, dashboards, self-service UI for Team Leads/Org Admins. |
| QA Engineer | `.claude/agents/qa-engineer.md` | Test strategy, edge cases, verifies each phase's stated Success Criteria before it's considered done. |
| Security Reviewer | `.claude/agents/security-reviewer.md` | Mandatory pass on every phase, not optional — this product's core function is handling API keys, PII/DLP, and compliance data. Required before any phase ships, and again whenever auth/crypto/secrets code changes. |
| DevOps Engineer | `.claude/agents/devops-engineer.md` | Docker deployment (Phase 1's under-an-hour setup requirement), CI/CD, environment/secrets management as the project matures. |
| Docs Writer | `.claude/agents/docs-writer.md` | Each phase commits to shipping docs sufficient for self-deploy — owned as its own step at phase close, not folded into another role. |
| Code Reviewer | `.claude/agents/code-reviewer.md` | Correctness/simplification/reuse/efficiency pass on each diff, distinct from QA (behavior) and security review (safety). |
| Release Manager | `.claude/agents/release-manager.md` | OSS versioning, changelog, release notes. Deliberately lightweight — becomes active from Phase 4/5 onward once there's a real user base and release cadence; not invoked for internal phase work before that. |

## Sequencing Per Phase (default pattern)

1. `orchestrator` reads the phase requirement file and kicks off the sequence.
2. `product-owner` turns it into a working spec.
3. `architect` designs the approach and splits it into tasks.
4. `database-admin` (if the phase touches data model) designs schema/migrations in parallel with backend work starting.
5. `backend-developer` and `frontend-developer` implement in parallel where their work doesn't block each other.
6. `qa-engineer` verifies against the phase's stated Success Criteria.
7. `security-reviewer` signs off — required gate, not optional, every phase.
8. `devops-engineer` confirms deployability (especially Phase 1's setup-time requirement).
9. `code-reviewer` passes before the phase is marked done.
10. `docs-writer` produces the phase's self-deploy documentation.
11. `scrum-master` tracks status throughout; `release-manager` engages only from Phase 4/5 onward for actual version cuts.

This is the default order; independent sub-tasks within a phase run in parallel rather than strictly serially where there's no real dependency — `orchestrator` decides parallelization per phase.

## Open Decision

Whether to run each phase as a manually-steered sequence of `orchestrator`-driven Agent delegations (more visibility between steps, more back-and-forth) or as a scripted Workflow pipeline (faster, runs in background, less step-by-step visibility) will be decided per phase when implementation actually starts.
