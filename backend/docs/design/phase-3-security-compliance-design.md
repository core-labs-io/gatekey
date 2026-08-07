---
title: Phase 3 — Security & Compliance Hardening — Architecture Design
status: accepted
author: architect
last_updated: 2026-08-04
---

# Phase 3 — Security & Compliance Hardening — Design

Scope: audit-trail gap-closure (source IP, CSV/JSON export, independent retention/purge),
Presidio-backed PII/DLP scanning (built-in + custom patterns, log/redact/block), data
residency rules (org/team, hard-block default), a single content-aware model-routing rule
(PII → `blocking_layer="content_classification"`), SCIM 2.0 provisioning/deprovisioning,
compliance documentation + independent usage/prompt retention, automatic service-account
key rotation + guided provider-key rotation, a standalone CLI credential-sync helper, and
three-level (org→team→key) scheduled access windows with emergency overrides.

Source of truth for scope/ACs/ratified ambiguities:
`backend/docs/design/phase-3-security-compliance-product-spec.md` (§0–§12) plus the
orchestrator's explicit ratification of the 12 flagged ambiguities in the handoff brief.
This document does not re-litigate those decisions; it designs against them. Builds
directly on Phase 2's RBAC (`require_role`/`require_team_role`), teams/model-policy
narrowing precedent (`resolve_model_access`), audit trail (`services/audit.py`,
append-only pattern), and notifier interface (`services/notifiers.py`).

Migration numbering: Phase 2's last migration is `0013_add_team_and_cost_columns_to_
usage_logs.py`; Phase 3 starts at `0014`. Migration ownership follows Phase 2's
convention exactly — this section specifies column/constraint/index/FK shape,
database-admin owns the actual Alembic revision files and may regroup differently if
cleaner.

---

## 1. Schema design

### 1.1 New enums (`create_type=False`, DDL owned by migrations, existing convention)

| Enum | Values |
|---|---|
| `dlp_action` | `log`, `redact`, `block` |
| `residency_violation_behavior` | `hard_block`, `warn` |
| `rotation_scope_type` | `org`, `service_account`, `provider_key` |
| `rotation_mode` | `automatic`, `manual_guided` |
| `access_schedule_scope_type` | `org`, `team`, `service_account` |

### 1.2 `compliance_settings` (new)

```
compliance_settings
  org_id                       uuid PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE
  audit_retention_days         integer NULL   -- NULL = never auto-purged (ratified #1)
  log_prompt_retention_days    integer NOT NULL DEFAULT 30
  access_schedule_timezone     text NOT NULL DEFAULT 'UTC'   -- AC9.4: one org-wide tz, no per-scope override
  created_at, updated_at
```

Mirrors `org_settings`'s ADR-2 exactly (absence of row = default state: no audit purge,
30-day usage retention, UTC). A separate table from `org_settings`, not new columns on
it — AC6.1/AC6.2 require the two retention windows to be "genuinely separable at the
infra level," and a dedicated compliance table keeps Phase 3's purge-job configuration
from being interleaved with Phase 2's budget/currency settings in the same row (two
unrelated concerns evolving independently). `access_schedule_timezone` lives here rather
than on any `access_schedules` row because AC9.4 is explicit that timezone is a single
org-wide setting, not per-scope.

### 1.3 `dlp_policies` (new)

```
dlp_policies
  org_id                        uuid PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE
  ssn_detector_enabled          boolean NOT NULL DEFAULT false
  credit_card_detector_enabled  boolean NOT NULL DEFAULT false
  email_detector_enabled        boolean NOT NULL DEFAULT false
  phone_detector_enabled        boolean NOT NULL DEFAULT false
  default_action                dlp_action NOT NULL DEFAULT 'log'
  store_raw_flagged_content     boolean NOT NULL DEFAULT false   -- ratified #3
  scan_inbound_responses        boolean NOT NULL DEFAULT false   -- ratified #4
  created_at, updated_at
```

Detector toggles default **off**, matching this phase's consistent off-by-default
posture (spec §0.6) — an org must deliberately turn PII scanning on, same as
rotation/access-schedules. `default_action` defaults to `log` (the least disruptive
choice) so that turning on a detector without also picking an action never surprises an
org with silent blocking.

### 1.4 `dlp_custom_patterns` (new)

```
dlp_custom_patterns
  id            uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id        uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  name          text NOT NULL
  pattern       text NOT NULL   -- regex source, validated compilable at write time
  action        dlp_action NOT NULL
  created_at, updated_at

  UNIQUE (org_id, name)
  INDEX ix_dlp_custom_patterns_org_id (org_id)
```

Org-level authoring only (AC2.3 — no team-level pattern authoring exists in the UI
spec). Each pattern carries its own independent `action`, never overridden by
`team_dlp_action_overrides` below (AC2.4).

### 1.5 `team_dlp_action_overrides` (new)

```
team_dlp_action_overrides
  team_id    uuid PRIMARY KEY REFERENCES teams(id) ON DELETE CASCADE
  action     dlp_action NOT NULL
```

One row per team, mirrors `TeamModelPolicy`'s ADR-1 shape exactly (`team_id`-as-PK, one
row max, absence = "use the org default"). Overrides only the *action* applied to
built-in-detector findings — this is AC2.4's explicit two-layer (not three-layer)
system: no per-key DLP override table exists, and there is deliberately no
`TeamDlpPatternOverride` — custom patterns stay org-authored-only.

### 1.6 `residency_rules` (new)

```
residency_rules
  id                    uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id                uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  scope_team_id         uuid NULL REFERENCES teams(id) ON DELETE CASCADE   -- NULL = org-wide
  allowed_regions       jsonb NOT NULL   -- string[] drawn from SUPPORTED_REGIONS (§3.1)
  violation_behavior    residency_violation_behavior NOT NULL DEFAULT 'hard_block'
  created_at, updated_at

  UNIQUE INDEX uq_residency_rules_org_wide ON residency_rules (org_id) WHERE scope_team_id IS NULL
  UNIQUE INDEX uq_residency_rules_team_scoped ON residency_rules (scope_team_id) WHERE scope_team_id IS NOT NULL
```

At most one rule per scope (org, or per team) — same "let the schema guarantee the
one-row-per-scope invariant" philosophy as `ModelPolicy`/`TeamModelPolicy`, via partial
unique indexes rather than app-level pre-check-then-insert. `violation_behavior`
defaults to `hard_block` at the column level (AC3.2 — the create path cannot silently
default to `warn`).

### 1.7 `content_aware_rules` (new)

```
content_aware_rules
  org_id           uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  category         text NOT NULL   -- 'pii' (functional), 'source_code'/'financial_data' (inert, A6)
  enabled          boolean NOT NULL DEFAULT false
  allowed_models   jsonb NOT NULL DEFAULT '[]'::jsonb
  created_at, updated_at

  PRIMARY KEY (org_id, category)
```

Org-wide only (AC4.2 — no team-level override exists in the UI spec). Per ratified #6,
all three rows the UI mock shows are ship-able (cheap, forward-compatible), but only
`category = 'pii'` is wired to a real signal (§3.2's DLP findings) this phase —
`source_code`/`financial_data` rows persist and render but never receive a triggered
finding, since no classifier produces one. `ModelAccessDecision.blocking_layer` gains
the literal `"content_classification"` (Python type only, no schema change — Phase 2's
design doc §12 pre-flagged exactly this extension point).

### 1.8 `audit_entries.source_ip` (alter existing table)

```
ALTER TABLE audit_entries ADD COLUMN source_ip inet NULL;
```

Native Postgres `INET` type (already used elsewhere in this stack's dialect imports),
nullable — AC1.2's best-effort contract: an audit write must never fail because a source
IP genuinely isn't available (e.g. an internal service call with no request context).

### 1.9 `dlp_scan_results` (new)

```
dlp_scan_results
  id                     uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id                 uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  request_id             text NOT NULL   -- the same opaque correlation id `common.new_request_id()` already generates
  team_id                uuid NULL
  user_id                uuid NULL
  model                  text NOT NULL
  ran_sync               boolean NOT NULL
  action_taken           dlp_action NOT NULL
  findings               jsonb NOT NULL DEFAULT '[]'::jsonb   -- [{detector_or_pattern_name, action}], never raw content unless store_raw_flagged_content
  raw_flagged_content     jsonb NULL   -- populated only when dlp_policies.store_raw_flagged_content = true
  created_at              timestamptz NOT NULL DEFAULT now()

  INDEX ix_dlp_scan_results_org_id_created_at (org_id, created_at)
  INDEX ix_dlp_scan_results_request_id (request_id)
```

Deliberately keyed by `request_id` (text, not a typed FK to `usage_logs`), not
`usage_log_id` — same rationale `audit_entries.target_id` already documents: the
log-only path's scan completes asynchronously, after the response has been sent and
independent of exactly when/whether a `usage_logs` row exists yet, so coupling this
table's write to that row's lifecycle would be a real ordering hazard for no benefit.
`raw_flagged_content` is `NULL` by default (ratified #3: default to NOT storing raw
flagged substrings, detector/pattern name + action only).

### 1.10 SCIM (new columns + table, no new identity tables)

```
ALTER TABLE users ADD COLUMN scim_external_id text NULL;
ALTER TABLE users ADD COLUMN scim_deactivated_at timestamptz NULL;
CREATE UNIQUE INDEX ix_users_scim_external_id ON users (scim_external_id) WHERE scim_external_id IS NOT NULL;

ALTER TABLE teams ADD COLUMN scim_external_id text NULL;
CREATE UNIQUE INDEX ix_teams_scim_external_id ON teams (scim_external_id) WHERE scim_external_id IS NOT NULL;

scim_config
  org_id                uuid PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE
  enabled               boolean NOT NULL DEFAULT false
  bearer_token_hash     bytea NULL   -- SHA-256, same lookup-hash discipline as every other secret in this codebase
  token_created_at      timestamptz NULL
  created_at, updated_at
```

Per §11's touchpoints note, Users/Groups map directly onto existing `User`/`Team`/
`TeamMembership` — `scim_external_id` is the IdP's durable per-resource identifier
(SCIM's own `externalId`), the correlation key for `PUT`/`PATCH` idempotency, distinct
from `sso_subject` (the OIDC `sub` claim used for SSO login correlation — see §6.3 for
why these two identifiers must be reconciled, not assumed to coincide).
`scim_deactivated_at` is a durable block flag (ratified #8's revocation of live
credentials is necessary but not sufficient — see §6.4). `scim_config` follows the
identical one-row-per-org, hash-only-secret shape as every other org-scoped
singleton config table in this codebase.

### 1.11 Credential rotation (`rotation_policies` + overlap columns)

```
rotation_policies
  id                        uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id                    uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  scope_type                rotation_scope_type NOT NULL
  scope_service_account_id  uuid NULL REFERENCES service_account_keys(id) ON DELETE CASCADE
  scope_provider_key_id     uuid NULL REFERENCES provider_keys(id) ON DELETE CASCADE
  enabled                   boolean NOT NULL DEFAULT false
  interval_days             integer NULL
  rotate_at_local_time      time NULL   -- e.g. '02:00' org-local; NULL falls back to org off-hours default
  overlap_buffer_minutes    integer NOT NULL DEFAULT 5
  next_rotation_at          timestamptz NULL
  last_rotated_at           timestamptz NULL
  mode                      rotation_mode NOT NULL
  created_at, updated_at

  UNIQUE INDEX uq_rotation_policies_org_wide ON rotation_policies (org_id) WHERE scope_type = 'org'
  UNIQUE INDEX uq_rotation_policies_sa_scoped ON rotation_policies (scope_service_account_id) WHERE scope_service_account_id IS NOT NULL
  UNIQUE INDEX uq_rotation_policies_pk_scoped ON rotation_policies (scope_provider_key_id) WHERE scope_provider_key_id IS NOT NULL
  CHECK (
    (scope_type = 'org' AND scope_service_account_id IS NULL AND scope_provider_key_id IS NULL) OR
    (scope_type = 'service_account' AND scope_service_account_id IS NOT NULL AND scope_provider_key_id IS NULL) OR
    (scope_type = 'provider_key' AND scope_provider_key_id IS NOT NULL AND scope_service_account_id IS NULL)
  )
  INDEX ix_rotation_policies_next_rotation_at (next_rotation_at) WHERE enabled
```

Same one-row-per-scope partial-unique-index pattern as `residency_rules`. `mode` is
`CHECK`-implied by `scope_type` at the app layer (`service_account` scope is always
`automatic`, `provider_key` scope is always `manual_guided` — AC7.1's explicit
instruction never to offer full automation for provider keys); not encoded as a DB
`CHECK` across `scope_type`/`mode` to avoid over-constraining a column pair the service
layer already fully controls at every write path (no ad hoc writer exists elsewhere).
The partial index on `next_rotation_at WHERE enabled` is what the scheduler loop (§4.2)
polls.

```
ALTER TABLE service_account_keys
  ADD COLUMN previous_secret_hash bytea NULL,
  ADD COLUMN previous_secret_valid_until timestamptz NULL;
CREATE UNIQUE INDEX ix_service_account_keys_previous_secret_hash
  ON service_account_keys (previous_secret_hash) WHERE previous_secret_hash IS NOT NULL;

ALTER TABLE provider_keys
  ADD COLUMN previous_ciphertext bytea NULL,
  ADD COLUMN previous_nonce bytea NULL,
  ADD COLUMN previous_auth_tag bytea NULL,
  ADD COLUMN previous_valid_until timestamptz NULL;
```

**This is the actual dual-secret overlap mechanism** — see §4.3 for why a new
`RotationEvent` table is *not* needed to satisfy this (the touchpoints checklist's own
suggested shape) and how these columns satisfy AC7.4's overlap NFR by construction.

### 1.12 Scheduled access windows

```
access_schedules
  id                          uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id                      uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  scope_type                  access_schedule_scope_type NOT NULL
  scope_team_id               uuid NULL REFERENCES teams(id) ON DELETE CASCADE
  scope_service_account_id    uuid NULL REFERENCES service_account_keys(id) ON DELETE CASCADE
  enabled                     boolean NOT NULL DEFAULT false
  allowed_days                jsonb NOT NULL DEFAULT '[]'::jsonb   -- ISO weekday ints 1(Mon)-7(Sun)
  allowed_hours_start         time NULL
  allowed_hours_end           time NULL
  created_at, updated_at

  UNIQUE INDEX uq_access_schedules_org_wide ON access_schedules (org_id) WHERE scope_type = 'org'
  UNIQUE INDEX uq_access_schedules_team_scoped ON access_schedules (scope_team_id) WHERE scope_team_id IS NOT NULL
  UNIQUE INDEX uq_access_schedules_sa_scoped ON access_schedules (scope_service_account_id) WHERE scope_service_account_id IS NOT NULL
```

No `timezone`/`holiday_calendar_ref` columns — timezone lives once on
`compliance_settings` (§1.2, AC9.4), and per ratified #10 there is no calendar-ref
indirection at all (a deviation from the product spec's tentative `§16` shape, made
explicit here): holidays are a flat org-wide date list, below.

```
holiday_dates
  id         uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id     uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  holiday_date date NOT NULL
  label      text NULL
  created_at

  UNIQUE (org_id, holiday_date)
```

```
emergency_overrides
  id                        uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id                    uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  service_account_id        uuid NOT NULL REFERENCES service_account_keys(id) ON DELETE CASCADE
  granted_by_user_id        uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT
  reason                    text NOT NULL
  granted_at                timestamptz NOT NULL DEFAULT now()
  expires_at                timestamptz NOT NULL
  revoked_at                timestamptz NULL
  revoked_by_user_id        uuid NULL REFERENCES users(id) ON DELETE SET NULL

  INDEX ix_emergency_overrides_service_account_id (service_account_id)
  CHECK (length(reason) > 0)   -- AC9.7: server-side non-empty, not just a UI hint
```

### 1.13 Model registry region metadata (application-level, not a migration)

Residency needs every routable model to resolve a region, but **no new column or table
is required** — this reuses data that either already exists or belongs in a column that
already exists for exactly this "non-secret per-provider extra data" purpose:

- **`provider_keys.key_metadata`** (existing JSONB, non-secret) already carries Vertex
  AI's `location` (`services/proxy_keys.py` confirms this is read from `key_metadata`,
  not the encrypted payload). Phase 3 adds one new *optional* key to the same column for
  **Ollama** only: `key_metadata["region"]`, an org-admin-settable free value (validated
  against `SUPPORTED_REGIONS` at write time) — Ollama is self-hosted, so there is no
  provider-side region to read; this is the ratified #5 "org-admin-settable region"
  requirement, satisfied with zero schema change by extending the column this codebase
  already built for exactly this shape of data.
- **OpenAI, Anthropic**: a small static, in-process constant (§3.1) — these are
  multi-tenant cloud APIs with a fixed, non-admin-configurable primary hosting region;
  no per-org config makes sense.
- **OpenRouter**: deliberately unresolved (`None`/"unknown") — it aggregates arbitrary
  backend providers/regions with no single knowable region; see §3.1 for the
  consequence this has under a hard-block rule.

---

## 2. Non-functional requirements — explicit accounting

- **AC2.10 (<50ms p99 for synchronous DLP scan)**: satisfied by restricting Presidio's
  recognizer set and NLP engine size — see §3.2's Presidio configuration.
- **AC7.4 (overlap holds across clock skew between gateway instances)**: satisfied by
  construction — the dual-secret validity window (§1.11/§4.3) is evaluated via
  `previous_secret_valid_until > now()` inside a single Postgres query; `now()` is
  evaluated by Postgres once per query, not by each app server's local clock, so no
  gateway instance's clock drift can produce an inconsistent accept/reject decision.
- **AC9.11 (negligible schedule-check latency)**: satisfied by the effective-schedule
  resolution being a pure in-process cache walk (§5.2) with zero extra DB round trips in
  the steady state — same order of magnitude as `resolve_model_access`.
- **AC1.4 (export must not OOM on a large audit table)**: satisfied by a streaming
  response (§7.2) — the query is paginated server-side via a keyset cursor on
  `(created_at, id)`, never a single `SELECT *` materialized in memory.
- **Self-hosted/no-mandatory-phone-home (cross-phase non-negotiable)**: Presidio is
  in-process (§0.1, locked), the rotation/purge scheduler is an in-process asyncio loop
  requiring no new container (§4.2), and SCIM/CLI-sync are both inbound-initiated or
  local-only — nothing in this phase adds an outbound dependency an operator didn't
  already choose to configure (SMTP/webhook, both already optional per Phase 2).

---

## 3. Residency enforcement design

### 3.1 Region resolution

```python
# services/residency.py
SUPPORTED_REGIONS = frozenset({"us", "eu", "apac"})

# Static, non-admin-configurable regions for multi-tenant cloud APIs whose
# hosting region Gatekey has no way to change per-org. `openrouter` is
# deliberately absent - see docstring below.
_PROVIDER_STATIC_REGION: dict[str, str] = {
    "openai": "us",
    "anthropic": "us",
}


def coarsen_gcp_location(location: str) -> str | None:
    """Map a Vertex AI location (e.g. 'us-central1', 'europe-west4',
    'asia-southeast1') to one of SUPPORTED_REGIONS. Unrecognized prefixes
    return None ('unknown') rather than guessing - an unrecognized location
    is treated exactly like a provider with no configured region at all
    (§3.2): it satisfies no allowlist and is blocked by any active
    hard-block rule, never silently passed through."""


def resolve_model_region(route: ModelRoute, provider_key_metadata: dict | None) -> str | None:
    """Region resolution, by provider (design doc §1.13):
    - vertex_ai: `provider_key_metadata["location"]`, coarsened. None if
      no key is configured yet (nothing to route to anyway).
    - ollama: `provider_key_metadata["region"]` (the new admin-settable
      field) verbatim. None if the operator never set it - a residency
      rule blocks self-hosted traffic by default until an admin explicitly
      tags its region, matching hard-block-by-default's own intent.
    - openrouter: always None - an aggregator with no single knowable
      region (see module docstring for why this is deliberate, not a gap).
    - openai/anthropic: the static lookup above.
    """
```

### 3.2 Enforcement — a separate check, not folded into `resolve_model_access`

Residency is a *routing-eligibility* concern (can this request reach this endpoint at
all), conceptually distinct from the model allow/deny decision `ModelAccessDecision`
represents — it is not given a fourth `blocking_layer` value. A parallel decision type
and cache follow the exact same "process-local, replace-whole-snapshot" contract as
`ModelPolicyCache`/`TeamModelPolicyCache`:

```python
@dataclass(frozen=True)
class ResidencyDecision:
    allowed: bool           # False only when violation_behavior == "hard_block"
    violated: bool          # True on ANY rule violation, hard_block or warn
    behavior: Literal["hard_block", "warn"] | None
    region: str | None      # the resolved region, for the audit-entry write


class ResidencyRuleCache:
    """org-wide rule + every team's rule, same lock-free GIL-atomic
    replace-the-whole-snapshot contract as ModelPolicyCache."""
    def get_org_rule(self) -> ResidencyRuleSnapshot | None: ...
    def get_team_rule(self, team_id: uuid.UUID) -> ResidencyRuleSnapshot | None: ...


def resolve_residency(
    region: str | None, *, cache: ResidencyRuleCache, team_id: uuid.UUID | None,
) -> ResidencyDecision:
    """Team rule first if present (already validated at write time to be a
    subset of the org rule - see AC3.2 defense-in-depth below - so checking
    only the most specific configured rule is equivalent to checking both),
    else the org rule, else unrestricted. `region=None` ('unknown', e.g. an
    unconfigured Ollama instance or OpenRouter) satisfies no allowlist -
    an active rule always treats it as a violation."""
```

**AC3.2 defense-in-depth (narrowing-only, ratified #12)**: `set_team_residency_rule`
re-reads the *current* org rule directly from the DB and rejects (422,
`residency_rule_widens_org_rule`) any `allowed_regions` value that isn't a subset of the
org rule's `allowed_regions` — identical shape to `set_team_model_policy`'s
`TeamModelRestrictsOrgDeniedModelError` check.

### 3.3 Pipeline integration

`check_residency()` is a new, cheap, zero-I/O step (in-process cache lookups only),
inserted immediately after `check_model_policy()` and before the DLP scan step —
`resolve_route → check_model_policy → check_residency → DLP scan → check_content_
classification → check_budget_available → fetch_credential`. On `hard_block`, raises a
new `ResidencyViolationError` (403, `code="residency_violation"`) — never a silent
reroute (AC3.6). On `warn`, the request proceeds unchanged. **Both** outcomes (ratified
#12/AC3.5) write an audit entry — `residency.hard_block` or `residency.warn` — via the
same `BackgroundTasks`-after-response mechanism §4's rotation notifications and Phase
2's threshold alerts already use, so a residency check never adds a synchronous DB write
to the hot path beyond the (already-cheap) audit-entry insert that every other blocking
decision in this phase incurs synchronously today — see §7.1 for why residency/DLP/
schedule blocks are the one exception written synchronously, not deferred.

---

## 4. Credential rotation design

### 4.1 Scope of what's "automatic" vs "guided"

Per AC7.1 (locked, non-negotiable): `service_account` scope is always `mode="automatic"`
(mint → one-time-reveal + notify → overlap → auto-revoke, zero admin action per cycle).
`provider_key` scope is always `mode="manual_guided"` (admin pastes a new key, Gatekey
validates it live, then manages the overlap/retirement — there is no provider-side
issuance API to automate against).

### 4.2 ADR — a minimal in-process scheduler, reused for three periodic jobs

**Decision**: Phase 3 introduces exactly one new piece of infrastructure — a single
`asyncio` background loop task, started in `main.py`'s lifespan alongside the existing
`provider_http_client`/`vertex_token_cache` singletons, that wakes on a fixed interval
(recommend 60s) and, on each tick, checks three independent due-work queries:

```python
# services/scheduler.py
async def run_scheduler_loop(app: FastAPI, *, poll_interval_seconds: int = 60) -> None:
    while True:
        await asyncio.sleep(poll_interval_seconds)
        async with app.state.db_session_factory() as session:
            await run_due_rotations(session, app)          # §4.4
            await run_audit_purge_if_due(session)           # §7.3
            await run_log_prompt_purge_if_due(session)       # §6.6 (compliance doc)
```

**Why this is necessary now, unlike Phase 2's ADR-10 lazy/touch-based pattern**: Phase
2's period rollover only needed to be *logically* correct by the next time anything
touched the team (no promise about *when* it fires). Phase 3's rotation and purge jobs
have a real, stated requirement to fire at specific wall-clock moments with **zero
admin/user action** (AC7.5: "No admin action required for a scheduled cycle to
complete"; AC6.2's separately-scheduled purge jobs) — there is no request path a
dormant, low-traffic org's rotation-due key would ever "touch" to trigger a lazy check.
A real, if minimal, scheduler is unavoidable for this requirement; the two alternatives
considered and rejected:
- **Do nothing / keep lazy-touch**: rejected — directly violates AC7.5, not a stylistic
  preference.
- **A separate scheduler process/container** (cron sidecar, Celery beat, etc.): rejected
  as disproportionate for three low-frequency, cheap-to-check jobs, and it would be the
  first departure from this project's "single backend container, `docker-compose up`"
  deployment story (no new service, no new inter-process coordination, no new
  dependency — `asyncio.sleep` in a loop is stdlib).

**Multi-worker safety**: if the backend runs as multiple worker processes/replicas, each
independently runs this loop and would, without care, double-fire the same due rotation.
Closed the same way `budget.py`'s atomic pattern closes concurrent-write races — an
atomic claim-and-advance `UPDATE ... WHERE id = :id AND next_rotation_at = :expected
RETURNING *`: the row's `next_rotation_at` is advanced to the next cycle *in the same
statement* that claims it for processing, so only one worker's `UPDATE` matches the
`WHERE` clause and returns a row; every other worker's identical `UPDATE` affects zero
rows and moves on. No distributed lock, no new dependency — this is `UPDATE ...
RETURNING` optimistic concurrency, the exact pattern already established in this
codebase, applied to a new problem shape.

**This is flagged explicitly as an architectural fork** (§10) since it is a genuinely
new category of infrastructure for this codebase, not a reuse of an existing pattern —
unlike the claim-and-advance mechanism itself, which *is* a direct reuse.

### 4.3 Dual-secret overlap — no new `RotationEvent` table needed

The touchpoints checklist (spec §11) suggests a `RotationEvent` shape; this design
resolves that need using the `previous_secret_hash`/`previous_secret_valid_until`
columns added directly to `service_account_keys` (§1.11) instead of a new table:

```python
async def rotate_service_account_key(
    session: AsyncSession, *, key_id: uuid.UUID, overlap_buffer_minutes: int,
) -> str:
    """Single UPDATE ... RETURNING: mint a fresh secret, move the CURRENT
    secret_hash into previous_secret_hash with previous_secret_valid_until
    = now() + overlap_buffer_minutes, write the new secret_hash as current.
    Returns the new plaintext secret (never persisted) for one-time-reveal
    delivery."""
```

The gateway auth lookup (`get_active_service_account_by_hash`) is extended to match
either column:

```sql
WHERE (secret_hash = :hash)
   OR (previous_secret_hash = :hash AND previous_secret_valid_until > now())
```

Both columns are indexed (§1.11), so this stays a single indexed-equality-shaped lookup,
not a table scan. A stale `previous_secret_hash` past its `valid_until` is simply never
matched again — no separate cleanup job is needed; the next rotation event overwrites it
anyway. `AuditEntry` (action `service_account_key.rotate`, `service_account_key.rotate_
now`) is the durable rotation history — no new events table duplicates what the audit
trail already records.

**Provider keys are asymmetric, and this is worth stating explicitly**: `provider_keys`
gets a parallel `previous_ciphertext`/`previous_nonce`/`previous_auth_tag`/
`previous_valid_until` shape (§1.11), but unlike service-account keys, this is *not*
functionally load-bearing for any live lookup — Gatekey is both the only writer and the
only reader of a provider credential (it presents the key to the provider on its own
outbound calls; no external caller ever holds a cached copy the way a CLI or app might
cache a service-account secret). Gatekey starts using the new key for every outbound
call the moment it's validated; the `previous_*` columns exist purely so the admin
console can still display "previous key, retiring in N minutes" during the overlap
window before those columns are cleared on the next rotation, and so a human operator
manually deactivating the old key at the *provider's own* console has a visible grace
window to do so. AC7.4's clock-skew NFR therefore only meaningfully applies to
service-account keys.

### 4.4 Off-hours timing resolution (AC7.3)

`compute_next_rotation(policy, *, access_schedule, org_off_hours_default) -> datetime`:
if the key has an `access_schedules` row (§5), use the first moment outside its
effective allowed window on `interval_days` from now; else use `rotate_at_local_time`
(org default `02:00` org-local, per `compliance_settings.access_schedule_timezone` —
Phase 3's one shared timezone setting is reused here too, not a second timezone
concept). Computed once per rotation (not re-derived per request) and stored in
`next_rotation_at`, read by the scheduler loop's due-work query.

### 4.5 Notification wiring

Reuses Phase 2's `Notifier`/`NotifierDispatcher` interface unchanged — a new
`RotationEvent`-shaped payload (`key_name`, `rotated_at`, `overlap_expires_at`) is
fanned out the same way `ThresholdAlertEvent` is, via `BackgroundTasks` scheduled from
the scheduler loop's own async context (not the gateway request path — rotation isn't
triggered by a request at all). Webhook delivery is testable end-to-end against a mock
receiver; email stays flagged unverified-live, same caveat class as every other email
path in this codebase (AC7.9).

---

## 5. Scheduled access windows design

### 5.1 Precedence — narrow at write time, take the innermost enabled layer at read time

**ADR**: unlike model policy (where every enabled layer is checked cumulatively at read
time), access-schedule resolution checks only the single most-specific *enabled* layer.
This is provably equivalent to checking every layer, because narrowing is validated at
**write** time (below), so any enabled child layer is already guaranteed to be a subset
of every enabled ancestor layer — walking straight to the innermost enabled layer and
stopping there cannot admit anything a full cumulative check would have rejected. This
is a meaningfully cheaper read-time shape (one cache lookup chain, no interval-
intersection arithmetic on the hot path) and is called out explicitly since it's a real
design choice the spec doesn't dictate, not an obvious consequence of AC9.2's wording.

```python
def resolve_effective_schedule(
    *, cache: AccessScheduleCache, team_id: uuid.UUID | None, service_account_id: uuid.UUID,
) -> EffectiveSchedule | None:   # None = "Always" (no restriction at any level)
    if (sa := cache.get_service_account(service_account_id)) is not None and sa.enabled:
        return sa
    if team_id is not None and (team := cache.get_team(team_id)) is not None and team.enabled:
        return team
    if (org := cache.get_org()) is not None and org.enabled:
        return org
    return None
```

A **disabled** (or absent) row at a more specific level defers to the next-less-specific
enabled level — same "absence/off = no further restriction beyond the parent" semantics
already established for team model policy, not "reopen access."

**Write-time narrowing validation** (`validate_schedule_narrows_parent`): a day-set
subset check plus an hour-range subset check against the resolved parent schedule,
identical defense-in-depth shape to `set_team_model_policy`'s AC3.2 check — rejects
(422, `access_schedule_widens_parent`) any write that would expand beyond what the
parent already allows.

### 5.2 Timezone and holiday evaluation

`compliance_settings.access_schedule_timezone` (stdlib `zoneinfo`, no new dependency)
converts the current instant to org-local weekday/time-of-day for the
`allowed_days`/`allowed_hours` comparison, and to an org-local **date** for the
`holiday_dates` lookup — deliberately not a UTC-date comparison, since a UTC-date vs.
org-local-date mismatch near midnight would make the wrong day's holiday status apply.

### 5.3 Enforcement point

`check_access_schedule(ctx: GatewayCallerContext, cache) -> None` is a new step called
immediately after `require_gateway_credential` resolves the caller — **before**
`resolve_route`, since a schedule block has nothing to do with which model was
requested and should reject as cheaply as every other early-reject step in this
pipeline. Only applies when `ctx.credential_type == "service_account"` — AC9.1's
`AccessSchedule.scope` values (`org`/`team_id`/`service_account_id`) never include a
personal-key scope; personal keys (a logged-in human via SSO) are unaffected by this
feature, consistent with its off-hours-automation framing. Raises
`OutsideAllowedScheduleError` (403, `outside_allowed_schedule`) on a hard reject.

**Emergency override**: checked only on the rejection path (zero extra I/O in the
common allowed case) — an active, non-revoked, non-expired `emergency_overrides` row for
this `service_account_id` covering the current instant allows the request through
regardless of the resolved schedule. AC9.6 (block itself is audited) and AC9.9
(grant/revoke are each audited) are both satisfied by the standard synchronous
audit-write convention (§7.1) — a schedule block is itself the auditable event here, not
a mutation, the one deliberate exception this phase's audit-write convention names
explicitly alongside residency (§3.3).

---

## 6. SCIM design

### 6.1 Protocol surface

Standard SCIM 2.0 resource endpoints under a dedicated router (`/scim/v2/...`), separate
from `/v1/...` since SCIM clients (IdPs) expect the RFC's own response/error shapes, not
Gatekey's generic `{"error": {code, message}}` envelope — see §9's API contract table
for the exact route list. Filtering is a deliberately **scoped, documented subset**, not
a silent partial implementation of the full RFC 7644 grammar: `eq` comparisons on
`userName`/`externalId` only (covers every real IdP's actual usage — existence checks
and correlation lookups), plus `startIndex`/`count` pagination. This satisfies AC5.1's
"not a silently partial subset" requirement by being explicit about the boundary, not by
claiming full grammar coverage.

### 6.2 Auth

`require_scim_token`: a new dependency, SHA-256 lookup against
`scim_config.bearer_token_hash` (same hash-only-secret discipline as every credential in
this codebase), constant-time comparison. Token generation/rotation reuses the exact
same one-time-reveal UI component already used for service-account secrets (per the
spec's own instruction) — `POST /v1/admin/scim-config/rotate-token` immediately
invalidates the prior token (no overlap; this is an inbound credential the IdP holds,
not a scheduled outbound rotation like §4).

### 6.3 Reconciling SCIM identity with OIDC identity — a necessary, undocumented gap this design closes

Neither source doc addresses how a SCIM-provisioned `User` row (created via `POST
/Users`, correlated by `scim_external_id`, no `sso_subject` set) is supposed to become
the *same* row a user later authenticates into via SSO (Phase 2's callback upserts by
`sso_subject = sub`, which a SCIM-only row doesn't have yet). Left unaddressed, every
SCIM-provisioned user's first SSO login would silently create a **second**, duplicate
`User` row instead of attaching to the one SCIM already set up (wrong team, wrong
history, a real correctness bug, not a cosmetic one). This design extends the Phase 2
SSO callback's upsert step (§2.1 step 4 of that design doc):

```
1. Look up by sso_subject = sub (existing).
2. If not found: look up by (org_id, sso_email = IdP-asserted email) among rows
   with sso_subject IS NULL (covers both SCIM-provisioned rows and pre-Phase-2
   legacy rows). If found, backfill sso_subject onto that row rather than
   inserting a new one.
3. If still not found: create a new row (existing behavior).
```

Flagged explicitly since it's a real, necessary consequence of SCIM's `POST /Users`
existing at all, not addressed by either source doc.

### 6.4 Group/User mapping and the two ratified decisions

- `POST /Users` → `User(org_id, name, sso_email, scim_external_id, org_role=NULL)`
  (AC5.3) — the request/response mapping never reads any org-role-shaped attribute from
  the SCIM payload at all (AC5.8): there is nothing to "ignore," the field simply has no
  mapping, which is the simplest way to make the defense-in-depth guarantee structural
  rather than a runtime check that could be forgotten.
- Group `PATCH`/`PUT` membership operations → `TeamMembership` create/delete.
  `budget_usd = NULL` on create (ratified #7 — unmetered, not `$0`; no new "needs
  attention" UI — the existing team-members table already renders unmetered state).
- `PATCH active:false` / `DELETE /Users/{id}` (ratified #8, extended): sets
  `users.scim_deactivated_at = now()`, revokes every active `Session` row, every active
  `PersonalApiKey` row, every active team-attributed `ServiceAccountKey` row (`team_id IS
  NOT NULL`) owned by the user, **and** every active `cli_refresh_credentials` row for
  that user (§8.2 — not explicitly named by the ratified decision's wording, but a live
  refresh credential can mint arbitrary future personal keys, so leaving it active after
  deactivation would silently defeat the rest of this revocation; this extends decision
  #8's stated rationale, it doesn't contradict it). One `AuditEntry` per revoked
  credential, `actor_label = "system:scim"` (a new sentinel, same shape as
  `"system:admin_token"`).
- `scim_deactivated_at` is also checked in the SSO login path (`try_get_session_context`
  query gains `AND users.scim_deactivated_at IS NULL`, and the login callback rejects
  issuing a **new** session for a deactivated user) — revoking existing sessions isn't
  sufficient on its own; without this, a deactivated user could simply log back in via
  SSO and mint a fresh session, silently undoing the deprovisioning action.
- `User`/`TeamMembership` rows are never deleted on deprovisioning (AC5.6) — matches the
  audit/history-preservation posture already built into `actor_label` snapshotting.

---

## 7. Audit gap-closure design (§3.1)

### 7.1 Source IP capture

`write_audit_entry` (existing helper, extended) gains an optional `source_ip: str | None`
parameter, resolved by call sites from `Request.client.host` (or the
`X-Forwarded-For`/`X-Real-IP` header when a `GATEKEY_TRUST_PROXY_HEADERS` setting is
enabled — mirrors the standard reverse-proxy caveat every self-hosted app with this
concern has; off by default, matching this phase's off-by-default posture, since
trusting a client-supplied header without a configured trusted-proxy boundary is itself
a spoofing risk). Genuinely unavailable → `NULL`, never blocks the write (AC1.2) — same
best-effort discipline as `last_seen_at`'s touch update.

Every synchronous audit-write call site introduced by this phase (DLP block, residency
block, schedule block, SCIM revocation, rotation event) threads this through; break-glass
admin-token actions also capture a source IP (a break-glass action still has a network
origin worth recording, per AC1.1).

### 7.2 CSV/JSON export

`GET /v1/admin/audit-entries?format=csv|json` (no `format` = today's unchanged
paginated JSON response — zero regression). Implemented as a `StreamingResponse` over a
server-side keyset-paginated query (`WHERE (created_at, id) < (:cursor_created_at,
:cursor_id) ORDER BY created_at DESC, id DESC LIMIT 500`, looped), never a single
`SELECT *` — satisfies AC1.4's OOM-safety requirement for an unboundedly large table.
Same role gate as the existing read endpoint (`require_role(org_admin, auditor)`,
AC1.5) — no new role surface.

### 7.3 Retention/purge — the one sanctioned exception to "never DELETE"

`run_audit_purge_if_due` (scheduler loop, §4.2): reads `compliance_settings.
audit_retention_days`; if `NULL` (ratified #1's default), the function returns
immediately — the purge job **never fires** for an org that hasn't explicitly
configured a finite window. When set, a single `DELETE FROM audit_entries WHERE org_id
= :org_id AND created_at < now() - interval '{days} days'`, batched (e.g. `LIMIT 5000`
per iteration, looped) to avoid a single long-running transaction against a
potentially large table.

**This is a documented, deliberate, narrowly-scoped exception to `audit_entries`'s
existing module docstring** ("service-layer code only ever INSERTs here, never UPDATE/
DELETE"). The exception is written into that docstring directly (`db/models/
audit_entry.py`), stated precisely: *the only DELETE against this table is the
config-driven, scheduled purge job in `services/scheduler.py`, and only when an org has
explicitly configured a finite `audit_retention_days`; it is never reachable via any
mutating API endpoint an admin or auditor can invoke directly.* Ratified #2's resolution
(narrow exception, not a contradiction) is satisfied by this being (a) not
caller-triggerable, (b) off by default, and (c) the single, centralized code path that
exists for exactly this purpose.

The `log_prompt_retention_days` purge (AC6.2 — usage/prompt log rows, default 30, hard
delete not soft-tombstone per AC6.4) is a **separate**, independently-scheduled check in
the same loop (`run_log_prompt_purge_if_due`), against a different table
(`usage_logs`/prompt content, not `audit_entries`) and a different config column — never
sharing a code path with the audit purge, so a future change to one window structurally
cannot affect the other's data (AC6.2's explicit requirement).

---

## 8. CLI sync helper design (§3.7a)

### 8.1 Package structure

Standalone package at repo root, sibling to `backend/`/`frontend/` (per orchestrator
ratification #11 — Python, own `pyproject.toml`, `keyring` for cross-platform OS
credential storage, installable independently of the FastAPI/SQLAlchemy stack):

```
cli-sync/
  pyproject.toml
  src/gatekey_sync/
    __init__.py
    cli.py       # `gatekey-sync login`, `gatekey-sync get-key`
    auth.py      # device-code flow; refresh credential -> OS keychain via `keyring`
    cache.py     # {secret, valid_until} JSON cache file read/write
    client.py    # httpx calls against the backend's device-auth + current-key endpoints
  tests/
    test_cache.py   # minimal self-check: cache-hit/miss/expiry logic, no fixtures/mocking framework
```

Cache file: `Path.home() / ".gatekey-sync" / "cache.json"`, `chmod 0600` best-effort on
POSIX (Windows ACLs are not separately hardened — a known, accepted limitation for this
phase, same "known limitation, not a blocking gap" framing Phase 2 used for its own
touch-based rollover consequence). No new dependency for this (stdlib `pathlib`) —
`platformdirs`-style OS-idiomatic config directories are a nice-to-have deferred until a
real cross-platform packaging need surfaces, not built speculatively now.

### 8.2 Backend contract this helper needs

A new credential type, **not** a `PersonalApiKey`: a long-lived refresh credential whose
only power is calling `GET /v1/me/current-key` — never usable directly against the
gateway routes.

```
cli_refresh_credentials
  id                       uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id                   uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  user_id                  uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE
  bound_personal_key_id    uuid NOT NULL REFERENCES personal_api_keys(id) ON DELETE CASCADE
  secret_hash              bytea NOT NULL   -- SHA-256, `gk_rf_` prefix, same shape as every other secret
  created_at               timestamptz NOT NULL DEFAULT now()
  revoked_at               timestamptz NULL

  UNIQUE INDEX ix_cli_refresh_credentials_secret_hash (secret_hash)
  INDEX ix_cli_refresh_credentials_user_id (user_id)
```

**Device-code-style auth flow** (AC8a.2):

1. `POST /v1/auth/device/start` (no auth) → `{device_code, user_code, verification_uri,
   expires_in, interval}` — standard OAuth 2.0 Device Authorization Grant shape.
2. CLI displays `user_code` + opens `verification_uri`; user is already logged into the
   console (session cookie) and approves.
3. `POST /v1/auth/device/approve` (session auth) — on approval, a fresh `PersonalApiKey`
   is created for the caller (named e.g. `"CLI Sync (<device hint>)"`, `team_id`
   auto-selected per Phase 2's A1 pattern if unambiguous, else required in the initial
   `start` request) and bound to a new `cli_refresh_credentials` row.
4. CLI polls `POST /v1/auth/device/poll {device_code}` (no auth) at the given interval;
   on approval, receives the plaintext refresh credential exactly once, stores it via
   `keyring.set_password("gatekey-sync", "refresh_credential", raw_token)`.

**`GET /v1/me/current-key`** (auth: `Bearer gk_rf_...`): **each call rotates the bound
personal key** (calls the existing `regenerate_personal_key` service function — the same
code path `POST /v1/keys/{id}/regenerate` already uses) and returns
`{secret: <new plaintext>, valid_until}`. This is a deliberate design resolution to a
real tension the spec doesn't settle at the schema level: AC8a.3/AC8a.4 imply repeatable
fetches of "the current" plaintext key, but every other credential in this codebase is
hash-only, shown-once. Minting fresh on every fetch keeps that discipline intact (no new
"temporarily cached plaintext" exception anywhere) and is self-healing across multiple
devices sharing one refresh credential: if device A's fetch rotates the key, device B's
stale cached copy simply fails its next gateway call, triggering **its own** AC8a.5
transparent re-fetch-once logic — no coordination between devices is needed. Flagged
explicitly (mirrors Phase 2's ADR-6 precedent: the spec specifies the *behavior*, this
design supplies the *mechanic*) — worth a second look since "every fetch rotates the
key" means a CLI polling more aggressively than intended would thrash the key faster
than a human might expect; AC8a.3's own "at most once a day/hour" cadence is what keeps
this rare in practice, not a server-side rate limit (none is added — a client behaving
badly only rotates its own key more often, no cross-user blast radius).

`valid_until` (server-computed, AC8a.4): `RotationPolicy.next_rotation_at` for the
bound key's resolved rotation config if one exists, else `now() + 1 hour` — purely a
hint for when the CLI should proactively re-check; the real enforcement is always the
live gateway auth check, never client-trusted (same posture as every other
client-supplied-vs-server-verified distinction in this codebase).

SCIM deactivation revokes `cli_refresh_credentials` rows too (§6.4).

---

## 9. API contract

Base path `/v1` unless noted. Every route not explicitly "no auth" requires at least
`get_current_session`/`get_privileged_session`, matching Phase 2's convention.

### 9.1 Audit & compliance settings

| Method & path | Auth | Notes |
|---|---|---|
| `GET /v1/admin/audit-entries?...&format=csv\|json` | `require_role(org_admin, auditor)` | extends existing endpoint; no `format` = unchanged |
| `GET/PUT /v1/admin/compliance-settings` | `require_role(org_admin)` | `audit_retention_days`, `log_prompt_retention_days`, `access_schedule_timezone` |

### 9.2 DLP

| Method & path | Auth | Notes |
|---|---|---|
| `GET/PUT /v1/admin/dlp-policy` | `require_role(org_admin)` | detector toggles, `default_action`, `store_raw_flagged_content`, `scan_inbound_responses` |
| `GET/POST/PATCH/DELETE /v1/admin/dlp-policy/custom-patterns[/{id}]` | `require_role(org_admin)` | regex validated compilable at write time |
| `GET/PUT /v1/teams/{team_id}/dlp-override` | `require_team_role(team_lead)` | action override only |

### 9.3 Residency

| Method & path | Auth | Notes |
|---|---|---|
| `GET/PUT/DELETE /v1/admin/residency-rules` | `require_role(org_admin)` | org-wide rule |
| `GET/PUT/DELETE /v1/teams/{team_id}/residency-rule` | `require_team_role(team_lead)` | `422 residency_rule_widens_org_rule` on a non-subset write — see §3.2's narrowing note; team-authoring role inferred by pattern (A12-style), not explicit in either source doc |

### 9.4 Content-aware routing

| Method & path | Auth | Notes |
|---|---|---|
| `GET/PUT /v1/admin/content-aware-rules` | `require_role(org_admin)` | org-wide, all three categories rendered, only `pii` functional |

### 9.5 SCIM

| Method & path | Auth | Notes |
|---|---|---|
| `POST/GET /scim/v2/Users`, `GET/PATCH/PUT/DELETE /scim/v2/Users/{id}` | `require_scim_token` | SCIM 2.0 shapes/errors, not Gatekey's generic envelope |
| `POST/GET /scim/v2/Groups`, `GET/PATCH/PUT/DELETE /scim/v2/Groups/{id}` | `require_scim_token` | Groups ↔ Teams |
| `GET/PUT /v1/admin/scim-config` | `require_role(org_admin)` | `enabled` toggle, base URL display |
| `POST /v1/admin/scim-config/rotate-token` | `require_role(org_admin)` | one-time-reveal, immediate prior-token invalidation |

### 9.6 Rotation

| Method & path | Auth | Notes |
|---|---|---|
| `GET/PUT /v1/admin/rotation-policy` | `require_role(org_admin)` | org-wide default |
| `GET/PUT /v1/admin/keys/{id}/rotation-policy` | `require_role(org_admin)` | per-service-account-key override |
| `POST /v1/admin/keys/{id}/rotate-now` | `require_role(org_admin)` | short-overlap, distinct from existing `DELETE /v1/admin/keys/{id}` (zero-overlap revoke, unchanged from Phase 2) |
| `GET/PUT /v1/admin/provider-keys/{provider}/rotation-policy` | `require_role(org_admin)` | always `mode="manual_guided"` |
| `POST /v1/admin/provider-keys/{provider}/rotate` | `require_role(org_admin)` | validate-then-overlap-swap guided flow, same three structured error states as Phase 1's add-key modal |

### 9.7 Scheduled access windows

| Method & path | Auth | Notes |
|---|---|---|
| `GET/PUT /v1/admin/access-schedule` | `require_role(org_admin)` | org-wide default |
| `GET/PUT /v1/teams/{team_id}/access-schedule` | `require_team_role(team_lead)` | narrowing-only, `422` on widen |
| `GET/PUT /v1/admin/keys/{id}/access-schedule` | `require_role(org_admin)` | per-service-account-key override |
| `GET /v1/admin/keys/schedules` | `require_role(org_admin)` | AC9.10 — resolved effective schedule per key |
| `GET/POST/DELETE /v1/admin/holiday-dates[/{id}]` | `require_role(org_admin)` | flat org-wide date list |
| `POST/DELETE /v1/teams/{team_id}/service-account-keys/{key_id}/emergency-override[/{override_id}]` | `require_team_role(team_lead)` | org-admin bypass automatic (AC9.8); `reason` server-validated non-empty |

### 9.8 CLI sync helper backend endpoints

| Method & path | Auth | Notes |
|---|---|---|
| `POST /v1/auth/device/start` | none | `{device_code, user_code, verification_uri, expires_in, interval}` |
| `POST /v1/auth/device/approve` | session | mints bound `PersonalApiKey` + `cli_refresh_credentials` row |
| `POST /v1/auth/device/poll` | none | `{device_code}`; `202` while pending, `200` + plaintext refresh credential once approved |
| `GET /v1/me/current-key` | `Bearer gk_rf_...` | rotates + returns the bound personal key's plaintext (§8.2) |

### 9.9 Compliance documentation — no backend endpoint

Per ratified #9, the data flow diagram and data handling policy are static document
deliverables (`backend/docs/compliance/`), not generated. No API route exists for this;
the admin console's "Download" buttons are plain static links to files docs-writer
produces (see §11's docs-writer task) — zero backend work, zero new frontend screen.

---

## 10. Architectural forks — orchestrator sign-off requested

1. **A minimal in-process `asyncio` scheduler loop (§4.2), the first true
   wall-clock-driven periodic-job mechanism in this codebase**, reused for rotation
   firing and both purge jobs. This is a real, new category of infrastructure —
   Phase 2's ADR-10 explicitly avoided exactly this, and this design concludes it's now
   unavoidable given AC7.5's "no admin action required" and the purge jobs' scheduled
   nature. The multi-worker double-fire race is closed via an atomic claim-and-advance
   `UPDATE ... RETURNING` (a direct reuse of an existing pattern), but the loop itself is
   new. Flagging for explicit sign-off since it's exactly the kind of "new architectural
   pattern, not a copy of an existing one" Phase 2's own design doc flagged its
   `SELECT ... FOR UPDATE` locking decision for (ADR-5) — same bar applied here.
2. **Presidio's recognizer set is restricted to pattern-based detectors only (SSN,
   credit card, email, phone, plus org custom patterns), with a small `en_core_web_sm`
   spaCy model rather than the default large one**, to hit the <50ms p99 NFR (§2). This
   is the correct-and-sufficient scope for AC2.2's four detectors (none of which are
   NER-dependent) — flagging only because it forecloses adding a NER-based detector
   (e.g. "PERSON name") in a later phase without revisiting the NLP engine choice; not a
   functional gap against this phase's own scope.
3. **`GET /v1/me/current-key` rotates the bound personal key on every call** (§8.2)
   rather than caching a temporarily-readable plaintext anywhere — the design's own
   supplied mechanic for a tension the spec doesn't resolve at the schema level (spec
   dictates behavior, not mechanic — same class of decision as Phase 2's ADR-6 rollover
   arithmetic). Flagging since "every fetch is a rotation" is a real, if minor,
   behavioral choice worth explicit sign-off rather than silent inference.

---

## 11. Task breakdown

Legend: `[P]` = can run in parallel with sibling `[P]` tasks; `[D: X]` = hard dependency
on task `X`.

### database-admin

- **DB-1** `[P]`: Migration `0014` — `compliance_settings`, `dlp_policies`,
  `dlp_custom_patterns`, `team_dlp_action_overrides`, enum `dlp_action`.
- **DB-2** `[P]`: Migration `0015` — `residency_rules`, enum
  `residency_violation_behavior`.
- **DB-3** `[P]`: Migration `0016` — `content_aware_rules`.
- **DB-4** `[P]`: Migration `0017` — `audit_entries.source_ip`.
- **DB-5** `[D: DB-4]`: Migration `0018` — `dlp_scan_results`.
- **DB-6** `[P]`: Migration `0019` — `scim_config`, `users.scim_external_id`,
  `users.scim_deactivated_at`, `teams.scim_external_id`.
- **DB-7** `[P]`: Migration `0020` — `rotation_policies` (+ enums
  `rotation_scope_type`/`rotation_mode`), `service_account_keys.previous_secret_*`,
  `provider_keys.previous_*`.
- **DB-8** `[P]`: Migration `0021` — `access_schedules` (+ enum
  `access_schedule_scope_type`), `holiday_dates`, `emergency_overrides`.
- **DB-9** `[D: DB-6]`: Migration `0022` — `cli_refresh_credentials`.
- **DB-10** `[D: DB-1..DB-9]`: ORM models for every new/altered table, registered in
  `db/models/__init__.py`.

### backend-developer — DLP/residency/content-classification (mostly `[P]` once DB-10 lands)

- **BD-1** `[D: DB-10]`: `services/dlp.py` — Presidio `AnalyzerEngine` singleton on
  `app.state` (small spaCy model, pattern-recognizer-only registry, §3.2/§10 fork #2),
  scan function (`asyncio.to_thread`-wrapped), custom-pattern loading, redaction
  mechanics.
- **BD-2** `[D: BD-1]`: `api/v1/admin/dlp_policy.py` — detector toggles, custom
  patterns CRUD, team override route.
- **BD-3** `[D: DB-10]`: `services/residency.py` — `resolve_model_region`,
  `ResidencyRuleCache`, `resolve_residency` (§3). `[P]` with BD-1.
- **BD-4** `[D: BD-3]`: `api/v1/admin/residency_rules.py` + team-scoped route, AC3.2
  defense-in-depth validation.
- **BD-5** `[D: DB-10]`: `services/model_policy.py` extension — `content_aware_rules`
  cache/resolution, `resolve_model_access`'s third layer (§1.7/§9.4). `[P]` with BD-1/3.
- **BD-6** `[D: BD-1, BD-3, BD-5]`: `api/v1/gateway/common.py` pipeline rewiring —
  `check_residency`, DLP scan step, second `check_model_policy` pass for
  content-classification, error classes (`ResidencyViolationError`, `DlpBlockedError`)
  in `errors.py`. Every gateway route handler updated to the new call order.
- **BD-7** `[D: BD-6]`: load-test acceptance check for AC2.10 (<50ms p99 synchronous DLP
  path).

### backend-developer — audit gap-closure

- **BD-8** `[D: DB-10]`: `services/audit.py` — `source_ip` param threading, all Phase 2
  call sites updated. `[P]` with the DLP/residency track.
- **BD-9** `[D: BD-8]`: `api/v1/admin/audit_entries.py` — CSV/JSON streaming export
  (keyset-paginated).
- **BD-10** `[D: DB-10]`: `services/compliance_settings.py` + admin route (§9.1). `[P]`
  with BD-8.

### backend-developer — scheduler + rotation + purge (shared infra, sequenced first within this track)

- **BD-11** `[D: DB-10]`: `services/scheduler.py` — `run_scheduler_loop`, wired into
  `main.py`'s lifespan (§4.2, fork #1). This is the shared dependency for BD-12/BD-13/
  BD-14 below.
- **BD-12** `[D: BD-11]`: `services/rotation.py` — `rotate_service_account_key`,
  atomic claim-and-advance due-rotation query, off-hours `compute_next_rotation`
  (§4.3/4.4), notifier wiring (reuses `services/notifiers.py` unchanged).
- **BD-13** `[D: BD-11, BD-10]`: audit purge job (§7.3) — the sanctioned exception,
  written into `db/models/audit_entry.py`'s docstring per §7.3.
- **BD-14** `[D: BD-11, BD-10]`: log-prompt-retention purge job (§6.6/AC6.2) — a
  genuinely separate function/code path from BD-13, never shared.
- **BD-15** `[D: BD-12]`: `api/v1/admin/rotation_policy.py` + provider-key guided-rotate
  route (three structured validation states, reusing Phase 1's add-key-modal pattern).

### backend-developer — access windows

- **BD-16** `[D: DB-10]`: `services/access_schedules.py` — `AccessScheduleCache`,
  `resolve_effective_schedule`, `validate_schedule_narrows_parent` (§5.1). `[P]` with
  every other backend track.
- **BD-17** `[D: BD-16]`: `api/v1/admin/access_schedule.py` + team-scoped +
  per-key routes, effective-schedule list endpoint (AC9.10), holiday-dates CRUD.
- **BD-18** `[D: BD-16]`: emergency-override create/revoke routes under the
  team-scoped tree (`require_team_role(team_lead)`), server-side non-empty `reason`.
- **BD-19** `[D: BD-16, BD-6]`: `check_access_schedule` wired into
  `api/deps.py`/gateway route handlers, immediately after `require_gateway_credential`
  (§5.3).

### backend-developer — SCIM

- **BD-20** `[D: DB-10]`: `api/deps.py` — `require_scim_token`.
- **BD-21** `[D: BD-20]`: SSO callback upsert extension (§6.3 — the
  sso_subject-vs-scim_external_id reconciliation fix).
- **BD-22** `[D: BD-20, BD-21]`: `api/v1/scim/users.py` — SCIM 2.0 User endpoints,
  deactivation revocation cascade (§6.4), `system:scim` audit sentinel.
- **BD-23** `[D: BD-22]`: `api/v1/scim/groups.py` — Group ↔ Team/TeamMembership
  mapping.
- **BD-24** `[D: BD-20]`: `api/v1/admin/scim_config.py` — enable toggle, token
  rotate. `[P]` with BD-21/22/23.

### backend-developer — CLI sync backend contract

- **BD-25** `[D: DB-10 (DB-9), BD-6]`: `services/cli_refresh_credentials.py` +
  `api/v1/auth_device.py` — device-code flow, `GET /v1/me/current-key` (§8.2, fork #3).
  `[D: BD-6]` only because it reuses `regenerate_personal_key`, which the DLP/pipeline
  rework doesn't touch — safe to build in parallel in practice, dependency listed for
  completeness.
- **BD-26** `[D: BD-22]`: thread `cli_refresh_credentials` revocation into the SCIM
  deactivation cascade (§6.4's extension).

### cli-sync-package (new track, outside `backend/`/`frontend/`)

- **CLI-1** `[P]`: package scaffold (`pyproject.toml`, `keyring` dependency),
  `cache.py` (§8.1) with its `test_cache.py` self-check — no backend dependency, can
  start immediately.
- **CLI-2** `[D: BD-25]`: `auth.py`/`client.py` — device-code flow client, OS-keychain
  storage, `/v1/me/current-key` polling per the published contract.
- **CLI-3** `[D: CLI-2]`: `cli.py` entrypoint, AC8a.5 transparent-reauth-on-rejection
  logic, AC8a.7 latency benchmark.

### devops-engineer

- **DO-1** `[P]`: no new container required — Presidio is in-process (§0.1/fork #2);
  confirm the base backend image's resource footprint (spaCy small model + Presidio)
  stays within the documented self-hosted sizing guidance, adjust `docker-compose`
  resource hints/README if needed.
- **DO-2** `[D: BD-11]`: confirm the scheduler loop's behavior under
  `docker-compose`'s default single-replica deployment, and document the
  multi-worker claim-and-advance safety property (§4.2) for operators considering
  horizontal scaling.

### docs-writer

- **DOC-1** `[P]`: `backend/docs/compliance/data-flow-diagram` (diagram) +
  `data-handling-policy.md` (or PDF) — static deliverables per ratified #9, out of
  backend/frontend engineering scope entirely. No dependency on any backend task.

### frontend-developer

Can start against §9's API contract as soon as it's stable — `[P]` with the
corresponding backend tasks.

- **FE-1** `[P]`: DLP policy tab (detector toggles, custom patterns table, team
  override) — `ui-requirements-admin.md` §10.1.
- **FE-2** `[P]`: Residency tab (rules table, hard-block/warn toggle, region checkboxes
  drawn from `SUPPORTED_REGIONS`) — §16.
- **FE-3** `[P]`: Content-Aware Routing tab — all three category rows rendered, `pii`
  functional, others visually present but documented as not-yet-wired (ratified #6).
- **FE-4** `[P]`: SCIM/Identity & Access screen — toggle, base URL, one-time-reveal
  rotatable token (reuses the existing service-account-secret reveal component)
  — `ui-requirements-admin.md` §14.
- **FE-5** `[P]`: Audit Log screen extension — source IP column, CSV/JSON export
  buttons, retention-days config (30/1yr-style preset dropdown, ratified default per
  compliance-settings shape) — §10.3/§10.4.
- **FE-6** `[P]`: Rotation & Access Windows tab — org/team/key rotation config, guided
  provider-key rotation flow (three structured error states), access schedule
  org→team→key editors, holiday date picker, emergency override grant/revoke — §16.
- **FE-7** `[P]`: Retention & Docs tab — log-prompt retention preset dropdown (30 days
  pre-filled), two static download links to docs-writer's compliance deliverables
  (§9.9 — no new screen logic beyond two `<a href>`s).
- **FE-8** `[D: FE-6]`: My Team → Access Schedule (non-admin, Team Lead narrowing view)
  — `ui-requirements-non-admin.md` §7's Access Schedule item.
- **FE-9** `[D: FE-6]`: CLI Auto-Sync section on My API Keys (device-code login
  instructions, status) — `ui-requirements-non-admin.md` §6.1.
- **FE-10** `[D: FE-1..FE-9]`: end-to-end smoke pass once backend routes are live —
  sequenced last, not parallelizable.

### Parallelization summary

`DB-1` through `DB-9` are entirely `[P]` with each other (touch disjoint tables/enums);
`DB-10` (ORM models) is the single gate almost every backend task waits on. Once
`DB-10` lands, five backend tracks proceed fully in parallel (DLP/residency/content-
classification; audit gap-closure; scheduler/rotation/purge; access windows; SCIM) —
they touch disjoint files except the shared gateway pipeline rewiring in
`api/v1/gateway/common.py`, which is deliberately concentrated into one task (BD-6) that
BD-19 (access-schedule wiring) depends on, so the two pipeline-touching changes don't
race each other. `cli-sync/`'s package scaffold and cache-file logic (CLI-1) has zero
backend dependency and should start immediately; the auth/client layer (CLI-2/3) waits
on the backend device-flow contract (BD-25) being stable, not on its implementation
being merged. `docs-writer`'s compliance-documentation deliverable is fully independent
of every other track. Frontend work is entirely parallel with backend and with itself,
gated only on §9's contract being stable.

---

## 12. Forward-looking rework flags

- **Phase 4 (caching, rate limiting, multi-worker scale)**: the new
  `ResidencyRuleCache`/`AccessScheduleCache`/`ContentAwareRuleCache` all share the exact
  same "in-process singleton, no cross-worker convergence" limitation Phase 1.3/Phase 2
  already documented for `ModelPolicyCache`/`TeamModelPolicyCache` — should be revisited
  together, once, alongside whatever shared-state mechanism Phase 4 introduces, not
  solved independently per cache. Phase 4's rate-limiter and Phase 3's scheduler loop
  (§4.2) both need "coordinate across replicas" answers — worth designing as one
  mechanism, not two, when Phase 4 is scoped.
- **Phase 5 (hash-chained audit ledger)**: `audit_entries.source_ip` is additive, no
  further rework anticipated. The audit purge exception (§7.3) needs re-examination
  once hash-chaining lands — a chained ledger with a sanctioned bulk-delete exception
  is a real tension (deleting a row breaks the chain unless purge is chain-aware) that
  Phase 5's design must resolve explicitly, not inherit silently.
- **Phase 5 (full dynamic content classification)**: `content_aware_rules.category` is
  already a free-text column (not an enum), specifically so Phase 5 can add real
  classifier-backed categories (Microsoft Purview/Google DLP sources) without a schema
  change — only the resolver function needs new category-handling logic.
- **Real region metadata for OpenRouter**: if a pilot org needs OpenRouter traffic to
  participate meaningfully in residency rules (rather than always reading as
  "unknown"/blocked), this needs OpenRouter's own per-model backend-region API (if one
  exists) — deliberately not built speculatively now (§3.1).
- **CLI-sync's every-fetch-rotates-the-key mechanic** (§8.2, fork #3): if a future
  pilot's usage pattern makes this too aggressive (e.g. many short-lived CI jobs sharing
  one refresh credential, each triggering a rotation), the fix is a short server-side
  debounce window on `GET /v1/me/current-key` (return the same still-valid secret if
  the last mint was under N seconds ago) rather than a redesign — flagging the extension
  point now so it isn't rediscovered as a production incident later.
