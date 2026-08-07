# Gatekey

An open-source, self-hostable enterprise AI gateway. Bring your own provider
API keys (OpenAI, Anthropic, Google Vertex AI, OpenRouter) or point it at a
self-hosted Ollama instance; Gatekey sits in the middle as a unified,
OpenAI-compatible proxy and governance layer — controlling which models
people can use, enforcing per-user budgets, and giving you observability
over all AI traffic.

Gatekey is **not** a model host. It never performs inference itself — it
mediates access to your existing provider keys under policy.

This repository currently implements **Phase 1 (Core Gateway / MVP)**,
**Phase 2 (Multi-Tenant Governance)**, **Phase 3 (Security & Compliance
Hardening)**, **Phase 4 (Reliability & Cost Efficiency)**, and **Phase 5
(Differentiators)** — see `gatekey/phase-1-core-gateway.md`,
`gatekey/phase-2-product-spec.md`, `gatekey/phase-3-security-compliance.md`,
`gatekey/phase-4-technical-design.md`, and `gatekey/phase-5-technical-design.md`
for the authoritative requirements, and `gatekey/00-overview.md` for the
overall phase roadmap.

## What's included (Phase 1)

- **Provider & key management** — store OpenAI/Anthropic/Vertex AI/
  OpenRouter API keys or a self-hosted Ollama instance's `base_url`,
  encrypted at rest (AES-256-GCM), validated on entry.
- **Unified gateway** — OpenAI-compatible `/v1/chat/completions`,
  `/v1/completions`, `/v1/embeddings`, streaming (SSE) supported.
- **Model access policy** — org-wide allow/denylist.
- **Budget enforcement** — per-user USD budget, hard cutoff, cost computed
  from actual provider-reported token usage.
- **Usage logging & dashboard** — per-request accounting, usage summaries by
  user/model/time range.
- **Admin console** — a web UI for all of the above (Providers, Users,
  Service Accounts, Model Policy, Dashboard).
- **`docker-compose up`** — Postgres + backend + frontend, one command.
- **Optional Redis** — run with `docker-compose --profile cache up` to enable
  rate limiting, response caching, and shared state for distributed deployments.

## What's included (Phase 2)

Nothing in Phase 2 is mandatory to set up: a fresh clone with only the two
Phase 1 secrets still comes up exactly as before, and the
`GATEKEY_ADMIN_TOKEN` remains a permanent break-glass credential with full
Org Admin rights on every endpoint and every console screen, including all
the new ones below.

- **Teams & roles** — an org → team → member hierarchy with four roles,
  enforced server-side on every privileged endpoint: **Org Admin** and
  **Auditor** (org-wide), **Team Lead** and **Member** (per team — a user
  can lead one team and be a plain member of another).
- **SSO (OIDC)** — optional single sign-on via the standard
  authorization-code flow against any spec-compliant IdP, with server-side
  sessions (httpOnly cookie, revocable, hashed at rest). A dev-only Keycloak
  IdP ships behind `docker-compose --profile sso` for local testing. See
  "Optional: SSO" below.
- **Three-level budgets** — org-wide ceiling, per-team ceiling, and a
  per-member spend cutoff within each team. Spend deduction is atomic
  (single-statement, concurrency-safe); ceiling allocation is enforced at
  assignment time under a row lock, so a team's allocated total never
  exceeds its ceiling even under concurrent approvals. Teams have a
  monthly or quarterly budget period with a **rollover** or **reset**
  (default) end-of-period policy.
- **Nested model policy** — a team can narrow the org-wide model baseline
  for its members, never widen it; re-enabling an org-denied model at the
  team level is rejected server-side.
- **Join-request onboarding** — a new SSO user picks a team and submits a
  join request; the team's Team Lead approves it with a budget in one atomic
  step (requests fall back to an Org Admin queue if the team has no lead or
  the request sits pending five business days).
- **Personal API keys** (`gk_pk_...`) — self-service keys any SSO user can
  mint for themselves (Team Leads and Org Admins can also manage members'
  keys), bound to a team, with optional expiration. Used at the gateway
  exactly like a service-account key — same `Authorization: Bearer` header,
  same endpoints.
- **Audit trail** — every governance mutation (team/member/budget/policy/key
  changes, join-request decisions, role grants) writes an append-only audit
  entry with actor, action, target, and old/new values; browsable and
  filterable on the new Audit Log screen. Break-glass-token actions are
  recorded as actor `system:admin_token`.
- **Threshold alerts** — when a team crosses 80% or 100% of its budget
  ceiling, Gatekey fires a webhook (generic JSON or Slack-compatible
  payload; webhook URLs are encrypted at rest) and, if SMTP is configured,
  an email. Email delivery is implemented but unverified against a live
  mailbox — see Known limitations.

## What's included (Phase 3)

Phase 3 (Security & Compliance Hardening) adds PII/DLP scanning (Presidio,
in-process), data residency rules, automatic service-account key rotation
and guided provider-key rotation, scheduled access windows, SCIM 2.0
provisioning, and audit-trail gap-closure (source IP, CSV/JSON export,
configurable retention). See `gatekey/phase-3-security-compliance.md` for
the full requirement and `backend/docs/design/
phase-3-security-compliance-design.md` for the implemented design.

## What's included (Phase 4)

Phase 4 (Reliability & Cost Efficiency) adds optional Redis-backed features
for production-grade reliability and cost optimization. Redis is optional and
gated behind `docker-compose --profile cache` — deployments without Redis
continue to work, but will not have rate limiting, caching, or shared state.

- **Multi-key & Failover** — configure multiple provider keys into backup
  groups (`Backup Groups` screen). Failover is off by default per key and
  must be explicitly turned on per key (`PUT /v1/admin/provider-keys/{id}/
  failover-config`, Org Admin only), pointing it at a same-provider backup
  key — this is a deliberate opt-in, not automatic, so a compliance-sensitive
  org never has traffic silently rerouted. Health checks are **active
  synthetic checks**: a scheduled job runs every 5 minutes, makes a real
  test request to each provider using the key's actual decrypted
  credential, and records health status/last error/24h availability —
  visible per-key on the **Providers** screen alongside a manual "Check now"
  button. When a primary key is down and failover is enabled for it, the
  gateway retries exactly once against its configured backup key.
- **Rate Limiting** — configure per-user or per-team request and token rate
  limits with sliding window counters (Redis-backed). A team's limit is a
  genuinely shared pool across that team's users; a user's personal limit
  (if configured) is additive on top of it. Two behaviors: immediate reject
  or queue-and-retry (configurable per rule). Admin console lets Org Admins
  manage rules org-wide and Team Leads manage their own team's rules. Both
  `requests_per_min` and `tokens_per_min` are enforced live on the hot path
  (`tokens_per_min` via a pre-emptive gate on prior usage plus a
  retrospective atomic true-up after the real response — see Known
  limitations below for one remaining edge case).
- **Caching** — exact-match response caching with configurable TTL, opt-in
  per team (`cache_enabled`, default off) with an org-level kill switch,
  respecting DLP and residency boundaries. Cache keys include team_id,
  user_id, provider, model, prompt hash, and residency zone to ensure data
  isolation, and are invalidated automatically whenever an org/team's
  residency or DLP policy changes (so a cache entry can never outlive the
  policy boundary it was written under). `POST /v1/admin/cache/clear`
  supports both org-wide and team-scoped manual purge. Dashboard shows real
  cache hit rate metrics, not a placeholder.
- **Graceful Degradation** — configure automatic model downgrades when
  spending reaches a configured percentage of budget. The substituted model
  is validated against the team's model-access policy both at config time
  (an Org Admin/Team Lead can't set a target model the team isn't allowed to
  use) and again at request time (if policy is tightened after the
  degradation policy was configured, the gateway falls back to a normal hard
  budget block instead of silently using the now-disallowed model). Response
  headers (`X-Gatekey-Degraded`, `-From`, `-To`) signal a downgrade to the
  calling app. Admin console shows active policies, threshold percentages,
  and a degradation-events log.
- **Cost & Reliability Dashboard** — real metrics tiles (not placeholders)
  showing cache hit rate, failover events count, and cost saved via caching
  + graceful degradation (shown separately and combined), filterable by time
  range/team/provider, with CSV/JSON export and a one-click "Cost Efficiency
  Report" (org-wide, 30-day) for finance/ROI reporting.

**Compliance documentation.** For a customer security review or vendor risk
assessment, see `backend/docs/compliance/data-flow-diagram.md` (a Mermaid
diagram of what data moves where, and what — if anything — leaves the
deployment boundary) and `backend/docs/compliance/data-handling-policy.md`
(what's stored, for how long, encryption at rest/in transit, DLP handling,
access controls, sub-processor disclosure, and an explicit list of what
hasn't been independently verified). These are static documents, not a
generated report — read them, don't just link them, before a real review.

## What's included (Phase 5)

Phase 5 (Differentiators) ships five features aimed at security- and
compliance-driven buyers, built in lowest-integration-risk-first order:

- **Cryptographically Hash-Chained Audit Ledger** — an opt-in, per-org tamper-
  evident chain over the existing audit log (`chain_enabled`, off by default).
  Each entry's hash covers the previous entry's hash, so a retroactive edit to
  any historical row is detectable. `GET /v1/admin/audit/verify` walks the
  whole chain and reports exactly which entry broke it, if any — not a bare
  pass/fail. Mutually exclusive with a finite audit-retention/purge window
  (you can have unlimited-history verifiability, or automatic purging, not
  both at once — the admin UI enforces this). In-database only in this
  release; no external timestamping/anchoring service integration (see Known
  limitations).
- **Provider Drift Detector** — a small, fixed, cheap canary prompt suite runs
  daily against every actively-used model (including registered self-hosted
  ones) and compares latency, refusal rate, and output similarity against a
  rolling baseline, flagging threshold-based drift. Canary cost is tracked in
  its own ledger and never touches a real team/user/org budget figure or the
  usage dashboard — it's a genuinely separate cost bucket, not just a
  differently-labeled one. Alerts can be exported to the audit log for
  compliance review.
- **Unified Governance for BYOK + Self-Hosted Models** — register a vLLM-,
  Ollama-, or any OpenAI-compatible self-hosted inference endpoint as a
  first-class provider, governed under the identical policy/budget/DLP/
  residency/rate-limit/audit pipeline as any paid provider key — never a
  bypass path. Cost is estimated from a configured GPU-hour rate × request
  latency and clearly labeled "estimated," distinct from BYOK providers'
  invoice-grade token pricing. Chat completions only in this release (not
  `/v1/completions` or `/v1/embeddings`).
- **Content-Classification-Aware Dynamic Routing** — extends Phase 3's basic
  PII-only content-classification rule into four real, functionally-equal
  categories (`pii`, `source_code`, `financial_data`, `legal`), each with an
  admin-defined allowed-models list; a request matching multiple enabled
  categories is only routed to the intersection of their allowed models (and
  blocked if that intersection is empty). An org that already applies its own
  sensitivity labels (e.g. via Microsoft Purview or Google DLP) can configure
  a mapping so a pre-set label short-circuits Gatekey's own classifier for
  that one category — the underlying DLP redaction/block scan always still
  runs regardless, this only affects which model-routing category a request
  is assigned to.
- **Shadow AI Discovery** — ingest normalized events from your existing
  SASE/proxy logging (a generic webhook contract, not a specific vendor
  integration) to detect employees calling unsanctioned AI tools directly,
  bypassing Gatekey entirely. Only destination hosts matching a curated,
  admin-managed allowlist are ever stored — everything else in an ingested
  batch is dropped, not retained. Detection/awareness first (a report of
  which users/teams are using which tools, how often); an opt-in
  notification-email and/or outbound-webhook enforcement mode is available,
  off by default, gated behind an explicit confirmation. This feature has its
  own dedicated data-handling policy — see
  `backend/docs/policy/shadow-ai-data-handling.md` — reviewed and linked from
  the admin UI before you turn it on, given its inherent privacy sensitivity.

**Security review.** All five sub-features passed a dedicated mandatory
security review — see
`backend/docs/design/phase-5-differentiators-security-review.md` — which
independently re-verified (not just trusted) two real issues a QA pass found
and backend-developer fixed: a sensitivity-label bypass that would have
silently skipped DLP redaction/blocking for financial data (fixed — the label
short-circuit now only affects routing, never the underlying scan), and a
plaintext-at-rest webhook URL for Shadow AI's enforcement callback (fixed —
now the same AES-256-GCM envelope every other secret in this codebase uses).

## What's included (Custom Model Registry)

A standalone feature (not tied to a specific phase doc) that lets an Org
Admin add a BYOK model the day a provider ships it — no Gatekey code
release or redeploy required. See
`gatekey/custom-model-registry-product-spec.md` and
`gatekey/custom-model-registry-technical-design.md` for the full spec/design.

- **Register a custom model** — name, provider (OpenAI, Anthropic, Vertex
  AI, or OpenRouter — not Ollama, which has its own self-hosted registration
  mechanism above), the provider's native model id, capability (chat or
  embeddings — embeddings only for OpenAI/Vertex AI, since Anthropic/
  OpenRouter expose no embeddings API), and real admin-entered USD-per-
  million-token pricing (input required; output required for chat, forbidden
  for embeddings). No new credential is stored — it routes through the
  BYOK key already on file for that provider.
- **One-time live verification gate** — a custom model is unusable until an
  Org Admin clicks "Test model," which fires exactly one minimal real call
  against the provider using the existing key. Success marks it verified and
  routable; failure surfaces the real provider error (never swallowed) so a
  typo is diagnosable immediately. Editing the native model id, provider, or
  capability resets verified back to false. Never charges budget or writes a
  usage-log row itself.
- **Fully wired into the existing pipeline** — once verified, a custom model
  is selectable in the org's Model Policy checklist (new "Custom" group) and
  a team's model restrictions, appears in the end-user Model Access view,
  and its cost is computed from its own real per-token rate (not an
  estimate) into the same budget/dashboard figures every BYOK model uses. A
  custom model rides its provider's existing Phase 4 failover/backup-group
  support automatically.
- **Shadowing detection** — if a later Gatekey release adds a static
  registry model with the same name as an already-registered custom model,
  the static entry always wins at request time (never a silent mix-up), an
  `ERROR`-level log fires at startup naming the org and model, and the admin
  console shows a "Shadowed by registry update" badge on the affected row.
  No auto-remediation — the admin renames or removes it manually, matching
  this codebase's existing "flag, never silently auto-resolve" convention.

**Security review.** Passed a dedicated mandatory security review — see
`backend/docs/design/custom-model-registry-security-review.md` — which
independently reproduced and required fixes for two real issues found
during implementation: a cross-table race condition in the name-collision
guard between custom and self-hosted models (fixed via a row lock), and a
resulting deadlock discovery between that lock and a pre-existing,
differently-ordered lock in the org-settings endpoint (fixed codebase-wide,
not just for this feature — see the review doc for the full list of
endpoints that needed reordering).

## Repository layout

```
backend/            FastAPI gateway + admin API (Python)
  src/gatekey/       application code
  alembic/           database migrations
  tests/             unit + integration tests
  docs/design/        architecture/design docs per phase slice
  examples/           Python & JS drop-in SDK-replacement examples
frontend/           Admin + non-admin console (Next.js/React)
cli-sync/           Standalone `gatekey-sync` CLI helper (Phase 3, §3.7a) —
                    keeps a rotated personal API key synced to a local CLI
                    tool via the OS keychain; installed and run separately,
                    not part of docker-compose (`pip install -e cli-sync/`)
devops/keycloak/    Dev-only Keycloak realm export for `--profile sso`
docker-compose.yml  Local self-hosted deployment
.env.example        Required environment variables for docker-compose
gatekey/            Product requirements (source of truth for scope)
```

## Quick start (docker-compose) — target: under 60 minutes, `git clone` to first proxied request

Phase 2 adds nothing to this flow: no SSO, no IdP, no new required secrets.
The two Phase 1 secrets are still the only required configuration, and the
admin token signs you into every screen.

1. **Copy the environment template and fill in two secrets:**

   ```bash
   cp .env.example .env
   python -c "import secrets; print(secrets.token_urlsafe(32))"      # -> GATEKEY_ADMIN_TOKEN
   python -c "import os,base64; print(base64.b64encode(os.urandom(32)).decode())"  # -> GATEKEY_MASTER_KEY
   ```

   Paste both values into `.env`.

2. **Start everything:**

   ```bash
   docker-compose up --build
   ```

   This starts Postgres, applies every database migration automatically
   (the backend container's entrypoint runs `alembic upgrade head` before
   starting the server — no separate manual migration step), starts the
   backend on `http://localhost:8000`, and the console on
   `http://localhost:3000`. The bundled Keycloak service does **not** start
   with plain `docker-compose up` — it is gated behind `--profile sso` and
   only relevant if you opt into SSO (see below).

3. **Open the console** at `http://localhost:3000`. Sign in with the
   `GATEKEY_ADMIN_TOKEN` value from your `.env` file. If no provider key is
   configured yet you'll land on the first-run "connect your first
   provider" step — add at least one real provider API key (OpenAI,
   Anthropic, or Vertex AI); it's validated against the provider live before
   being saved. (The first-run wizard is scoped to these three providers to
   keep initial setup fast — Ollama and OpenRouter are configured afterward
   from the Providers screen, see below.)

4. **Create a user, a team, and a service-account key.** This step changed
   slightly from Phase 1: every **new** service-account key must be
   attributed to a *(user, team)* pair, and the user must already be a
   member of that team (keys created before Phase 2 keep working unchanged
   against the old flat per-user budget).

   1. **Users** screen — create a user (the human or cost center spend is
      attributed to).
   2. **Teams** screen — create a team (optionally with a budget ceiling),
      open it, and add the user as a member with a budget. For team-bound
      keys, this membership budget is what's enforced — not the flat budget
      on the Users screen.
   3. **Service Accounts** screen — create the key, picking that user and
      team. The key's plaintext secret (`gk_sk_...`) is shown exactly once
      at creation time, copy it immediately.

5. **Make your first proxied request:**

   ```bash
   curl http://localhost:8000/v1/chat/completions \
     -H "Authorization: Bearer gk_sk_..." \
     -H "Content-Type: application/json" \
     -d '{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hello"}]}'
   ```

   Switching an existing internal app from a direct provider SDK call to
   Gatekey requires changing only the base URL (to `http://localhost:8000`)
   and the API key (to the `gk_sk_...` service-account secret) — see
   `backend/examples/` for drop-in Python and JavaScript examples. A
   personal key (`gk_pk_...`, see SSO below) works identically in that
   header.

## Optional: SSO (single sign-on)

SSO is entirely opt-in. With it, real people sign in through your identity
provider instead of sharing the admin token: each user gets an individual
identity, a role, self-service personal API keys, and their actions show up
attributably in the audit log. Without it, the admin token remains the only
auth path and the SSO routes simply return 404.

### Configuration

Four env vars enable SSO, and they are all-or-none — set all four or none;
the backend fails fast at startup on a partial set:

```
GATEKEY_OIDC_ISSUER_URL      # e.g. https://your-tenant.okta.com or http://keycloak:8080/realms/gatekey-dev
GATEKEY_OIDC_CLIENT_ID
GATEKEY_OIDC_CLIENT_SECRET   # Gatekey is a confidential client - the browser never sees this
GATEKEY_OIDC_REDIRECT_URI    # http://<backend-host>:8000/v1/auth/sso/callback
```

Two session vars tune the resulting cookie:

```
GATEKEY_SESSION_COOKIE_SECURE   # default true. Set false ONLY for local http dev -
                                # otherwise the browser never sends the cookie over http
                                # and every SSO login appears to silently fail.
GATEKEY_SESSION_TTL_HOURS       # default 12
```

### Trying it locally with the bundled Keycloak

A dev-only Keycloak IdP ships in `docker-compose.yml` behind the `sso`
profile — plain `docker-compose up` never starts it.

> **WARNING — dev-only credentials.** The checked-in Keycloak admin login
> (`admin`/`admin`), the realm's fixed client secret
> (`devops/keycloak/gatekey-realm.json`), and the seeded test user are for
> local development and testing **only**. Never expose this Keycloak
> container publicly and never front a production Gatekey deployment with
> it — for production, point the `GATEKEY_OIDC_*` vars at your real IdP
> with a real client secret.

1. In `.env`, uncomment the SSO block (the values are pre-filled to match
   the checked-in realm):

   ```
   GATEKEY_OIDC_ISSUER_URL=http://keycloak:8080/realms/gatekey-dev
   GATEKEY_OIDC_CLIENT_ID=gatekey-backend
   GATEKEY_OIDC_CLIENT_SECRET=gatekey-dev-client-secret
   GATEKEY_OIDC_REDIRECT_URI=http://localhost:8000/v1/auth/sso/callback
   GATEKEY_SESSION_COOKIE_SECURE=false
   ```

   Note the issuer host is `keycloak:8080` (the in-compose service name) —
   the backend reaches Keycloak container-to-container, while Keycloak's
   own hostname config keeps browser redirects on `localhost:8080`, so both
   work at once. Only if you run the backend *outside* compose (local dev
   against `--profile sso` Keycloak alone) should the issuer be
   `http://localhost:8080/realms/gatekey-dev` instead.

2. Start everything including Keycloak:

   ```bash
   docker compose --profile sso up --build
   ```

   Keycloak imports the `gatekey-dev` realm on startup: OIDC client
   `gatekey-backend` and one seeded test user, **`testuser`** /
   **`testpassword`**. The Keycloak admin console is at
   `http://localhost:8080` (`admin`/`admin`) if you want to add more test
   users — you'll need at least two users to exercise the Team Lead
   approval flow end-to-end.

3. Open `http://localhost:3000` — the login screen now shows a
   "Sign in with SSO" button above the admin-token field (it probes the
   backend and only appears when SSO is actually configured). Sign in as
   `testuser`/`testpassword`.

4. **First login lands on onboarding**, not the console: a brand-new SSO
   user has no role and no team. They confirm their name, pick a team, and
   submit a join request (one pending request per user at a time), then sit
   on a holding screen until it's decided. A Team Lead of that team sees
   the request under **My Team → Join Requests** and approves it with a
   budget in one step; if the team has no Team Lead (or the request has
   been pending five business days), it appears in the Org Admin queue
   instead — the break-glass admin session can approve it from the team's
   detail page on the **Teams** screen.

5. **Granting org-wide roles** (Org Admin, Auditor) currently has **no
   console UI** — it's API-only this phase. Find the user's id on the
   Users screen, then:

   ```bash
   curl -X PATCH http://localhost:8000/v1/admin/users/<user-id>/org-role \
     -H "Authorization: Bearer $GATEKEY_ADMIN_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"org_role": "org_admin"}'    # or "auditor", or null to clear
   ```

   Team-level roles (Team Lead / Member) *are* editable in the UI, on the
   team's members table.

6. Once approved, the user lands in the non-admin console (see "Non-admin
   console" below), can mint a personal key under **My API Keys**
   (`gk_pk_...`, plaintext shown once), and can call the gateway with it
   exactly as in Quick Start step 5.

### Production IdPs (Okta, Azure AD, Google Workspace, ...)

The flow is a standard, provider-agnostic OIDC authorization-code flow
(discovery document, PKCE, `sub` claim as the durable user identifier), so
any spec-compliant IdP should work by pointing the four `GATEKEY_OIDC_*`
vars at it: register Gatekey as a **confidential** web client with the
callback URL `https://<your-backend>/v1/auth/sso/callback` and scopes
`openid profile email`. The same caveat convention as elsewhere in this
README applies: **only Keycloak has actually been exercised end-to-end** —
Okta/Azure AD/Google Workspace are structurally compatible but were not
live-verified in the environment this code was produced in (no real IdP
tenant available). The Identity & Access screen's "Test connection" button
does a live discovery-document fetch against your configured issuer, which
is a quick first sanity check. Verify a full login round-trip against your
real IdP before relying on it, and report back what you find.

## Optional: threshold-alert email (SMTP)

Webhook alerts need no server config (the URL is set per team on the
team's alert settings). Email alerts additionally need SMTP env vars —
leave them all unset and the email notifier is a silent no-op (an
informational log line at startup, never an error), regardless of any
team's email toggle. If any `GATEKEY_SMTP_*` var is set, `HOST` and
`FROM_ADDRESS` are required (startup fails fast otherwise). Reminder:
email delivery is implemented but **unverified-live** — see Known
limitations.

## Environment variables reference

| Variable | Required | Description |
|---|---|---|
| `GATEKEY_ADMIN_TOKEN` | yes | Break-glass/bootstrap bearer token for the admin API/console. Phase 1's only credential; since Phase 2 it is a permanent secondary path alongside SSO, keeping full Org Admin rights on every endpoint and screen. Audit entries record its actions as `system:admin_token`. |
| `GATEKEY_MASTER_KEY` | yes | Base64-encoded 32-byte AES-256 key. Encrypts provider keys (and, since Phase 2, team webhook URLs) at rest. Losing this key makes the stored secrets unrecoverable — back it up. |
| `GATEKEY_PROVIDER_VALIDATION_TIMEOUT_SECONDS` | no (default `8`) | Timeout for the live validation call made when a provider key is entered. |
| `GATEKEY_PUBLIC_API_BASE_URL` | no (default `http://localhost:8000`) | Browser-facing backend URL the frontend is built against — override if the console is accessed from somewhere other than `localhost`. |
| `GATEKEY_FRONTEND_ORIGIN` | no (default `http://localhost:3000`) | The console's browser origin — always in the CORS allowlist and the origin session cookies are exchanged with. Must be the exact real origin if the console isn't on `localhost:3000`: credentialed CORS (session cookies) forbids wildcards. |
| `GATEKEY_CORS_ALLOWED_ORIGINS` | no | Comma-separated *additional* browser origins allowed to call the API. `*` entries are ignored since Phase 2 (wildcards are incompatible with cookie auth). |
| `GATEKEY_OIDC_ISSUER_URL` | no† | OIDC issuer base URL (discovery document is fetched from `<issuer>/.well-known/openid-configuration`). Unset = SSO disabled, SSO routes 404. |
| `GATEKEY_OIDC_CLIENT_ID` | no† | OIDC client id registered at your IdP. |
| `GATEKEY_OIDC_CLIENT_SECRET` | no† | Confidential-client secret. Never logged; the Identity & Access screen reports only whether it is configured, never the value. |
| `GATEKEY_OIDC_REDIRECT_URI` | no† | Must exactly match the redirect URI registered at the IdP: `http(s)://<backend>/v1/auth/sso/callback`. |
| `GATEKEY_SESSION_COOKIE_SECURE` | no (default `true`) | `Secure` flag on the session cookie. Set `false` only for local plain-http dev — over http with the default, the cookie is never sent and SSO login silently fails. |
| `GATEKEY_SESSION_TTL_HOURS` | no (default `12`) | Session lifetime. |
| `GATEKEY_SMTP_HOST` | no‡ | SMTP server for threshold-alert email. Unset = email notifier is a no-op. |
| `GATEKEY_SMTP_PORT` | no (default `587`) | SMTP port. |
| `GATEKEY_SMTP_USERNAME` | no | SMTP auth username (omit for unauthenticated relays). |
| `GATEKEY_SMTP_PASSWORD` | no | SMTP auth password. |
| `GATEKEY_SMTP_FROM_ADDRESS` | no‡ | `From:` address on alert emails. |
| `GATEKEY_SMTP_USE_TLS` | no (default `true`) | STARTTLS on the SMTP connection. |
| `GATEKEY_TRUST_PROXY_HEADERS` | no (default `false`) | Trust `X-Forwarded-For`/`X-Real-IP` for audit source-IP capture. Only enable behind a reverse proxy that overwrites/strips these headers on the way in — a client-supplied header is otherwise fully spoofable. Leave unset for a self-hosted instance exposed directly to the internet. |
| `DATABASE_URL` | set by `docker-compose.yml` | Postgres connection string (`postgresql+asyncpg://...`). Only needed manually for local (non-docker) backend development. |
| `GATEKEY_REDIS_URL` | no | Redis connection URL for Phase 4 features (rate limiting, caching, shared state). Format: `redis://<host>:<port>/<db>`. Unset = Redis features disabled. |

† The four `GATEKEY_OIDC_*` vars are all-or-none: set all four or none
(startup fails fast on a partial set).
‡ If *any* `GATEKEY_SMTP_*` var is set, `HOST` and `FROM_ADDRESS` become
required (startup fails fast otherwise).

## Local development (without docker-compose)

**Backend:**

```bash
cd backend
python -m venv .venv && .venv/Scripts/activate   # or: source .venv/bin/activate
pip install -e ".[dev]"
# Start Postgres yourself (or reuse docker-compose's postgres service), then:
alembic upgrade head
uvicorn gatekey.main:create_app --factory --reload
```

Run tests: `pytest tests/unit` (no external dependencies) and
`pytest tests/integration` (spins up a throwaway Postgres container via
Docker automatically, or set `GATEKEY_TEST_DATABASE_URL` to point at one
you already have running).

For SSO work against a locally-running (non-docker) backend, start just the
IdP with `docker compose --profile sso up keycloak` and use
`GATEKEY_OIDC_ISSUER_URL=http://localhost:8080/realms/gatekey-dev` (the
host-published port, not the in-compose service name).

**Frontend:**

```bash
cd frontend
npm install
cp .env.example .env.local   # NEXT_PUBLIC_API_BASE_URL, defaults to http://localhost:8000
npm run dev
```

## Admin console screens (Phase 4 additions in **bold**)

Visible to break-glass-token sign-ins and Org Admin SSO sessions alike (the
token sees the identical nav — it just has no personal identity, so the
personal screens listed under "Non-admin console" never appear for it).

- **Dashboard** — total spend, request count, avg latency, error rate, spend
  over time, spend by model, spend by user (with budget bars).
- **Providers** — add/edit/remove OpenAI, Anthropic, Vertex AI, Ollama, and
  OpenRouter keys (5 fixed provider slots). Keys are validated live against
  the provider before saving and are never shown again in plaintext after
  entry. Ollama takes a `base_url` (your self-hosted instance) instead of an
  API key; OpenRouter is a plain API key like OpenAI's.
- **Users** — every user in the org: admin-created cost centers and
  SSO-provisioned people alike. The flat USD budget set here now only
  governs **legacy** (pre-Phase-2, team-less) service-account keys — once a
  user belongs to a team, new keys charge against their per-team membership
  budget instead (set on the Teams screen).
- **Service Accounts** — per-app credentials (`gk_sk_...`) your internal
  apps authenticate with. Every new key is attributed to a *(user, team)*
  pair — the user must already be a member of that team — and charges that
  membership's budget; the secret is shown once. The keys table is a
  unified listing of **both** key types org-wide (app `gk_sk_` and personal
  `gk_pk_`, with a type/owner column); an Org Admin can revoke or
  regenerate either type from here. Personal keys are never *created* here
  — they come from My API Keys or a Team Lead.
- **Model Policy** — org-wide allowlist or denylist of which models traffic
  may reach. This is the baseline that team-level restrictions can only
  narrow.
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
  time range. Break-glass actions appear as `system:admin_token`.
- **Identity & Access** — read-only view of the SSO configuration (issuer,
  client id, redirect URI, and whether a client secret is set — never the
  value), plus a "Test connection" button that live-fetches the issuer's
  discovery document. Deliberately not editable in the UI: SSO config is
  env-var-only this phase (design doc ADR-8) — edit `.env` and restart to
  change it.

### Phase 4 Admin Screens

- **Providers** (extended) — each configured key now shows health status
  (healthy/degraded/unavailable/unknown), last check time, last error, a
  manual "Check now" trigger, and a per-key failover control (enable/disable
  + pick a same-provider backup key).
- **Backup Groups** — create/list/delete named groups of provider keys for
  failover routing (Org Admin only).
- **Failover Events** — read-only, filterable (date range) log of every
  failover retry that occurred, with the from/to key resolved to its label.
- **Rate Limiting** — configure org-wide or per-team rate limit rules
  (requests and/or tokens per minute, plus an individual-user scope), choose
  "reject immediately" or "queue and retry" behavior, and set maximum queue
  wait time. Org Admins manage org-wide rules; Team Leads manage their own
  team's rules from the same screen (or the Team Lead's "Reliability" view).
- **Caching Settings** — org-level kill switch + TTL, plus per-team opt-in
  and TTL (Team Lead-manageable for their own team). Includes a teaser-only
  cache-entries browser (never full prompt/response content) and a
  clear-cache action (org-wide or team-scoped).
- **Degradation Policy** — configure automatic model downgrades when spending
  reaches a threshold percentage of budget, org-wide or per-team. Set trigger
  percentage and target cheaper model (validated against the team's allowed
  model list). A degradation-events log shows every downgrade that fired.
- **Team Lead "Reliability" view** — a Team Lead's own-team equivalent of the
  four screens above, scoped to only the team(s) they lead.

### Phase 5 Admin Screens

- **Audit Log** (extended) — a hash-chain integrity badge + "Verify now"
  button once the chain is enabled (Compliance Settings screen toggles it,
  disabled/greyed out while a finite retention window is configured, and vice
  versa); CSV/JSON export includes the chain columns when enabled.
- **Differentiators → Drift Detector** — per-model status/trend table,
  expandable plain-language alert detail ("+45% latency vs. baseline"), view
  canary history, per-model enable/disable, export an alert to the audit log.
  Org Admin configures; Org Admin + Auditor view.
- **Providers** (extended) — a new "Self-Hosted Models" card to register/
  edit/remove a vLLM/Ollama/OpenAI-compatible self-hosted endpoint (name,
  base URL, bearer token — never shown again after entry, GPU-hour cost
  basis, served model ids, verified badge + re-verify button).
- **Differentiators → Self-Hosted Governance** — cost-normalization cross-
  link view (requests/estimated cost/latency), explicitly labeled
  "estimated, not an invoice."
- **Model Policy → Content-Aware Routing** (extended) — all four categories
  (PII, source code, financial data, legal) are now functionally equal, each
  with its own admin-defined allowed-models list; a "Sensitivity Label
  Mappings" table (replacing the earlier exclusive Purview/Google-DLP radio
  mock) lets you map your own enterprise label strings to a Gatekey category.
- **Differentiators → Shadow AI** — detection-source selector, one-time-
  reveal ingestion token generation, known-AI-tool-hostname allowlist CRUD,
  the usage report (user/tool/frequency/last-seen, "not linked to a Gatekey
  user" for unmatched rows, a repeat-violator indicator), enforcement-mode
  controls (notification/webhook, both off by default, each behind an
  explicit confirmation dialog), and a link to the feature's data-handling
  policy. Org Admin (full) and Auditor (read-only) see the org-wide view; a
  Team Lead sees an own-team-scoped equivalent under "My Team."

### Custom Model Registry Admin Screens

- **Providers** (extended) — a new "Custom Models" card to register/edit/
  remove/test a BYOK custom model (name, provider, native model id,
  capability, pricing, verified badge + "Test model" button, "Shadowed by
  registry update" warning badge). Org Admin full CRUD + verify; Auditor
  read-only; not shown to Team Lead/Member.
- **Model Policy** (extended) — a new "Custom" group in the allow/denylist
  checklist, sourced from registered custom models; only verified models are
  selectable (unverified ones show disabled with a "re-verify on Providers"
  link).

## Non-admin console

SSO sessions whose user is *not* an Org Admin get a role-appropriate nav
instead of the admin screens — server-side enforcement backs every one of
these (the UI hiding a control is never the only guard).

- **Every SSO user** (Member and up): **My Usage** (own spend/requests over
  time, budget position), **Model Access** (per-model allowed/blocked with
  a plain-language reason naming which layer blocks it — org policy or team
  restriction), **My API Keys** (mint/regenerate/revoke own `gk_pk_` keys;
  team-bound, optional expiration, secret shown once).
- **Team Leads** additionally get a "My Team" section: **Join Requests**
  (approve with a budget / reject with a reason), **Team Dashboard** (team
  spend vs. ceiling, period position), **Members & Budgets** (add/remove
  members, roles, per-member budgets, budget reassignment, members' keys),
  and **Model Restrictions** (narrow the org baseline for this team) — all
  scoped strictly to teams they actually lead.
- **Auditors** get a read-only org-wide nav: **Org Usage** (aggregate
  summaries), **Org Logs** (the audit-event trail; per-request log browsing
  is a Phase 3 item — see Known limitations), and **Policy Viewer** (org
  baseline + every team's restrictions). No mutation controls anywhere.
- **New users** see only the onboarding screens (profile + team selection,
  then a holding screen) until their join request is approved.

## Known limitations / deviations from the design docs

### Phase 1

- **First-run "setup wizard" auth step.** `gatekey/phase-1-admin-console-ui-requirements.md`
  describes a wizard step that "sets" the admin credential. The actual
  backend auth model (`backend/src/gatekey/api/deps.py`) is a single shared
  token provisioned via the `GATEKEY_ADMIN_TOKEN` environment variable
  before the process starts — there is deliberately no API that persists a
  new admin credential (an unauthenticated "set admin token" endpoint would
  itself be a privilege-escalation hole). The frontend's `/setup` screen
  therefore only covers "connect your first provider"; signing in at
  `/login` with the already-provisioned token is the de facto first step.
  See `frontend/src/lib/api.ts`'s module docstring for the same note in
  code.
- **Provider pricing figures** (`backend/src/gatekey/providers/pricing.py`)
  are standard, non-cached, non-batch published rates as documented at
  implementation time. This build environment has no live web access, so
  they could not be freshly re-verified against each provider's current
  pricing page before shipping — **confirm these against OpenAI's,
  Anthropic's, Google Cloud's, and OpenRouter's live pricing pages before
  relying on this for real budget enforcement**, and update the table (a
  code change +
  redeploy) if a provider has since repriced. Every entry records the
  `as_of` date and `source` URL it was sourced from for exactly this reason.
- **`docker-compose up` was not executed end-to-end in the environment this
  code was produced in** (Docker Desktop's service was stopped and could
  not be started without elevated permissions in that sandbox). The compose
  file's syntax/interpolation was validated (`docker compose config`), the
  Dockerfiles/entrypoint were reviewed by inspection, and the same
  migrations were validated against a real Postgres via the integration
  test suite earlier in that session (before Docker became unavailable) —
  but a fresh `git clone` → `docker-compose up` → first-request pass has not
  been personally re-run against these exact final files. Run it yourself
  before depending on this for a real pilot; report back if anything in the
  Quick Start steps above doesn't match what you see.
- **Streaming usage accuracy for Vertex AI (Gemini)** is the least formally
  guaranteed of the three providers' streaming-usage contracts (Google
  documents `usageMetadata`'s presence on every streamed chunk less
  formally than OpenAI's/Anthropic's own streaming-usage guarantees) — see
  `backend/src/gatekey/providers/vertex_ai.py`'s `stream_chat_completion`
  docstring. Verify against a real or recorded Vertex streaming response if
  precise per-request billing accuracy for Gemini streaming traffic matters
  to your deployment.
- **Ollama-routed requests are priced at $0.00**, not because inference is
  free but because there is no per-token provider invoice for a self-hosted
  target to charge against. This does not capture real GPU/infrastructure
  cost — do not treat Ollama's dashboard spend figure as your true cost of
  running those models. Streaming Ollama requests' token-usage accounting
  (independent of the $0 cost) also depends on Ollama's OpenAI-compatible
  layer honoring `stream_options.include_usage` the same way OpenAI does;
  this was not verified against a live Ollama instance as of this writing —
  see `backend/src/gatekey/providers/ollama.py`'s `stream_chat_completion`
  docstring.

### Phase 2

- **Email threshold alerts are implemented but UNVERIFIED-LIVE.** The SMTP
  notifier is built and unit-tested, but no real SMTP credentials were
  available in the environment this code was produced in, so it has never
  delivered to a real mailbox. Same treatment as the pricing caveat above:
  test it against your own SMTP relay before relying on it. Webhook
  delivery *is* the verified alert path.
- **Only Keycloak has been exercised as an SSO IdP.** The OIDC flow is
  standard and provider-agnostic (structurally compatible with Okta, Azure
  AD, Google Workspace), but no live-IdP round-trip was possible in the
  build environment. See "Production IdPs" above.
- **No console UI for granting org-wide roles.** `PATCH
  /v1/admin/users/{id}/org-role` exists and is audited, but making a user
  an Org Admin or Auditor is curl-only this phase (see SSO step 5 above).
  Team-level roles are fully manageable in the UI.
- **The <10ms RBAC/policy-resolution overhead target (AC1.7) is
  designed-for, not load-tested.** The steady-state hot path adds only
  in-process lookups (no new DB round trips vs. Phase 1), which is
  consistent with the target, but no load-test acceptance run has verified
  it under real concurrency.
- **Budget period rollover is lazy/touch-based, not scheduled** (design doc
  ADR-10 — there is no cron/worker container). A boundary crossing is
  applied the next time *anything* touches the team: a gateway request, or
  just opening the team's page. Consequence: a fully dormant team's numbers
  won't visibly roll over at midnight on the boundary — they roll over on
  next touch, computed correctly for however many periods elapsed.
- **`rollover` compounds unspent budget indefinitely by design** (ADR-6): a
  member's unused allowance is added onto their budget for the next period,
  and keeps compounding if left unspent. This is the documented consequence
  of opting into rollover — it is why `reset` is the default — not a bug.
  Rollover credits are deliberately not re-checked against the team
  ceiling (that check is scoped to deliberate assignment-time writes).
- **Per-request log browsing is a Phase 3 item.** The Org Logs screen shows
  audit events and aggregate usage; a queryable/exportable individual
  request table (model, tokens, latency per request) does not exist yet —
  the data is recorded, the listing endpoint isn't built.
- **Currency "normalization" is identity/USD-only this phase** (ADR-9). The
  org currency setting and the `raw_provider_cost_usd`/`fx_rate_applied`
  columns exist for forward compatibility, but every rate is `1` and every
  cost is USD — there is no FX conversion.
- **Spend-time budget enforcement is check-before-call**, matching Phase
  1's model: the budget gate runs before the provider call, and the charge
  is recorded after it. Truly simultaneous in-flight requests can therefore
  briefly overshoot a member's budget, bounded by whatever is concurrently
  in flight at the cutoff moment — the counter itself is updated atomically,
  so the recorded total is exact. (Assignment-time ceiling allocation, by
  contrast, is fully serialized under a row lock and cannot over-allocate.)
- **Audit append-only is application-level discipline**, not yet a
  database-level guarantee: service code only ever inserts audit rows, but
  there is no DB trigger/rule blocking `UPDATE`/`DELETE` from a direct DB
  connection. Trigger-level hardening (and Phase 5's hash-chained ledger)
  is deferred.

### Phase 3

- **Automatic service-account key rotation has no propagation path to the
  consuming app.** Rotation mints a new secret, keeps the previous one valid
  for a short overlap window, and notifies — but unlike personal keys (which
  have the `cli-sync/` helper's "fetch my current key" mechanism), there is
  no equivalent for a server-side app holding a `gk_sk_...` secret in its own
  config/env. If the app's config isn't manually updated with the new secret
  before the overlap window expires, it will start failing auth. Either
  disable automatic rotation for service-account keys whose consuming app you
  can't update in time, or treat the overlap window as your update deadline.
- **The `cli-sync/` device-code pending-auth state is in-process, single-worker
  only** (`backend/src/gatekey/services/cli_refresh_credentials.py`'s
  `DeviceAuthStore`) — a login in flight when the backend restarts, or a
  multi-replica deployment routing the `/start` and `/poll` calls to
  different workers, will fail. Fine for the shipped single-container
  `docker-compose` deployment; needs a DB-backed store before horizontal
  scaling.
- **`log_prompt_retention_days` is enforced and always-on, with no "never
  purge" option** (unlike `audit_retention_days`, which defaults to never).
  `usage_logs` and `dlp_scan_results` rows (including any raw flagged PII
  substrings, if that opt-in is enabled) are hard-deleted after 30 days by
  default. Raise the setting on the Compliance Settings screen before that
  window elapses if you need longer retention — there's no undo after a row
  is purged. See `backend/docs/compliance/data-handling-policy.md` §3.2.
- **Inbound (provider response) DLP scanning is not implemented.** The
  `scan_inbound_responses` toggle exists in the schema and API for forward
  compatibility but is rejected (422) if set to `true`; the frontend disables
  the checkbox accordingly. Only outbound (prompt) scanning is functional
  this phase.
- **Two content-aware-rule categories are inert by design.** `source_code`
  and `financial_data` rows exist in the schema and render in the console
  alongside the functional `pii` category, but nothing classifies content
  into them — only PII-triggered model restriction actually enforces.
- **Residency and access-schedule narrowing is validated at write time
  (defense-in-depth) and re-checked cumulatively at read time** (org AND
  team AND, for schedules, per-key layers are all evaluated on every
  request) — this was tightened during Phase 3's security review after an
  earlier "innermost-layer-only" read model was found to let a team's
  already-narrower rule silently outlive a subsequent org-level tightening.
  If you're auditing this area, verify against `services/residency.py`'s
  `resolve_residency` and `services/access_schedules.py`'s
  `resolve_access_schedule_decision` directly, not this note alone.
- **SCIM has not been exercised against a real IdP's live SCIM client**
  (Okta/Azure AD/etc.) — endpoint shapes follow the SCIM 2.0 RFC and are
  covered by integration tests against a real Postgres, but no live
  provisioning round-trip from an actual IdP has been run.
- **Presidio's PII detection coverage is exactly what the underlying
  library's four pattern-based recognizers (SSN, credit card, email, phone)
  plus your own custom regex patterns provide** — not independently audited
  by this project for false-negative/false-positive rates. Validate against
  representative traffic before relying on it for a real compliance
  requirement.
- **The <50ms p99 synchronous DLP scan target is measured on the build
  machine (~10-19ms warm), not load-tested at production scale/concurrency.**

### Phase 4

- **Redis is optional** — deployments without Redis continue to work but
  will not have rate limiting, caching, or shared state (failover still
  works without Redis; it doesn't depend on it). Redis is required only for
  the rate-limiting/caching/shared-state features, and is started with
  `docker-compose --profile cache up` (plain `docker-compose up` does not
  start it).
- **Health checks are active synthetic checks**, not passive/traffic-derived
  — a scheduled job makes a real test request to each provider (using the
  key's real decrypted credential) every 5 minutes and records the result.
- ~~`tokens_per_min` rate limiting is configured/validated but not yet
  enforced~~ **Closed** (hardening pass) — now genuinely enforced on the
  live path via a pre-emptive gate on real prior usage plus a retrospective
  atomic true-up (a Redis Lua script) once the real response's token count
  is known. Remaining caveat: the atomic per-minute concurrency-safety bound
  only holds when `requests_per_min` is *also* configured on the same rule —
  a token-only rule (legal under the current schema) has no atomic
  per-minute request-count gate, so its burst-overshoot exposure isn't
  bounded the way the design intends. Set a `requests_per_min` value
  alongside any `tokens_per_min` rule until this is tightened further. This
  is a cost-shaping/availability gap either way, not a budget-bypass risk:
  the hard budget-exhaustion cutoff always applies regardless of rate-limit
  configuration.
- **Rate-limit queue depth (for `queue_and_retry` teams) is not tracked or
  exposed** in the admin console — the live queue-and-retry path polls
  in-process rather than using a persisted, inspectable queue.
- **Caching is exact-match only** (no semantic/near-duplicate caching — this
  was an explicit stretch goal in the phase requirements, not a commitment).
  `POST /v1/admin/cache/clear` performs a real delete, not the literal "soft
  clear / sentinel value" described in some design-doc drafts — functionally
  equivalent to a caller, just not reversible.
- **Degradation is configured per-org/per-team** — the resolved policy uses
  cumulative resolution (both org and team layers checked, matching Phase
  3's residency/DLP precedent). The substituted model is re-validated against
  model-access/content-classification/residency policy both at config time
  and again at request time, so a policy tightened after configuration can't
  be silently bypassed.
- ~~A provider key's `failover_enabled`/`failover_target_id` can't be read
  back through the admin API~~ **Closed** (hardening pass) — `GET
  /v1/admin/provider-keys` now returns both fields directly, so the Providers
  screen shows accurate failover configuration on a fresh page load, not just
  immediately after a save.
- **Cache-lookup overhead (<10ms NFR target for a cache miss) measured
  higher (~30-36ms) than the target** in this project's Docker Desktop / WSL2
  development sandbox, even after warming the relevant config caches — the
  dominant cost observed was the Redis network round-trip itself over that
  specific networking path, not application logic. Worth re-measuring
  against your own deployment's actual Redis topology (e.g. a colocated
  Redis instance) before treating this NFR as unmet in production.

### Phase 5

- **The hash-chained audit ledger cannot detect a deleted tail.** Tampering
  with or reordering an existing entry is always caught by `GET
  /v1/admin/audit/verify`, but if someone with raw database write access
  (bypassing the application entirely) deletes the most-recent N entries
  outright, the remaining chain still verifies as internally consistent —
  there is no independently-stored "expected latest position" to compare
  against without external anchoring, which is explicitly out of scope for
  this release (see below). Don't treat an "intact" verification result as
  proof nothing was ever deleted from the tail, only that nothing already in
  the chain was altered or reordered. This limitation is now disclosed
  directly in the admin console (a tooltip on the hash-chain integrity
  badge), not just here (hardening pass).
- **No external hash-chain anchoring/timestamping service integration** —
  ships as an in-database chain only in this release, per the phase's own
  stated scope; a fast-follow if a real regulated-industry deployment
  specifically needs third-party timestamping for a compliance framework.
- **Enabling the hash chain and configuring a finite audit-retention/purge
  window are mutually exclusive** — you get either full historical
  verifiability or automatic purging, not both simultaneously in this
  release (deleting an entry structurally breaks a hash chain).
- **Drift detector's "statistically significant" drift flagging is
  threshold-based** (fixed percentage deviations against a rolling 7-run
  baseline), not a true statistical hypothesis test — and refusal detection/
  output-similarity comparison are keyword-heuristic and deterministic-
  text-metric respectively, not ML/embeddings-based. Canary prompts are a
  fixed, code-seeded set of 5 in this release, not admin-authorable.
- **Self-hosted provider cost is an estimate**, computed from your
  configured GPU-hour rate × wall-clock request latency — a proxy that
  ignores request queueing, multi-tenant GPU sharing, and cold-start
  latency. It is clearly labeled "estimated" everywhere it's shown and is
  not invoice-grade the way BYOK providers' token-based pricing is.
  Self-hosted models support chat completions only in this release, not
  `/v1/completions` or `/v1/embeddings`.
- **Content-classification's `source_code`/`financial_data`/`legal`
  categories are regex/keyword-heuristic classifiers**, not ML/embeddings-
  based — validate false-positive/negative rates against your own traffic
  before relying on them for a compliance boundary, same caveat as Phase 3's
  PII detection. Gatekey does not call out to Microsoft Purview's or Google
  DLP's own classification APIs — the sensitivity-label mapping only trusts
  a caller-supplied label string your own tooling has already computed.
- ~~Shadow AI's optional free-form ingestion metadata field has no size
  limit enforced at the schema level~~ **Closed** (hardening pass) — a
  4096-byte serialized-size cap is now enforced on `raw_metadata` at
  request-validation time (a clean 422, never a silent truncation), so the
  "connection metadata only" claim in
  `backend/docs/policy/shadow-ai-data-handling.md` §2 is technically
  enforced, not just a documented convention.
- **Shadow AI detects via SASE/proxy-log ingestion only** — no browser
  extension in this release (deferred per the phase's own stated default,
  pending confirmation that a real design partner's security stack doesn't
  already provide the SASE/proxy logging this approach assumes). It cannot
  perform true inline network blocking — the "enforcement mode" is a
  notification email and/or an outbound webhook your own SASE/SOAR tooling
  can act on, not Gatekey intercepting traffic itself (architecturally
  impossible from a passive log-ingestion detection mechanism).
- ~~No per-self-hosted-endpoint cost/usage breakdown endpoint~~ **Closed**
  (hardening pass) — `GET /v1/admin/self-hosted-providers/{id}/usage`
  returns a real per-endpoint breakdown (requests/cost/latency), built on
  the existing `usage_logs.self_hosted_provider_id` column and independently
  verified to never cross-contaminate two different endpoints' figures or
  BYOK traffic.
- **"Validate demand before building all five" (the phase doc's own stated
  success criterion) was not run as a real exercise in this build** — all
  five sub-features were built per an explicit instruction to do so, using
  the phase doc's own stated interim build-order default (lowest
  integration risk first) in place of a real design-partner-demand signal.
  A real prioritization/feedback pass with actual pilot orgs remains a
  genuinely open, valuable next step, not something this build substitutes
  for.

### Custom Model Registry

- **No auto-discovery from a provider's own list-models API.** The admin
  types the native model id by hand — deliberate, matching the static
  registry's own "not a mirror" philosophy, not a v2 candidate.
- **`ollama` is out of scope for this feature.** Ollama has its own,
  separate, already-editable mechanism (register the model under an
  existing Self-Hosted endpoint's model list instead).
- **No org-vs-team scoping, no bulk import, no tiered pricing, no scheduled
  re-verification, no price-staleness auto-detection, no versioning/
  deprecation workflow** — one flat model at a time, one flat per-token rate
  pair, manual on-demand verification only, admin fully responsible for
  keeping the entered rate current, removal is a hard delete. All deliberate
  v1 simplifications matching this codebase's existing patterns for the
  identical tradeoffs elsewhere (e.g. self-hosted models' identical set of
  simplifications).
- **A downgrade/degradation policy configured to fall back to a custom (or
  self-hosted) model name will fail rather than degrade** — the
  degradation-target resolution path doesn't yet thread the custom-model or
  self-hosted caches through. Pre-existing gap shared identically with
  self-hosted models, not introduced or worsened by this feature; fails
  closed (breaks the degradation attempt cleanly, never misroutes).

## Non-negotiables carried through these phases

- Self-hosted first — no mandatory phone-home telemetry; SSO, Keycloak, and
  SMTP are all strictly opt-in.
- No plaintext secrets at rest or in logs — provider keys, service-account
  and personal-key secrets, session tokens (hashed), and team webhook URLs
  (encrypted; a Slack webhook URL is bearer-equivalent).
- OpenAI-compatible API surface.
- Docs sufficient to self-deploy without engineering support (this file).

See `gatekey/00-overview.md` for the full cross-phase rationale,
`gatekey/phase-1-core-gateway.md` for Phase 1's exact scope, and
`gatekey/phase-2-product-spec.md` for Phase 2's stories, acceptance
criteria, and locked architecture decisions.
