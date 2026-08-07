---
title: Phase 3 — Security & Compliance Hardening — Buildable Product Spec
status: draft
last_updated: 2026-08-04
source_docs:
  - phase-3-security-compliance.md
  - ui-requirements-admin.md (§6, §7, §8, §9, §10, §14, §16)
  - ui-requirements-non-admin.md (§6, §6.1, §7.5, §8)
  - 00-overview.md
  - phase-2-multi-tenant-governance-design.md (§12, forward-looking rework flags)
  - phase-2-product-spec.md (precedent format; A4/A6/A7 decisions carried forward)
  - backend/src/gatekey/services/audit.py, db/models/audit_entry.py,
    api/v1/admin/audit_entries.py, db/models/service_account_key.py,
    db/models/personal_api_key.py, db/models/team.py,
    services/model_policy.py, api/v1/gateway/common.py (current-state grounding)
author: product-owner (sub-agent)
consumed_by: architect
---

# Phase 3 — Security & Compliance Hardening — Buildable Spec

This translates `phase-3-security-compliance.md` §3.1–§3.8 (including §3.7a)
into user stories and testable acceptance criteria. The source phase doc
states its own "Open Questions" section is fully resolved inline — this doc
does not re-litigate those resolutions (Presidio as the DLP library, hard-block
residency default, 30-day usage/prompt retention default, short rotation
overlap, rotation/schedule off-by-default, SCIM emergency-override rules).
It operationalizes each into buildable acceptance criteria and separately
flags the handful of implementation-detail gaps that surfaced only when
cross-referencing the phase doc against the UI docs and the current codebase
— these were never actually pinned down at the level a developer can build
against without guessing, so they're listed in §12, not silently decided.

**Scope framing carried through every section below:** §3.1 (audit) is a
**gap-closure**, not a rebuild — Phase 2 already shipped an append-only,
actor/action/target/old/new audit trail, queryable by org_admin/auditor.
Phase 3 adds exactly three things to it: source IP capture, CSV/JSON export,
and a separately-configurable retention/purge window. Do not redesign the
existing table or write path.

---

## 0. Non-Negotiable Architecture Decisions (carried in, not re-decided here)

1. **DLP library.** Presidio, self-hosted, in-process within the existing
   FastAPI backend (Python) — no external network call to a hosted PII
   service. This is the phase doc's own resolved decision (build-vs-integrate)
   and the overview's self-hosted-first/no-phone-home non-negotiable; not
   open for reconsideration.
2. **Audit trail is gap-closure, not a rebuild.** Extend the existing
   `AuditEntry` table/write path (`services/audit.py`,
   `db/models/audit_entry.py`) — add a `source_ip` column, a CSV/JSON export
   mode on the existing read endpoint, and a separately-configured retention
   window. Hash-chaining (`chain_hash`/`prev_hash`) remains explicitly
   Phase 5 scope; do not add those columns or any chain-verification logic
   now.
3. **`ModelAccessDecision.blocking_layer` extension.** Phase 2's design doc
   (§12) already documents this exact Phase 3 extension point: add the
   literal value `"content_classification"` to the existing
   `Literal["org", "team"] | None` type. Implement precisely that — do not
   redesign the type or the two-layer resolution function it currently wraps.
4. **Holiday-calendar extension, not a second calendar.** Phase 2's A7
   ("5 business days" = Mon–Fri, org timezone, no holiday-awareness) is
   extended — not replaced — by this phase's org-wide holiday calendar.
   Both the join-request stale-escalation computation (Phase 2) and this
   phase's schedule-window enforcement (§3.8) read from the same single
   holiday-calendar source.
5. **Nested/narrowing precedence is reused, not reinvented.** Every layered
   policy this phase introduces (residency rules org→team, access schedules
   org→team→key) uses the identical most-specific-wins,
   narrower-only-never-wider mechanism and server-side (not UI-only)
   enforcement already established for Phase 2's model policy (§2.3). Do not
   design a second precedence mechanism per feature.
6. **Rotation's short overlap and off-by-default posture are non-negotiable.**
   The phase doc is explicit and deliberate that the overlap buffer is
   minutes (not the pre-resolution 24–72h assumption) and that both rotation
   and scheduled-access-windows default to off org-wide. Do not loosen either
   back under implementation-convenience pressure — flag explicitly per the
   task's own instruction if either constraint turns out to be genuinely
   infeasible, do not silently widen them.
7. **CLI sync helper (§3.7a) placement/language is an open architecture
   decision, not pre-decided by this spec.** It is real, separate
   client-side engineering outside `backend/` and `frontend/` — see §8 and
   Ambiguity A11 for the recommendation and tradeoffs; the architect makes
   the final call.

---

## 1. §3.1 Audit Trail Gap-Closure

**User stories**

- As an Org Admin/Auditor, I can see the source IP on every audit entry, so
  I can attribute an action to a network origin during an investigation.
- As an Org Admin/Auditor, I can export the audit log (filtered or full) as
  CSV or JSON, so I can hand it to an external reviewer.
- As an Org Admin, I can configure audit log retention independently from
  usage/prompt data retention (§3.6), so legal-hold requirements on audit
  data don't force over- or under-retaining usage data.

**Acceptance criteria**

- AC1.1 — `AuditEntry` gains a nullable `source_ip` column via an additive
  migration (same "additive, don't reshape" pattern Phase 5's own
  `chain_hash`/`prev_hash` columns are documented to use). `write_audit_entry`
  captures the actor's source IP at write time from the same
  request-context resolution the auth/session layer already uses for
  every actor shape, including break-glass admin-token actions (a
  break-glass action still has a network origin worth recording).
- AC1.2 — IP capture is best-effort: if genuinely unavailable (e.g., an
  internal service call with no request context), `source_ip` is `NULL`
  rather than blocking the write — an audit entry must never fail to write
  because of a missing IP.
- AC1.3 — `GET /v1/admin/audit-entries` gains `?format=csv|json`, honoring
  the exact same filters already implemented (`action`, `actor`,
  `from`/`to`). No `format` param preserves today's paginated JSON response
  unchanged — no regression to the existing Phase 2 read path.
- AC1.4 — CSV/JSON export streams rather than fully buffering in memory —
  the audit table grows unboundedly by design, so an org with a long history
  must not be able to OOM the export endpoint.
- AC1.5 — Export is restricted to `org_admin`/`auditor`, identical to the
  existing read endpoint's role gate — no new role surface introduced.
- AC1.6 — A new, independent org setting `audit_retention_days` (separate
  column/table from `log_prompt_retention_days`, §3.6) drives a scheduled
  purge job that hard-deletes `AuditEntry` rows older than the configured
  window. **Default value is not numerically pinned by the phase doc**
  (it only says audit retention is "typically held longer" than the 30-day
  usage default) — see Ambiguity A1.
- AC1.7 — The purge job is a documented, deliberate exception to
  `AuditEntry`'s existing "never UPDATE, never DELETE" design note
  (`db/models/audit_entry.py` docstring) — it is the one sanctioned bulk
  delete, config-driven and scheduled, never exposed via a mutating API
  endpoint an admin/auditor can invoke directly. See Ambiguity A2 — this
  needs to be an explicit, written exception in the architect's design doc,
  not a silent contradiction of the existing docstring.

**Deferred / explicitly out of scope for this section**

- Hash-chained/tamper-evident ledger, chain verification tooling (Phase 5).
- Any change to the audit write path's transactional discipline
  (same-transaction flush-not-commit behavior stays exactly as Phase 2 built
  it) beyond adding the one column.

---

## 2. §3.2 PII / DLP Scanning

**User stories**

- As an Org Admin, I enable built-in PII scanning (SSN, credit card, email,
  phone) org-wide with a configurable default action (log / redact / block).
- As an Org Admin, I define custom regex patterns (name + regex + action)
  layered on top of Presidio's built-in detectors.
- As a Team Lead, I can override the DLP action for my own team without
  needing Org Admin involvement.
- As any user, when my prompt triggers "redact," my request still succeeds
  with flagged content redacted before the provider ever sees it, and the
  redaction event is visible in the request log.
- As any user, when my prompt triggers "block," I get a clear structured
  error and nothing reaches the provider.

**Acceptance criteria**

- AC2.1 — Presidio runs in-process, self-hosted, within the existing FastAPI
  backend — no external network call for PII detection (§0.1).
- AC2.2 — Built-in detectors (SSN, credit card, email, phone) are
  individually toggleable org-wide, matching the four checkboxes in
  `ui-requirements-admin.md` §10.1.
- AC2.3 — Custom patterns (`{name, regex, action}`) are org-level only for
  authoring (per the UI's Custom Patterns table living on the org-wide DLP
  tab, with no team-level pattern-authoring control anywhere in the UI
  spec) — layered on top of, not instead of, Presidio's built-in detectors.
  A team-level DLP override changes only the *action* applied to existing
  findings, never adds new patterns.
- AC2.4 — Two-level action precedence: org-wide default action, optional
  per-team override action (most-specific-wins) — this is a two-layer
  system, not the three-layer org→team→key pattern used elsewhere in this
  doc; do not build a per-key DLP override, it doesn't exist in the UI spec.
  Custom patterns carry their own independent per-pattern action regardless
  of the org/team default.
- AC2.5 — The three actions are defined precisely:
  - **log** — best-effort/async (AC2.6); a finding, if any, is recorded
    after the fact, no redaction applied, request never blocked on the scan.
  - **redact** — synchronous: the scan MUST complete and redaction MUST be
    applied before the request is forwarded to the provider — the provider
    never sees unredacted flagged content (phase doc's explicit hard
    requirement for this mode).
  - **block** — synchronous: scan completes before forwarding; on a finding,
    the request is rejected with a structured error (e.g., `dlp_blocked`)
    and never reaches the provider.
- AC2.6 — "Async/best-effort for log-only" is operationalized as a hard
  constraint: when the resolved action is `log`, the implementation MUST
  NOT hold the request open waiting on the Presidio scan — either the scan
  runs after the response has already started returning, or it runs
  concurrently with the provider call and only feeds the log, never the
  response path. A synchronous scan under `log`-only mode violates this AC
  and the NFR below.
- AC2.7 — Request-log entries for a scanned request record: whether a scan
  ran, which detector(s)/pattern(s) fired (by name/type), and the action
  taken. Whether the raw flagged substring itself is also stored is
  policy-dependent per the phase doc's own phrasing — see Ambiguity A3, no
  UI control for this exists yet.
- AC2.8 — Pipeline insertion point: the DLP scan is a new step in the
  existing gateway request sequence (`api/v1/gateway/common.py`'s
  `resolve_route → check_model_policy → ... ` chain), placed **after**
  `check_model_policy` (a request already denied by model policy shouldn't
  pay for a scan) and **before** `fetch_credential`/the provider call, for
  the `redact`/`block` synchronous paths. `log`-only may run concurrently
  with credential fetch/the provider call per AC2.6.
- AC2.9 — When a §3.4 content-aware routing rule is configured for a
  category, scanning for that category runs synchronously regardless of the
  org's default action being `log`-only — the routing decision in §3.4
  needs a completed scan result before model routing can finalize. This is
  a necessary consequence of §3.4 existing, not an independent policy
  choice; state it as a build requirement, not something admins configure
  separately.
- AC2.10 — NFR: DLP scanning (synchronous `redact`/`block` path) adds no
  more than ~50ms p99 latency for typical prompt sizes — needs a load-test
  acceptance check (not unit coverage alone), matching this doc's general
  posture toward latency NFRs.

**Deferred / explicitly out of scope for this section**

- Microsoft Purview / Google DLP as classification sources (shown as radio
  options on the Model Policy §7 "Content-Aware Routing" tab) — those are
  Phase 5 scope; Phase 3's DLP tab only ever produces Gatekey/Presidio
  findings.
- Inbound (response) scanning — see Ambiguity A4; the phase doc marks it
  "(optionally)" and no UI control for it exists in the reviewed admin doc.

---

## 3. §3.3 Data Residency Controls

**User stories**

- As an Org Admin, I define a residency rule scoped org-wide or to a
  specific team, restricting which provider regions/endpoints a request may
  reach.
- As an Org Admin, a newly created residency rule defaults to hard-block; I
  can explicitly opt a specific rule down to warn-only.
- As any user, a request that would violate an active hard-block residency
  rule fails with a clear structured error, never a silent reroute.

**Acceptance criteria**

- AC3.1 — `ResidencyRule: { scope: "org"|team_id, allowed_regions: string[],
  violation_behavior: "hard_block"|"warn" }` per `ui-requirements-admin.md`
  §16's shape.
- AC3.2 — Default `violation_behavior` on rule creation is `hard_block` —
  the create-rule path must not silently default to `warn`. Downgrading an
  existing rule to `warn` is a distinct, explicit save action, recorded in
  the audit trail with an action name that reads as "residency rule
  weakened" rather than a generic policy-update entry, so an auditor doesn't
  have to diff JSON to notice a compliance rule was loosened.
- AC3.3 — Precedence: a team-scoped rule can only **narrow** (restrict to a
  subset of) whatever an org-wide rule already allows, never widen it —
  inferred from this phase doc's consistent narrowing-only pattern applied
  to every other nested policy in this system (§0.5) and the UI's reuse of
  "the same team-picker component as elsewhere in this doc"; the phase doc's
  own §3.3 text doesn't spell this out in words. Flagged for confirmation,
  not blocking (see Ambiguity A12) — build it this way pending sign-off.
- AC3.4 — Which region a request "belongs to" is resolved from the target
  model/provider endpoint's own region metadata. **This is a real,
  unresolved data-model gap**: neither the reviewed provider/model registry
  code nor the UI docs show an existing region tag on standard cloud
  provider endpoints (only self-hosted providers carry a `base_url`, no
  region field). See Ambiguity A5 — residency enforcement cannot be built
  until this is resolved.
- AC3.5 — A blocked-by-residency request writes an audit trail entry (or at
  minimum a flagged request-log entry) — inferred from §3.8's explicit "the
  block itself is logged, not just successful requests" precedent applied
  consistently to this feature; the phase doc's §3.3 text doesn't state this
  explicitly for residency. Flagged for confirmation (Ambiguity A12,
  bundled with AC3.3).
- AC3.6 — Error shape is a structured, named error (e.g.,
  `residency_violation`), never a generic 403 or a silent reroute (phase
  doc's explicit stated intent behind choosing hard-block-by-default at
  all).

**Deferred / explicitly out of scope for this section**

- A dedicated residency-violations dashboard/report beyond the rules table
  itself (nothing in the UI spec beyond the Residency tab's rule list).

---

## 4. §3.4 Nested Model Policy — Content Awareness (initial version)

**User stories**

- As an Org Admin, I configure a simple rule: "if a request's prompt is
  DLP-flagged for PII, restrict routing to models flagged compliant for
  sensitive data."
- As any user, when this rule restricts my model choice, the existing
  policy-precedence trace names `content_classification` as the layer that
  did it — not a generic "blocked by policy" string.

**Acceptance criteria**

- AC4.1 — `ModelAccessDecision.blocking_layer` gains the literal value
  `"content_classification"`, exactly as Phase 2's design doc §12 already
  documents as the intended extension point (§0.3) — implement that
  extension, do not redesign the type or the org/team resolution it wraps.
- AC4.2 — Rule shape is a fixed mapping of `{DLP category → allowed model
  set}`, org-wide only — no team-level override of content-aware rules
  exists in the UI spec (§7's Content-Aware Routing tab shows only org-wide
  category rows). Per the phase doc's own "simple rule engine, not the full
  Phase 5 dynamic classification system" framing, Phase 3 need only wire
  **one** category end-to-end: `PII`, sourced directly from §3.2's DLP
  findings. The UI mock also shows "Source code" and "Financial data" rows
  with no backing classifier in Phase 3 — see Ambiguity A6 on whether to
  ship those rows as configurable-but-inert or omit them entirely.
- AC4.3 — Precedence: the content-classification layer applies **after**
  the static org/team baseline (per the UI's own precedence-trace copy: "a
  model blocked by the static baseline stays blocked even if a category
  would otherwise allow it") — it can only further restrict, never
  re-enable a statically-blocked model, same non-loosening principle as
  every other layer in this system.
- AC4.4 — A triggered category with zero allowed models configured actually
  **blocks all traffic in that category**, not just shows a UI warning —
  matches the UI copy's explicit claim ("that category's traffic will be
  blocked entirely once this tab is enabled"); this must be real enforcement
  behavior, not only a visual warning state.
- AC4.5 — Enabling a content-aware rule for a category implicitly requires
  synchronous scanning for that category regardless of the org's configured
  DLP default action (per §3.2 AC2.9) — document this coupling explicitly
  so it isn't discovered as a surprise interaction during QA.

**Deferred / explicitly out of scope for this section**

- Microsoft Purview / Google DLP as classification sources (Phase 5).
- "Source code" / "Financial data" as functioning trigger categories
  (Phase 5 — no classifier produces these signals yet, see AC4.2/A6).
- Per-team override of content-aware rules.

---

## 5. §3.5 SCIM

Per Phase 2's own resolution (`phase-2-product-spec.md` AC1.6: "SCIM is
explicitly not built this phase... documented Phase 3-or-later item"), SCIM
**is in scope now**. `ui-requirements-admin.md` §14 already specifies the
provisioning UI (toggle, base URL, one-time-reveal rotatable token).

**User stories**

- As an Org Admin, I enable SCIM 2.0 provisioning from our IdP, so
  user/team lifecycle is automated rather than manual/self-service-only.
- As an Org Admin, I get a SCIM base URL + bearer token (one-time-reveal,
  rotatable) to configure in the IdP.
- As a SCIM-provisioned user, my first login skips Phase 2's self-service
  join-request flow entirely (the exact carve-out Phase 2's AC6.1 already
  anticipated: "this flow triggers only when SCIM has not already resolved
  team membership").
- As an Org Admin, when a user is deprovisioned in the IdP, their Gatekey
  access is revoked on next SCIM push, without me taking a manual action in
  the console.

**Acceptance criteria**

- AC5.1 — Standard SCIM 2.0 resource types: Users and Groups. Groups map to
  Gatekey `Team`s; group-membership push maps to `TeamMembership`
  create/update. Build against RFC 7644 filtering/pagination properly, not
  a silently partial subset.
- AC5.2 — Auth: one bearer token per org, generated/rotated via the exact
  same one-time-reveal component already used for service-account secrets
  (Phase 1 §7.6 pattern, reused per the UI doc's explicit instruction).
  Token rotation invalidates the prior token immediately — no overlap
  needed (this is an inbound credential the org's IdP holds, not an
  outbound-rotation-scheduled one like §3.7).
- AC5.3 — `POST /Users` creates a `User` row with no `org_role` set
  (Member-equivalent) and does **not** bypass Phase 2 §2.6's team-assignment
  gate — team membership comes only from Group push, not from `User`
  creation alone. A SCIM-provisioned user with no group assignment yet has
  zero gateway access, same "zero access until resolved" principle as the
  self-service flow, via a different resolution path.
- AC5.4 — Group push creates/removes `TeamMembership` rows. A newly-added
  member gets a `TeamMembership` with **no budget allocated by default**
  (SCIM's standard schema has no budget concept) — an Org Admin or Team
  Lead must separately allocate budget before the member can spend, same
  as an existing zero-budget membership elsewhere in the system. See
  Ambiguity A7 on whether this needs its own "needs attention" surfacing.
- AC5.5 — Deactivation (`PATCH active:false` or `DELETE /Users/{id}`)
  immediately revokes all active `Session` rows for that user (server-side
  revocation, per the Phase 2 architecture decision that sessions are
  opaque and server-revocable) and revokes all of that user's
  `PersonalApiKey` rows. `ServiceAccountKey` rows attributed to that user
  are **not** auto-revoked (an app credential may still be needed by the
  team independent of its named owner, consistent with the existing
  `ON DELETE RESTRICT` precedent that already decouples key lifecycle from
  user lifecycle). See Ambiguity A8 — the personal-key auto-revocation
  behavior is a real security-relevant default not explicitly stated in
  either source doc.
- AC5.6 — Deprovisioning never deletes the `User` row or `TeamMembership`
  history — matches the audit/history-preservation posture already built
  into `AuditEntry.actor_label` snapshotting.
- AC5.7 — SCIM is an org-wide on/off toggle, default off; when off, Phase
  2's self-service onboarding remains the only path and its AC6.1 carve-out
  becomes live/testable for the first time now that SCIM actually exists.
- AC5.8 — SCIM Group/User attributes can **never** set `org_role`
  (`org_admin`/`auditor`) — any such attempt via a custom SCIM attribute is
  ignored server-side, enforced regardless of what the IdP sends. This is
  the same server-side defense-in-depth already required for Phase 2's
  AC1.5 ("only an Org Admin can assign org_admin/auditor") — a SCIM push
  must not be a backdoor around it.

**Deferred / explicitly out of scope for this section**

- SCIM as a role-assignment channel (AC5.8).
- Live-verified integration against a specific real IdP (Okta/Azure AD) in
  this build environment — same "implemented and spec-compliant but
  unverified-live" caveat class as Phase 2's email-alerting and SSO gaps
  (A8 in `phase-2-product-spec.md`); do not mark SCIM "verified" on
  spec-compliance testing alone.

---

## 6. §3.6 Compliance Documentation & Retention

**User stories**

- As an Org Admin, I download a data flow diagram and a written data
  handling policy document from the console.
- As an Org Admin, I configure log/prompt retention (default 30 days) with
  auto-purge, independent of audit retention (§3.1).
- As a compliance reviewer, I complete a vendor review using only the docs
  and controls shipped in this phase (phase doc's stated success criterion).

**Acceptance criteria**

- AC6.1 — Two independent org-level settings:
  `log_prompt_retention_days` (default **30**, per the phase doc's resolved
  default and the UI's pre-filled value) and `audit_retention_days` (§1
  AC1.6, separately configured) — enforced as genuinely separable at the
  infra level (different tables/purge jobs), matching the phase doc's
  explicit NFR.
- AC6.2 — Two independent, separately-scheduled purge jobs — one for
  usage/prompt log rows against `log_prompt_retention_days`, one for
  `AuditEntry` rows against `audit_retention_days` — never one shared purge
  routine, so a future change to one window can never accidentally affect
  the other's data.
- AC6.3 — Retention is a preset dropdown, 30 days shown pre-filled (not
  blank) by default, matching `ui-requirements-admin.md` §10.4.
- AC6.4 — Purge is a hard delete, not soft/tombstone — a retention control
  that leaves data queryable after its window would fail the feature's own
  stated compliance purpose.
- AC6.5 — Compliance documentation: a downloadable data flow diagram and a
  written data handling policy, both intended to be handed directly to a
  customer's security/vendor-risk reviewer. Whether the data handling
  policy document is a static template or dynamically generated to reflect
  the org's actual live retention/DLP/residency configuration is not
  specified by either source doc — see Ambiguity A9.
- AC6.6 — Success criterion, operationalized as a QA acceptance test: using
  only the Retention & Docs tab's two documents plus the DLP/Residency/Audit
  Log tabs, a simulated vendor-security-review checklist run against a
  pilot org surfaces no "feature doesn't exist yet" gaps.

**Deferred / explicitly out of scope for this section**

- Automated/continuous compliance certification (SOC 2 report generation,
  etc.) — not referenced anywhere in the phase doc's scope.

---

## 7. §3.7 Credential Rotation (Service-Account Keys + Provider Keys)

**User stories**

- As an Org Admin, I set an org-wide default rotation interval for
  service-account keys (off by default); any key can override it.
- As a key owner, I'm notified (email/webhook) whenever my key rotates,
  with the new secret delivered via the existing one-time-reveal pattern.
- As an Org Admin, "Rotate now" on any service-account key uses the same
  short-overlap mechanism as scheduled rotation, distinct from an immediate,
  zero-overlap "Revoke" for a compromised key.
- As an Org Admin, I get an email reminder before a provider key's
  suggested rotation date, and a guided flow to paste/validate/overlap-swap
  a new provider key.

**Acceptance criteria**

- AC7.1 — `RotationPolicy: { scope: "org"|provider_key_id|service_account_id,
  enabled, interval_days, rotate_at_local_time, overlap_buffer_minutes,
  next_rotation_at, last_rotated_at, mode: "automatic"|"manual_guided" }`
  per `ui-requirements-admin.md` §16. `service_account_key` scope is always
  `mode="automatic"`; `provider_key` scope is always `mode="manual_guided"`
  — never offer a "fully automatic" toggle for provider keys (phase doc's
  explicit instruction not to overstate what's possible without a
  provider-side issuance API).
- AC7.2 — Default state: rotation **disabled** org-wide — matches this
  phase's consistent off-by-default posture for every traffic-shaping
  feature (§0.6).
- AC7.3 — Off-hours timing resolution for service-account key rotation is
  computed per key, not one blanket cron time: (a) outside the key's own
  §3.8 access-schedule window if one is configured, else (b) the org-wide
  off-hours setting (default 02:00 org-local time).
- AC7.4 — Overlap buffer defaults to a few minutes (UI default: 5),
  configurable, **never** long/multi-day by default (§0.6, non-negotiable).
  NFR: old and new secret validity overlaps (not merely abuts), holding
  even across clock skew between gateway instances in a multi-instance
  deployment — needs an explicit multi-instance test (two gateway processes
  with deliberately offset clocks; confirm no zero-valid-secret window).
- AC7.5 — Scheduled service-account key rotation is fully automatic
  end-to-end: mint new secret → deliver via one-time-reveal (surfaced on
  next view) + notification (email/webhook, reusing Phase 2's existing
  pluggable notifier interface, not a new one) → keep old secret valid for
  `overlap_buffer_minutes` → auto-revoke old secret. No admin action
  required for a scheduled cycle to complete.
- AC7.6 — "Rotate now" (manual) uses the identical short-overlap mechanism
  as scheduled rotation — never an instant swap. This must be a visibly
  distinct action from "Revoke" (immediate, zero overlap, for the
  compromised-key case) — different buttons/endpoints, not two modes of one
  action, given the very different security implications of hitting the
  wrong one.
- AC7.7 — Provider key rotation (guided manual flow): admin pastes the new
  key → Gatekey validates it live against the provider (same three
  structured error states as Phase 1's add-key modal: invalid format, auth
  rejected, provider unreachable) → on success, old and new both stay
  active for a **fixed** short overlap (not access-schedule-anchored — a
  provider key backs potentially many teams/apps at once, no single
  "off-hours") → old auto-retires after the buffer.
- AC7.8 — Provider key rotation reminder: email N days before a
  configured/suggested rotation date (UI default: 14), defaulting **on**
  (unlike rotation/schedule *activation*, which defaults off) — a passive
  reminder email changes no gateway behavior, so it doesn't carry the
  "silently changes existing behavior" risk the off-by-default principle
  exists to guard against.
- AC7.9 — Same pluggable-notifier treatment as Phase 2's budget alerts:
  webhook delivery verified end-to-end against a mock receiver + one live
  target in tests; email flagged **unverified-live** (same caveat class as
  Phase 1's pricing gap and Phase 2's email-alerting gap) — QA must not
  mark rotation email notifications "verified" without a real mailbox test.

**Deferred / explicitly out of scope for this section**

- True automatic provider-key rotation via a provider-side issuance API
  (explicit phase doc exclusion — revisit only per-provider if/when
  supported).

---

## 8. §3.7a Local Credential Sync Helper (CLI Auto-Sync)

**This is a separate, real client-side engineering deliverable — see §0.7.**
Recommend it live in its own top-level directory (e.g., `cli-helper/` or
`gatekey-sync/`), not folded into `backend/src/gatekey` or a frontend
package, since it ships as software the end user installs and runs locally,
not a web surface or a backend service.

**User stories**

- As a CLI user, I run a one-time interactive login (device-code-style) to
  connect my personal key to a local helper, so I never manually copy-paste
  a rotated key again.
- As a CLI user, the helper checks a local cache before every invocation
  and calls Gatekey at most once a day (or once an hour with no off-hours
  anchor), so there's no perceptible added latency on normal use.
- As a CLI user, if my key is force-revoked, my next command transparently
  re-fetches instead of showing a cryptic auth error.

**Acceptance criteria**

- AC8a.1 (architecture decision, flagged for the architect — not decided
  here) — **Language/placement**: recommend **Python** for consistency with
  the backend (one less language for the team, and `keyring` already
  abstracts Keychain/Credential Manager/Secret Service exactly as required
  "from day one"). Tradeoff: a compiled option (Go, with `zalando/go-keyring`
  as the equivalent abstraction) ships a single static binary with no
  runtime dependency — a real adoption-friction reducer for a CLI tool aimed
  at non-backend-engineer end users who may not have Python installed. This
  is a genuine toss-up between team velocity (Python) and end-user install
  friction (Go); flagging rather than picking, per the task's own
  instruction — see Ambiguity A11.
- AC8a.2 — One-time interactive auth: device-code-style flow (helper
  displays/opens a URL + code; user confirms in browser; helper polls) —
  stores a **refresh credential**, not the API key itself, in the OS
  keychain via the cross-platform abstraction chosen in AC8a.1.
- AC8a.3 — Local cache: `{secret, valid_until}` (`LocalKeyCacheHint` per
  `ui-requirements-admin.md` §16). Cache hit (`now < valid_until`) → used
  immediately, zero network call. Cache miss/expired → call the "get my
  current active key" endpoint (authenticated via the stored refresh
  credential), write the result + new `valid_until`, proceed.
- AC8a.4 — Server-side `valid_until` computation (this is server behavior,
  the client just trusts whatever it's given): set to just past the key's
  next scheduled rotation (`RotationPolicy.next_rotation_at`, resolved per
  §7 AC7.3's off-hours anchor) when the key has an access schedule or an org
  off-hours window configured; falls back to `now + 1 hour` (fixed, per the
  phase doc's resolved decision) when neither exists.
- AC8a.5 — On an auth rejection specifically attributable to "key no longer
  valid" (not a generic network error), the helper invalidates its cache
  and re-fetches exactly once, transparently. A second consecutive
  rejection after a fresh fetch surfaces the real error rather than looping
  silently.
- AC8a.6 — The helper writes the fetched key only to the one
  file/location the target CLI reads its credential from — it does not
  parse or understand arbitrary CLI config formats beyond that single
  target. Exact file/path is CLI-specific and confirmed per real tool, same
  "confirm per design partner, don't guess" posture Phase 2 already applied
  to CLI wire-protocol compatibility — not re-litigated here.
- AC8a.7 (NFR) — Cache-hit path latency must be negligible relative to the
  target CLI's own startup time — needs an actual timed benchmark, not a
  qualitative "should be fast" claim.
- AC8a.8 — Success criterion, operationalized: a pilot user's first CLI
  invocation the day after a personal-key rotation succeeds with the new
  key, zero manual action, no perceptible delay, and Gatekey's server logs
  show exactly one "get my current key" call that day for that user (a
  request-count assertion, not just "it worked").

**Deferred / explicitly out of scope for this section**

- Supporting every possible CLI's config format out of the box (only the
  one target file/location per tool, confirmed per real CLI).
- A persistent background daemon — explicitly ruled out by the phase doc
  ("without a persistent background process").

---

## 9. §3.8 Scheduled Access Windows

**User stories**

- As an Org Admin, I set an org-wide default access schedule (days/hours/
  timezone) for service-account keys, off by default.
- As a Team Lead, I can narrow (never widen) my team's effective schedule.
- As an Org Admin, I can set a per-key override, narrower than its
  team/org-resolved window.
- As a user whose key is blocked outside its window, I get a clear
  structured error (`outside_allowed_schedule`), and the block itself is
  logged.
- As a Team Lead or Org Admin, I can grant a time-boxed emergency override
  with a required reason, for legitimate off-hours work.

**Acceptance criteria**

- AC9.1 — `AccessSchedule: { scope: "org"|team_id|service_account_id,
  enabled, timezone, allowed_days, allowed_hours: {start, end},
  holiday_calendar_ref }` per `ui-requirements-admin.md` §16.
- AC9.2 — Precedence: org default → team override (narrowing-only,
  server-side enforced, not merely UI-hidden) → per-service-account
  override (narrowing-only relative to its resolved team/org window) — the
  identical mechanism as Phase 2's nested model policy (§0.5); reuse that
  enforcement pattern, don't reimplement it.
- AC9.3 — Default state: **off** at every level org-wide — enabling is
  opt-in per org/team/key, matching this phase's rotation-default posture
  (§0.6).
- AC9.4 — Timezone is explicit and org-configured, never inferred from
  request IP/origin — one org-wide timezone setting interprets every
  schedule's `allowed_hours` at every level (no per-team/per-key timezone
  override exists in the UI spec).
- AC9.5 — Holiday calendar: org-wide list of specific dates, blocks access
  on that date regardless of an otherwise-allowed weekday, extending (not
  duplicating) Phase 2's A7 business-day computation (§0.4). No UI surface
  for a non-org-wide ("custom") holiday calendar exists anywhere in the
  admin doc — see Ambiguity A10 on whether `holiday_calendar_ref="custom"`
  should be buildable in Phase 3 at all.
- AC9.6 — A blocked request produces the structured error
  `outside_allowed_schedule` (never a generic 403 or silent failure) **and**
  writes an audit trail entry — both the block-attempt and any mutation are
  logged; a blocked authentication attempt is itself the auditable event
  here, distinct from most of this doc's audit entries which record
  mutations.
- AC9.7 — Emergency override:
  `{ id, scope: service_account_id, granted_by, reason, granted_at,
  expires_at, revoked_at }` per §16. `reason` is required and non-empty,
  enforced **server-side** (not just a UI required-field hint).
- AC9.8 — Grant/revoke authorization: a Team Lead may act only on service
  accounts attributed to their own team; an Org Admin may act on any team's
  — server-side check, matching this doc's consistent defense-in-depth
  posture for every scoped permission.
- AC9.9 — Granting and revoking an override each write a distinct audit
  trail entry (grant: who/what/until/reason; revoke: who/when/which
  override) — a security-control bypass and its reversal are both
  auditable events.
- AC9.10 — The list endpoint returns the fully-resolved **effective**
  schedule per key (e.g., "Mon-Fri 9-6" or "Always"), not merely
  "has-an-override: yes/no" — a real server-side precedence-resolution
  requirement (matches the UI's "Sched." column needing to scan without
  opening each row), not a UI-side merge.
- AC9.11 (NFR) — Schedule-window check adds negligible latency to the auth
  path, same order of magnitude as Phase 2's <10ms RBAC-check budget —
  recommend reusing the existing in-memory cache pattern
  (`ModelPolicyCache`/`TeamModelPolicyCache` in `services/model_policy.py`)
  rather than a new caching approach, since it's the codebase's own
  established precedent for exactly this kind of hot-path policy lookup.

**Deferred / explicitly out of scope for this section**

- Per-team/per-key custom holiday calendars (Ambiguity A10).
- Inferring timezone from request origin (explicitly rejected by the phase
  doc as unreliable).

---

## 10. Explicit Scope Boundary Summary

**In scope for Phase 3 (build now):**
- Audit trail gap-closure: source IP, CSV/JSON export, separable
  configurable retention + purge — extending, not rebuilding, Phase 2's
  audit system.
- Presidio-backed PII/DLP scanning (built-in + custom regex patterns),
  three-action policy (log/redact/block) with the specific sync/async
  behavior each implies, org + optional team-level action override.
- Data residency rules (org/team scope, hard-block default, explicit
  opt-down to warn), pending the region-metadata gap (A5).
- The single-category (PII) content-aware routing rule, wired end-to-end
  from §3.2's DLP findings, extending `blocking_layer` per Phase 2's own
  pre-flagged extension point.
- SCIM 2.0 provisioning/deprovisioning (Users, Groups→Teams), token
  rotation via the existing one-time-reveal pattern.
- Compliance documentation downloads; independently configured, auto-purged
  usage/prompt retention (default 30d) separate from audit retention.
- Fully automatic service-account key rotation (short overlap, off-hours
  timed, off by default) + guided manual provider-key rotation +
  reminders; distinct immediate-revoke path.
- The CLI local sync helper as a standalone client-side deliverable.
- Three-level (org→team→key) scheduled access windows, holiday calendar,
  time-boxed reason-required emergency overrides, off by default.

**Explicitly deferred / out of scope (matches the phase doc's own
Out-of-Scope list, plus items surfaced only by operationalizing it):**
- Cryptographic hash-chained audit ledger, chain verification (Phase 5).
- Full dynamic content-classification routing beyond the single PII rule
  (Phase 5) — Microsoft Purview/Google DLP sources, "Source code"/
  "Financial data" as functioning trigger categories.
- Caching, rate limiting, failover (Phase 4).
- True automatic provider-key rotation via provider issuance APIs.
- Inbound/response DLP scanning (no UI representation found — A4).
- Per-team/per-key custom holiday calendars (A10).
- Automated/continuous compliance certification generation (SOC 2, etc.).

---

## 11. Data Model Touchpoints (for architect — not a schema design, a checklist)

- `AuditEntry`: add `source_ip` (nullable), add `audit_retention_days` as
  an org-level setting (new column/table, separate from usage-data
  retention), add a scheduled purge job — flagged as a documented exception
  to the table's existing "never DELETE" design note (A2).
- New `log_prompt_retention_days` org setting (default 30) + its own
  independent purge job over usage/prompt log rows.
- `DlpRule`: built-in detector toggles (org-wide), custom pattern rows
  (`{name, regex, action}`, org-level), default action (org), optional
  per-team action override — per `ui-requirements-admin.md` §16 shape.
- `ResidencyRule: { scope, allowed_regions, violation_behavior }` — blocked
  on resolving region metadata for standard (non-self-hosted) provider
  endpoints first (A5).
- `ContentAwareRule` (or equivalent): `{category, allowed_models, source}`,
  org-wide only, PII category wired to §3.2's findings.
- `ModelAccessDecision.blocking_layer`: add `"content_classification"`
  literal (Phase 2's pre-flagged extension point — no other type change).
- SCIM: no new core identity tables — Users/Groups map directly onto
  existing `User`/`Team`/`TeamMembership`; new SCIM bearer-token
  storage (hashed, one-time-reveal on rotation) + an org-level
  `scim_enabled` toggle.
- `RotationPolicy`, `RotationEvent` per `ui-requirements-admin.md` §16
  shapes — attach to `ServiceAccountKey` and provider-key rows (extends
  existing tables, does not replace them).
- `AccessSchedule`, `HolidayDate`, `EmergencyOverride` per §16 shapes —
  `AccessSchedule` rows at org/team/service-account scope, one shared
  `HolidayDate` list org-wide (A10 — no per-scope calendar in Phase 3).
- `LocalKeyCacheHint` — not a persisted table, the response shape of the
  "get my current key" endpoint the CLI helper (§8) calls.

---

## 12. Flagged Ambiguities (genuinely open — not re-litigating resolved items)

The phase doc's own open questions are all resolved inline and used as-is.
The following surfaced only by cross-referencing the phase doc against the
UI docs and the current codebase — they are not called out as resolved
anywhere in the source docs, and building against a guess risks rework.

- **A1 — No numeric default for `audit_retention_days`.** The phase doc
  only says audit retention is "typically held longer" than the 30-day
  usage default; `ui-requirements-admin.md` §10.4 shows "1 year" as an
  example dropdown value, not stated as the resolved default. **Recommend**
  adopting 1 year as the default (the only concrete number in any source
  doc), but this needs explicit sign-off since it's the one retention
  number this phase doesn't already pin.

- **A2 — The audit purge job is the first-ever DELETE against
  `AuditEntry`, contradicting that table's documented "never UPDATE, never
  DELETE" design note from Phase 2.** Not a design flaw — the phase doc's
  NFR explicitly requires configurable retention — but it needs to be an
  explicit, written exception in the architect's design doc (a scheduled,
  config-driven bulk purge, never reachable via a mutating API endpoint),
  not something that silently contradicts existing documented invariants.

- **A3 — No UI control for "store raw flagged content" in DLP findings.**
  The phase doc's phrasing ("without necessarily storing the raw flagged
  content if policy says not to") implies a configurable choice, but
  `ui-requirements-admin.md` §10.1's DLP tab shows no such toggle.
  **Recommend**: default to NOT storing the raw flagged substring
  (matches the general PII-minimization instinct behind the feature
  existing at all), storing only detector/pattern name + action taken,
  with the toggle deferred until the UI doc actually specifies one. Flag
  for product/architect sign-off since it affects what an auditor can and
  can't see after the fact.

- **A4 — Inbound (response) DLP scanning has zero UI representation.** The
  phase doc marks it "(optionally)"; `ui-requirements-admin.md` §10.1's
  header is "Scan outbound prompts for:" with no toggle for responses
  anywhere in the doc. **Recommend** treating outbound-only as the shipped
  Phase 3 slice; response scanning stays a backend-optional, UI-deferred
  item pending explicit design, not silently built without a control
  surface.

- **A5 — Provider/model region metadata is a real, unresolved data-model
  gap.** Residency enforcement (§3.3) needs every routable model/provider
  endpoint to declare a region. The reviewed provider/model registry
  (Phase 1/2) shows a `base_url` concept for self-hosted providers only —
  no region field for standard cloud endpoints (OpenAI/Anthropic/Vertex) is
  documented anywhere reviewed. This blocks residency from being built at
  all until resolved — flagging as high-priority, not low-risk.

- **A6 — Should Phase 3 ship the "Source code"/"Financial data" rows in
  the Content-Aware Routing tab as configurable-but-inert, or omit them
  entirely?** Only "PII" has a real backing signal in Phase 3 (§3.2's DLP).
  **Recommend** shipping the full table UI (cheap, forward-compatible) with
  only PII functionally wired, documented in-product as a current
  limitation — but flag to avoid the system feeling silently broken when a
  configured "Financial data" rule never triggers.

- **A7 — SCIM group-push creates a $0-budget `TeamMembership` with no
  "needs attention" surfacing anywhere.** Neither source doc addresses
  this. **Recommend** reusing the same "needs your action" visual treatment
  already built for join requests (Phase 2 §8) so a SCIM-added member
  doesn't sit invisibly at zero budget indefinitely — needs product
  sign-off, this is new UI surface neither doc shows.

- **A8 — SCIM deactivation auto-revoking `PersonalApiKey` rows (and
  session) is a real, security-relevant default not explicitly stated in
  either source doc.** **Recommend** building it this way (the safer
  default, consistent with the stated intent behind 3.8's "prevent a
  company-provisioned credential from being used" framing) but flag for
  explicit sign-off since it's currently an inference, not a stated
  requirement.

- **A9 — Is the data handling policy document (§3.6) a static template or
  dynamically generated from the org's live retention/DLP/residency
  config?** The UI mock just shows two static "Download" buttons.
  **Recommend** dynamic generation — a static doc that drifts from an org's
  actual settings would fail its own stated purpose the moment a default
  changes — but this is materially more engineering scope than "ship a
  PDF," so flag for explicit sign-off on which approach to build.

- **A10 — `AccessSchedule.holiday_calendar_ref = "custom"` has no
  authoring UI anywhere in the admin doc** — only the org-wide holiday list
  under Rotation & Access Windows exists. **Recommend** Phase 3 only ever
  produces `"org_default"`; keep `"custom"` as a reserved-but-unused enum
  value for forward compatibility (matching the extensibility precedent
  already set for `blocking_layer`), and don't build any path that
  produces it this phase.

- **A11 (architecture decision, not a build ambiguity) — CLI sync helper
  language/placement.** See §8 AC8a.1: Python (team consistency, mature
  `keyring` abstraction) vs. a compiled option like Go (zero-dependency
  end-user install, `go-keyring` equivalent). Genuinely a tradeoff, not a
  guess — the architect should decide and record the choice.

- **A12 (minor, not blocking) — Residency's narrowing-only precedence and
  "log the block" behavior (§3 AC3.3/AC3.5) are inferred from this doc's
  established patterns elsewhere, not explicitly stated in the phase doc's
  §3.3 text itself.** Low risk (the inference is a straightforward
  application of a pattern this phase doc uses everywhere else), but
  flagging so it's a documented, deliberate choice rather than an
  assumption baked in silently — parallel treatment to Phase 2's own A7
  ("minor, not blocking" business-day default).
