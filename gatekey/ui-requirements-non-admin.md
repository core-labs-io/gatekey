---
title: Gatekey — UI Design Requirements (Non-Admin Roles, All Phases)
status: draft
last_updated: 2026-07-22
companion_doc: ui-requirements-admin.md
---

# Gatekey — Non-Admin UI Requirements (Team Lead, Member, Auditor)

This document specs everyone in Gatekey who **isn't** an Org Admin:
**Team Lead**, **Member**, and **Auditor** (roles defined in
`gatekey/phase-2-multi-tenant-governance.md` §2.1). It shares one design
system and one app with the companion doc,
[`ui-requirements-admin.md`](ui-requirements-admin.md) — same components,
same visual style, role-based nav/routing rather than a separate product.
Read that doc's §2 (Shared Design System) before this one; it isn't repeated
here.

**Important scoping note:** Phase 1 (`phase-1-core-gateway.md`) has **no
non-admin UI at all** — it is explicitly "single org admin role only," and
end users authenticate to the *gateway* (not a web console) via
service-account keys minted by the admin. Every screen in this document is
introduced by **Phase 2 or later**, once RBAC and human login (SSO) exist.
If you're building a Phase-1-only prototype, this entire document doesn't
apply yet — build it once Phase 2's roles exist.

## 1. Roles at a Glance

| Role | Can do | Cannot do |
|---|---|---|
| **Member** `[P2]` | View own usage/spend/budget, see which models are available to them and why, view team-level info they belong to (read-only), **self-serve create/regenerate/revoke their own personal API keys** `[P2.5]` | Change anything for other users; manage other users; see other teams' data; grant themselves broader access/budget than their account already has |
| **Team Lead** `[P2]` | Everything a Member can, plus: **approve/reject join requests to their team, with mandatory budget allocation on approval** `[P2.6]`, manage own team's members, reassign budget within team ceiling, set team-level model restrictions (narrowing only), set/narrow team access schedule and grant time-boxed emergency overrides for own team `[P3]`, **create/regenerate/revoke API keys on behalf of any member of their own team** `[P2.5]`, view team usage, list/request budget marketplace transfers `[P6]` | Touch org-wide settings, providers, other teams, RBAC beyond their own team; approve a budget allocation that exceeds their team's unallocated ceiling |
| **Auditor** `[P2, P3, P5]` | Read-only visibility into usage, logs, policy state, rotation/access-schedule configuration, and audit ledger integrity **across the whole org** (not just one team) | Change anything, anywhere |

A Team Lead is themselves a member of their own team, so their nav is the
Member nav **plus** a "My Team" section — it is additive, not a different
app. Auditor is a separate, narrower nav (read-only, org-wide).

## 2. Login & Onboarding `[P2]`

SSO only for non-admin roles (per `phase-2-multi-tenant-governance.md` §2.1
— OIDC via Okta/Azure AD/Google Workspace). There is no shared-token login
here; that pattern is Org-Admin-only and lives in the companion doc.

```
┌───────────────────────────────────────────────────────────────────────┐
│                                                                       │
│                         ⛨  Gatekey                                   │
│                    Sign in to Acme Corp's gateway                    │
│                                                                       │
│                  ┌─────────────────────────────┐                     │
│                  │   Continue with Okta         │                     │
│                  └─────────────────────────────┘                     │
│                                                                       │
│                  Having trouble? Contact your Gatekey admin.          │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** single SSO button (provider name pulled from the org's
Identity & Access config in the companion doc §14) — no username/password
field exists for these roles. If SCIM (§2.1 of the spec) has already
resolved the user's team membership, they land straight in the app (§3
onward). Otherwise — no SCIM, or SCIM didn't map this user to a team — a
first-time login instead routes into the onboarding flow below (§2.1, §2.2)
before any gateway access is granted.

### 2.1 First-Time Setup: Profile & Team Selection `[P2.6]`

```
┌───────────────────────────────────────────────────────────────────────┐
│                                                                       │
│                    Welcome to Gatekey                                │
│           A couple of details before you can get started.            │
│                                                                       │
│  Full name                                                           │
│  ┌─────────────────────────────────────────┐                        │
│  │ Ben Torres                                │  ← pre-filled from SSO │
│  └─────────────────────────────────────────┘                        │
│                                                                       │
│  Team                                                                 │
│  ┌─────────────────────────────────────────┐                        │
│  │ Select your team...                    ▾  │                        │
│  └─────────────────────────────────────────┘                        │
│  ⓘ This list is set by your org admin. Don't see your team? Ask       │
│    your admin to add it first.                                       │
│                                                                       │
│                                    ┌─────────────────────────────┐   │
│                                    │   Submit for approval →      │   │
│                                    └─────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** Full name pre-fills from the SSO identity claim (editable, not
locked — an IdP display name isn't always what someone wants shown).
"Team" is a single-select dropdown of the org's existing teams only — a
user cannot type a new team into existence here; that list is entirely
admin-managed (companion doc §8, "+ Add team"). Submitting does **not**
grant access — it creates a pending join request (§2.6 of the spec) and
immediately moves to §2.2 below. Nothing about role is chosen here; every
self-service join request defaults to the **Member** role — only an Org
Admin can later promote someone to Team Lead/Auditor (companion doc §8).

### 2.2 Pending Approval `[P2.6]`

```
┌───────────────────────────────────────────────────────────────────────┐
│                                                                       │
│                      ⏳  Request submitted                            │
│                                                                       │
│         Your request to join ml-platform is awaiting approval        │
│                    from that team's Team Lead.                       │
│                                                                       │
│                  You'll get access as soon as it's approved —        │
│                  no action needed from you right now.                │
│                                                                       │
│                              [ Submitted Jul 22, 2026, 10:14 ]        │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** this replaces any dashboard/nav until the request resolves —
a pending user has no gateway access and no self-serve key creation is
possible yet (§6 needs a team/budget context to inherit, which doesn't
exist pre-approval). If **rejected**, this screen updates in place to show
the rejection and, if the approver left one, their reason, plus a
**"Choose a different team"** action that routes back to §2.1 — rejection
is not a dead end. If the team the user picked has no Team Lead assigned at
all, the copy substitutes "awaiting approval from your org admin" (routing
fallback per the spec's §2.6) rather than naming a role nobody holds.

## 3. Global Shell

Same shell pattern as the companion doc (sidebar + top bar), but nav
contents are role-scoped. No org name/switcher chrome needed here — a
non-admin user only ever sees their own org.

```
Member nav:              Team Lead nav:            Auditor nav:
┌───────────────┐        ┌───────────────┐         ┌───────────────┐
│ My Usage       │        │ My Usage       │         │ Org Usage      │
│ Model Access   │        │ Model Access   │         │ Org Logs       │
│ My API Keys    │        │ My API Keys    │         │ Policy Viewer  │
│                │        │                │         │ Audit Ledger   │
│                │        │ ── My Team ──   │         │                │
│                │        │ Join Requests  │         │                │
│                │        │ Team Dashboard │         │                │
│                │        │ Members &      │         │                │
│                │        │  Budgets       │         │                │
│                │        │ Model          │         │                │
│                │        │  Restrictions  │         │                │
│                │        │ Access         │         │                │
│                │        │  Schedule      │         │                │
│                │        │ Budget         │         │                │
│                │        │  Marketplace   │         │                │
└───────────────┘        └───────────────┘         └───────────────┘
```

## 4. My Usage `[P2]`

Personal landing page for Member and Team Lead — "what am I spending, what's
my budget, am I close to a limit." This is the non-admin analog of the
companion doc's org Dashboard, scoped to one person.

```
┌───────────────────────────────────────────────────────────────────────┐
│  My Usage                                       Time range: [7 days ▾]│
│                                                                       │
│  ┌───────────────────────────────┐  ┌────────────────────────────┐  │
│  │ My budget                       │  │ Requests this period         │  │
│  │ $62.10 spent of $100.00          │  │ 3,204                        │  │
│  │ ████████░░░░░░░░  62%             │  │                              │  │
│  │ Resets Aug 1, 2026                │  │                              │  │
│  └───────────────────────────────┘  └────────────────────────────┘  │
│                                                                       │
│  Spend by model                                                      │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ gpt-4o-mini            ███████████ $38.20                       │  │
│  │ claude-haiku-4-5        ████ $23.90                              │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  My API keys                                       (manage — see §6) │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** if the viewer's budget is null (unmetered), render "Unmetered
— no spend cutoff" instead of a progress bar, matching the same rule the
companion doc's budget-bar component uses. Approaching/at-limit states use
the same amber/red badges as the admin doc's Users screen ("Near limit" ≥
90%, "Budget exhausted" at 100%) so the visual language is identical across
both docs. Nothing on this page is editable — a Member cannot change their
own budget; that's a Team Lead/Org Admin action elsewhere.

## 5. Model Access `[P2]`

Directly answers the success criterion in
`phase-2-multi-tenant-governance.md` §2.3: "a user should be able to see
*why* a model is unavailable to them." This is a read-only render of the
**policy precedence trace** component defined in the companion doc (§2),
scoped to the viewer.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Model Access                                                        │
│  Which models you can currently use, and why.                        │
│                                                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ ✓ gpt-4o-mini            Available                                │  │
│  │ ✓ claude-haiku-4-5        Available                                │  │
│  │ ✕ gpt-4o                  Blocked — restricted by your team        │  │
│  │                             (ml-platform) beyond the org baseline. │  │
│  │ ✕ claude-opus-5           Blocked — org-wide policy does not        │  │
│  │                             allow this model.                       │  │
│  │ ✕ gemini-2.5-pro          Blocked — flagged for PII-sensitive        │  │
│  │                             content only; your last request wasn't  │  │
│  │                             classified as sensitive.                 │  │
│  └───────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** every blocked row states the specific layer that blocked it
(org baseline / team restriction / content-classification rule from the
companion doc §7) in plain language — never a bare "blocked." This is the
single most important screen in this document for the spec's stated
non-admin success bar; get the wording specific per layer, don't collapse
all three into one generic "restricted by policy" line.

## 6. My API Keys `[P2, P3, P3.7a]`

**Self-service**, per `phase-2-multi-tenant-governance.md` §2.5 — the whole
point of this section is that a Member (or anyone) generates and manages
their own personal API keys without filing a ticket to an admin. This
supersedes the earlier admin-only-creation assumption: creation,
regeneration, and revocation of a key **attributed to yourself** are all
self-serve actions on this screen. (Admin-minted app/service credentials
from Phase 1 §1.2 still exist and are still admin-only to create — those
are a different thing: a non-human app identity, not a person's own key.
This screen is about the latter.)

```
┌───────────────────────────────────────────────────────────────────────┐
│  My API Keys                                        [ + Create key ] │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Name          Key           Expires      Status    Sched.  ⋮   │  │
│  ├───────────────────────────────────────────────────────────────┤  │
│  │ claude-cli      gk_sk_8f2a…   90 days      Active   Mon-Fri  ⋮   │  │
│  │                                                        9-6      │  │
│  │ local-dev        gk_sk_c91d…   Never         Active   Always   ⋮   │  │
│  └───────────────────────────────────────────────────────────────┘  │
│  ⓘ Keys are bound by your account's model access and budget — see    │
│    Model Access (§5). Creating a key never grants more than you       │
│    already have.                                                     │
└───────────────────────────────────────────────────────────────────────┘
```

**"Create key" flow — same two-step, one-time-reveal pattern already
established for admin-minted service accounts** (companion doc §9): name
the key, optionally set an expiration, then the plaintext secret is shown
exactly once with a quick-start snippet for the stated use case.

```
Step 1 — form                              Step 2 — one-time reveal + quick start
┌─────────────────────────────────────┐   ┌─────────────────────────────────────────┐
│  Create API key                 ✕     │   │  Save this key now                   ✕   │
│                                        │   │  ⚠ This is the only time you'll see it.  │
│  Name                                 │   │                                            │
│  ┌───────────────────────────────┐   │   │  ┌───────────────────────────────────┐   │
│  │ e.g. claude-cli                  │   │   │  │ gk_sk_8f2a91c...e93f      [Copy]   │   │
│  └───────────────────────────────┘   │   │  └───────────────────────────────────┘   │
│                                        │   │                                            │
│  Expires                              │   │  Quick start:                             │
│  ( ) No expiration                    │   │  export GATEKEY_BASE_URL=https://gatekey. │
│  (•) 90 days                          │   │    acme.internal/v1                        │
│  ( ) Custom date                      │   │  export GATEKEY_API_KEY=gk_sk_8f2a...      │
│  ⓘ Org max: 180 days                  │   │  [ Copy snippet ]                          │
│                                        │   │                                            │
│           [ Cancel ]  [ Create ]      │   │      [ I've saved it, close ]              │
└─────────────────────────────────────┘   └─────────────────────────────────────────┘
```

**Behavior & states:**
- "Expires" options are clamped to whatever the org's max-allowed-expiration
  setting is (companion doc's Identity & Access or Settings screen); if the
  org caps at 180 days, "No expiration" simply isn't offered as an option
  rather than being offered and then silently overridden.
- Row ⋮ menu on each key: **Regenerate** and **Revoke**. Regenerate is a
  self-initiated, immediate swap — no overlap at all, not even the short
  buffer scheduled rotation uses — with a plain confirm: "Regenerating
  claude-cli invalidates the current secret immediately." If the key has
  CLI Auto-Sync connected (§6.1), that confirm changes to "...your CLI
  will pick up the new one automatically next time you use it," since
  there's nothing manual left to warn about; otherwise it keeps the
  original "anywhere it's still configured will start failing until you
  update it" wording. Revoke uses the same destructive-confirm pattern as
  everywhere else in this doc.
- "Sched." column shows the effective access-schedule restriction (§7.5 in
  the Team Lead section, or the org default) exactly as before — self-serve
  creation doesn't change how schedule/budget/model-policy apply, it only
  changes who can press "create."
- Every create/regenerate/revoke here writes to the audit trail per
  `phase-2-multi-tenant-governance.md` §2.5, same as an admin-minted key.
- **Compatibility caveat worth surfacing in the quick-start copy itself**:
  the exact env-var names/snippet depend on which CLI a user is pointing at
  Gatekey and what wire protocol that CLI expects — Gatekey's committed
  guarantee is an OpenAI-compatible surface (`00-overview.md`), and the
  resolved default (`phase-2-multi-tenant-governance.md` §2.5) is to build
  against that surface only, not a speculative second passthrough for
  CLIs with a different native shape — that gets evaluated only if a real
  design partner actually hits it. Practically: don't hardcode a specific
  CLI's exact env var names in the wireframe copy until a given CLI is
  confirmed to work against the OpenAI-compatible surface — the mock above
  uses generic
  `GATEKEY_BASE_URL`/`GATEKEY_API_KEY` placeholders for that reason.

### 6.1 CLI Auto-Sync (recommended) `[P3.7a]`

Copy-pasting a key into an env var (§6 above) still works, but it's the
manual path — it's what breaks the moment that key rotates. This is the
**one-time setup** for the local sync helper from
`phase-3-security-compliance.md` §3.7a, which keeps a personal key's
on-disk value current automatically, including through rotation, with no
copy-paste ever needed again.

```
┌───────────────────────────────────────────────────────────────────────┐
│  claude-cli · CLI Auto-Sync                                          │
│                                                                       │
│  ○ Not connected                                                      │
│  Connect once so this key stays current on your machine automatically │
│  — including after it rotates — with nothing for you to do.           │
│                                                                       │
│                                    [ Connect this key to my CLI → ]    │
└───────────────────────────────────────────────────────────────────────┘
```

**Setup flow (one-time):**

```
Step 1                                      Step 2
┌─────────────────────────────────────┐   ┌─────────────────────────────────────────┐
│  Connect CLI Auto-Sync           ✕    │   │  Connected ✓                          ✕   │
│                                        │   │                                            │
│  Run this once in your terminal:      │   │  claude-cli will now stay in sync         │
│  ┌───────────────────────────────┐   │   │  automatically — including through         │
│  │ gatekey login                    │   │   │  rotation. Nothing else to do.            │
│  └───────────────────────────────┘   │   │                                            │
│                                        │   │  Use it same as always:                    │
│  This opens a browser to confirm it's │   │  ┌───────────────────────────────────┐   │
│  really you (device-style login) and  │   │  │ claude                             │   │
│  stores your access securely in your  │   │  └───────────────────────────────────┘   │
│  system's credential store — never a  │   │                                            │
│  plaintext file.                       │   │      [ Done ]                              │
│                                        │   │                                            │
│           [ Cancel ]  [ Waiting... ]  │   │                                            │
└─────────────────────────────────────┘   └─────────────────────────────────────────┘
```

Back on the key list (§6), a connected key's row reflects it:

```
┌───────────────────────────────────────────────────────────────────────┐
│  My API Keys                                        [ + Create key ] │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Name        Key          Expires  Status  Sched.    Sync    ⋮   │  │
│  ├───────────────────────────────────────────────────────────────┤  │
│  │ claude-cli   gk_sk_8f2a…  90 days  Active  Mon-Fri   ✓ Auto  ⋮   │  │
│  │                                              9-6               │  │
│  │ local-dev     gk_sk_c91d…  Never    Active  Always    Manual  ⋮   │  │
│  └───────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:**
- "Sync: Manual" is the default for every key until this flow is completed
  for it — no auto-sync is assumed just because a key was created.
- After setup, the local helper caches the current secret plus a
  `valid_until` hint and only re-checks with Gatekey once that expires
  (spec §3.7a) — practically, once per day at most, timed to land after
  that key's off-hours rotation window, not on every command. This is why
  the "no manual change required" promise holds even through rotation: the
  first `claude` invocation each working day silently refreshes if needed,
  and every invocation after that in the same day is instant with no
  network call.
- If a key is force-revoked outside the normal schedule (compromise
  response), the next invocation gets a clean auth error from the gateway,
  which the helper treats as "cache is stale" and silently re-fetches once
  — the user sees, at worst, one command needing to be re-run, never a
  cryptic failure.
- "Disconnect" (row ⋮ menu) revokes the helper's stored access without
  touching the key itself — useful when someone's switching machines, not
  something they'd need often.
- This is genuinely a piece of local client software the user installs
  once (not just a web UI action) — the wireframe above assumes it exists
  and is invoked from this screen, but building it is real scope beyond
  the console itself (`phase-3-security-compliance.md` §3.7a builds it on
  a cross-platform credential-storage abstraction from day one rather than
  one OS first, but it's still separate engineering effort from the web
  console these two docs otherwise describe).

## 7. Team Lead — My Team Section `[P2, P6]`

Everything below is additive on top of §§4–6, visible only to Team Leads,
scoped strictly to their own team.

### 7.1 Join Requests & Approvals `[P2.6]`

The Team Lead's side of §2.6 (`phase-2-multi-tenant-governance.md`) — this
is where a pending request from §2.2 actually gets resolved. Put this
first in the Team Lead's nav, not buried after the dashboard — it's the
one screen with a real queue that needs regular attention, closer to an
inbox than a settings page.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Join Requests · ml-platform                                         │
│  Team unallocated budget: $190 of $2,500 ceiling                      │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Ben Torres · requested Jul 22, 10:14        [ Reject ] [ Approve ]│ │
│  │ Dana Kim · requested Jul 21, 15:02          [ Reject ] [ Approve ]│ │
│  └───────────────────────────────────────────────────────────────┘  │
│  Recently resolved                                                    │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Cy Nguyen · approved Jul 20 · $200 allocated                     │  │
│  │ Eve Park · rejected Jul 19 · "wrong team, meant support-eng"      │  │
│  └───────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

**Approve flow** — a single modal that combines the decision with the
mandatory budget allocation the spec requires:

```
┌─────────────────────────────────────────┐
│  Approve Ben Torres                 ✕     │
│                                            │
│  Budget for this member (USD) *            │
│  ┌───────────────────────────────────┐   │
│  │                                     │   │
│  └───────────────────────────────────┘   │
│  ⓘ Max: $190 (team has $190 unallocated   │
│    of its $2,500 ceiling)                  │
│                                            │
│           [ Cancel ]  [ Approve & allocate ] │
└─────────────────────────────────────────┘
```

**Reject flow** — a lighter modal, no budget involved:

```
┌─────────────────────────────────────────┐
│  Reject Ben Torres's request        ✕     │
│                                            │
│  Reason (optional — shown to the requester)│
│  ┌───────────────────────────────────┐   │
│  │                                     │   │
│  └───────────────────────────────────┘   │
│                                            │
│           [ Cancel ]  [ Reject request ]  │
└─────────────────────────────────────────┘
```

**Behavior:** the budget field on Approve is **required** — "Approve &
allocate" stays disabled until a value is entered — and is clamped to the
team's live unallocated headroom exactly like every other budget-entry
field in these two docs (companion doc §8's "Add/Edit member" modal, this
doc's §7.3 reassignment flow); typing an amount over the shown max either
rejects the input or clamps it, never silently overspends the ceiling.
Approving creates the team membership and budget allocation in one atomic
action — there's no in-between state where someone is "approved but
unbudgeted." Reject's reason field is optional, but if filled in, it's what
the requester sees on their §2.2 Pending Approval screen when it updates.
Both actions log to the audit trail (companion doc §10.3) and, if the
request originated because the team had no Team Lead and routed to an Org
Admin instead (spec's fallback case), that same modal appears on the
companion doc's Teams & Users screen for the Org Admin rather than here.

### 7.2 Team Dashboard

Same layout as the companion doc's Dashboard (§5 there) but scoped to one
team and with no cross-team comparison — a Team Lead should never see
another team's numbers here (that's Auditor-only, §8).

```
┌───────────────────────────────────────────────────────────────────────┐
│  ml-platform · Team Dashboard                  Time range: [7 days ▾]│
│  ┌─────────┐┌─────────┐┌─────────┐┌─────────┐                       │
│  │ Team     ││ Requests ││ Allocated││ Members  │                       │
│  │ spend     ││          ││ vs ceiling││          │                       │
│  │ $2,000    ││ 8,120    ││ 80%      ││ 12       │                       │
│  └─────────┘└─────────┘└─────────┘└─────────┘                       │
│  Spend by member (top 5) · Spend by model                            │
└───────────────────────────────────────────────────────────────────────┘
```

### 7.3 Members & Budgets

This is the Team-Lead-scoped subset of the companion doc's Teams & Users
screen (§8 there, "Members" table + "Reassign budget" flow) — **identical
table and reassignment modal**, just pre-filtered to one team and without
the "Add team" / cross-team affordances an Org Admin has. Reuse that
component as-is; do not redesign it.

```
┌───────────────────────────────────────────────────────────────────────┐
│  ml-platform · Members                              [ + Add member ] │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Name         Budget    Spent      Status    Actions             │  │
│  ├───────────────────────────────────────────────────────────────┤  │
│  │ ben@acme.co   $300     $294 (98%) ⚠ Near limit  ⋮                │  │
│  │ cy@acme.co    $200     $ 40 (20%) Active     ⋮                  │  │
│  └───────────────────────────────────────────────────────────────┘  │
│  [ Reassign budget between members ]                                 │
└───────────────────────────────────────────────────────────────────────┘
```

**Constraint to enforce visibly:** the budget-reassignment amount field
must be clamped to the team's own ceiling (never let a Team Lead type an
amount that would push the team over its Org-Admin-set ceiling) — same
enforcement as the companion doc §8, just without the ability to *change*
the ceiling itself (only an Org Admin can raise a team's ceiling).

Each member row's ⋮ menu includes **"Manage API keys"** — per
`phase-2-multi-tenant-governance.md` §2.5, a Team Lead can create,
regenerate, or revoke keys on behalf of any member of their own team. This
opens the same key list/create/regenerate/revoke component from §6 (My API
Keys), scoped to the selected member instead of the viewer themselves —
reuse that component, don't rebuild it. The one difference: since the Team
Lead isn't the one who'll use the resulting secret, the one-time-reveal
step should offer **"Send to \{member\}"** (e.g., email the one-time
value directly to the key's owner) as an alternative to "Copy," so the
secret doesn't have to be relayed by hand through a side channel.

### 7.4 Team Model Restrictions

Same component as the companion doc §8's "Team Model Restrictions" card —
Team Lead can narrow further within whatever the org baseline currently
allows, cannot re-enable anything the Org Admin banned (checkboxes for
org-banned models are simply absent, not shown-disabled, matching the
companion doc's exact rule).

### 7.5 Access Schedule `[P3]`

Same "Access Schedule Override" component as the companion doc §8's team
detail page — a Team Lead can set (or narrow) the days/hours their team's
service-account keys are allowed to authenticate, on top of whatever the
Org Admin's org-wide default already restricts (narrowing only, same
non-loosening rule as §7.4's model restrictions — a team override can never
be *wider* than the org default).

```
┌───────────────────────────────────────────────────────────────────────┐
│  ml-platform · Access Schedule                                       │
│  Org default: Mon-Fri, 09:00-18:00 (America/New_York)                 │
│                                                                       │
│  ☐ Narrow further for this team's service accounts                    │
│  Days: ☐M ☐T ☐W ☐T ☐F ☐S ☐S   Hours: [ __:__ ] to [ __:__ ]           │
│                                                                       │
│  ┌─ Emergency overrides for this team ─────────────[+ Grant override]┐│
│  │ billing-service · until Jul 23, 06:00 · "Incident #4821 on-call"  ││
│  │                                                        [ Revoke ] ││
│  └─────────────────────────────────────────────────────────────────┘│
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** per the companion doc's resolved open question
(`phase-3-security-compliance.md` §3.8), a Team Lead may grant a
time-boxed emergency override **only for their own team's service
accounts** — the same "Grant emergency override" modal from the companion
doc §9 (allow-until timestamp + required reason, since it's a logged
security-control bypass), scoped here to this team. An Org Admin can grant
or revoke an override for any team from the companion doc's Security &
Compliance → Rotation & Access Windows tab (§10.5 there); this Team Lead
view only ever shows and manages its own team's overrides.

### 7.6 Budget Marketplace `[P6]`

Team Lead's side of the companion doc's Marketplace & Growth → Budget
Marketplace approval queue (§13 there). A Team Lead **lists** surplus and
**requests** access to another team's surplus; approval (auto or manual)
happens on the Org Admin side.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Budget Marketplace                                                  │
│  Your team's unallocated budget: $190                                │
│                                                                       │
│  [ List $__ as available ]                                            │
│                                                                       │
│  Available from other teams:                                         │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ support-eng     $80 available     [ Request ]                    │  │
│  │ growth-marketing $40 available     [ Request ]                    │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                       │
│  Your pending requests:                                              │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Requested $50 from support-eng · Jul 20 · ⏳ Awaiting approval    │  │
│  └───────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** "Request" opens a small amount-entry modal; submitting writes
a `BudgetTransferRequest` (shape defined in the companion doc §16) that
appears in the Org Admin's approval queue. If the org's auto-approve
threshold covers the amount, the request should visibly resolve to
"Approved" quickly rather than sitting in "Awaiting approval" indefinitely —
don't hide the auto-approve behavior from the requester.

## 8. Auditor `[P2, P3, P5]`

Read-only, org-wide. No "My Usage" framing — an Auditor isn't consuming the
gateway themselves in this role, they're reviewing it. Three screens.

### 8.1 Org Usage (read-only)

Identical visual layout to the companion doc's Dashboard (§5 there),
rendered fully read-only (no config affordances, no forecast-tab deep link
since Forecasting is an Org-Admin planning tool, not an audit concern) —
reuse the component, strip interactive controls.

### 8.2 Org Logs (read-only)

Same table as the companion doc's Audit Log tab (§10.3 there) plus
per-request logs (user, model, provider, tokens, cost, latency, timestamp,
success/failure — the Phase 1 log schema), with export to CSV/JSON, exactly
as specified in `phase-3-security-compliance.md` §3.1 ("queryable and
exportable... by Org Admin and Auditor roles").

```
┌───────────────────────────────────────────────────────────────────────┐
│  Org Logs                                            [ Export ▾ ]    │
│  [ Audit events ]  [ Request log ]  ← tabs                            │
│  Filter: [ All actions ▾ ] [ All teams ▾ ] [ All users ▾ ] [30 days ▾]│
│  (same table components as companion doc §10.3, read-only, org-wide)  │
└───────────────────────────────────────────────────────────────────────┘
```

### 8.3 Policy Viewer (read-only)

Org-wide version of §5's Model Access screen — instead of "what's available
to me," it's "what's the full precedence stack for every team," so an
Auditor can review the whole policy surface at once without impersonating a
user.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Policy Viewer                                                       │
│  Org baseline: Denylist — claude-opus-5, gemini-2.5-pro blocked        │
│  Org access schedule default: Mon-Fri 09:00-18:00 (America/New_York)  │
│                                                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Team           Further restricts to        DLP    Access sched. │  │
│  ├───────────────────────────────────────────────────────────────┤  │
│  │ ml-platform     gpt-4o-mini, haiku only      Redact  Mon-Fri 9-6 │  │
│  │ support-eng      (no further restriction)      Log    Org default │  │
│  │ growth-marketing  gpt-4o-mini only              Block  Mon-Fri 9-5│  │
│  └───────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

### 8.4 Audit Ledger Verification `[P5]`

The spec's explicit Auditor use case for the Phase 5 hash-chained ledger:
"a pilot org uses the audit ledger verification tool to confirm chain
integrity... as part of a real (or simulated) compliance exercise"
(`phase-5-differentiators.md` §5.2, success criteria). Same
hash-chain-integrity badge and "Verify now" action as the companion doc
§10.3, surfaced here as a standalone, exportable verification report rather
than buried inside a log table — an Auditor needs something they can hand
to an external reviewer.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Audit Ledger Verification                                           │
│  ⛓ Chain status: ✓ Verified intact — 14,204 entries, genesis Jan 3 2026│
│  Last verified: Jul 22, 2026, 09:14 by priya@acme.co                  │
│                                                                       │
│                    [ Run verification now ]  [ Download report (PDF) ]│
└───────────────────────────────────────────────────────────────────────┘
```

## 9. Cross-Cutting Behavior Notes

- **The only editable surfaces in this document are:** §6 (every role
  self-serve manages their own API keys), and the Team-Lead-scoped actions
  in §7 — 7.1 approving/rejecting join requests (with mandatory budget
  allocation on approve), 7.3 budget reassignment within ceiling *and*
  managing API keys on behalf of their team's members, 7.4 narrowing team
  model restrictions, 7.5 narrowing the team access schedule and
  granting/revoking time-boxed emergency overrides for their own team, and
  7.6 marketplace list/request. Every other screen is read-only by design —
  if an AI building this prototype finds itself adding a save button somewhere else
  in this doc, that's a scope error against the spec, not a missing
  feature to fill in.
- **Empty/loading/error conventions** are identical to the companion doc
  §12 (skeleton loading, specific empty states with a next action, inline
  structured errors) — don't invent a second convention for non-admin
  screens.
- **No org-switching, no role-switching UI.** A user's role and team
  membership are assigned by an Org Admin/SCIM, not self-selected anywhere
  in this document.
