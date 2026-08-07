---
title: Gatekey — UI Design Requirements (Org Admin Console, All Phases)
status: draft
last_updated: 2026-07-22
companion_doc: ui-requirements-non-admin.md
---

# Gatekey — Org Admin Console UI Requirements (Phases 1–6)

This document specs the **Org Admin** experience across Gatekey's full
roadmap (`gatekey/phase-1-core-gateway.md` through `phase-6-ecosystem-scale.md`).
Its companion, [`ui-requirements-non-admin.md`](ui-requirements-non-admin.md),
covers everyone who isn't an Org Admin (Team Lead, Member, Auditor). Hand
both to an AI coding tool together — they share one design system (§2) and
are meant to be built as a single app with role-based routing/nav, not two
separate products.

**How to read the phase tags.** Every screen/section below is tagged with
the phase that introduces it (`[P1]`…`[P6]`), matching the phase files in
`gatekey/`. This is for traceability back to the spec, not a build order
instruction — build the full end-state console described here as one
working prototype. Where a later phase changes how an earlier screen
behaves (e.g., Phase 2 turns the flat Phase 1 "Users" list into a
team-nested hierarchy), this doc describes the **final, merged** behavior
only — it does not re-describe the superseded Phase 1 version.

Since this spans a large roadmap, most Phase 5/6 screens are lower-traffic
and specified at "layout + fields + behavior" detail rather than full boxed
wireframes; the core operator screens (Dashboard, Providers, Teams & Users,
Model Policy, Security & Compliance, Reliability) get full wireframes since
those are used constantly.

## 1. Role Recap

Org Admin (per `phase-2-multi-tenant-governance.md` §2.1) has **full control
over the org**: providers, keys, org-wide policy, all teams/budgets, RBAC
assignment, and everything in Phases 3–6. This document is written entirely
from that role's point of view — every screen here assumes org-admin
permissions. Phase 1 predates RBAC entirely (single shared admin
credential, no other roles exist yet); from Phase 2 onward, Org Admin is one
of four roles (Org Admin, Team Lead, Member, Auditor) — see the companion
doc for the other three.

## 2. Shared Design System

This reuses and extends the system defined in the Phase 1 doc
(`phase-1-admin-console-ui-requirements.md` §4, §8, §9) — same principles
(dense-over-spacious, secrets shown once, structured errors, confirm before
destructive actions), same base components (stat tile, data table, modal,
confirm dialog, badge, progress bar, toast, inline field error), same visual
style (neutral palette + single accent, monospace for IDs/secrets, light
mode primary). New components introduced by later phases:

- **Tab bar** — used inside multi-concern screens (Reliability & Cost,
  Security & Compliance, Differentiators, Marketplace & Growth) to group
  related settings under one nav item instead of proliferating top-level nav
  items. Horizontal tabs directly under the screen title.
- **Health dot** — small colored dot + label for live status (Healthy /
  Degraded / Down), distinct from the Providers "Connected/Not configured"
  badge — health is about live availability, connected-ness is about
  key-on-file.
- **Policy precedence trace** — a small expandable "why" panel showing the
  ordered list of policy layers evaluated for a decision (org → team →
  content-classification) with which layer decided the outcome highlighted.
  Used in Model Policy and surfaced read-only to end users in the companion
  doc.
- **Hash-chain integrity badge** — "✓ Verified — chain intact" / "⚠
  Verification failed at entry #N" for the Phase 5 audit ledger.
- **Marketplace listing card** — for policy packs and budget-surplus
  listings: title, publisher/team, short description, a primary action
  (Install / Request), and a secondary metadata line.
- **Trend sparkline** — inline mini-chart in table cells (e.g., a user's
  7-day spend trend next to their total) — used sparingly, not in every
  table.

## 3. Information Architecture

```
Gatekey Admin
├── Dashboard                       [P1,P4,P6]  usage, cost, reliability, forecast
├── Providers                        [P1,P4,P5]  keys, multi-key/failover, self-hosted models
├── Model Policy                     [P1,P5]     org baseline + content-classification routing
├── Teams & Users                    [P1,P2]     org→team→user hierarchy, budgets, RBAC, nested policy, join approvals
├── Service Accounts & Keys           [P1,P2,P3]  app credentials + personal keys
├── Security & Compliance            [P3,P5]     DLP · Residency · Audit Log/Ledger · Retention & Docs
├── Reliability & Cost                [P4]        Failover · Rate Limits · Caching · Degradation
├── Differentiators                  [P5]        Shadow AI · Drift Detector · Self-Hosted Governance
├── Marketplace & Growth              [P6]        Policy Packs · Budget Marketplace · ROI · Model Sandbox
├── Identity & Access                 [P2,P3]     SSO/SCIM configuration
└── Settings                          [P1]        org profile, retention defaults
```

Tab-grouped screens (Security & Compliance, Reliability & Cost,
Differentiators, Marketplace & Growth) keep top-level nav from growing
unbounded as phases stack up — this is the pattern to keep using if you add
more sub-features later.

## 4. Global Shell

Same shell as Phase 1 (`phase-1-admin-console-ui-requirements.md` §6):
persistent left sidebar (now 11 items, grouped visually with subtle section
dividers — Core / Governance / Platform), top bar with org identity +
admin's own account menu. From Phase 2 onward the top bar also needs an
**org name label** (still no org *switcher* — Gatekey remains single-org per
deployment even at Phase 6; multi-org-per-deployment was never in scope in
any phase file) and, once SSO exists, the admin's avatar/name comes from
the identity provider rather than a static "admin@acme.co" placeholder.

```
┌───────────────────────────────────────────────────────────────────────┐
│ ⛨ Gatekey · Acme Corp                          Priya Shah (Org Admin) ▾│
├───────────┬───────────────────────────────────────────────────────────┤
│ CORE       │                                                          │
│ Dashboard │                                                           │
│ Providers │                    <screen content>                      │
│ Model     │                                                           │
│  Policy   │                                                           │
│           │                                                           │
│ GOVERNANCE │                                                          │
│ Teams &    │                                                          │
│  Users     │                                                          │
│ Service    │                                                          │
│  Accounts  │                                                          │
│ Security & │                                                          │
│  Compliance│                                                          │
│ Identity & │                                                          │
│  Access    │                                                          │
│           │                                                           │
│ PLATFORM   │                                                          │
│ Reliability│                                                          │
│  & Cost    │                                                          │
│ Differen-  │                                                          │
│  tiators   │                                                          │
│ Marketplace│                                                          │
│  & Growth  │                                                          │
│           │                                                           │
│ Settings  │                                                           │
│ ────────  │                                                           │
│ v1.0      │                                                           │
└───────────┴───────────────────────────────────────────────────────────┘
```

## 5. Dashboard `[P1, P4, P6]`

Extends the Phase 1 dashboard (`phase-1-admin-console-ui-requirements.md`
§7.3 — total spend, requests, latency, error rate, spend-by-day,
spend-by-model, spend-by-user) with three additional stat tiles and one
additional panel, gated behind whether the relevant phase's features are
configured (don't show a "Cache hit rate" tile with permanent zeros if
caching was never enabled — see empty-state rule in §12 of the Phase 1 doc,
same rule applies here).

```
┌───────────────────────────────────────────────────────────────────────┐
│  Dashboard                                    Time range: [ 7 days ▾ ]│
│                                                                       │
│  ┌─────────┐┌─────────┐┌─────────┐┌─────────┐┌─────────┐┌─────────┐│
│  │ Spend    ││ Requests ││ Latency  ││ Errors   ││ Cache    ││ Failovers││
│  │ $184.32  ││ 12,406   ││ 842ms    ││ 0.4%     ││ hit 38%  ││ 2 events ││
│  └─────────┘└─────────┘└─────────┘└─────────┘└─────────┘└─────────┘│
│                                                                       │
│  [ same spend-by-day / spend-by-model / spend-by-user panels as P1 ]  │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐   │
│  │ Forecast                                            [P6]     │   │
│  │ Projected month-end org spend: $2,340  (baseline $2,000 → 17% │   │
│  │ over). Team "ml-platform" is the largest driver (62% of trend).│   │
│  └─────────────────────────────────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** the Cache/Failover tiles only render once Reliability features
(§9) have been enabled at least once; before that, the six-tile row reverts
to the original four from Phase 1. Forecast panel `[P6]` is trend-based
("simple trend extrapolation" per spec) — render as a short natural-language
summary + a one-line delta, not a heavy new chart; clicking it deep-links to
Marketplace & Growth → Forecasting tab (§10.4) for detail.

## 6. Providers `[P1, P4, P5]`

Extends Phase 1 Providers (`phase-1-admin-console-ui-requirements.md` §7.4:
one card per provider, add/edit/remove, live validation, three structured
error states) with multi-key support, health status, and self-hosted model
registration.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Providers                                       [ + Register model ] │
│                                                                       │
│  ┌─ OpenAI ─────────────────────────────────────────────────────┐  │
│  │ ● prod-key-1        gk keys...abc1   ● Healthy    [Edit][Remove]│  │
│  │   Rotation: every 90 days · next Sep 4, 2026     [ Rotate now ]  │  │
│  │ ● failover-key-2     ...def2         ● Healthy    [Edit][Remove]│  │
│  │                                          [ + Add another key ]  │  │
│  │ Failover: ☑ Enabled — retry against failover-key-2 on error     │  │
│  └─────────────────────────────────────────────────────────────┘  │
│  ┌─ Anthropic ──────────────────────────────────────────────────┐  │
│  │ ● prod-key-1        ...aa91          ⚠ Degraded    [Edit][Remove]│  │
│  │   Elevated error rate in the last 15 min — investigate before   │  │
│  │   traffic fails over.                                           │  │
│  └─────────────────────────────────────────────────────────────┘  │
│  ┌─ Self-Hosted Models ─────────────────────────────[P5]──────────┐  │
│  │ ○ vllm-internal-llama3   http://vllm.internal:8000  Not verified │  │
│  │   Cost basis: $2.10/GPU-hour (configured)          [Edit][Remove]│  │
│  └─────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

**New behavior on top of Phase 1:**
- Each provider card becomes a **list of keys**, not one key — "+ Add
  another key" opens the same add-key modal from Phase 1 §7.4, now labeled
  with a required unique key name (needed once >1 key can exist per
  provider).
- **Health dot** (Healthy/Degraded/Down) is a *live* signal distinct from
  "validated at creation" — poll or subscribe to the health-check result and
  render a one-line explanation under a Degraded/Down key, since an admin
  needs to know *why* before manually intervening.
- **Failover toggle** per provider, off by default (per spec's NFR: "bias
  toward off... some compliance-sensitive teams won't want traffic silently
  rerouted"). When on, admin picks which key(s) act as backup.
- **Self-Hosted Models** card registers a vLLM/Ollama-style endpoint as a
  pseudo-provider: base URL, optional auth, and a **cost basis** field
  (GPU-hour rate) since spend must normalize the same way token-priced
  providers do (§11 data reference in the Phase 1 doc extends here — add
  `cost_basis_per_gpu_hour: number` to this provider's shape). "Not
  verified" badge instead of "Connected" until a live health probe succeeds
  — self-hosted endpoints don't get the same up-front provider-API
  validation call OpenAI/Anthropic/Vertex do.
- **Rotation** `[P3]` — each provider key shows its rotation interval and
  next-due date, plus a manual "Rotate now." Because Gatekey doesn't issue
  provider keys itself (see `phase-3-security-compliance.md` §3.7), "Rotate
  now" here opens the **guided manual rotation flow**, not an instant swap:
  admin pastes the new key from the provider's own console, Gatekey
  validates it live (same three structured error states as the Phase 1
  add-key modal), then both old and new keys stay active for a short
  overlap buffer before the old one auto-retires — never an instant
  cutover, but also not a multi-day window. Don't build a "fully automatic"
  toggle for provider keys; that would overstate what's actually possible
  without the provider's own key-issuance API. Provider keys don't have a
  natural "off-hours" the way a personal key does (a provider key backs
  traffic from potentially many apps/teams at once), so their overlap
  buffer stays a fixed short duration rather than being timed against an
  access schedule.

**"Rotate now" / rotation config modal** (also reused by Service Accounts,
§9):

```
┌─────────────────────────────────────────┐
│  Rotation settings — prod-key-1    ✕     │
│                                            │
│  ☑ Auto-rotate on a schedule               │
│  Interval:        [ 90 days ▾ ]            │
│  Rotate at:       [ 02:00 ▾ ] org time      │
│  ⓘ Timed off-hours (outside this key's      │
│    access schedule, if one is set) so a     │
│    rotation almost never lands mid-request. │
│  Overlap buffer:  [ 5 minutes ▾ ]           │
│  ⓘ Short technical buffer only — old and    │
│    new keys both stay valid for this long   │
│    to absorb clock skew or a request        │
│    genuinely in flight, not to give someone  │
│    days to notice and update.                │
│                                            │
│           [ Cancel ]  [ Save ]  [ Rotate now → ] │
└─────────────────────────────────────────┘
```

## 7. Model Policy `[P1, P5]`

Extends Phase 1 Model Policy (`phase-1-admin-console-ui-requirements.md`
§7.7: org-wide allowlist/denylist, grouped checklist by provider) with
content-classification-aware dynamic routing as a second tab.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Model Policy                                                        │
│  [ Static Allow/Deny ]  [ Content-Aware Routing ]  ← tabs             │
│  ─────────────────────────────────────────────────────────────────   │
│  (Static tab: identical to Phase 1 §7.7 — mode radio, per-provider    │
│   checklists, unconfigured-state banner, save bar.)                  │
│                                                                       │
│  Content-Aware Routing tab:                                          │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Category           Allowed models                    Source    │  │
│  ├───────────────────────────────────────────────────────────────┤  │
│  │ PII                 claude-3-5-sonnet (compliant-flag) Gatekey  │  │
│  │ Source code          gpt-4o, claude-opus-5              Gatekey  │  │
│  │ Financial data        [ none configured ]  ⚠            Gatekey  │  │
│  │ General               [ all allowed models ]             Gatekey  │  │
│  └───────────────────────────────────────────────────────────────┘  │
│  Classification source: (•) Gatekey built-in classifier              │
│                          ( ) Microsoft Purview labels                │
│                          ( ) Google DLP classifications              │
│                                            [ + Add category rule ]   │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** a category with no allowed models configured shows a warning
badge — that category's traffic will be blocked entirely once this tab is
enabled, so the empty state must be visibly a warning, not a neutral empty
row. "Add category rule" opens a modal: category name, model multi-select
(same checklist component as the static tab), and which DLP scan result(s)
trigger it (links to Security & Compliance → DLP tab, §8.1). Precedence
between this tab and the static allow/denylist must be explained via the
**policy precedence trace** component (§2) — e.g., "Content-aware rules
apply after the static baseline; a model blocked by the static baseline
stays blocked even if a category would otherwise allow it."

## 8. Teams & Users `[P1, P2, P3]`

Replaces Phase 1's flat Users screen with the Phase 2 org→team→user
hierarchy, RBAC, budget management, and self-service join-request approval
(`phase-2-multi-tenant-governance.md` §2.1, §2.2, §2.3, §2.6). This is the
single busiest screen in the console — most day-to-day admin time (outside
a pilot's first setup) is spent here.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Teams & Users                                        [ + Add team ] │
│                                                                       │
│  ┌─ Join Requests needing your action ───────────────[P2.6]────────┐│
│  │ dan@acme.co → growth-marketing · no Team Lead assigned yet         ││
│  │                                    [ Reject ]  [ Approve & allocate ]││
│  └─────────────────────────────────────────────────────────────────┘│
│  ⓘ Only shown here when a team has no Team Lead — otherwise its own    │
│    Team Lead handles approval and it won't appear in this queue.      │
│                                                                       │
│  ┌─ Teams ──────────────────────────────────────────────────────┐   │
│  │ ▸ ml-platform        12 members   $2,000 / $2,500 ceiling  ⋮   │   │
│  │ ▸ support-eng          6 members   $   400 / $  500 ceiling  ⋮   │   │
│  │ ▸ growth-marketing      4 members   $  180 / $  300 ceiling  ⋮   │   │
│  └─────────────────────────────────────────────────────────────┘   │
│  Org-wide budget ceiling: $5,000 / period · unallocated: $1,600       │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** this reuses the exact same Approve/Reject-with-mandatory-
budget component from the companion non-admin doc's §7.1 (Team Lead's Join
Requests screen) — same clamping to the team's unallocated ceiling, same
optional rejection reason, same audit trail entry — it's the identical
control surface, just triggered here instead because the fallback routing
rule in `phase-2-multi-tenant-governance.md` §2.6 sent it to Org Admin
(the target team has no Team Lead). Once that team gets a Team Lead
assigned, new requests for it stop appearing here and start appearing in
that Team Lead's own queue instead — don't build two independent
implementations of the same approval logic.

**Team detail (expand a row, or click through to a dedicated page):**

```
┌───────────────────────────────────────────────────────────────────────┐
│  ← Teams   ml-platform                              [Edit team] [⋮]  │
│                                                                       │
│  Budget ceiling: $2,500 / month     Period ends: Aug 1, 2026         │
│  On period end: (•) Roll over unused   ( ) Reset to zero               │
│  Current allocation to members: $2,000 of $2,500 (80% allocated)      │
│  ⓘ 2 pending join requests — handled by this team's Team Lead(s)      │
│                                                                       │
│  ┌─ Members ────────────────────────────────────[+ Add member]───┐  │
│  │ Name         Role        Budget    Spent      Status    Actions│  │
│  ├───────────────────────────────────────────────────────────────┤  │
│  │ ana@acme.co   Team Lead   $500     $310 (62%)  Active     ⋮    │  │
│  │ ben@acme.co   Member      $300     $294 (98%)  ⚠ Near limit ⋮ │  │
│  │ cy@acme.co    Member      $200     $ 40 (20%)  Active     ⋮    │  │
│  └───────────────────────────────────────────────────────────────┘  │
│  [ Reassign budget between members ]                                 │
│                                                                       │
│  ┌─ Team Model Restrictions ───────────────────────────────────┐    │
│  │ Org baseline allows: gpt-4o, gpt-4o-mini, claude-3-5-sonnet,  │    │
│  │  claude-haiku-4-5, gemini-2.5-pro, gemini-2.5-flash            │    │
│  │ This team further restricts to:                                │    │
│  │  ☑ gpt-4o-mini   ☑ claude-haiku-4-5   ☐ (others unchecked)     │    │
│  │  ⓘ A team can only narrow the org baseline, never re-enable a  │    │
│  │    model the Org Admin has banned.                              │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌─ Access Schedule Override ──────────────────────────[P3]──────┐    │
│  │ ☐ Override the org default access schedule for this team's      │    │
│  │   service accounts (org default: Mon-Fri 09:00-18:00)            │    │
│  │ Days: ☐M ☐T ☐W ☐T ☐F ☐S ☐S   Hours: [ __:__ ] to [ __:__ ]        │    │
│  │ ⓘ A team override can only be equal to or narrower than the org  │    │
│  │   default — never wider. Per-service-account overrides (in       │    │
│  │   Service Accounts) can still narrow this further.                │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                       │
│  ┌─ Alert Thresholds ──────────────────────────────────────────┐    │
│  │ Notify Team Lead + Org Admin at:  ☑ 80%   ☑ 100%              │    │
│  │ Delivery: ☑ Email   ☑ Webhook  [ https://hooks.slack.com/... ] │    │
│  └─────────────────────────────────────────────────────────────┘    │
└───────────────────────────────────────────────────────────────────────┘
```

**Add/Edit team modal:** name, budget ceiling (USD), org-baseline-derived
model-restriction checklist (pre-checked to match org baseline; admin
narrows from there — checkboxes for models the org baseline has already
denied are not shown at all, not shown-disabled, since they're categorically
unselectable).

**Add/Edit member modal:** name/email, role (Org Admin / Team Lead / Member
/ Auditor — **only Org Admin can assign the Org Admin or Auditor role**;
assigning "Team Lead" requires picking which team), budget within the
team's unallocated ceiling (the input must show the ceiling as a live
constraint, e.g., "Max: $190 (team has $190 unallocated)" and reject/clamp
over that — this mirrors the backend's assignment-time enforcement in
`phase-2-multi-tenant-governance.md` §2.2, not just a spend-time check).

**"Reassign budget between members" flow:** a simple two-select + amount
form (From member → To member → Amount), constrained to the team's own
members and ceiling, producing an audit trail entry (visible in Security &
Compliance → Audit Log, §10.3) recording old→new for both members.

**Behavior notes:**
- Team list row shows allocated-vs-ceiling as the primary signal (not
  spend-vs-ceiling) — allocation is the thing Org Admin actually manages
  here; spend-vs-budget lives on the Dashboard and inside each team.
- A user belonging to multiple teams is explicitly supported
  (`phase-2-multi-tenant-governance.md` §2.2 — resolved: a user *can*
  belong to more than one team, and each team-membership carries its
  **own** budget allocation, i.e., budget is per (user, team) pair, not a
  single global figure) — the member row must show which team's budget
  context is being displayed, and a user appearing in two teams' member
  lists is expected, not a bug.
- Removing a team requires reassigning or removing all its members first —
  block with a specific inline reason, same pattern as the Phase 1 user-delete
  block (`phase-1-admin-console-ui-requirements.md` §7.5).

## 9. Service Accounts & Keys `[P1, P2, P3]`

Extends Phase 1 (`phase-1-admin-console-ui-requirements.md` §7.6 — per-app
credentials, one-time secret reveal) with a **team-aware** "Attributed user"
picker for admin-minted app credentials, and, from Phase 2 (§2.5 of
`phase-2-multi-tenant-governance.md`), a second key type this screen must
now also surface: **self-serve personal keys** that Members/Team Leads
create themselves (§6 of the companion non-admin doc). Both key types live
in the same underlying credential system and the same list here — an Org
Admin's oversight must cover every key in the org regardless of who created
it, self-serve or admin-minted — but they're visually distinguished by an
**Owner** column so an admin can tell "this app's shared credential" apart
from "this specific person's own key" at a glance. From Phase 3, both types
also get automatic rotation + scheduled access windows.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Service Accounts & Keys      [ Filter: All ▾ ]  [ + Create app key ]  │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Name           Key          Owner         Rotation   Sched  ⋮   │  │
│  ├───────────────────────────────────────────────────────────────┤  │
│  │ billing-service  gk_sk_8f2a… App · ana@acme.co 90d·Sep4 Mon-Fri │⋮│
│  │                                 (cost center)              9-6  │ │
│  │ claude-cli        gk_sk_9b21…  Personal · ben@acme.co 90d Mon-Fri│⋮│
│  │                                 (self-serve)                9-6  │ │
│  │ support-bot         gk_sk_c91d…  App · ben@acme.co  Manual  Always│⋮│
│  │ legacy-cron (revoked) gk_sk_1ae0…  App · legacy       —      —   │⋮│
│  └───────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** "Filter" narrows to All / App keys / Personal keys — a large
org will accumulate many personal keys quickly (multiple per person, per
§2.5) and an admin auditing app credentials specifically shouldn't have to
scroll past all of them. "+ Create app key" (admin-only, unchanged from
Phase 1) mints an app/service credential; personal keys never get created
from this screen — they only ever originate from the key owner's own §6 (or
a Team Lead acting on their team member's behalf) so that "who can create a
personal key" stays exactly as scoped in the spec. Row ⋮ menu for a
personal key is narrower than for an app key: **Revoke** always available to
an Org Admin (oversight per §2.5's explicit requirement); **Regenerate** is
available to an Org Admin too, but doing so on someone else's personal key
should carry a stronger confirm than usual ("This regenerates \{name\}'s
own key — they'll need to update anything using it themselves") since,
unlike an app key, a human is depending on that exact secret.

**Rotation** `[P3]` — row ⋮ menu gains "Rotation settings," opening the same
modal introduced in §6 (interval, off-hours rotation time, overlap buffer,
"Rotate now"). Because these are Gatekey-issued secrets, rotation here **is**
fully automatic end-to-end: at the scheduled off-hours time, Gatekey mints a
new secret, delivers it via the one-time-reveal flow plus a notification to
the key's owner/team (email/webhook), and keeps the old secret valid for
only the short overlap buffer before auto-revoking it. For a **personal**
key, "off-hours" defaults to outside that person's access-schedule window if
one is set (§9's Sched. column already shows it), or the org's general
off-hours setting (§10.5) otherwise — timing the rotation to land when the
key is expected to be idle is what makes the short buffer safe, not a long
grace window. Default org-wide rotation is **off** — an admin opts a key
(or the whole org) into a schedule, rather than every existing key suddenly
starting to rotate the day this feature ships. Personal keys also pair with
a local sync helper (`phase-3-security-compliance.md` §3.7a) that fetches
the rotated key onto the user's own machine automatically — see the
companion non-admin doc §6 for that setup flow; this admin screen only
controls the server-side rotation policy, not how any individual user's CLI
picks the new key up.

**Scheduled access windows** `[P3]` — row ⋮ menu gains "Access schedule":

```
┌─────────────────────────────────────────┐
│  Access schedule — billing-service  ✕    │
│                                            │
│  ☑ Restrict to a schedule                  │
│  Days:   ☑M ☑T ☑W ☑T ☑F ☐S ☐S              │
│  Hours:  [ 09:00 ] to [ 18:00 ]  (org tz: America/New_York)│
│  Holidays: follows org holiday calendar    │
│           [ Edit org holiday calendar → ]  │
│                                            │
│  Outside this window, requests using this  │
│  key are blocked with a clear error.       │
│                                            │
│           [ Cancel ]  [ Save ]            │
└─────────────────────────────────────────┘
```

Inherits from the org default (Security & Compliance → Rotation & Access
Windows tab, §10.5) unless overridden here — same most-specific-wins
precedence as team model restrictions. "Sched" column in the list shows the
resolved effective window (e.g., "Mon-Fri 9-6" or "Always" if unrestricted),
not just whether an override exists, so an admin can scan the whole list
without opening each row. A blocked-by-schedule event is logged to the
audit trail (§10.3) exactly like a blocked-by-policy event, and an Org
Admin/Team Lead can grant a time-boxed emergency override from this same
modal:

```
┌─────────────────────────────────────────┐
│  Grant emergency override           ✕     │
│  billing-service                          │
│                                            │
│  Allow access until: [ Jul 23, 06:00 ▾ ]   │
│  Reason (required, goes to audit log):     │
│  ┌───────────────────────────────────┐   │
│  │ Incident #4821 on-call response     │   │
│  └───────────────────────────────────┘   │
│                                            │
│           [ Cancel ]  [ Grant override ]  │
└─────────────────────────────────────────┘
```

The reason field is required, not optional — an override is a deliberate
bypass of a security control and must be justified in the audit trail, same
as any other policy exception in this console.

## 10. Security & Compliance `[P3, P5]`

Five tabs: DLP, Residency, Audit Log/Ledger, Retention & Docs, Rotation &
Access Windows.

### 10.1 DLP `[P3]`

```
┌───────────────────────────────────────────────────────────────────────┐
│  Security & Compliance                                               │
│  [ DLP ] [ Residency ] [ Audit Log ] [ Retention & Docs ]             │
│  [ Rotation & Access Windows ]                          ← tabs        │
│  ─────────────────────────────────────────────────────────────────   │
│  Scan outbound prompts for:                                          │
│  ☑ SSNs   ☑ Credit card numbers   ☑ Email addresses   ☑ Phone numbers │
│                                                                       │
│  ┌─ Custom patterns ────────────────────────────────[+ Add pattern]┐ │
│  │ Name              Regex                            Action        │ │
│  ├─────────────────────────────────────────────────────────────────┤ │
│  │ Employee ID        EMP-\d{6}                        Redact        │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                                                                       │
│  Default action for built-in patterns:                               │
│  ( ) Log only   (•) Redact and forward   ( ) Block entirely            │
│                                                                       │
│  Scope: (•) Org-wide   ( ) Per-team override available                │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** action choice (log-only / redact / block) is explained
inline with a one-line consequence for each radio (e.g., "Block entirely —
the request never reaches the provider; the caller gets a clear error").
"Per-team override" surfaces a per-team action picker inside each team's
detail page (§8) rather than duplicating the whole DLP UI per team.

### 10.2 Residency `[P3]`

```
┌─ Residency ────────────────────────────────────────────────────────┐
│  Restrict which provider regions requests may reach.                │
│                                                                       │
│  ┌─────────────────────────────────────────────────[+ Add rule]───┐ │
│  │ Scope          Allowed regions          Violation behavior       │ │
│  ├─────────────────────────────────────────────────────────────────┤ │
│  │ Team: eu-ops    EU only (eu-west-1)      Hard block               │ │
│  │ Org-wide        No restriction            —                       │ │
│  └─────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** "Violation behavior" per rule toggles Hard block vs.
Warn-only (spec leaves this admin-configurable, default hard block per the
spec's stated bias). A rule referencing a team must use the same
team-picker component as elsewhere in this doc.

### 10.3 Audit Log `[P3]` → Audit Ledger `[P5]`

```
┌─ Audit Log ──────────────────────────────────────────────────────────┐
│  ⛓ Hash-chain: ✓ Verified — chain intact (last checked 2 min ago)     │
│                                              [ Verify now ] [ Export ]│
│  Filter: [ All actions ▾ ] [ All actors ▾ ] [ Last 30 days ▾ ]       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Time        Actor          Action              Old → New       │  │
│  ├───────────────────────────────────────────────────────────────┤  │
│  │ 14:02 Jul22 priya@acme.co   budget.reassign     $200 → $190     │  │
│  │ 09:41 Jul22 ana@acme.co     policy.team_model    +haiku          │  │
│  │ 08:15 Jul22 (system)        key.validation_fail  openai key      │  │
│  └───────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** Phase 3 ships this as a plain append-only log (no hash-chain
UI); Phase 5 adds the chain-integrity badge and "Verify now" button, which
runs the verification tool end-to-end and reports either the "intact"
state or the specific entry index where the chain broke — the failure
message must name the entry so an admin has somewhere to start
investigating, not just "verification failed." Every row here is what
other screens' "produces an audit trail entry" behavior (§8's budget
reassignment, §6's key add/remove, §7's policy changes) writes to — this is
the one screen that consumes all of them, so its filter dropdown should
list every action type this doc defines elsewhere, not a hardcoded few.

### 10.4 Retention & Docs `[P3]`

```
┌─ Retention & Docs ─────────────────────────────────────────────────┐
│  Log/prompt retention period:  [ 30 days ▾ ]   Auto-purge: ☑ On     │
│  Audit log retention (separate from usage data): [ 1 year ▾ ]        │
│                                                                       │
│  Compliance documentation:                                           │
│  [ 📄 Download data flow diagram ]  [ 📄 Download data handling policy ]│
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** two independent retention settings (usage/prompt data vs.
audit data) per the spec's explicit requirement that these be separable at
the infra level — never merge them into one control. Default retention
shown pre-filled at 30 days per the spec's stated safe default, not blank.

### 10.5 Rotation & Access Windows `[P3]`

Org-wide defaults for the two features detailed on the Providers (§6) and
Service Accounts (§9) screens, plus the shared holiday calendar both
inherit from.

```
┌─ Rotation & Access Windows ────────────────────────────────────────┐
│  Credential rotation (org default)                                  │
│  ☐ Auto-rotate service-account keys on a schedule                   │
│  Default interval: [ 90 days ▾ ]   Rotate at: [ 02:00 ▾ ] org time   │
│  Default overlap buffer: [ 5 minutes ▾ ]                             │
│  ⓘ Off by default — enabling this starts every key without its own   │
│    override rotating on this schedule going forward. Rotation is     │
│    timed off-hours by design, so the overlap only needs to cover      │
│    clock skew / a request in flight — not days of "time to notice."   │
│  Provider keys: rotation reminders only (guided manual flow, §6) —   │
│  ☑ Email me 14 days before a provider key's suggested rotation date. │
│                                                                       │
│  Scheduled access windows (org default)                              │
│  ☐ Restrict service-account keys to a schedule by default            │
│  Default days: ☑M ☑T ☑W ☑T ☑F ☐S ☐S   Default hours: 09:00–18:00     │
│  Org time zone: [ America/New_York ▾ ]                                │
│                                                                       │
│  ┌─ Holiday calendar ───────────────────────────────[+ Add date]───┐ │
│  │ Jan 1, 2026   New Year's Day                                     │ │
│  │ Jul 4, 2026    Independence Day                                   │ │
│  │ Dec 25, 2026   Christmas Day                                      │ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                                                                       │
│  Recent emergency overrides                                          │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ billing-service · until Jul 23, 06:00 · priya@acme.co ·          │  │
│  │  "Incident #4821 on-call response"                    [ Revoke ]│  │
│  └───────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** both org-default toggles start **off**, per the spec's
resolved open question — this is opt-in traffic-shaping like caching and
failover (§11), not a default every existing deployment suddenly inherits.
Team- and service-account-level overrides (§8, §9) inherit these org
defaults until explicitly overridden, using the same most-specific-wins
precedence used everywhere else in this doc; changing an org default here
should show a warning if any lower-level overrides already exist ("12
service accounts have their own rotation/schedule settings and won't be
affected by this change"), so an admin isn't surprised that a global toggle
didn't actually apply globally. The "Recent emergency overrides" list is a
short org-wide rollup of every override granted anywhere (Providers,
Service Accounts, or a Team Lead's own team) — gives an admin one place to
audit who's bypassing a schedule restriction and why, with a "Revoke" action
to end an override early if it's no longer warranted.

## 11. Reliability & Cost `[P4]`

Three tabs: Failover & Health, Rate Limits, Caching & Degradation.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Reliability & Cost                                                  │
│  [ Failover & Health ]  [ Rate Limits ]  [ Caching & Degradation ]    │
│  ─────────────────────────────────────────────────────────────────   │
│  Rate Limits tab:                                                    │
│  ┌─────────────────────────────────────────────────[+ Add limit]──┐  │
│  │ Scope          Limit                Behavior on hit             │  │
│  ├───────────────────────────────────────────────────────────────┤  │
│  │ Org default     100 req/min · 50k tok/min   Reject immediately  │  │
│  │ Team: ml-plat.   500 req/min · 250k tok/min   Queue & retry      │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Caching & Degradation tab:                                          │
│  Exact-match caching: ☑ Enabled org-wide   TTL: [ 15 min ▾ ]          │
│  ⓘ Per-team opt-out available in each team's detail page.             │
│  Semantic caching (near-duplicate detection): ☐ Enabled  [Beta]       │
│                                                                       │
│  Graceful cost degradation:                                          │
│  ☑ Auto-downgrade when a user is within [ 10% ▾ ] of budget           │
│  Downgrade target model: [ gpt-4o-mini ▾ ]                            │
│  ☑ Tag downgraded responses so calling apps can detect it             │
└───────────────────────────────────────────────────────────────────────┘
```

**Failover & Health tab** shows the same health-dot data surfaced compactly
on the Providers screen (§6), but as a dedicated timeline/log view: each
failover event with timestamp, from-key → to-key, and detection-to-switch
time (should read comfortably under the spec's 2-second NFR — flag in red
if any logged event exceeds it).

**Behavior notes:** every reliability control here defaults to the
spec-mandated safe default (caching off unless explicitly enabled per team
sensitivity concerns is *not* required — spec says caching is "opt-in per
team," so org-wide-enabled-with-team-opt-out, as wireframed, is the correct
default framing; failover defaults off per §6). The "Tag downgraded
responses" checkbox exists because the spec explicitly asks whether this
should be caller-detectable — default it **on**.

## 12. Differentiators `[P5]`

Three tabs: Shadow AI, Drift Detector, Self-Hosted Governance (the third tab
here is a thin cross-link — actual configuration lives on the Providers
screen's "Self-Hosted Models" card, §6; this tab surfaces the
cost-normalization audit view for it).

### 12.1 Shadow AI Discovery

```
┌─ Shadow AI ──────────────────────────────────────────────────────────┐
│  Detection source: (•) SASE/proxy log ingestion   ( ) Browser extension│
│  Enforcement: ( ) Detect only (recommended)   ( ) Block & redirect     │
│                                                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ User          Unsanctioned tool     Frequency    Last seen      │  │
│  ├───────────────────────────────────────────────────────────────┤  │
│  │ dan@acme.co    api.openai.com direct  14x/week    2 hrs ago      │  │
│  │ eve@acme.co    chat.deepseek.com       3x/week     1 day ago      │  │
│  └───────────────────────────────────────────────────────────────┘  │
│  ⓘ This feature collects [data described in the data-handling policy].│
│    Review before enabling. [ View policy ]                            │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** enforcement mode defaults to "Detect only" and switching to
"Block & redirect" requires an explicit confirm dialog quoting the spec's
own framing ("this is intrusive — are you sure?") given the spec's note
that this must be gated behind explicit org opt-in. The data-handling
disclosure link is not optional chrome — the spec calls this out as needing
its own reviewed policy before building; the UI should treat it with the
same weight as a legal consent screen, not a tooltip.

### 12.2 Drift Detector

```
┌─ Drift Detector ─────────────────────────────────────────────────────┐
│  Canary suite runs daily against actively-used models.                │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Model                  Status         Last checked   Trend      │  │
│  ├───────────────────────────────────────────────────────────────┤  │
│  │ gpt-4o                  ✓ Stable        6 hrs ago      ▁▁▂▁▁     │  │
│  │ claude-3-5-sonnet        ⚠ Drift detected 6 hrs ago      ▁▂▅▇▆     │  │
│  └───────────────────────────────────────────────────────────────┘  │
│  ▸ claude-3-5-sonnet — refusal rate up 34% vs. 30-day baseline         │
│    [ View canary history ]  [ Export to audit log ]                   │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** a drift alert states *what* changed (latency / refusal rate /
output-similarity score) in plain language with the percentage delta, not
just a red badge — an admin acting on this needs to know whether to escalate
to the provider or just note it. "Export to audit log" writes the alert into
§10.3's log, per the spec's requirement that drift alerts be exportable
alongside the audit trail.

## 13. Marketplace & Growth `[P6]`

Four tabs: Policy Packs, Budget Marketplace, ROI Attribution, Forecasting.
Frame the whole section header with the spec's own caveat: this phase is
"a menu, not a fixed scope" — the UI can ship all four tabs, but don't
imply any is mandatory to configure (each tab's empty state should read as
optional, e.g., "Not using budget marketplace — nothing to configure until
you install or list something").

```
┌───────────────────────────────────────────────────────────────────────┐
│  Marketplace & Growth                                                │
│  [ Policy Packs ]  [ Budget Marketplace ]  [ ROI ]  [ Forecasting ]   │
│  ─────────────────────────────────────────────────────────────────   │
│  Policy Packs tab:                                                    │
│  ┌───────────────────────┐ ┌───────────────────────┐                │
│  │ HIPAA Baseline          │ │ EU Residency + GDPR      │                │
│  │ by gatekey-community    │ │ by acme-org (private)     │                │
│  │ 3.2k installs · ★4.6     │ │ Internal, not published    │                │
│  │        [ Install ]       │ │        [ Edit ]             │                │
│  └───────────────────────┘ └───────────────────────┘                │
│  Installed packs apply as a starting point you can still customize   │
│  further in Model Policy / Security & Compliance.                    │
└───────────────────────────────────────────────────────────────────────┘
```

**Policy Packs tab:** marketplace-listing-card grid, Install opens a diff
preview ("this pack will set: DLP action = redact, residency = EU-only,
model policy = allowlist [...]") before applying — never silently overwrite
existing config. Every pack install/apply must land in the audit log (§10.3)
since it's equivalent to a bulk policy change. A vetting/trust signal
(verified-publisher badge, install count, rating) must be visible on every
card per the spec's trust-and-safety framing — this is a surface where a
malicious pack is a real risk, not just a style nicety.

**Budget Marketplace tab:** approval queue for cross-team transfer requests
raised by Team Leads (created in the companion doc's Team Lead flow) —
table of pending requests (from team, to team, amount, requested by) with
Approve/Deny, plus an **auto-approve threshold** setting ("auto-approve
under $___"). Every transfer, auto- or manually-approved, appends to a
transaction ledger table below the queue (reconciles against each team's
ceiling — show the running ceiling impact inline).

**ROI Attribution tab:** a short list of configured integrations (Jira,
GitHub, etc. — 1-2 per spec, not a generic framework), each showing a
correlation stat ("$1,240 spent · 38 tickets closed → $32.60/ticket") and a
"+ Connect integration" action limited to whichever 1-2 the org actually
uses.

**Forecasting tab:** the detail view the Dashboard's forecast panel (§5)
deep-links to — trend chart with a projected line extending past "today,"
plus a per-team breakdown table of projected-vs-ceiling so an admin can see
which team is likely to blow its budget before it happens, not just the
org aggregate.

## 14. Identity & Access `[P2, P3]`

```
┌───────────────────────────────────────────────────────────────────────┐
│  Identity & Access                                                    │
│                                                                       │
│  SSO Provider: (•) OIDC   Configured: Okta                            │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Client ID       [ ••••••••••••••••1a2b ]                        │  │
│  │ Client secret   [ ••••••••••••••••••••• ]  (set, never shown)   │  │
│  │ Issuer URL      [ https://acme.okta.com                    ]   │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                            [ Test connection ] [Save] │
│                                                                       │
│  SCIM provisioning:  ☑ Enabled                                        │
│  SCIM base URL:  https://gatekey.acme.internal/scim/v2   [ Copy ]     │
│  SCIM token:     [ Rotate token ]  (shown once on rotation, like §7.6  │
│                    of the Phase 1 doc's service-account pattern)      │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** "Test connection" performs a live OIDC discovery/handshake
check before saving — same structured-error pattern as provider key
validation (§6 / Phase 1 §7.4): distinguish "issuer unreachable" from
"credentials rejected" in the inline error. SCIM token rotation follows the
exact one-time-reveal pattern already established for service-account
secrets (Phase 1 doc §7.6) — reuse that component, don't reinvent it.

## 15. Settings `[P1, P2]`

```
┌─ Settings ─────────────────────────────────────────────────────────┐
│  Org name:  [ Acme Corp                    ]                        │
│  Currency:  [ USD ▾ ]   (all budgets normalize to this)              │
│  Admin credential:  [ Rotate admin token ]                            │
│                                                                       │
│  ── Personal API Keys (P2 §2.5) ──────────────────────────────────   │
│  ☐ Auto-provision a personal key for every user on first login        │
│  Max self-serve key expiration:  [ 180 days ▾ ]   ( ○ No max )         │
└───────────────────────────────────────────────────────────────────────┘
```

Minimal by design — most org-level configuration lives in its dedicated
screen elsewhere in this doc; this page is just identity + the credential-
rotation action that has nowhere else to live once SSO (§14) becomes the
primary login path for everyone else, plus the two org-wide toggles for
self-serve personal keys that don't belong on any single team's page:
auto-provisioning defaults to **off** (per §2.5's stated bias — don't
silently hand every new hire a live credential unless the org opts in),
and the max-expiration cap constrains what §6 of the companion non-admin
doc's "Create key" flow is allowed to offer.

## 16. Data Reference Additions

Extends `phase-1-admin-console-ui-requirements.md` §11 with shapes
introduced by later phases (field names are illustrative — no backend
endpoints exist yet for Phases 2–6, design against these mock shapes with an
obvious seam for real wiring, same convention as the Phase 1 doc uses for
its not-yet-built endpoints):

```
Team:      { id, name, budget_ceiling_usd, allocated_usd, spend_usd,
             period: "monthly"|"quarterly", on_period_end: "rollover"|"reset",
             model_restrictions: string[], access_schedule_override: AccessSchedule|null }
TeamMember:{ user_id, team_id, role: "org_admin"|"team_lead"|"member"|"auditor",
             budget_usd: number|null, spend_usd }
JoinRequest: { id, requester_name, requester_email, team_id,
              status: "pending"|"approved"|"rejected",
              requested_at, resolved_at: string|null,
              resolved_by_user_id: string|null,
              approved_budget_usd: number|null,  // set only when status = "approved"
              rejection_reason: string|null,
              routed_to: "team_lead"|"org_admin" }  // "org_admin" = fallback, no Team Lead on the team
ApiKey:    { id, name, key_prefix, key_type: "app"|"personal",
             owner_user_id, created_by_user_id, team_id: string|null,
             created_at, expires_at: string|null, revoked_at: string|null,
             rotation_policy: RotationPolicy|null, access_schedule: AccessSchedule|null }
             // key_type "app" = Phase 1.2's admin-minted, non-human-attributed
             // service credential; "personal" = Phase 2 §2.5's self-serve key,
             // where owner_user_id is the human who'll actually use it.
             // created_by_user_id differs from owner_user_id when a Team Lead
             // or Org Admin creates a personal key on someone else's behalf.
OrgKeySettings: { auto_provision_on_first_login: boolean,
                  max_self_serve_expiration_days: number|null }
ProviderKeyMulti: { id, provider, label, health: "healthy"|"degraded"|"down",
                    failover_enabled: boolean, failover_target_id: string|null }
SelfHostedProvider: { id, name, base_url, cost_basis_per_gpu_hour: number,
                      verified: boolean }
DlpRule:   { pattern_name, regex|builtin_type, action: "log"|"redact"|"block", scope }
ResidencyRule: { scope: "org"|team_id, allowed_regions: string[],
                 violation_behavior: "hard_block"|"warn" }
AuditEntry: { id, timestamp, actor, action, target, old_value, new_value,
              source_ip, chain_hash, prev_hash }
DriftAlert: { model, checked_at, status: "stable"|"drift", metric, delta_pct }
ShadowAiEvent: { user, tool, frequency_per_week, last_seen }
PolicyPack: { id, name, publisher, installs, rating, private: boolean, diff_preview }
BudgetTransferRequest: { from_team, to_team, amount_usd, requested_by, status,
                          auto_approved: boolean }
ForecastPoint: { date, projected_spend_usd, ceiling_usd }

RotationPolicy: { scope: "org"|provider_key_id|service_account_id,
                  enabled: boolean, interval_days: number,
                  rotate_at_local_time: string,   // e.g. "02:00" — off-hours anchor
                  overlap_buffer_minutes: number,  // short technical buffer, not a multi-day grace period
                  next_rotation_at: string|null,
                  last_rotated_at: string|null, mode: "automatic"|"manual_guided" }
                  // provider keys are always mode: "manual_guided" (see §6);
                  // service-account keys are always mode: "automatic" (see §9)
RotationEvent: { key_id, old_key_prefix, new_key_prefix, rotated_at,
                 overlap_ends_at, old_key_revoked_at: string|null }
LocalKeyCacheHint: { secret, valid_until }  // returned by the "get my current key" endpoint
                    // the local sync helper (spec §3.7a) caches this and only
                    // re-calls once `valid_until` has passed — see companion
                    // non-admin doc §6 for the client-side behavior this drives
AccessSchedule: { scope: "org"|team_id|service_account_id, enabled: boolean,
                  timezone: string, allowed_days: string[],
                  allowed_hours: { start: string, end: string },
                  holiday_calendar_ref: "org_default"|"custom" }
HolidayDate: { date: string, label: string }
EmergencyOverride: { id, scope: service_account_id, granted_by, reason: string,
                     granted_at: string, expires_at: string, revoked_at: string|null }
```
