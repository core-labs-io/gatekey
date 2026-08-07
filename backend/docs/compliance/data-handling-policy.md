# Gatekey — Data Handling Policy

This document describes what a **self-hosted Gatekey deployment** actually
does with data, grounded in the shipped code as of Phase 3 (Security &
Compliance Hardening). It is written for a customer's security review or
vendor risk assessment. Companion document: `data-flow-diagram.md` in this
same directory.

Read this alongside the caveats it states explicitly — several items below
are marked **not independently verified** or **known gap**, matching this
project's existing disclosure convention (see the main `README.md`'s "Known
limitations" sections). Verify anything you plan to rely on for a real
compliance sign-off.

## 1. Deployment model — there is no Gatekey-operated service

Gatekey is self-hosted software, not a SaaS product. A deployment is a
`docker-compose` stack (or an equivalent manual setup) that the customer
runs entirely on its own infrastructure: one Postgres instance, one backend
container, one frontend container. Gatekey's maintainers never receive,
store, process, or have access to any data from a deployment — there is no
Gatekey-side telemetry, license-check, update-check, or analytics call
anywhere in the codebase (`backend/src/gatekey/`). This is a stated,
repository-wide non-negotiable (`gatekey/00-overview.md`).

## 2. What Gatekey stores

All storage below is a single Postgres database that lives entirely inside
the customer's own infrastructure.

### 2.1 Request usage records (`usage_logs`)

One row per gateway request. **Gatekey does not store prompt or response
text anywhere in this table** — only request metadata:

- who/what made the request: `user_id`, `service_account_key_id`,
  `personal_api_key_id`, `team_id` (each nullable + `SET NULL` on deletion,
  so a usage row outlives the credential/user that generated it)
- what was requested: `endpoint`, `provider`, `model` (the raw string the
  caller sent, even if invalid/denied)
- accounting: `prompt_tokens`, `completion_tokens`, `cost_usd`,
  `raw_provider_cost_usd`, `fx_rate_applied`, `latency_ms`, `stream`
- outcome: `status` (e.g. `ok`, `model_denied`, `budget_exhausted`,
  `provider_error`, ...), `success`
- `created_at`

This has been true since this table was introduced (Phase 1.5) and remains
true through Phase 3 — no schema change in this phase added a prompt/
response body column. Source: `backend/src/gatekey/db/models/usage_log.py`.

### 2.2 DLP scan results (`dlp_scan_results`)

One row per scanned request (when any DLP detector or custom pattern is
enabled). By default this stores only the **detector or pattern name** and
the **action taken** (`log`/`redact`/`block`) per finding — never the
flagged text itself:

```json
{"findings": [{"detector_or_pattern_name": "US_SSN", "action": "redact"}], "raw_flagged_content": null}
```

`raw_flagged_content` (the actual flagged substring(s)) is stored **only**
if an Org Admin explicitly turns on `dlp_policies.store_raw_flagged_content`
— off by default. Turning this on means Gatekey will start persisting the
literal PII values it detects; understand that tradeoff before enabling it.
Source: `backend/src/gatekey/services/dlp.py` (`record_scan_result`),
`backend/src/gatekey/db/models/dlp_scan_result.py`.

### 2.3 Governance audit trail (`audit_entries`)

Every governance mutation (team/member/budget/policy/key changes, DLP
policy changes, residency rule changes, rotation events, access-schedule
changes, SCIM provisioning/deprovisioning, join-request decisions, role
grants) writes an append-only row: `actor_user_id` + `actor_label`
(name/email snapshot, or the `system:admin_token` / `system:scim`
sentinels), `action`, `target_type`/`target_id`, `old_value`/`new_value`
(JSON), `created_at`, and (Phase 3) `source_ip`. `old_value`/`new_value`
capture field-level state, not raw request content — this table is a
who-changed-what ledger, not a content log.

### 2.4 Credentials

- **Provider API keys** (OpenAI, Anthropic, Vertex AI, OpenRouter) and
  **Ollama's `base_url`** — encrypted (see §4).
- **Team webhook URLs** — encrypted (see §4); a Slack incoming-webhook URL
  is bearer-equivalent, so it gets the same treatment as a provider key.
- **Service-account key secrets** (`gk_sk_...`), **personal key secrets**
  (`gk_pk_...`), **CLI refresh credentials** (`gk_rf_...`), **session
  cookie tokens**, and the **SCIM bearer token** — one-way SHA-256 hashed,
  never stored reversibly, never shown again after creation (see §4).

### 2.5 Identity data

`User` rows hold a name and email (self-entered, or asserted by the org's
own OIDC IdP / provisioned via SCIM). `sso_subject` (OIDC `sub`) and
`scim_external_id` are correlation identifiers for the customer's own IdP,
not data Gatekey originates.

### 2.6 What is not built: data-subject erasure

SCIM deprovisioning revokes a deactivated user's active sessions, personal
keys, team-attributed service-account keys, and CLI refresh credentials —
but `User` and `TeamMembership` rows themselves are never deleted (by
design, for audit/history preservation). Gatekey does not implement any
"erase all data for this person" workflow (e.g. a GDPR/CCPA deletion
request) beyond an Org Admin manually deleting rows via the admin API. If
your review requires a formal data-subject-erasure capability, treat this
as a gap to plan around, not a shipped feature.

## 3. Retention

Retention is configured per org on the Compliance Settings screen
(`GET`/`PUT /v1/admin/compliance-settings`), backed by the
`compliance_settings` table.

### 3.1 Audit trail — `audit_retention_days`

Default: `NULL`, meaning **never auto-purged**. `audit_entries` is
otherwise append-only by application-level discipline (service-layer code
only ever `INSERT`s into it) — the **one sanctioned exception** is a
scheduled purge job (`services/scheduler.py: run_audit_purge_if_due`) that
only runs, and only deletes rows older than the configured cutoff, when an
Org Admin has explicitly set a finite `audit_retention_days`. This purge is
not reachable from any admin-facing mutation endpoint — it only fires from
the in-process background scheduler loop, on a fixed poll interval, batched
(5,000 rows per delete) to avoid a long-running transaction. Source:
`backend/src/gatekey/db/models/audit_entry.py` (module docstring states the
exception explicitly), `backend/src/gatekey/services/scheduler.py`.

**Important caveat**: this append-only guarantee is application-level, not
database-level. There is no Postgres trigger or rule blocking a direct
database connection from issuing `UPDATE`/`DELETE` against `audit_entries`
outside of Gatekey's own service code. If your review requires a
tamper-evident ledger (e.g. hash-chaining), that is not yet built — it is
an explicitly deferred Phase 5 item.

### 3.2 Usage logs / DLP scan content — `log_prompt_retention_days`

Default: **30 days, and always enforced** — unlike `audit_retention_days`
(§3.1), this column is `NOT NULL` at the schema level: there is no
"disable purging" state for this setting. The background scheduler loop
runs `run_log_prompt_purge_if_due`
(`backend/src/gatekey/services/scheduler.py`) on the same fixed poll
interval as the audit purge, hard-deleting `usage_logs` rows **and**
`dlp_scan_results` rows (including any `raw_flagged_content`, if that
opt-in is turned on) older than the configured cutoff, batched (5,000 rows
per delete) to avoid a long-running transaction.

**Practical implication for a fresh deployment**: usage/request metadata
and any stored flagged-PII substrings are deleted after 30 days by
default, with no admin action required and no way to fully disable it
(the minimum is a very large `log_prompt_retention_days` value, not
`NULL`/off). If your review requires longer retention of usage metadata
for internal reporting, raise `log_prompt_retention_days` on the
Compliance Settings screen accordingly *before* the default 30-day window
elapses on data you need to keep — there is no undo once a row is purged.

### 3.3 Everything else

No other table has a retention/purge mechanism. Provider keys, webhook
URLs, and hashed credentials persist until explicitly deleted/revoked by an
admin action.

## 4. Encryption

### 4.1 At rest

Two distinct mechanisms are used, deliberately different for reversible
vs. never-needs-to-be-reversed data:

- **AES-256-GCM, reversible** (`backend/src/gatekey/services/encryption.py`)
  — used for provider API keys and team webhook URLs. A fresh random
  12-byte nonce is generated per encryption call (`os.urandom`); ciphertext,
  nonce, and the 16-byte GCM authentication tag are stored in three separate
  columns. Associated data binds each ciphertext to `{org_id}:{provider}`
  (or `{team_id}` for webhooks), so a ciphertext row copied to a different
  org/provider/team fails authentication rather than decrypting silently —
  tampering is detected, not just encryption. The 32-byte master key comes
  from the `GATEKEY_MASTER_KEY` environment variable (`EnvKeyProvider`); it
  is never persisted in the database. **Losing this key makes every stored
  provider key and webhook URL permanently unrecoverable — back it up
  outside the deployment.** During guided provider-key rotation, the prior
  key's ciphertext/nonce/tag are kept in `previous_*` columns for the
  overlap-display window, encrypted the same way.
- **SHA-256, one-way, not reversible** — used for service-account key
  secrets, personal key secrets, CLI refresh credentials, session cookie
  tokens, and the SCIM bearer token. These are all "shown once at creation,
  never again" secrets by design — Gatekey never needs to recover the
  plaintext, only confirm a presented value hashes to the stored digest, so
  a fast cryptographic hash (not a slow password KDF) is deliberately used;
  the design doc notes the auth path's own latency budget as the reason.

No plaintext secret of any kind is written to application logs — decryption
and hash-comparison error paths return static, generic error messages that
never include plaintext, ciphertext, or nonce bytes.

### 4.2 In transit

**Gatekey does not terminate TLS itself.** The default `docker-compose.yml`
publishes the backend on plain `http://localhost:8000` and the console on
plain `http://localhost:3000` — there is no TLS/HTTPS listener, certificate
configuration, or reverse proxy in the shipped compose file. If you deploy
this beyond a single trusted host (i.e. any real deployment), you are
responsible for putting a TLS-terminating reverse proxy (nginx, Caddy,
your cloud load balancer, etc.) in front of both the backend and the
console, and for routing browser-to-console and client-to-gateway traffic
through it. Do not treat "Gatekey supports HTTPS" as a shipped feature to
verify — it isn't one; the deployer supplies it.

Outbound calls Gatekey itself makes — to the configured AI provider, to a
webhook URL, to an OIDC IdP's discovery/token endpoints — use whatever
scheme the configured URL specifies; providers' documented API endpoints
are `https://` by convention, so that leg is normally TLS-protected, but
Gatekey does not enforce or validate this — a self-hosted Ollama `base_url`
or a webhook URL configured as plain `http://` will be called as configured.

## 5. DLP / PII handling

Built on Presidio (`backend/src/gatekey/services/dlp.py`), running
in-process inside the backend container — no external call, no data leaves
the deployment for scanning.

- **What's scanned**: four built-in detectors (SSN, credit card, email,
  phone — each independently toggleable, all off by default) plus any
  org-authored custom regex patterns. The Presidio recognizer set is
  deliberately restricted to pattern-based detectors only (no NER/"person
  name" detection) to keep the synchronous scan path fast — see §7 for the
  latency caveat.
- **`log`**: the finding is recorded (see §2.2); the request proceeds
  unmodified.
- **`redact`**: the flagged substring is replaced with `[REDACTED]` in the
  text actually forwarded to the provider — the redacted version, not the
  original, is what the provider sees. Redaction happens synchronously,
  before the outbound provider call, when any configured action anywhere
  (org/team default or any custom pattern) could redact or block.
- **`block`**: the request is rejected (403) before it ever reaches the
  provider — no partial/redacted forward happens for a blocked request.
- **Action precedence**: built-in-detector findings use the org's
  `default_action`, which a team can override (action only, one layer, no
  per-key override); each custom pattern carries its own independent action
  that is never overridden by the team setting.
- **Raw flagged content is not stored** unless `store_raw_flagged_content`
  is explicitly turned on (§2.2, §3.2).
- **Inbound (provider response) scanning** is a separate opt-in
  (`scan_inbound_responses`, off by default) — outbound (prompt) scanning
  and inbound (response) scanning are independently controlled.
- **Content-aware model routing**: an org can additionally restrict which
  models PII-flagged content is allowed to reach (`content_aware_rules`,
  category `pii`). Two other category rows (`source_code`,
  `financial_data`) exist in the schema and render in the console but are
  **not wired to any real classifier this phase** — they persist and
  display but never trigger, by design, not by bug.

## 6. Data residency controls

Residency enforcement is **entirely opt-in**: with no `residency_rules` row
configured, there is no residency restriction of any kind, org-wide or per
team. If an Org Admin creates a rule, its `violation_behavior` defaults to
`hard_block` at the database column level (a create request cannot silently
default to `warn`). A team-level rule can only narrow the org-wide rule's
allowed regions, never widen it (rejected server-side, 422, if attempted).

Region resolution is provider-dependent: OpenAI and Anthropic resolve to a
static `us` region (fixed, non-configurable — these are multi-tenant cloud
APIs with no per-org region choice); Vertex AI resolves from the configured
key's location; a self-hosted Ollama instance's region is whatever an admin
explicitly tags it as (unset by default); OpenRouter's region is always
unresolved (`unknown`) because it aggregates arbitrary backend
providers/regions with no single knowable answer. Under an active rule, an
unresolved/unknown region always fails the allowlist — it is never silently
passed through.

Both hard-block and warn outcomes write a synchronous audit entry.

## 7. Access controls

Four roles, enforced server-side on every privileged endpoint (never a
UI-only guard): **Org Admin** and **Auditor** (org-wide), **Team Lead** and
**Member** (per team). The `GATEKEY_ADMIN_TOKEN` break-glass credential
remains a permanent path with full Org Admin rights on every endpoint and
console screen; its actions are recorded in the audit trail as actor
`system:admin_token`.

`audit_entries.source_ip` (Phase 3) is populated best-effort from the
request's client address, or from `X-Forwarded-For`/`X-Real-IP` only if the
deployer explicitly enables `GATEKEY_TRUST_PROXY_HEADERS` (off by default —
trusting a client-supplied header without a configured trusted-proxy
boundary is itself a spoofing risk, so this is deliberately not on by
default). A genuinely unavailable source IP never blocks the audit write —
it is recorded as `NULL`.

SCIM deprovisioning (`PATCH active:false` / `DELETE /Users/{id}`) revokes
every active session, personal key, team-attributed service-account key,
and CLI refresh credential belonging to the deactivated user in one step,
and blocks that user from starting a new SSO session — not just revoking
existing credentials, but preventing re-authentication.

## 8. Credential rotation

- **Service-account keys**: fully automatic when enabled (org default or
  per-key policy) — mint a new secret, notify (webhook and/or email), keep
  the prior secret valid for a configurable overlap window
  (`overlap_buffer_minutes`, default 5), then it stops being accepted. No
  admin action is required per cycle; a background scheduler loop
  (in-process `asyncio`, no separate container) fires due rotations on a
  fixed poll interval and is safe under multiple backend replicas via an
  atomic claim-and-advance database update.
- **Provider keys**: always manual/guided, never fully automatic — there is
  no provider-side key-issuance API to automate against. An admin pastes a
  new key, Gatekey validates it live against the provider, then starts using
  it for every outbound call immediately; the prior key's ciphertext is kept
  only so the console can display "previous key, retiring in N minutes"
  during the overlap window. **Gatekey does not deactivate the old key at
  the provider's own console** — that remains a manual step for the admin to
  perform at OpenAI/Anthropic/Google Cloud/OpenRouter directly.
- Every rotation event is written to the audit trail; no separate rotation-
  events table duplicates it.

## 9. Sub-processor disclosure

**There are none.** Gatekey does not call out to any third-party service on
its own initiative. The only outbound network destinations a Gatekey
deployment ever contacts are ones the deploying organization explicitly
configured:

- the org's own AI provider account (OpenAI, Anthropic, Vertex AI,
  OpenRouter) or its own self-hosted Ollama instance,
- the org's own SMTP relay, if configured, for threshold/rotation email,
- the org's own webhook receiver, if configured, for threshold/rotation
  alerts,
- the org's own OIDC identity provider, if SSO/SCIM is configured.

None of these are Gatekey sub-processors in the vendor-risk sense — they
are infrastructure the customer already owns and chose to configure. Gatekey
itself has no vendor relationship with any of them.

## 10. What's not independently verified

Matching this project's disclosure convention (see `README.md`'s "Known
limitations" sections):

- **Email delivery (SMTP) is implemented but unverified against a live
  mailbox** — no real SMTP credentials were available at build time. Test
  against your own relay before relying on it for threshold or rotation
  alerts; webhook delivery is the verified alert path.
- **Only Keycloak has been exercised as an SSO IdP end-to-end.** The OIDC
  flow is standard and provider-agnostic, but no live round-trip against
  Okta/Azure AD/Google Workspace/etc. was possible at build time.
- **SCIM has not been exercised against a real IdP's live SCIM client**
  (Okta/Azure AD/etc.) — the endpoint shapes follow the SCIM 2.0 RFC, but a
  live provisioning round-trip has not been verified.
- **Presidio's detection coverage is exactly what the underlying library
  provides for its four pattern-based recognizers (SSN, credit card, email,
  phone) plus your own custom regex patterns — it has not been
  independently audited by this project for false-negative/false-positive
  rates.** Do not treat DLP scanning as a guarantee that all PII will be
  caught; validate it against your own representative traffic before
  relying on it for a real compliance requirement.
- **The <50ms p99 synchronous DLP scan target is measured (~10ms warm on
  the build machine), not load-tested at production scale or concurrency.**
- **The audit append-only guarantee is application-level discipline, not a
  database-level constraint** (§3.1) — a direct database connection could
  still bypass it.
- **`log_prompt_retention_days` is enforced and always-on** (§3.2) — the
  opposite caveat from an earlier draft of this document: there is no way
  to fully disable the 30-day-default purge of `usage_logs`/
  `dlp_scan_results`, so a retention-focused review should confirm the
  configured value matches your organization's actual reporting/
  legal-hold needs *before* relying on historical usage data being present.
- **A fresh `git clone` → `docker-compose up` → first-request pass, and a
  full multi-worker/horizontal-scale deployment, have not been personally
  re-run against the exact final Phase 3 code** in the environment this was
  built in. Run it yourself and report back if anything here doesn't match
  what you observe.

## 11. Where to verify claims yourself

Every factual claim in this document cites the actual source file it comes
from. Before relying on this document for a real vendor security review,
independently re-check at minimum: `backend/src/gatekey/services/
encryption.py` (encryption mechanics), `backend/src/gatekey/services/
dlp.py` (DLP behavior), `backend/src/gatekey/db/models/usage_log.py` and
`audit_entry.py` (what's stored, retention exception), `backend/src/
gatekey/services/scheduler.py` (what's actually scheduled today), and
`docker-compose.yml` (transport/TLS posture).
