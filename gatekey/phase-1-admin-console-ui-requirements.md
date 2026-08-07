---
title: Gatekey — Phase 1 Admin Console UI Design Requirements
status: draft
last_updated: 2026-07-22
---

# Gatekey Admin Console — UI Design Requirements (Phase 1)

This document specifies the UI for Gatekey's **Phase 1 admin console** — the
minimal web UI a self-hosted org admin uses to configure and monitor the
gateway. It is written to be handed to an AI coding tool to produce a working
prototype (React/Next.js, per the project's decided stack — see
`tech-stack.md`).

## 1. Product Context

Gatekey is a self-hosted proxy that sits between a company's internal apps
and AI providers (OpenAI, Anthropic, Google Vertex AI). It never performs
inference itself. The admin console is the control plane: an org admin
configures provider keys, issues app credentials, sets which models are
allowed, sets per-user budgets, and watches spend.

Phase 1 has **exactly one role** (org admin, single shared admin credential —
no RBAC yet) and **exactly one org** (no multi-tenant signup flow). Design
for that reality — don't build org-switcher chrome or role pickers that
don't apply yet; Phase 2 adds teams/RBAC/SSO on top of this shell later.

## 2. Scope

In scope for this prototype (Phase 1 spec, `phase-1-core-gateway.md` §1.6):

1. Login
2. First-run setup wizard (first admin + first provider key)
3. Dashboard — usage summary (by user, by model, over a time range)
4. Provider key management (OpenAI, Anthropic, Vertex AI)
5. User management (add/remove users, set per-user budget, view spend)
6. Service account key management (per-app credentials, one-time secret reveal)
7. Org-wide model access policy (allowlist/denylist)

**Explicitly out of scope** (do not design for these; they arrive in later
phases and their presence would misrepresent what Phase 1 does): teams,
roles beyond a single admin, SSO/SCIM, budget rollover, DLP/PII redaction,
data residency, caching/rate-limit/failover controls, audit ledger UI beyond
a simple activity list.

**Backend status note:** the users and usage-dashboard endpoints described
below are specified but **not yet implemented** in the backend (only
`/v1/admin/providers`, `/v1/admin/model-policy`, and
`/v1/admin/service-accounts` exist today). Build those two screens against
mocked/static data with an obvious seam for wiring to a real API later; build
Providers, Service Accounts, and Model Policy against the real response
shapes in §11, since those endpoints already exist.

## 3. Primary User & Use Case

One persona: an **org admin** — a technical operator (DevOps/platform
engineer, not an end-user consumer) standing up Gatekey for their company and
returning periodically to add providers, onboard app teams, and check spend.
Optimize for a dense, fast, keyboard-and-table-friendly internal tool, not a
consumer marketing feel. Think Stripe Dashboard / Vercel / Supabase admin
panels as the visual reference point, not a SaaS landing page.

## 4. Design Principles

- **Dense over spacious.** This is an operator tool used repeatedly, not a
  first-time-user funnel. Favor compact tables and inline actions over large
  cards and multi-step flows, except where a step genuinely needs isolation
  (secret reveal, destructive delete).
- **Secrets are shown once, then never again.** Any screen that displays a
  plaintext secret (service account creation, provider key entry) must make
  the one-time nature unmissable — a distinct modal state, a persistent
  "copy before you close this" warning, no way to re-reveal later.
- **State precedence must be legible.** When a model is unavailable, or a
  request would be blocked, the UI should make it obvious *why* (policy
  mode, budget exhausted, key not configured) — never a bare "forbidden."
- **Errors are structured, not generic.** Surface the actual failure reason
  the API returns (e.g., `invalid_key`, `provider_unreachable`,
  `unknown_model_in_policy`) as a specific inline message, not a toast that
  just says "Something went wrong."
- **No destructive action without confirmation.** Revoking a service account
  key, deleting a provider key, or removing a user must confirm first and
  explain the consequence (e.g., "apps using this key will fail
  immediately").

## 5. Information Architecture

```
Gatekey Admin
├── Dashboard              (usage overview — landing page after login)
├── Providers               (provider key management)
├── Users                   (user + budget management)
├── Service Accounts         (per-app credentials)
└── Model Policy             (org allow/denylist)
```

Flat, five-item nav — no nesting needed at this scale. No search/command
palette required for Phase 1 (data volumes are small: 3 providers, a
handful of users/keys).

## 6. Global Shell

Persistent left sidebar + top bar, content area on the right. Desktop-first
(this is an internal admin tool; see §10 for responsive behavior).

```
┌───────────────────────────────────────────────────────────────────────┐
│ ⛨ Gatekey                                          admin@acme.co  ▾   │ ← top bar
├───────────┬───────────────────────────────────────────────────────────┤
│           │                                                           │
│ Dashboard │                                                           │
│ Providers │                    <screen content>                      │
│ Users     │                                                           │
│ Service   │                                                           │
│  Accounts │                                                           │
│ Model     │                                                           │
│  Policy   │                                                           │
│           │                                                           │
│           │                                                           │
│ ─────────  │                                                           │
│ v0.1.0    │                                                           │
│ (Phase 1) │                                                           │
└───────────┴───────────────────────────────────────────────────────────┘
```

- Sidebar: fixed width (~220px), current section highlighted with a filled
  background + left accent bar.
- Top bar: product mark left, admin identity + sign-out menu right. No
  org-switcher (single org in Phase 1).
- Footer of sidebar: version string, useful for support/bug reports on a
  self-hosted tool.

## 7. Screens

### 7.1 Login

Single admin credential (Phase 1 has no user accounts with passwords beyond
the seeded admin — see `require_admin` stub). Simple centered form.

```
┌───────────────────────────────────────────────────────────────────────┐
│                                                                       │
│                                                                       │
│                         ⛨  Gatekey                                   │
│                    Sign in to your gateway                           │
│                                                                       │
│                  ┌─────────────────────────────┐                     │
│                  │ Admin token                 │                     │
│                  └─────────────────────────────┘                     │
│                                                                       │
│                  ┌─────────────────────────────┐                     │
│                  │        Sign in              │                     │
│                  └─────────────────────────────┘                     │
│                                                                       │
│                  ⚠ Invalid admin token           ← error state       │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
```

**States:** default / submitting (button shows spinner, disabled) / error
(inline red text under the field, field gets red border — do not use a
generic toast for auth failure). No "forgot password" flow — this is a
self-hosted shared secret, not a user account system.

### 7.2 First-Run Setup Wizard

Shown automatically instead of the login screen when the instance has never
been configured (no admin token set, no provider key configured yet — per
`phase-1-core-gateway.md` §1.7, "seed/setup wizard for first admin account
and first provider key"). Two steps, linear, no skipping step 1.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Welcome to Gatekey                                    Step 1 of 2   │
│  ─────────────────────────────────────────────────────────────────   │
│                                                                       │
│  Set your admin credential                                          │
│  This is the shared token used to sign in to this console.           │
│                                                                       │
│  ┌─────────────────────────────┐                                    │
│  │ Set admin token             │                                    │
│  └─────────────────────────────┘                                    │
│  ┌─────────────────────────────┐                                    │
│  │ Confirm token                │                                    │
│  └─────────────────────────────┘                                    │
│                                                                       │
│                                              ┌─────────────────┐     │
│                                              │   Continue →    │     │
│                                              └─────────────────┘     │
└───────────────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────────────┐
│  Welcome to Gatekey                                    Step 2 of 2   │
│  ─────────────────────────────────────────────────────────────────   │
│                                                                       │
│  Connect your first provider                                        │
│  Add at least one key so requests have somewhere to route to.        │
│                                                                       │
│  ○ OpenAI      ○ Anthropic      ○ Google Vertex AI   ← provider tabs │
│                                                                       │
│  ┌─────────────────────────────────────────────┐                     │
│  │ API key                                     │                     │
│  └─────────────────────────────────────────────┘                     │
│                                                                       │
│  ⏳ Validating key with OpenAI…            ← inline validation state │
│                                                                       │
│                              ┌──────────┐  ┌─────────────────────┐  │
│                              │ ← Back   │  │  Save & finish setup │  │
│                              └──────────┘  └─────────────────────┘  │
│                                                                       │
│                                    Skip for now, I'll add this later │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior:** Step 2's provider tabs swap the form fields shown (see §7.4 for
exact fields per provider — identical form component, reused). "Save & finish
setup" performs the same live validation call the Providers page uses
(§7.4) before accepting the key; a failed validation keeps the wizard open
with an inline error, never silently drops to the dashboard. "Skip for now"
is allowed (a pilot admin may want to explore the UI before having a key
ready) and lands on the Dashboard in its empty state (§7.3).

### 7.3 Dashboard (Usage Overview)

Landing page after login. Shows aggregate spend/usage per
`phase-1-core-gateway.md` §1.5 ("totals by user, by model, over a selectable
time range").

```
┌───────────────────────────────────────────────────────────────────────┐
│  Dashboard                                                           │
│                                                                       │
│  Time range: [ Last 7 days ▾ ]                                       │
│                                                                       │
│  ┌───────────────┐ ┌───────────────┐ ┌───────────────┐ ┌───────────┐│
│  │ Total spend    │ │ Requests       │ │ Avg latency    │ │ Errors    ││
│  │ $184.32        │ │ 12,406         │ │ 842ms          │ │ 0.4%      ││
│  └───────────────┘ └───────────────┘ └───────────────┘ └───────────┘│
│                                                                       │
│  ┌─────────────────────────────────┐ ┌─────────────────────────────┐│
│  │ Spend by day                     │ │ Spend by model               ││
│  │  $                               │ │  gpt-4o          ███████ 61%││
│  │  │      ▄▄                       │ │  claude-3.5-sonnet ████ 24% ││
│  │  │   ▄▄ ██ ▄▄  ▄▄                │ │  gemini-2.5-pro   ██ 10%    ││
│  │  │▄▄ ██ ██ ██▄ ██▄▄              │ │  gpt-4o-mini      █ 5%      ││
│  │  └───────────────────────        │ │                              ││
│  │   M  T  W  T  F  S  S            │ │                              ││
│  └─────────────────────────────────┘ └─────────────────────────────┘│
│                                                                       │
│  Spend by user                                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ User          Requests    Spend        Budget      Status      │  │
│  ├───────────────────────────────────────────────────────────────┤  │
│  │ ana@acme.co     3,204     $62.10       $100.00     ████░░ 62%  │  │
│  │ ben@acme.co     5,881     $98.40       $100.00     █████░ 98%⚠│  │
│  │ svc-billing-app 3,321     $23.82       Unmetered    —          │  │
│  └───────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

**Elements:**
- Time-range selector (24h / 7d / 30d / custom range).
- Four stat tiles: total spend, request count, avg latency, error rate.
- Two charts: spend-over-time (line/bar) and spend-by-model (horizontal bar
  or donut — horizontal bar reads better with 3-10 models).
- "Spend by user" table: requests, spend, budget ceiling, and a progress bar
  showing spend/budget with a warning color past ~90% (matches
  `User.budget_usd` being nullable → render "Unmetered" with no bar, not
  "$∞" or a broken 0-width bar).

**States:** empty (no requests yet — show a friendly "No traffic yet. Point
an app at your gateway to see usage here," with a link to service-account
setup, not a blank chart); loading (skeleton tiles/table rows, not a
full-page spinner); error (inline banner, keep nav usable).

### 7.4 Providers

List + per-provider key management. Exactly 5 provider slots in Phase 1 —
render as fixed cards, not a generic "add new provider" list, since the set
is closed (`ProviderName` enum: `openai`, `anthropic`, `vertex_ai`, `ollama`,
`openrouter`).

```
┌───────────────────────────────────────────────────────────────────────┐
│  Providers                                                           │
│  Bring your own API keys. Gatekey never performs inference itself —   │
│  it routes to these providers under your policy.                     │
│                                                                       │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ ● OpenAI                                    ✓ Connected      │    │
│  │   Validated Jul 20, 2026 · 4:12 PM                            │    │
│  │                                     [ Edit key ]  [ Remove ]  │    │
│  └─────────────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ ● Anthropic                                 ✓ Connected      │    │
│  │   Validated Jul 18, 2026 · 9:03 AM                            │    │
│  │                                     [ Edit key ]  [ Remove ]  │    │
│  └─────────────────────────────────────────────────────────────┘    │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │ ○ Google Vertex AI                          Not configured   │    │
│  │   No key on file yet.                                        │    │
│  │                                            [ + Add key ]      │    │
│  └─────────────────────────────────────────────────────────────┘    │
└───────────────────────────────────────────────────────────────────────┘
```

**"Add / Edit key" modal** — form differs by provider (per §11 schemas):

```
┌─────────────────────────────────────────┐   ┌─────────────────────────────────────────┐
│  Add OpenAI key                    ✕     │   │  Add Google Vertex AI key           ✕    │
│                                            │   │                                            │
│  API key                                  │   │  Service account JSON                     │
│  ┌───────────────────────────────────┐   │   │  ┌───────────────────────────────────┐   │
│  │ sk-••••••••••••••••••••••••••     │   │   │  │ [ Upload .json ]  or paste below   │   │
│  └───────────────────────────────────┘   │   │  └───────────────────────────────────┘   │
│                                            │   │  Project ID                               │
│                                            │   │  ┌───────────────────────────────────┐   │
│                                            │   │  │                                     │   │
│                                            │   │  └───────────────────────────────────┘   │
│                                            │   │  Location                                 │
│                                            │   │  ┌───────────────────────────────────┐   │
│                                            │   │  │ us-central1                        │   │
│                                            │   │  └───────────────────────────────────┘   │
│                                            │   │                                            │
│           [ Cancel ]  [ Validate & save ]  │   │           [ Cancel ]  [ Validate & save ]  │
└─────────────────────────────────────────┘   └─────────────────────────────────────────┘
```

**Behavior & states:**
- Key input is masked like a password field (never rendered in plaintext
  once typed elsewhere on screen — no "show key" toggle, since the backend
  never returns the plaintext back anyway).
  is a live provider round-trip (per backend: a test call before save), so
  it can take a couple seconds and can fail three distinct ways — the modal
  must render each distinctly, not collapse them into one generic error:
  - `invalid_key` → "This key was rejected by \{provider}. Double-check it
    and try again." (422)
  - `provider_unreachable` → "Couldn't reach \{provider} to validate this
    key. Check your network and try again." (502)
  - `unknown_error` → generic fallback, still distinct copy from the above two.
- "Remove" requires a confirm dialog: "Remove the OpenAI key? Any request
  routed to an OpenAI model will start failing immediately." Destructive
  button styled red.
- Card status dot: filled/green = connected & validated, hollow/gray = not
  configured. (Phase 1 has no "key present but failing" state since a row
  only ever exists post-validation — don't design a third status for it.)

### 7.5 Users

Manage the budget-owning cost-center entities (`User` in the backend — note:
these are cost centers/service consumers, not admin login accounts; Phase 1
has no per-user login, only per-user budget tracking attributed via service
account keys).

```
┌───────────────────────────────────────────────────────────────────────┐
│  Users                                              [ + Add user ]   │
│                                                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Name          Budget        Spent          Status     Actions  │  │
│  ├───────────────────────────────────────────────────────────────┤  │
│  │ ana@acme.co    $100.00       $62.10 (62%)   Active     ⋮        │  │
│  │ ben@acme.co    $100.00       $98.40 (98%)   ⚠ Near limit ⋮      │  │
│  │ legacy-default  Unmetered     $412.90        Active     ⋮        │  │
│  └───────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

**"Add / Edit user" modal:**

```
┌─────────────────────────────────────────┐
│  Add user                          ✕     │
│                                            │
│  Name                                     │
│  ┌───────────────────────────────────┐   │
│  │ e.g. ana@acme.co                   │   │
│  └───────────────────────────────────┘   │
│                                            │
│  Budget (USD)                             │
│  ┌───────────────────────────────────┐   │
│  │ 100.00                              │   │
│  └───────────────────────────────────┘   │
│  ☐ Unmetered (no spend cutoff)            │
│                                            │
│           [ Cancel ]  [ Save ]            │
└─────────────────────────────────────────┘
```

**Behavior:** checking "Unmetered" disables/clears the budget field (maps to
`budget_usd = NULL`). Row-level ⋮ menu: Edit, Remove. Removing a user that
still has an active or revoked service-account key attached must fail with
an explicit inline reason ("Can't remove this user — 1 service account key
still references it. Revoke or reassign it first.") rather than a raw
500/constraint error, since the backend enforces this via `ON DELETE
RESTRICT`. "Near limit" badge appears ≥90% of budget; a fully exhausted user
should read "Budget exhausted" in red, not just 100%+ green.

### 7.6 Service Accounts

Per-app credentials used to authenticate gateway traffic
(`gk_sk_...` secrets).

```
┌───────────────────────────────────────────────────────────────────────┐
│  Service Accounts                          [ + Create service account ]│
│  Credentials your internal apps use to call the gateway. Each key is   │
│  attributed to a user for budget purposes.                            │
│                                                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ Name              Key            Attributed to   Created   ⋮    │  │
│  ├───────────────────────────────────────────────────────────────┤  │
│  │ billing-service    gk_sk_8f2a…    ana@acme.co     Jul 12    ⋮    │  │
│  │ support-bot         gk_sk_c91d…    ben@acme.co     Jul 15    ⋮    │  │
│  │ legacy-cron  (revoked, gray)  gk_sk_1ae0…  legacy-default  Jun 30│  │
│  └───────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────────┘
```

**"Create service account" flow — two-step modal, because the secret is
shown exactly once:**

```
Step 1 — form                              Step 2 — one-time reveal
┌─────────────────────────────────────┐   ┌─────────────────────────────────────────┐
│  Create service account        ✕     │   │  Save this secret now                ✕   │
│                                        │   │  ⚠ This is the only time you'll see it.  │
│  Name                                 │   │                                            │
│  ┌───────────────────────────────┐   │   │  ┌───────────────────────────────────┐   │
│  │ e.g. billing-service            │   │   │  │ gk_sk_8f2a91c...e93f      [Copy]   │   │
│  └───────────────────────────────┘   │   │  └───────────────────────────────────┘   │
│                                        │   │                                            │
│  Attributed user                      │   │  Name: billing-service                    │
│  ┌───────────────────────────────┐   │   │  Created: Jul 22, 2026                    │
│  │ ana@acme.co                 ▾  │   │   │                                            │
│  └───────────────────────────────┘   │   │      [ I've saved it, close ]              │
│                                        │   │                                            │
│           [ Cancel ]  [ Create ]      │   │                                            │
└─────────────────────────────────────┘   └─────────────────────────────────────────┘
```

**Behavior & states:**
- Step 2 cannot be dismissed by clicking outside / pressing Esc — only the
  explicit "I've saved it, close" button, to prevent accidental loss of a
  secret that can never be retrieved again.
- "Copy" button gives brief confirmation feedback (e.g., button label flips
  to "Copied" for ~1.5s).
- List view never shows a full secret — only `key_prefix` (e.g.,
  `gk_sk_8f2a…`), consistent with the backend never persisting or returning
  plaintext after creation.
- Revoked rows: visually muted (gray text, "Revoked" badge with date),
  remain in the list rather than disappearing (audit value), sort below
  active rows.
- Row ⋮ menu on active rows: "Revoke" only (no edit — name/user attribution
  aren't mutable per current backend). Revoke requires confirm: "Revoke
  billing-service? Any app using this key will immediately start failing
  authentication." Revoked rows' menu is empty/disabled (revoke is
  idempotent server-side, but re-showing the action invites confusion).

### 7.7 Model Policy

Org-wide allow/denylist controlling which models employees/apps can route
to.

```
┌───────────────────────────────────────────────────────────────────────┐
│  Model Policy                                                        │
│  Control which models this org's traffic is allowed to reach.         │
│                                                                       │
│  Mode:   ( ) Allowlist    (•) Denylist                                │
│          Only listed models    All models except those listed        │
│          may be used            are allowed                          │
│                                                                       │
│  ┌─ OpenAI ─────────────────────────────────────────────────────┐   │
│  │ ☐ gpt-4o                        ☑ gpt-4o-mini                  │   │
│  │ ☐ text-embedding-3-small        ☐ text-embedding-3-large       │   │
│  └─────────────────────────────────────────────────────────────┘   │
│  ┌─ Anthropic ──────────────────────────────────────────────────┐   │
│  │ ☑ claude-opus-5        ☐ claude-sonnet-5  │   │
│  │ ☐ claude-haiku-4-5-20251001                                    │   │
│  └─────────────────────────────────────────────────────────────┘   │
│  ┌─ Google Vertex AI ───────────────────────────────────────────┐   │
│  │ ☐ gemini-2.5-pro                ☐ gemini-2.5-flash            │   │
│  │ ☐ gemini-embedding-001                                           │   │
│  └─────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  2 models selected                          [ Save policy ]          │
└───────────────────────────────────────────────────────────────────────┘
```

**Behavior & states:**
- Models are grouped by provider (from `MODEL_REGISTRY`), each row shows the
  gateway-facing model name only (never the provider's internal
  `native_model_id`).
- Switching Allowlist ↔ Denylist should visibly reframe the checkbox list's
  meaning (e.g., a helper line above the groups restates "✓ = allowed to be
  used" vs. "✓ = blocked from use" depending on mode) — the same checkboxes
  must never look identical in both modes without a clear label of what a
  check *means* right now.
  - **Unconfigured state** (no policy row exists yet — the backend's
  explicit third state, distinct from either enum value): show neither radio
  pre-selected, all checkboxes unchecked, and a banner: "No policy configured
  — all models are currently allowed by default." Saving for the first time
  requires picking a mode explicitly (mirrors backend: `mode` can never be
  submitted as "unconfigured").
- `unknown_model_in_policy` save error (shouldn't be reachable via this UI
  since choices come from a fixed checklist, but the modal/toast copy should
  still exist as a safety net): "One or more selected models aren't
  recognized. Refresh and try again."
- "Save policy" is disabled until at least one change is made (dirty-state
  tracking), and shows a save confirmation toast on success.

## 8. Shared Components

- **Stat tile** — label, large value, optional trend delta.
- **Data table** — sticky header, right-aligned numeric columns, row hover
  state, row-level overflow (⋮) menu for actions, empty state row when no
  data ("No providers configured yet" etc., not just a blank table).
- **Modal** — centered, max-width ~480px for forms, backdrop click closes
  *except* the one-time-secret reveal step (§7.6).
- **Confirm dialog** — used for every destructive action; states the
  consequence in plain language, not just "Are you sure?"; destructive
  button is red and requires explicit label matching the action ("Remove
  key", "Revoke", "Delete user" — never a bare "OK").
- **Badge** — status pills: Connected (green), Not configured (gray),
  Active (green), Revoked (gray/strikethrough-adjacent), Near limit
  (amber), Exhausted (red).
- **Progress/budget bar** — spend-vs-budget, color ramps green → amber
  (≥80%) → red (≥100% / exhausted); renders as "Unmetered" text with no bar
  when budget is null.
- **Toast** — bottom-right, auto-dismiss ~4s for success, persistent
  (manual dismiss) for errors.
- **Inline field error** — red text directly under the offending field, not
  only a top-of-form banner.

## 9. Visual Style Guidance

Not a branded consumer product — style it like a clean operator dashboard.

- **Palette:** neutral gray/slate base (background, borders, body text),
  one accent color for primary actions/links/focus rings, semantic colors
  reserved strictly for status (green = healthy/active, amber = warning/near
  limit, red = error/exhausted/destructive). Don't use the accent color for
  status — keep semantic meaning unambiguous.
- **Typography:** a single system/sans font stack, one monospace font for
  secrets, keys, and IDs (`gk_sk_...`, UUIDs) so they're visually
  distinguishable from prose and easy to select/copy accurately.
- **Density:** comfortable-but-tight table row height (~40-44px), consistent
  8px spacing scale.
- **Icons:** simple line-icon set (nav items, status dots, actions) — avoid
  decorative illustration; this tool is read constantly by the same admin,
  not browsed once.
- Light mode as the default/primary target; dark mode is a nice-to-have, not
  a requirement, for this phase.

## 10. Responsive Behavior

Desktop-first (primary usage is an admin at a workstation), but shouldn't
break badly on a laptop-narrow viewport:

- ≥1024px: full sidebar + content layout as wireframed above.
- 768–1023px: sidebar collapses to icon-only rail (labels on hover/tooltip).
- <768px: not a design priority for Phase 1 — acceptable to stack sidebar
  into a top hamburger menu and let tables scroll horizontally rather than
  reflow into cards. Don't invest prototype effort in a bespoke mobile
  layout here.

## 11. Data Reference (for accurate mock data / API wiring)

Field names/types below are taken directly from the current backend
Pydantic schemas — use these exact shapes so the prototype can later be
wired to the real API with no field renaming.

**ProviderKeyResponse** (`GET /v1/admin/providers`, per-provider)
```
{ provider: "openai" | "anthropic" | "vertex_ai",
  configured: true,
  validated_at: string (ISO datetime),
  created_at: string, updated_at: string,
  metadata: { ...non-secret label info... } }
```
Add-key request bodies (`PUT /v1/admin/providers/{provider}/key`) differ by
provider:
- OpenAI / Anthropic: `{ api_key: string }`
- Vertex AI: `{ service_account_json: object, project_id: string, location: string }`

**ServiceAccountKeyResponse** (`GET /v1/admin/service-accounts`)
```
{ id: uuid, name: string, key_prefix: string,
  created_at: string, revoked_at: string | null, active: boolean }
```
**ServiceAccountKeyCreateResponse** (`POST /v1/admin/service-accounts`,
returned once)
```
{ id: uuid, name: string, key_prefix: string, secret: string, created_at: string }
```

**ModelPolicyResponse** (`GET/PUT /v1/admin/model-policy`)
```
{ mode: "unconfigured" | "allowlist" | "denylist", models: string[] }
```
Known model IDs for mock data (grouped by provider, from `MODEL_REGISTRY`):
- OpenAI: `gpt-4o`, `gpt-4o-mini`, `text-embedding-3-small`, `text-embedding-3-large`
- Anthropic: `claude-sonnet-5`, `claude-haiku-4-5-20251001`, `claude-opus-5`
- Vertex AI: `gemini-2.5-pro`, `gemini-2.5-flash`, `gemini-embedding-001`

**User (mock — no admin endpoint yet)**
```
{ id: uuid, name: string,
  budget_usd: number | null,   // null = unmetered
  current_spend_usd: number }
```

**Usage/dashboard (mock — no endpoint yet)** — design against an aggregate
shape like:
```
{ total_spend_usd: number, request_count: number,
  avg_latency_ms: number, error_rate: number,
  spend_by_day: { date: string, spend_usd: number }[],
  spend_by_model: { model: string, spend_usd: number }[],
  spend_by_user: { user: string, requests: number, spend_usd: number,
                   budget_usd: number | null }[] }
```

## 12. Error/Empty/Loading Conventions (apply across all screens)

- **Loading:** skeleton placeholders matching the real layout's shape
  (skeleton table rows, skeleton stat tiles) — never a full-page spinner
  that blanks the nav.
- **Empty:** every list/table has a specific empty-state message + a primary
  action to resolve it (e.g., Providers empty → prompts to add a key;
  Service Accounts empty → prompts to create one). Never just an empty
  table with a header row and nothing else.
- **Error:** inline, specific, and recoverable — show the actual reason
  where the backend provides a structured error code (see §7.4's
  `invalid_key` / `provider_unreachable` example as the pattern to follow
  everywhere), with a retry affordance where applicable. Never let an error
  take down the whole shell (nav/sidebar stays usable).
