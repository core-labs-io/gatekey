# Admin console tour

The console at `http://localhost:3000` serves two audiences: admin screens
(break-glass-token sign-ins and Org Admin SSO sessions see the identical
nav — the token just has no personal identity, so the personal screens
under "Non-admin console" below never appear for it), and role-appropriate
non-admin screens for everyone else. Server-side enforcement backs every
screen — the UI hiding a control is never the only guard.

## Core screens

- **Dashboard** — total spend, request count, avg latency, error rate, spend
  over time, spend by model, spend by user (with budget bars). Includes
  reliability/cost tiles: cache hit rate, failover event count, and cost
  saved via caching + graceful degradation (shown separately and combined),
  filterable by time range/team/provider, with CSV/JSON export and a
  one-click 30-day org-wide "Cost Efficiency Report".
- **Providers** — add/edit/remove OpenAI, Anthropic, Vertex AI, Ollama, and
  OpenRouter keys (5 fixed provider slots). Keys are validated live against
  the provider before saving and are never shown again in plaintext after
  entry. Ollama takes a `base_url` (your self-hosted instance) instead of an
  API key; OpenRouter is a plain API key like OpenAI's. Each configured key
  also shows health status (healthy/degraded/unavailable/unknown), last
  check time, last error, a manual "Check now" trigger, and a per-key
  failover control (enable/disable + pick a same-provider backup key).
  Two more cards live here:
  - **Self-Hosted Models** — register/edit/remove a vLLM/Ollama/
    OpenAI-compatible self-hosted endpoint (name, base URL, bearer token —
    never shown again after entry, GPU-hour cost basis, served model ids,
    verified badge + re-verify button).
  - **Custom Models** — register/edit/remove/test a BYOK custom model
    (name, provider, native model id, capability, pricing, verified badge +
    "Test model" button, "Shadowed by registry update" warning badge). Org
    Admin full CRUD + verify; Auditor read-only.
- **Users** — every user in the org: admin-created cost centers and
  SSO-provisioned people alike. The flat USD budget set here only governs
  **legacy** (team-less) service-account keys — once a user belongs to a
  team, new keys charge against their per-team membership budget instead
  (set on the Teams screen).
- **Service Accounts** — per-app credentials (`gk_sk_...`) your internal
  apps authenticate with. Every new key is attributed to a *(user, team)*
  pair — the user must already be a member of that team — and charges that
  membership's budget; the secret is shown once. The keys table is a
  unified listing of **both** key types org-wide (app `gk_sk_` and personal
  `gk_pk_`, with a type/owner column); an Org Admin can revoke or
  regenerate either type from here. Personal keys are never *created* here
  — they come from My API Keys or a Team Lead.
- **Model Policy** — org-wide allowlist or denylist of which models traffic
  may reach; the baseline that team-level restrictions can only narrow.
  Includes a "Custom" group sourced from registered custom models (only
  verified ones are selectable) and the **Content-Aware Routing** rules:
  four categories (PII, source code, financial data, legal), each with its
  own admin-defined allowed-models list, plus a "Sensitivity Label
  Mappings" table for mapping your own enterprise label strings to a
  Gatekey category.
- **Teams** — create/edit/delete teams; per team: budget ceiling, budget
  period (monthly/quarterly, rollover-or-reset on period end), the members
  table (add/remove members, set per-member role and budget, reassign
  budget between two members in one audited step), team model restrictions,
  pending join requests, and alert thresholds (80%/100% toggles, webhook
  URL, email toggle — alert config is Org-Admin-only by design; Team Leads
  receive alerts but don't configure them). A team can't be deleted while
  it still has members or join-request history, and a member can't be
  removed while they still hold active keys bound to that team.
- **Audit Log** — the append-only governance trail: timestamp, actor,
  action, target, old/new values; filterable by action type, actor, and
  time range. Break-glass actions appear as `system:admin_token`. Once the
  tamper-evident hash chain is enabled (Compliance Settings), this screen
  gains an integrity badge + "Verify now" button, and CSV/JSON export
  includes the chain columns.
- **Identity & Access** — read-only view of the SSO configuration (issuer,
  client id, redirect URI, and whether a client secret is set — never the
  value), plus a "Test connection" button that live-fetches the issuer's
  discovery document. Deliberately not editable in the UI: SSO config is
  env-var-only — edit `.env` and restart to change it.

## Compliance screens

- **Compliance & DLP** — PII/DLP scanning configuration (log, redact, or
  block), org custom regex patterns, per-team action overrides.
- **Residency Rules** — org/team allowed-regions rules resolved against
  each request's provider/region.
- **Rotation Policy** — automatic service-account key rotation and guided
  provider-key rotation settings.
- **Access Windows** — scheduled access windows for service-account keys.
- **Compliance Settings** — retention windows, the hash-chain toggle
  (mutually exclusive with a finite audit-retention window — the UI
  enforces this), and related org-wide compliance switches.

## Reliability & cost screens

- **Rate Limiting** — org-wide or per-team rules (requests and/or tokens
  per minute, plus an individual-user scope), "reject immediately" or
  "queue and retry" behavior, maximum queue wait. Org Admins manage
  org-wide rules; Team Leads manage their own team's rules.
- **Caching Settings** — org-level kill switch + TTL, per-team opt-in and
  TTL. Includes a teaser-only cache-entries browser (never full
  prompt/response content) and a clear-cache action (org-wide or
  team-scoped).
- **Degradation Policy** — automatic model downgrades when spending reaches
  a threshold percentage of budget, org-wide or per-team, with a
  degradation-events log of every downgrade that fired.
- **Backup Groups** — named groups of provider keys for failover routing.
- **Failover Events** — read-only, filterable log of every failover retry,
  with the from/to key resolved to its label.

## AI oversight screens

- **Shadow AI** — detection-source selector, one-time-reveal ingestion
  token generation, known-AI-tool-hostname allowlist CRUD, the usage report
  (user/tool/frequency/last-seen, "not linked to a Gatekey user" for
  unmatched rows, a repeat-violator indicator), enforcement-mode controls
  (notification/webhook, both off by default, each behind an explicit
  confirmation dialog), and a link to the feature's data-handling policy
  (`backend/docs/policy/shadow-ai-data-handling.md`).
- **Drift Detector** — per-model status/trend table, expandable
  plain-language alert detail ("+45% latency vs. baseline"), canary
  history, per-model enable/disable, export an alert to the audit log.
- **Self-Hosted Governance** — cost-normalization cross-link view
  (requests/estimated cost/latency), explicitly labeled "estimated, not an
  invoice".

Org Admin configures these; Auditors get the identical read-only view.

## Non-admin console

SSO sessions whose user is *not* an Org Admin get a role-appropriate nav
instead of the admin screens.

- **Every SSO user** (Member and up): **My Usage** (own spend/requests over
  time, budget position), **Model Access** (per-model allowed/blocked with
  a plain-language reason naming which layer blocks it — org policy or team
  restriction), **My API Keys** (mint/regenerate/revoke own `gk_pk_` keys;
  team-bound, optional expiration, secret shown once).
- **Team Leads** additionally get a "My Team" section: **Join Requests**
  (approve with a budget / reject with a reason), **Team Dashboard** (team
  spend vs. ceiling, period position), **Members & Budgets** (add/remove
  members, roles, per-member budgets, budget reassignment, members' keys),
  **Model Restrictions** (narrow the org baseline for this team),
  **Reliability & Cost** (own-team rate-limit/caching rules), and an
  own-team-scoped Shadow AI report — all scoped strictly to teams they
  actually lead.
- **Auditors** get a read-only org-wide nav: **Org Usage** (aggregate
  summaries), **Org Logs** (the audit-event trail), **Policy Viewer** (org
  baseline + every team's restrictions), and the read-only AI oversight
  screens. No mutation controls anywhere.
- **New users** see only the onboarding screens (profile + team selection,
  then a holding screen) until their join request is approved.
