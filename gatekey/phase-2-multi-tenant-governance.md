---
title: Phase 2 — Multi-Tenant Governance
status: draft
last_updated: 2026-07-10
---

# Phase 2 — Multi-Tenant Governance

## Goal
Turn Gatekey from "a proxy one team runs" into something a whole company can run, with delegated administration so IT isn't a bottleneck for every budget or access change.

## Depends On
Phase 1 (Core Gateway) must be stable in production for at least one pilot org before starting this phase.

## In Scope

### 2.1 Identity & Hierarchy
- Org → Team → User hierarchy (a user belongs to one or more teams; a team belongs to one org).
- Role-based access control with at least these roles:
  - **Org Admin** — full control over the org: providers, keys, org-wide policy, all teams/budgets.
  - **Team Lead** — manages their own team's members, team budget reassignment, team-level model restrictions (can only restrict further, never loosen org baseline — see 2.3).
  - **Member** — consumes the gateway under whatever policy/budget applies to them; no admin access.
  - **Auditor (read-only)** — can view usage, logs, and policy state across the org but cannot change anything.
- SSO login via OIDC (Okta, Azure AD, Google Workspace as initial targets).
- SCIM-based auto-provisioning/deprovisioning of users and team membership (if this proves too large for this phase, it may slip to Phase 3, but should not slip further). **Resolved (is SCIM a hard requirement for this phase):** no. The Self-Service Onboarding & Approval Workflow (2.6) provides a complete, non-SCIM path for a new user to reach working gateway access (SSO login → profile + team selection → Team Lead approval with budget). That path didn't exist when this question was first raised; now that it does, SCIM is a nice-to-have automation on top of an already-functional manual flow, not a blocker to pilot launch. It can slip to Phase 3 without leaving pilot orgs stuck.

### 2.2 Team & Budget Management
- Budgets settable at three levels: **company (org)**, **team**, **user**.
- **Resolved (multi-team membership):** a user *can* belong to more than one team simultaneously. Budget is per **(user, team)** pair, not a single global figure per user — each team membership carries its own allocation from that team's ceiling. This means a user's personal API keys (2.5) and any service-account key attributed to them must each declare which team context they're operating under at creation time, so budget/policy resolution for a given request is always unambiguous (resolve against that key's declared team, never "guess" from the user's team list). This is already reflected in the admin console's Teams & Users design (a member row shows which team's budget context it's displaying).
- A team's total allocated budget cannot be exceeded by the sum of its members' individual budgets — enforced at assignment time, not just at spend time.
- **Budget rollover / reassignment:** a Team Lead can move unused budget from one team member to another, constrained so the team's total never exceeds its org-allocated ceiling.
- Configurable period boundaries (monthly/quarterly) per team, with a policy choice per team for what happens to unused budget at period end: roll over to next period, or reset to zero. **Resolved (default if unconfigured):** reset to zero. Rollover accumulating indefinitely by default risks silent, unintended budget growth that no one explicitly chose; reset-to-zero is the conservative, auditable default and matches this doc's general pattern of defaulting anything that changes standing behavior to the safer/off option (rotation off by default, schedule restrictions off by default, failover off by default in Phase 4). A Team Lead who wants rollover opts into it explicitly.
- Soft threshold alerts (e.g., 80%, 100%) delivered via email and a generic webhook (Slack-compatible payload) to the relevant Team Lead and/or Org Admin.
- Cost normalization: since providers price differently (per-token, per-character, per-request), budgets must be enforceable in a single currency unit regardless of which provider/model is actually used, with the normalization logic auditable (an admin can see how a raw provider cost was converted).

### 2.3 Nested Model Policy
- Team-level model allow/deny lists that can only be a subset of (i.e., further restrict) the org-wide baseline set in Phase 1 — a Team Lead cannot re-enable a model the Org Admin has banned.
- Policy precedence must be deterministic and visible in the admin console (a user should be able to see *why* a model is unavailable to them — which policy layer blocked it).

### 2.4 Delegated Admin Console
- Team Lead self-service views: manage own team's members, reassign budget within team ceiling, set team-level model restrictions, view own team's usage.
- Org Admin views from Phase 1, extended to manage teams, org-wide RBAC assignment, and org-level budget ceilings per team.
- Audit trail (lightweight in this phase — full immutable audit ledger is Phase 3) of budget reassignments and policy changes: who changed what, when, old value → new value.

### 2.5 Self-Service API Keys
- Once a human can log in via SSO (2.1), they can generate their own personal API key(s) directly — this is the primary point of this section: routine key issuance stops requiring an admin ticket, matching the self-service model of comparable gateway tools (e.g., LiteLLM's "virtual keys").
- Every authenticated user (Member, Team Lead, or Org Admin) may create, name, regenerate, and revoke API keys **attributed to themselves**, without any elevated role — this is additive to, not a replacement for, Phase 1.2's admin-minted service-account keys (those remain how a non-human app identity gets a credential; this is how a human gets one for their own direct use, e.g., a CLI tool).
- A user may hold **multiple** named keys at once (e.g., `claude-cli`, `local-dev`), each independently revocable — mirrors how people actually use personal keys across several tools, rather than forcing one shared secret. **Resolved (cap on personal keys):** yes, a default org-wide soft cap of **10 personal keys per user**, configurable (raise or remove) by an Org Admin. Unlimited-with-visibility was considered and rejected as the default: self-serve creation with no cap at all risks exactly the credential sprawl that undermines the point of being able to audit/revoke keys per person — 10 is generous enough to cover realistic multi-tool, multi-machine use without needing to think about it, while still bounding the problem for an admin's oversight view.
- A Team Lead may additionally create/revoke keys **attributed to any member of their own team** (delegated, consistent with 2.4's existing Team Lead powers) — not for users outside their team. Org Admin retains the ability to create/revoke a key attributed to anyone, unchanged from Phase 1.
- **No policy bypass**: a self-serve key is bound by exactly the same budget, model-policy, and (once Phase 3 ships) access-schedule precedence that already applies to the human it's attributed to — creating your own key must never grant access broader than what an admin-minted key for the same person would have.
- Optional per-key **expiration**, chosen by the creator at creation time (no expiration / 30 / 90 / 180 days / custom date); the org can set an org-wide **maximum allowed expiration** so a user can't mint a key that outlives whatever rotation posture the org wants (ties into, but is independent of, Phase 3.7's admin-driven rotation policy for admin-managed keys).
- Regeneration is an explicit, self-initiated action (the same person choosing to invalidate their own current secret and get a new one) — unlike Phase 3.7's scheduled/admin-forced rotation, this does **not** need a dual-active grace period; the old key can be revoked immediately since the person revoking it is the same person who'll update their own tooling.
- Optional org-wide toggle: **auto-provision one default personal key on first login**, so a brand-new user has something usable immediately without a manual "create key" step. Default **off** at the org level (consistent with this doc's general bias toward opt-in for anything that changes existing behavior) — an Org Admin turns it on if they want zero-friction onboarding.
- All self-serve key lifecycle events (create, regenerate, revoke — by self or by a delegating Team Lead/Org Admin) write to the audit trail (2.4), identical treatment to admin-minted keys.

### 2.6 Self-Service Onboarding & Approval Workflow
- Applies to any first-time SSO login that doesn't already arrive with team membership resolved by SCIM (2.1) — if SCIM has already assigned the user to a team, this flow is skipped entirely; this is the path for orgs without SCIM, or for any user SCIM didn't map to a team.
- On first successful SSO login, before any gateway access is granted, the user is prompted to complete a short profile: **full name** (pre-filled from the IdP claim if available, editable) and **team** (a single-select dropdown sourced from the org's existing team list — the same list an Org Admin manages in Teams & Users; the user cannot create a new team from this dropdown, only choose an existing one).
- Submitting the profile creates a **pending join request**, not team membership — the user has no gateway access yet. The request routes to whoever holds the **Team Lead** role on the selected team. **Resolved (approver = Team Lead vs. a distinct manager role):** the Team Lead role is the approver — no separate manager/approver identity. Introducing a second role that overlaps with Team Lead's existing responsibilities would be designing for a hypothetical need before any design partner has asked for it; if one later does (a people-manager who approves headcount but doesn't otherwise run the team), that's an additive RBAC change on top of this, not a redesign of it.
- If a team has more than one Team Lead, the request is visible to and actionable by any of them (first action wins — the queue reflects it as resolved, not still-pending, for the others). If a team has **no** Team Lead assigned yet, the request routes to Org Admin as a fallback so it never gets stuck with no possible approver.
- **Resolved (stale requests):** a pending request with no Team Lead action within **5 business days** auto-escalates to also appear in the Org Admin's queue (companion admin doc's fallback view) — visibility only, not reassignment; the original Team Lead can still act on it. This exists purely so a request can't go silently stuck forever; 5 business days is a starting default, adjustable if pilot usage shows it's the wrong number.
- **Resolved (multiple pending requests per user):** at most one pending request at a time — a new request cannot be submitted while one is still pending. This avoids the ambiguous state of a user having two simultaneous pending budget allocations across different teams. Once a request is resolved (approved or rejected), a new one can be submitted immediately — rejection already explicitly allows re-requesting a different team.
- The approver sees requester name, requested team, and timestamp, and can **Approve** or **Reject**.
- **Approving is a single combined action that also sets the new member's budget** — an approver cannot approve without providing a budget value; this is mandatory, not a follow-up step. The budget value is constrained exactly as 2.2 already requires: it cannot push the team's total allocated budget (existing members' allocations + this new allocation) past the team's org-set ceiling. The approval UI must show the team's current unallocated headroom live and reject/clamp an entry that exceeds it, same enforcement as 2.2's existing assignment-time check — this section doesn't introduce a new rule, it's the same rule applied at a new entry point.
- Rejecting a request does not create any team membership or budget allocation. A reason is optional but, if provided, is shown to the requester and stored in the audit trail. A rejected user can submit a new request (same or a different team) — rejection isn't a permanent lockout.
- Until approved, the user sees a holding state (no gateway access, no API key issuance possible — this precedes and gates Phase 2.5's self-service key creation, since there's no team/budget context yet for a key to inherit).
- Every step (request submitted, approved with amount, rejected with reason) writes to the audit trail (2.4), same treatment as every other budget/RBAC change in this phase.

## Out of Scope for Phase 2
- PII/DLP scanning
- Data residency routing
- Immutable/tamper-evident audit ledger (Phase 3)
- Caching, failover, graceful cost degradation (Phase 4)
- Budget marketplace/bidding between teams (Phase 6 — this phase is reassignment within a team only)

## Non-Functional Requirements
- Budget enforcement must be consistent under concurrent requests (no race condition allowing a team to spend past its ceiling under high parallel load) — this needs atomic spend-check-and-deduct, not eventual consistency.
- RBAC checks add negligible latency (target: under 10ms added per request for policy resolution).

## Success Criteria
- A pilot org with 2+ teams can run entirely through Team Leads for day-to-day budget/access changes without filing a ticket to the platform's core admin.
- Budget rollover between team members is exercised by at least one real Team Lead in the pilot and matches expected ceiling behavior (no team exceeds its allocated total).
- SSO login works against at least one real identity provider used by a pilot org (not just a test IdP).
- A pilot user logs in via SSO, self-serve-generates a personal key with zero admin involvement, and successfully makes a request through the gateway with it — validating that 2.5 actually removes the admin bottleneck it's meant to remove.
- A new pilot hire's full path — first SSO login → profile + team selection → Team Lead approval with a budget allocation → working gateway access — is completed without an Org Admin touching it, and a team's allocated total never exceeds its ceiling even when exercised concurrently by multiple pending requests.

## Open Questions to Resolve Before Building
All questions originally listed here are now resolved inline above (2.1, 2.2, 2.5, 2.6) — see those sections for each decision and its rationale. One item remains genuinely open because it depends on facts only real design partners can supply, not a product-design call this doc can make on its own:

- **For 2.5, still open:** Gatekey's cross-phase non-negotiable is an *OpenAI-compatible* API surface (`00-overview.md`) — that commitment isn't being revisited here. What's still unknown is whether the CLI tools actual pilot users want to point at Gatekey all work against that surface, or whether some (e.g., Anthropic's own Claude Code CLI, which natively speaks the Messages API rather than OpenAI's chat/completions shape) need something more. Default position until real usage data says otherwise: **don't build a second wire-protocol passthrough speculatively** — ship against the committed OpenAI-compatible surface, and treat "CLI X doesn't work out of the box" as a signal to evaluate a dedicated passthrough route only once a specific design partner actually hits it, not before.
