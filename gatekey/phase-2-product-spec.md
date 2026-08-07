---
title: Phase 2 — Multi-Tenant Governance — Buildable Product Spec
status: draft
last_updated: 2026-07-29
source_docs:
  - phase-2-multi-tenant-governance.md
  - ui-requirements-admin.md (§8, §9, §14)
  - ui-requirements-non-admin.md (entire doc)
  - 00-overview.md
author: product-owner (sub-agent)
consumed_by: architect
---

# Phase 2 — Multi-Tenant Governance — Buildable Spec

This translates `phase-2-multi-tenant-governance.md` §2.1–§2.6 into user
stories and testable acceptance criteria. All "Open Questions" in the
source phase file are already resolved inline there (2.1, 2.2, 2.5, 2.6);
this doc does not re-litigate them, only operationalizes them. It also
folds in four locked orchestrator architecture decisions (auth model, RBAC
data model, SSO/OIDC, threshold-alert notifiers) as non-negotiable scope,
and the two companion UI docs as the authoritative UI spec.

## 0. Non-Negotiable Architecture Decisions (carried in, not re-decided here)

1. **Auth model.** `GATEKEY_ADMIN_TOKEN` (Phase 1 shared bearer) becomes a
   permanent, documented break-glass/bootstrap credential — keeps full Org
   Admin rights indefinitely, does not get deprecated by SSO. New primary
   path: SSO (OIDC) → session for a real `User` row carrying role info.
2. **RBAC data model.** Extend existing `User` table (no parallel identity
   table) with `org_role` (nullable: `org_admin`|`auditor`) and a
   unique-indexed SSO identity-claim column. New tables: `Team`,
   `TeamMembership` (per-user-per-team budget, `NUMERIC(20,10)` matching
   `User.budget_usd`), `JoinRequest`, `PersonalApiKey` (separate table from
   `ServiceAccountKey`). `ServiceAccountKey` gets a nullable `team_id`;
   legacy (pre-Phase-2) rows keep resolving against the owning User's flat
   budget; new keys should require `team_id` going forward — enforcement
   layer (schema constraint vs. service-layer validation) is the
   architect's call.
3. **SSO/OIDC.** Standard authorization-code flow, provider-agnostic,
   config-driven via env vars following the exact `.env.example` pattern
   already used for `GATEKEY_ADMIN_TOKEN`/`GATEKEY_MASTER_KEY`. Keycloak
   ships as an optional `docker-compose --profile sso` service (never
   starts on plain `docker-compose up` — must not regress Phase 1's
   under-60-minute setup promise). Session = server-side `Session` table +
   httpOnly cookie holding an opaque, server-revocable token.
4. **Threshold alerts.** Pluggable notifier interface. Webhook (generic +
   Slack-compatible payload) is the primary testable path (mock receiver in
   tests + live verification). Email is wired via SMTP env-var config but
   flagged **unverified-live** — same treatment as Phase 1's
   pricing-needs-live-verification gap; do not claim it's been verified
   against a real mailbox.

---

## 1. §2.1 Identity & Hierarchy

**User stories**

- As an Org Admin, I can create/edit/delete teams, so the org has a
  structure for team-scoped budgets and policy.
- As an Org Admin, I can assign any user's `org_role` (Org Admin, Auditor)
  or their `TeamMembership.role` (Team Lead, Member) for any team, so RBAC
  reflects real org structure.
- As any user, I log in via SSO (OIDC) instead of a shared credential, so
  my identity and role are individually attributable.
- As a Team Lead, I can manage membership/role/budget for my own team only.
- As a Member, I have no admin surface at all.
- As an Auditor, I can view usage/logs/policy state org-wide but cannot
  mutate anything.

**Acceptance criteria**

- AC1.1 — Four roles exist and are enforced server-side on every
  privileged endpoint, not just hidden in the UI: `org_admin`, `team_lead`
  (per-team, via `TeamMembership.role`), `member`, `auditor` (`org_role`).
- AC1.2 — A user can hold `TeamMembership` rows in more than one team
  simultaneously (multi-team membership is supported, not a v-next item).
- AC1.3 — SSO login (OIDC authorization-code flow) works against any
  spec-compliant provider; Keycloak is the test/dev provider, gated behind
  `docker-compose --profile sso`; `docker-compose up` alone still succeeds
  and does not start Keycloak.
- AC1.4 — `GATEKEY_ADMIN_TOKEN` continues to authenticate as a full Org
  Admin after this phase ships — regression-tested explicitly, since it's
  now a secondary/break-glass path rather than the only path.
- AC1.5 — Only an Org Admin can assign the `org_admin` or `auditor` role;
  assigning `team_lead` requires selecting the specific team (matches
  `ui-requirements-admin.md` §8 Add/Edit member modal). A Team Lead cannot
  elevate themselves or anyone else to Org Admin/Auditor, nor grant
  Team Lead on a team they don't already administer.
- AC1.6 — SCIM is explicitly **not** built this phase (per the phase doc's
  own resolution) — no SCIM endpoints, no auto-deprovisioning. This is not
  an oversight to flag; it is the documented Phase 3-or-later item.
- AC1.7 — RBAC/policy-resolution overhead per request stays under the
  stated NFR (<10ms added latency) — needs a load-test acceptance check,
  not just unit coverage.

**Deferred / explicitly out of scope for this section**

- SCIM auto-provisioning/deprovisioning (phase doc's own resolution —
  slips to Phase 3 or later).
- A distinct "manager/approver" role separate from Team Lead (phase doc
  §2.6 resolution — Team Lead is the approver, full stop).

---

## 2. §2.2 Team & Budget Management

**User stories**

- As an Org Admin, I set a budget ceiling per team and a normalization
  currency for the org.
- As a Team Lead, I reassign budget between my team's members without
  exceeding the team's ceiling.
- As an Org Admin or Team Lead, I configure a team's period boundary
  (monthly/quarterly) and its end-of-period policy (rollover or reset).
- As a Team Lead/Org Admin, I receive a webhook (and, best-effort, email)
  alert when a team crosses 80%/100% of its budget.
- As an Org Admin, I can see how a raw provider cost was normalized into
  the org's budget currency for any given request.

**Acceptance criteria**

- AC2.1 — Budget is settable at three levels: org (ceiling), team
  (ceiling, ≤ org ceiling — not explicitly stated as enforced in the phase
  doc but implied by "nested" governance; see Ambiguity A3 below for the
  one genuinely open piece here), and `(user, team)` pair via
  `TeamMembership.budget_usd`.
- AC2.2 — **Assignment-time enforcement**: creating/editing a
  `TeamMembership.budget_usd` (directly, or via join-request approval, §2.6)
  is rejected/clamped if `sum(team's existing member allocations) + new
  amount > team.budget_ceiling`. This check happens at write time, not
  just reflected later in a spend report.
- AC2.3 — **Spend-time enforcement is atomic and concurrency-safe**: two
  simultaneous requests against the same team/user cannot jointly exceed
  the applicable budget (matches NFR: "atomic spend-check-and-deduct, not
  eventual consistency"). Acceptance test: N concurrent requests fired
  against a budget with headroom for fewer than N must reject the excess
  ones deterministically, verified under real concurrent load (not just a
  single-threaded test).
- AC2.4 — **Budget reassignment** (Team Lead moves unused budget between
  two of their team's members) never lets the team's allocated total
  exceed its ceiling; produces one audit trail entry recording both
  members' old→new values (per `ui-requirements-admin.md` §8's reassignment
  flow).
- AC2.5 — **Period boundary & rollover/reset**: each team has a configurable
  period (monthly/quarterly) and an explicit `on_period_end` setting
  (`rollover`|`reset`), **defaulting to `reset`** per the phase doc's
  resolved default. At period rollover, unused `TeamMembership.budget_usd`
  either carries into the new period (rollover) or zeroes out (reset),
  per team config — a Team Lead must explicitly opt into rollover.
- AC2.6 — **Threshold alerts**: configurable per team at 80%/100% (at
  minimum), delivered via the pluggable notifier interface. Webhook
  delivery (generic + Slack-compatible payload) is verified end-to-end in
  tests against a mock receiver plus one live webhook target. Email
  delivery is implemented and config-driven (SMTP host/port/creds as env
  vars, matching the `.env.example` pattern) but **flagged
  unverified-live** — no real SMTP credentials available in this build
  environment, same caveat class as Phase 1's pricing-needs-live-verification
  gap. QA must not mark email alerting "verified" without a real mailbox
  test.
- AC2.7 — **Cost normalization audit**: for any given request/cost record,
  an admin can see the raw provider cost, the conversion applied, and the
  resulting normalized amount in the org's currency — this needs a visible
  field/detail view (e.g., request log row expansion), not just an internal
  computation. No dedicated screen for this exists in the UI docs beyond
  the org Dashboard totals; recommend surfacing it in the existing
  per-request log detail (Phase 1 schema) rather than inventing a new
  screen — flagged to architect/frontend as an implementation note, not a
  scope gap (see Ambiguity A5).

**Deferred / explicitly out of scope for this section**

- Budget marketplace / cross-team bidding — reassignment is intra-team
  only in this phase (Phase 6 territory, called out explicitly in the
  phase doc's Out-of-Scope list).
- Caching/failover/graceful degradation — Phase 4.

---

## 3. §2.3 Nested Model Policy

**User stories**

- As a Team Lead, I can further restrict my team's allowed models beyond
  the org baseline, but never re-enable a model the Org Admin banned.
- As any user, I can see exactly which policy layer (org vs. team) blocked
  a given model, in plain language.

**Acceptance criteria**

- AC3.1 — Team model restriction UI/API only ever offers a subset of the
  currently-allowed org baseline — a model already denied at org level is
  not shown as a selectable (even disabled) option, matching
  `ui-requirements-admin.md` §8's explicit "checkboxes for org-banned
  models are simply absent, not shown-disabled" rule.
- AC3.2 — Attempting to set a team restriction that would re-enable an
  org-banned model is rejected server-side (defense in depth — this must
  not rely on the UI alone to prevent it).
- AC3.3 — Policy resolution for "is model X available to user Y" is
  deterministic and returns which layer decided the outcome (org baseline
  / team restriction). Non-admin `Model Access` screen
  (`ui-requirements-non-admin.md` §5) renders this per-model, per-layer,
  in plain language — not a generic "blocked by policy" string.
- AC3.4 — Content-classification-aware routing (the third policy layer
  referenced in the UI docs' precedence trace, e.g. "flagged for
  PII-sensitive content only") is **Phase 3/5 scope, not Phase 2** — Phase
  2's policy trace only ever has two layers (org, team). Build the trace
  component extensibly (it's shared with Phase 3/5) but do not implement
  content-classification logic now.

**Deferred / explicitly out of scope for this section**

- Content-classification / DLP-driven dynamic model routing (Phase 3/5).

---

## 4. §2.4 Delegated Admin Console

**User stories**

- As a Team Lead, I have a self-service console scoped to my own team:
  members, budget reassignment, model restrictions, usage.
- As an Org Admin, my Phase 1 console extends to manage teams, org-wide
  RBAC, and per-team budget ceilings.
- As an Org Admin or Auditor, I can see a lightweight audit trail of every
  budget reassignment and policy change: who, what, when, old→new.

**Acceptance criteria**

- AC4.1 — Team Lead nav/screens are strictly scoped to their own team(s) —
  server-side authorization enforced per team, not just UI-hidden (a Team
  Lead API call referencing another team's ID must be rejected).
- AC4.2 — Audit trail is a **plain, append-only table** (not the Phase 3
  hash-chained ledger) capturing at minimum: actor (user id or the
  break-glass-token sentinel identity — see Ambiguity A4), action type,
  target, old value, new value, timestamp. Every mutation this phase
  introduces writes an entry: team CRUD, RBAC role changes, budget
  ceiling/assignment/reassignment, team model-restriction changes,
  join-request lifecycle (submit/approve/reject), personal-key lifecycle
  (create/regenerate/revoke, self or delegated).
- AC4.3 — Audit Log screen (`ui-requirements-admin.md` §10.3, Phase 2 slice
  only — no hash-chain badge, no "Verify now," that's Phase 5) is filterable
  by action type and actor and readable by Org Admin and Auditor.
- AC4.4 — Auditor role can view the audit trail, usage, and policy state
  org-wide, read-only, with zero mutating endpoints reachable.

**Deferred / explicitly out of scope for this section**

- Hash-chained/tamper-evident audit ledger (Phase 3/5 per the phase doc's
  own Out-of-Scope list) — this phase's audit trail is a plain table with
  no cryptographic verification.

---

## 5. §2.5 Self-Service API Keys

**User stories**

- As any authenticated user (Member/Team Lead/Org Admin), I can create,
  name, regenerate, and revoke my own personal API keys without needing an
  admin.
- As a user, I can hold multiple named personal keys at once, each
  independently revocable.
- As a Team Lead, I can additionally create/regenerate/revoke a key on
  behalf of any member of my own team (not outside it).
- As an Org Admin, I retain the ability to create/revoke a key attributed
  to anyone.
- As a user, I choose an optional expiration for my key at creation,
  bounded by the org's configured maximum.
- As a user, regenerating my own key is immediate — no grace/overlap
  period, since I'm the one updating my own tooling.
- As an Org Admin, I can optionally enable auto-provisioning one default
  personal key on a new user's first working login.

**Acceptance criteria**

- AC5.1 — `PersonalApiKey` is a distinct table from `ServiceAccountKey`
  (per architecture decision #2) — not a repurposed/overloaded row type.
- AC5.2 — A user can create a personal key naming it, with optional
  expiration (`no expiration` / 30 / 90 / 180 days / custom date), clamped
  to the org-wide `max_self_serve_expiration_days` setting — options
  exceeding the org max are not offered, not silently overridden (per
  `ui-requirements-non-admin.md` §6).
- AC5.3 — Default org-wide soft cap of **10** personal keys per user,
  configurable (raise/remove) by an Org Admin org-wide. Creating an 11th
  key with the default cap in force is rejected with a clear structured
  error, not a silent failure.
- AC5.4 — A personal key is bound by exactly the same budget/model-policy
  precedence as its owning human — creating a key never grants broader
  access than an admin-minted key for the same person would have. Test:
  a key created by a Member is rejected at the same policy layer their
  human identity would be.
- AC5.5 — Every personal key **declares a team context at creation time**
  (per architecture decision #2 — budget/policy resolution must always
  resolve against the key's declared team, never inferred from the user's
  team list). See Ambiguity A1 below — the current UI wireframe for "Create
  key" (`ui-requirements-non-admin.md` §6) does **not** show a team
  selector; this must be added for any user with more than one active
  `TeamMembership`, or the multi-team budget model this phase promises to
  enforce cannot actually be satisfied.
- AC5.6 — Regeneration is immediate, self-initiated, no dual-active grace
  period (unlike Phase 3.7's scheduled admin rotation) — old secret is
  invalid the instant regeneration completes.
- AC5.7 — Revocation is immediate and irreversible; revoked keys reject
  all subsequent requests with a clear structured error.
- AC5.8 — A Team Lead can create/regenerate/revoke a personal key
  attributed to any member of their own team only — server-side check
  against `TeamMembership`, not just UI restriction.
- AC5.9 — Auto-provision-on-first-login toggle defaults **off** at the org
  level; when on, a new user gets exactly one default personal key
  automatically upon reaching working gateway access (see Ambiguity A2 —
  "first login" needs precise definition given §2.6's approval gate).
- AC5.10 — Every personal-key lifecycle event (create/regenerate/revoke,
  self or delegated) writes an audit trail entry (§2.4), identical
  treatment to admin-minted `ServiceAccountKey` events.
- AC5.11 — Org Admin retains unrestricted create/revoke over any key in
  the org (personal or app), with a stronger confirmation when
  regenerating someone else's personal key (per
  `ui-requirements-admin.md` §9: "This regenerates {name}'s own key —
  they'll need to update anything using it themselves").

**Deferred / explicitly out of scope for this section**

- Scheduled/automatic key rotation, access-schedule windows, CLI Auto-Sync
  local helper — all explicitly tagged `[P3]`/`[P3.7a]` in the UI docs.
  Phase 2 ships plain create/name/expire/regenerate/revoke only; do not
  build rotation policy, schedule enforcement, or the sync helper now,
  even though the UI docs show them on the same screens (those docs
  describe the merged end-state across all phases, not Phase-2-only
  scope — see UI docs' own framing note in their headers).
- A second (non-OpenAI-compatible) wire protocol/passthrough for CLIs like
  Claude Code — phase doc's own resolution: ship against the committed
  OpenAI-compatible surface only; revisit only if a real design partner
  hits a concrete gap.

---

## 6. §2.6 Self-Service Onboarding & Approval Workflow

**User stories**

- As a new user without SCIM-resolved team membership, on first SSO login
  I complete a short profile (name + team) before getting any gateway
  access.
- As a new user, submitting my profile creates a pending join request; I
  see a holding state until it's resolved.
- As a Team Lead, I see pending join requests for my team and can approve
  (with mandatory budget) or reject (optional reason).
- As an Org Admin, I see and can act on requests for teams with no Team
  Lead assigned, and I get visibility (not reassignment) on any request
  stale beyond 5 business days.
- As a rejected requester, I can immediately submit a new request (same or
  different team).

**Acceptance criteria**

- AC6.1 — This flow triggers only when SCIM has **not** already resolved
  team membership (today: always, since SCIM isn't built this phase — see
  §1 AC1.6 — so in practice this flow applies to every first-time SSO
  login in Phase 2).
- AC6.2 — Profile form: full name (pre-filled from IdP claim, editable),
  team (single-select from existing org teams only — no team creation from
  this dropdown). Role is never chosen here — every self-service request
  defaults to `Member`.
- AC6.3 — Submitting creates exactly one `JoinRequest` (status `pending`)
  and grants **zero** gateway access — no team membership, no budget, no
  personal-key issuance possible until resolved.
- AC6.4 — **One pending request per user at a time**, enforced server-side:
  submitting while a `pending` request exists is rejected. A resolved
  (approved or rejected) request unblocks a new submission immediately.
- AC6.5 — **Routing**: request routes to any Team Lead of the selected
  team (first action by any of them resolves it; it disappears from other
  Team Leads' queues once acted on). If the team has zero Team Leads, it
  routes to the Org Admin queue instead (fallback, per
  `ui-requirements-admin.md` §8's "Join Requests needing your action"
  panel).
- AC6.6 — **Stale-request escalation**: a request with no Team Lead action
  within 5 business days also appears in the Org Admin's queue as
  visibility-only (Org Admin can still act on it directly, since the UI's
  fallback queue reuses the same approve/reject component) — the original
  Team Lead retains the ability to act too; this is not a reassignment.
- AC6.7 — **Approval is atomic with budget allocation**: an approver
  cannot approve without entering a budget amount; the UI/API rejects
  submission without one. The amount is clamped to the team's live
  unallocated headroom (`ceiling − sum(existing allocations)`), same rule
  as §2.2 AC2.2 applied at this entry point — approving creates the
  `TeamMembership` row and its budget in one atomic transaction (no
  intermediate "approved but unbudgeted" state).
- AC6.8 — **Rejection**: creates no membership/budget; optional reason,
  shown to requester if provided, stored in audit trail regardless.
  Requester can immediately resubmit.
- AC6.9 — Every step (submit, approve+amount, reject+reason) writes an
  audit trail entry (§2.4).
- AC6.10 — Holding-state screen (`ui-requirements-non-admin.md` §2.2)
  correctly substitutes "awaiting approval from your org admin" copy when
  the team has no Team Lead (routing fallback), rather than naming a role
  nobody holds.

**Deferred / explicitly out of scope for this section**

- SCIM-driven auto-assignment (this whole section is explicitly the
  non-SCIM path; SCIM itself is Phase 3+).
- A distinct manager/approver role — Team Lead is the approver, full stop
  (phase doc's own resolution).

---

## 7. Explicit Scope Boundary Summary

**In scope for Phase 2 (build now):**
- Org→Team→User hierarchy, 4 roles, SSO/OIDC login, break-glass admin
  token preserved.
- Three-level budgets (org/team/user-in-team), atomic concurrent-safe
  enforcement, rollover-or-reset at period boundary (default reset),
  assignment-time ceiling enforcement, threshold alerts (webhook verified,
  email unverified-live).
- Nested (narrowing-only) team model policy + two-layer precedence trace.
- Delegated Team Lead console + Org Admin extensions + plain (non-hash-chained)
  audit trail.
- Self-service personal API keys: create/name/expire/regenerate/revoke,
  10-key soft cap, org-wide max expiration, delegated Team Lead management
  within their team, optional auto-provision toggle (off by default).
- Full onboarding/approval workflow: profile+team → pending request →
  Team Lead/Org-Admin-fallback approval with mandatory budget → audit
  trail, 5-business-day stale escalation, one-pending-request rule.

**Explicitly deferred / out of scope (do not build, even where the UI docs
show the control on a shared screen because those docs describe the
full-roadmap end state):**
- SCIM auto-provisioning/deprovisioning (phase doc's own resolution;
  targets Phase 3+).
- PII/DLP scanning, content-classification-driven model routing.
- Data residency routing.
- Immutable/hash-chained/tamper-evident audit ledger (Phase 3/5) — Phase 2
  ships a plain audit table only.
- Caching, failover, graceful cost degradation (Phase 4).
- Budget marketplace/cross-team bidding (Phase 6) — reassignment stays
  intra-team only.
- Scheduled/automatic key & provider-key rotation, access-schedule
  windows/holiday calendars, emergency-override grants, CLI Auto-Sync
  local helper (all Phase 3, tagged `[P3]`/`[P3.7a]` in the UI docs).
- A second (non-OpenAI-compatible) wire-protocol passthrough for CLIs
  (phase doc's own resolution — revisit only on real design-partner need).
- A distinct manager/approver role separate from Team Lead.
- Any Phase 5/6 differentiator or marketplace feature appearing in the
  admin UI doc's later sections.

---

## 8. Data Model Touchpoints (for architect — not a schema design, a checklist)

- `User`: add `org_role` (nullable enum: `org_admin`|`auditor`), unique
  SSO identity-claim column. `budget_usd` (Phase 1 flat field) remains for
  legacy/unmigrated resolution paths only — see Ambiguity A6 on precedence
  once a user also has `TeamMembership` rows.
- `Team`: ceiling, period type, `on_period_end` (`rollover`|`reset`,
  default `reset`), model-restriction list, alert-threshold config,
  notifier config (webhook URL + email toggle).
- `TeamMembership`: `(user_id, team_id)` unique-ish (a user can have at
  most one active membership per team, but many teams), `role`
  (`team_lead`|`member` — org_admin/auditor live on `User.org_role`
  instead), `budget_usd NUMERIC(20,10)`, spend tracking.
- `JoinRequest`: requester, team, status, timestamps, resolved_by,
  approved_budget_usd, rejection_reason, `routed_to`
  (`team_lead`|`org_admin`).
- `PersonalApiKey`: separate from `ServiceAccountKey`; owner_user_id,
  created_by_user_id (differs when delegated), **team_id (non-null —
  every personal key must declare its team context, per AC5.5)**,
  expires_at, revoked_at.
- `ServiceAccountKey`: add nullable `team_id`; legacy rows (team_id null)
  resolve against owner's flat `User.budget_usd`; architect decides
  schema-vs-service-layer enforcement of "new keys require team_id."
- `Session`: server-side table, opaque token, httpOnly cookie, revocable.
- `AuditEntry` (plain table, per §2.4/AC4.2): actor, action, target,
  old_value, new_value, timestamp — no hash-chain columns this phase
  (those are Phase 5 additions on top of this same table, per
  `ui-requirements-admin.md` §10.3's "Phase 3 ships this as a plain
  append-only log... Phase 5 adds the chain-integrity badge").

---

## 9. Flagged Ambiguities (genuinely open — not re-litigating resolved items)

The phase doc's own open questions are all resolved inline (2.1, 2.2, 2.5,
2.6) and used as-is per the orchestrator's instruction. The following are
gaps I found only by cross-referencing the phase doc against the UI docs
and the architecture decisions — they were not called out as resolved
anywhere, and building against a guess risks rework:

- **A1 — Personal-key team-context selector is missing from the UI spec.**
  Architecture decision #2 mandates every personal key declare a team
  context at creation (required for correct budget resolution under
  multi-team membership). But `ui-requirements-non-admin.md` §6's "Create
  key" Step 1 form shows only Name and Expires fields — no team picker.
  For a user in exactly one team this is a non-issue (auto-select), but
  for a user in 2+ teams there is currently no specified UI control to
  choose. **Recommend:** auto-select silently when the user has exactly
  one active `TeamMembership`; require an explicit selector when they have
  more than one. Flagging to architect/frontend rather than guessing at
  final UI copy/placement.

- **A2 — "Auto-provision on first login" (§2.5) appears to conflict with
  §2.6's access gate.** §2.5 says the org can auto-provision a personal
  key "on first login." §2.6 says a user has *zero* access, including no
  key issuance, until their join request is approved. Read together, the
  only consistent interpretation is that "first login" for the
  auto-provision feature means "first login that results in resolved
  gateway access" — i.e., immediately for SCIM-provisioned users (not
  built this phase, so moot for now), or at the moment of join-request
  approval for everyone else. **Recommend building it this way** (trigger
  auto-provision at approval time, not literal SSO callback time) since
  it's the only reading that doesn't contradict §2.6 — flagging so the
  architect/QA don't independently reach a different reading.

- **A3 — Is there an explicit rule that a team's ceiling itself cannot
  exceed the org-wide ceiling, and what happens if an Org Admin lowers a
  team's ceiling below its current allocated total?** The phase doc is
  explicit about *allocations within a team* never exceeding that team's
  ceiling (§2.2), and the admin UI shows an org-wide ceiling with
  "unallocated" tracking across teams, implying org-level enforcement
  too — but the phase doc never states the rule in those words, and never
  addresses a ceiling *reduction* below current allocation. **Recommend:**
  (a) enforce org ceiling ≥ sum of team ceilings the same way team ceiling
  ≥ sum of member allocations is enforced (consistent with the doc's
  general nested-governance pattern), and (b) block a ceiling reduction
  that would put a team retroactively over-allocated, with the same
  specific-inline-reason pattern used elsewhere in this doc, rather than
  silently leaving a team over its own ceiling. Flagging for architect
  sign-off before building, since NFR correctness (§2.2) depends on this
  being unambiguous.

- **A4 — Audit-actor identity for break-glass-token-driven actions.** The
  audit trail (§2.4) records "who" for every change. Actions taken via
  `GATEKEY_ADMIN_TOKEN` have no `User` row/session behind them.
  **Recommend:** log a fixed sentinel actor value (e.g.,
  `system:admin_token`) distinct from any real user, so audit review can
  tell break-glass activity apart from a named Org Admin's SSO session.
  Flagging for confirmation, not blocking — low risk either way but should
  be a deliberate choice, not an accident of whatever the auth middleware
  happens to return.

- **A5 — No dedicated UI surface for per-request cost-normalization detail
  (§2.2's "auditable" requirement).** Recommend exposing raw cost +
  conversion rate + normalized cost on the existing per-request log detail
  view (Phase 1 schema) rather than a new screen — implementation note,
  not a scope gap, but the UI docs don't currently show this field
  anywhere, so calling it out so it isn't dropped.

- **A6 — Precedence between legacy `User.budget_usd` and
  `TeamMembership.budget_usd` once a Phase-1 user is migrated into a
  team.** Architecture decision #2 is explicit that `ServiceAccountKey`
  legacy rows keep resolving against the owner's flat budget until
  migrated. It does not say what happens to that same user's flat budget
  the moment they *also* pick up a `TeamMembership` (e.g., via onboarding,
  or an Org Admin adding a pre-existing Phase 1 user to a new team).
  **Recommend:** once a user has at least one `TeamMembership`, all
  *new* personal keys and any team-attributed `ServiceAccountKey` resolve
  against that specific `TeamMembership.budget_usd`, never
  `User.budget_usd` — the flat field becomes read-only legacy state,
  relevant only to pre-existing, not-yet-migrated `ServiceAccountKey` rows
  with `team_id = null`. This needs to be an explicit, written rule in the
  architect's design doc, not left implicit, since it directly affects
  budget-enforcement correctness (the phase's top NFR).

- **A7 (minor, not blocking) — "5 business days" for stale-request
  escalation has no defined calendar.** Phase 3 introduces an org holiday
  calendar; Phase 2 has none. **Recommend:** Phase 2 computes "business
  day" as Monday–Friday in the org's configured timezone, with no
  holiday-awareness (that refinement arrives naturally once Phase 3's
  holiday calendar exists). Low-risk default, flagging only so it's a
  documented choice rather than an implicit one.

- **A8 (verification-scope note, not a build gap) — Success criteria asks
  for SSO against "at least one real identity provider used by a pilot
  org," parallel to the email-notifier caveat in architecture decision #4.**
  Keycloak (dev/test, `--profile sso`) satisfies "any spec-compliant OIDC
  provider" for automated testing, but is not itself "a real pilot IdP"
  (Okta/Azure AD/Google Workspace). Recommend treating this exactly like
  the email-alerting gap: implemented and spec-compliant, but
  **unverified-live** without a real design-partner IdP in this
  environment. Flagging so QA doesn't overstate this success criterion as
  met by Keycloak testing alone.
