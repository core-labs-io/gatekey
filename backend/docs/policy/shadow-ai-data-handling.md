# Gatekey — Shadow AI Discovery: Data-Handling Policy

This document is the AC5.1.9 deliverable for Phase 5 (Differentiators, 5.1
Shadow AI Discovery). It is written to be shown to an Org Admin **before**
they opt into this feature — it should be read with the same weight as a
legal consent screen, per the admin console's own framing (`ui-requirements-
admin.md` §12.1). It describes exactly what the shipped code does, grounded
in the actual implementation (`backend/src/gatekey/services/shadow_ai.py`,
`backend/src/gatekey/db/models/shadow_ai_ingest_event.py`,
`backend/src/gatekey/db/models/known_ai_tool_hostname.py`,
`backend/src/gatekey/db/models/shadow_ai_ingest_config.py`), not aspirational
copy. Companion documents: `backend/docs/compliance/data-handling-policy.md`
(the org-wide data-handling policy this feature is an addendum to).

## 1. What this feature is

Shadow AI Discovery is **passive log ingestion**, not active monitoring.
Gatekey does not watch your network traffic itself. Your organization's own
SASE/proxy tool (a system you already operate, e.g. a corporate web proxy or
SASE gateway) exports its own connection logs, and a lightweight transform
you (or your SASE vendor) write sends a normalized batch of events to a
Gatekey API endpoint. Gatekey never initiates outbound monitoring, never
installs a browser extension or network agent, and never intercepts live
traffic — see §6 for what this means for "blocking."

## 2. Exactly what is collected

For each event your SASE/proxy tool submits, and **only** for events whose
destination hostname matches a curated allowlist of known AI-tool hostnames
(§3), Gatekey stores exactly four pieces of information in the
`shadow_ai_ingest_events` table:

| Column | What it is |
|---|---|
| `user_identifier` | The email/username your SASE/proxy tool reports for the request — as-is, whatever string it sends |
| `destination_host` | The hostname the connection was made to (e.g. `chat.openai.com`) |
| `occurred_at` | The timestamp of the connection, as reported by your tool |
| `source` | Which detection mechanism reported it (`sase_log` or `proxy_log`) |

Optionally, an `raw_metadata` JSON field may carry additional
**non-content** connection metadata your tool chooses to attach. This field is
size-capped and enforced, not just a documented convention: the ingestion
schema (`ShadowAiIngestEventRequest.raw_metadata`) rejects any event whose
`raw_metadata` serializes to more than 4096 bytes with a clean `422` — the
event (and, since ingestion is all-or-nothing per request body, the whole
batch) is refused outright rather than silently truncated or partially
persisted. This bound is deliberately small enough to make attaching
meaningful request/response content structurally impractical, reinforcing
(not merely restating) the "connection metadata only" boundary in §2.1.

### 2.1 What is explicitly **never** collected

Gatekey's Shadow AI Discovery **never** collects, and has no database column
capable of holding:

- **Full URLs** — only the bare hostname, never a path, query string, or
  fragment.
- **Query strings** — not stored under any circumstance.
- **Request or response bodies** — Gatekey never sees, requests, or stores
  the content of what was sent to or received from the unsanctioned AI tool.
  This feature only ever knows *that* a connection happened, never *what*
  was in it.

This is a structural guarantee, not a configuration option: the
`shadow_ai_ingest_events` table has no column that could hold a URL path,
query string, or body content even by mistake (see that model's own module
docstring — "no body/URL column exists on this table, by design"). This
feature scopes out payload capture entirely; it is connection metadata only.

## 3. Data minimization: only known-AI-tool traffic is ever stored

Your SASE/proxy tool's export may contain a batch mixing AI-tool traffic
with completely unrelated web traffic (internal apps, general browsing,
etc.). Gatekey applies an allowlist filter (`known_ai_tool_hostnames`,
a curated + admin-editable list of hostnames like `chat.openai.com`,
`claude.ai`, `chat.deepseek.com`, `gemini.google.com`) **before** persisting
anything: any submitted event whose `destination_host` does not match an
enabled row on that list is **dropped in memory and never written to the
database** — not even in a log line, not even in an aggregate count that
names the host. This bounds the feature's privacy exposure by construction:
Gatekey physically cannot retain a record of your employees' general web
browsing through this feature, only their connections to a small,
admin-curated list of known AI-tool destinations.

## 4. Why this data is collected

The purpose is narrow and specific: to detect employees bypassing Gatekey's
own governed gateway by connecting directly to an unsanctioned AI tool (one
that isn't routed through — and therefore isn't subject to — your org's
budget, DLP, residency, and audit policies configured in Gatekey). The
report this data feeds (`GET /v1/admin/shadow-ai/report`) exists to give
admins visibility into which users/teams are doing this, how often, and how
recently — not to monitor general employee activity.

## 5. Retention

Retention is a **dedicated** configuration value,
`shadow_ai_ingest_config.shadow_ai_retention_days` (admin-configurable,
default **90 days**) — deliberately kept **separate** from the org's audit
log retention (`compliance_settings.audit_retention_days`) and prompt/usage
log retention (`compliance_settings.log_prompt_retention_days`), because
this is a distinct, privacy-sensitive data category (network destination
metadata about individual employees), not AI-gateway traffic.

Unlike the audit log's retention window (which can be set to "never purge"),
`shadow_ai_retention_days` is **always finite** — there is no way to
configure unlimited retention for this data. A background scheduler job
(`run_shadow_ai_purge_if_due`, `backend/src/gatekey/services/scheduler.py`)
runs on every scheduler tick and hard-deletes any `shadow_ai_ingest_events`
row older than the configured window, batched to avoid a long-running
transaction. There is no soft-delete/tombstone — a purged row is gone.

## 6. What this feature does **not** do: it cannot block traffic

Gatekey has no presence in your SASE/proxy tool's traffic path — it only
ever receives log data *after the fact*, from a system you already operate.
This means Shadow AI Discovery **cannot** perform true inline
network-level blocking or redirection, no matter how it is configured. Two
enforcement mechanisms exist instead, both off by default and both requiring
explicit confirmation (`confirm: true`) to turn on:

- **Notification** — an automated email is sent to the flagged user (if
  their `user_identifier` was successfully matched to a known Gatekey user's
  email — an unmatched identifier has no email address to notify) and to
  their Team Lead(s), on each newly-detected event.
- **Webhook** — an outbound HTTP POST is fired to an admin-configured URL
  for each newly-detected event, carrying the same connection metadata
  described in §2. This is intended to let *your own* SASE/SOAR/automation
  tooling enact an actual network-level block on its end — Gatekey itself
  never blocks anything.

Both are opt-in, off by default (`enforcement_mode = "detect_only"`
initially), and enabling either requires the same explicit
confirm-before-enabling gate the admin console uses for other intrusive
actions.

## 7. Who can see this data (RBAC)

- **Org Admin** — full, org-wide read access to the report, plus all
  configuration (detection source, enforcement mode, retention window,
  hostname allowlist, ingestion token generation/rotation).
- **Auditor** — full, org-wide **read-only** access to the report and
  configuration (compliance-relevance rationale — this is the same posture
  Auditors get for the audit log and DLP configuration elsewhere in
  Gatekey). Auditors cannot change any Shadow AI configuration.
- **Team Lead** — **read-only**, and **scoped to their own team's
  members only**. A Team Lead can never see another team's flagged users,
  and cannot widen this scope by any client-supplied parameter — the
  backend resolves and enforces which team(s) a Team Lead leads on every
  request, server-side.
- **Member** — no access to this report at all.

Unmatched events (where `user_identifier` couldn't be resolved to a known
Gatekey user) appear only in the org-wide (Org Admin/Auditor) view, labeled
"not linked to a Gatekey user" — a Team Lead's team-scoped view cannot show
them, since there is no team membership to check them against.

## 8. How to disable this feature

Shadow AI Discovery is **functionally opt-in**: it does nothing at all until
an Org Admin completes setup (selecting a detection source and generating an
ingestion token via `POST /v1/admin/shadow-ai/ingest-token`). Before that,
the ingestion endpoint rejects every request with a `401 Unauthorized` —
there is no data to collect until an Org Admin has deliberately turned this
on.

To fully disable it after setup:

1. **Stop sending data** — instruct your SASE/proxy tool's export/transform
   to stop calling `POST /v1/admin/shadow-ai/ingest`. This alone stops all
   further collection.
2. **Revoke the ingestion token** — an Org Admin can rotate
   (`POST /v1/admin/shadow-ai/ingest-token`) to immediately invalidate the
   currently-issued token with no overlap window, so even a
   still-configured external tool can no longer authenticate.
3. **Turn off enforcement**, if enabled, by setting
   `enforcement_mode` back to `"detect_only"` via
   `PUT /v1/admin/shadow-ai/config`.
4. **Wait out the retention window** (§5), or have an Org Admin manually
   delete existing rows — there is no "erase immediately" API endpoint for
   this table beyond the standard retention purge; a manual database-level
   deletion is a documented gap (see
   `backend/docs/compliance/data-handling-policy.md` §2.6 for this
   codebase's general stance on data-subject erasure).

There is no separate master on/off toggle beyond the setup/token-generation
gate itself and the steps above — this matches the design's stated
"the setup flow itself is the opt-in gate" decision.

## 9. `webhook_url` is encrypted at rest

The Shadow AI enforcement webhook URL is stored using the same AES-256-GCM
envelope a team's alert-webhook URL (`teams.webhook_ciphertext`/
`webhook_nonce`/`webhook_auth_tag`) uses —
`shadow_ai_ingest_config.webhook_ciphertext`/`webhook_nonce`/
`webhook_auth_tag` (migration `0043`, replacing an earlier plaintext
`webhook_url` `Text` column from migration `0042` — that plaintext-at-rest
gap has been closed). The plaintext URL is never persisted anywhere else,
never logged, and never returned by any read path — `GET`/`PUT
/v1/admin/shadow-ai/config` expose only `webhook_configured: bool`. If your
webhook URL embeds a bearer-equivalent secret (as Slack-style incoming
webhooks do), it is protected the same way any other Gatekey-managed
webhook secret is.

## 10. Where to verify these claims yourself

Every claim above cites the actual source. Before relying on this document,
independently check: `backend/src/gatekey/db/models/shadow_ai_ingest_event.py`
(schema/no-body-column guarantee), `backend/src/gatekey/services/shadow_ai.py`
(the data-minimization gate, RBAC scoping, notification/webhook delivery),
`backend/src/gatekey/api/deps.py`'s `require_shadow_ai_ingest_token` (the
ingestion trust boundary), and `backend/src/gatekey/services/scheduler.py`'s
`run_shadow_ai_purge_if_due` (the retention purge job).
