---
title: Phase 2 — Multi-Tenant Governance — Architecture Design
status: accepted
author: architect
last_updated: 2026-07-29
---

# Phase 2 — Multi-Tenant Governance — Design

Scope: Org → Team → User hierarchy, four-role RBAC (`org_admin`, `team_lead` per-team,
`member`, `auditor`), OIDC/SSO login with server-side sessions, three-level nested
budgets (org ceiling / team ceiling / per-(user,team) spend cutoff) with atomic
concurrency-safe enforcement, nested (narrowing-only) team model policy, a delegated
Team Lead console, self-service personal API keys, the join-request onboarding/approval
workflow, a plain append-only audit trail, and pluggable threshold-alert notifiers
(webhook verified, email unverified-live).

Source of truth for scope/ACs/ratified ambiguities: `gatekey/phase-2-product-spec.md`
(product-owner spec, §0–§9) plus the orchestrator's explicit ratification of A1–A8 in
the handoff brief. This document does not re-litigate those decisions; it designs
against them. Builds directly on Phase 1.1 (`require_admin`, `DEFAULT_ORG_ID`
single-org precedent), Phase 1.2 (`ServiceAccountContext`/gateway route-handler chain),
Phase 1.3 (`ModelPolicyCache`'s cache/invalidation pattern — this phase executes that
doc's own §8 forward-looking rework flag), and Phase 1.4 (`services/budget.py`'s atomic
`UPDATE ... RETURNING` pattern, `NUMERIC(20,10)` precision convention, `ON DELETE
RESTRICT` credential-blocks-user-deletion pattern).

**A6 restated as a binding rule** (carried in verbatim, referenced throughout this
document — not re-derived per section):

> Once a user has at least one `TeamMembership` row, all NEW personal keys and any
> team-attributed `ServiceAccountKey` resolve budget against that `TeamMembership
> .budget_usd`, never the legacy flat `User.budget_usd`. The flat field becomes
> read-only legacy state relevant only to pre-existing `team_id = null`
> `ServiceAccountKey` rows.

---

## 1. Schema design

Every new table is scoped to the single default org (`constants.DEFAULT_ORG_ID`),
matching every existing Phase 1 table — Phase 2 does not introduce multi-org support,
only the team layer beneath the existing single org. Migration ownership: DB-admin
writes the actual Alembic revisions (next available is `0007`, following `0006_add_
ollama_openrouter_providers.py`); this section specifies column/constraint/index/FK
shape, not migration file contents. Suggested logical grouping (DB-admin's call to
split differently if it's cleaner): `0007` org_settings + teams + team_model_policies,
`0008` team_memberships, `0009` users additions + sessions, `0010` join_requests,
`0011` personal_api_keys + `service_account_keys.team_id`, `0012` audit_entries,
`0013` usage_logs additions.

New Postgres enums (all `create_type=False`, DDL owned by the migration, following
`provider_name`/`model_policy_mode`'s existing convention exactly):

| Enum | Values |
|---|---|
| `user_org_role` | `org_admin`, `auditor` |
| `team_role` | `team_lead`, `member` |
| `team_period_type` | `monthly`, `quarterly` |
| `team_period_end` | `rollover`, `reset` |
| `join_request_status` | `pending`, `approved`, `rejected` |
| `join_request_routed_to` | `team_lead`, `org_admin` |

### 1.1 `org_settings` (new)

```
org_settings
  org_id                                    uuid PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE
  budget_ceiling_usd                        numeric(20,10) NULL   -- NULL = no org-wide ceiling
  currency                                  varchar(3) NOT NULL DEFAULT 'USD'   -- see ADR-9
  max_self_serve_key_expiration_days        integer NULL           -- NULL = no max
  personal_key_soft_cap                     integer NOT NULL DEFAULT 10
  auto_provision_personal_key_on_approval   boolean NOT NULL DEFAULT false
  created_at, updated_at
```

Mirrors `ModelPolicy`'s ADR-1 exactly (`org_id` as PK, not a surrogate id — "exactly
one settings row per org" is a schema-level invariant) and its ADR-2 (absence of row =
default state: no ceiling, `USD`, no max expiration, cap 10, auto-provision off — no
signup-seed dependency needed).

### 1.2 `teams` (new)

```
teams
  id                            uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id                        uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  name                          text NOT NULL
  budget_ceiling_usd            numeric(20,10) NULL     -- NULL = unmetered team ceiling
  current_spend_usd             numeric(20,10) NOT NULL DEFAULT 0   -- denormalized aggregate, see ADR-7
  period_type                   team_period_type NOT NULL DEFAULT 'monthly'
  on_period_end                 team_period_end NOT NULL DEFAULT 'reset'   -- product spec's resolved default
  current_period_started_at     timestamptz NOT NULL DEFAULT now()
  alert_threshold_80_enabled    boolean NOT NULL DEFAULT true
  alert_threshold_100_enabled   boolean NOT NULL DEFAULT true
  webhook_alert_enabled         boolean NOT NULL DEFAULT false
  webhook_ciphertext            bytea NULL   -- AES-256-GCM envelope, see note below
  webhook_nonce                 bytea NULL
  webhook_auth_tag              bytea NULL
  email_alert_enabled           boolean NOT NULL DEFAULT false
  created_at, updated_at

  UNIQUE (org_id, name)
  INDEX ix_teams_org_id (org_id)
```

**Webhook URL is encrypted at rest, not stored as plaintext `text`.** A Slack incoming-
webhook URL embeds a bearer-equivalent secret in its path; storing it in plaintext
would violate the cross-phase non-negotiable ("no plaintext provider keys at rest,"
which this extends to any bearer-equivalent URL). Reuses `services/encryption.py`'s
existing AES-256-GCM envelope exactly as `ProviderKey` does (`ciphertext`/`nonce`/
`auth_tag` columns, same three-piece-always-written-together discipline), with
associated data bound to `team_id` instead of `org_id:provider`.

### 1.3 `team_model_policies` (new)

```
team_model_policies
  team_id      uuid PRIMARY KEY REFERENCES teams(id) ON DELETE CASCADE
  models       jsonb NOT NULL DEFAULT '[]'::jsonb   -- allowed subset of the org baseline
  created_at, updated_at
```

Directly executes `phase-1.3-model-governance.md` §8's own forward-looking rework flag
("a new `team_model_policies` table alongside this one... rather than reshaping this
table, so Phase 1 policy data and behavior are preserved unchanged"). Mirrors
`ModelPolicy`'s ADR-1 (team_id-as-PK, one row max per team) and ADR-2 (absence of row =
"no further restriction beyond the org baseline," not a third state needing a column).
There is deliberately no `mode` column: a team can only ever narrow (AC3.1/3.2), so
`models` is always interpreted as an allowlist-intersected-with-org-baseline, never a
denylist — this makes "team re-enables an org-banned model" structurally impossible to
express as anything other than a no-op (see §4).

### 1.4 `team_memberships` (new)

```
team_memberships
  id                    uuid PRIMARY KEY DEFAULT (app-side uuid4)
  team_id               uuid NOT NULL REFERENCES teams(id) ON DELETE CASCADE
  user_id               uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE
  role                  team_role NOT NULL DEFAULT 'member'
  budget_usd            numeric(20,10) NULL    -- NULL = unmetered for this (user, team) pair
  current_spend_usd     numeric(20,10) NOT NULL DEFAULT 0
  created_at, updated_at

  UNIQUE (team_id, user_id)
  INDEX ix_team_memberships_user_id (user_id)
  INDEX ix_team_memberships_team_id (team_id)
```

Removal (Team Lead/Org Admin "remove member") is a hard row delete, not a soft
`revoked_at` marker — unlike keys/sessions, a membership's full history (who/when/what
budget) is already durably captured by `AuditEntry`, so the live row only needs to
represent current state. `ON DELETE CASCADE` on `user_id`: deleting the underlying
`User` row (a Phase-1-style admin action, rare and orthogonal to normal team-membership
removal) also removes their team memberships — see ADR-4 for why member/key cleanup is
still gated at the *membership-removal* level regardless.

### 1.5 `join_requests` (new)

```
join_requests
  id                     uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id                 uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  requester_user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE
  requester_name         text NOT NULL   -- snapshot at submit time (AC6.2's editable IdP claim), independent of users.name
  team_id                uuid NOT NULL REFERENCES teams(id) ON DELETE RESTRICT
  status                 join_request_status NOT NULL DEFAULT 'pending'
  routed_to              join_request_routed_to NOT NULL   -- snapshot at submit time, see §4.3 for why live queue visibility is NOT solely derived from this column
  requested_at           timestamptz NOT NULL DEFAULT now()
  resolved_at            timestamptz NULL
  resolved_by_user_id    uuid NULL REFERENCES users(id) ON DELETE SET NULL
  approved_budget_usd    numeric(20,10) NULL   -- set only when status = 'approved'
  rejection_reason       text NULL
  created_at, updated_at

  INDEX ix_join_requests_team_id_status (team_id, status)
  INDEX ix_join_requests_requester_user_id (requester_user_id)
  UNIQUE INDEX uq_join_requests_one_pending_per_user
    ON join_requests (requester_user_id) WHERE status = 'pending'
```

**AC6.4 ("one pending request per user at a time") is enforced as a schema-level
invariant** via the partial unique index, not app-level pre-check-then-insert — same
"let the type system/schema guarantee it" philosophy already used for `ModelPolicy`'s
absence-of-row invariant. A second `INSERT` while one is pending fails on the unique
violation; the service layer catches `IntegrityError` and maps it to a clean 409, same
shape as every other structured-error path in this codebase.

`team_id` is `ON DELETE RESTRICT`: a team cannot be deleted while it has any
`join_requests` row referencing it (pending *or* historical) — extends the UI doc's own
"removing a team requires reassigning/removing all members first" rule to pending
requests, which is the more conservative, audit-preserving choice (a team's request
history staying attached to a real team row, never silently orphaned).

### 1.6 `personal_api_keys` (new)

```
personal_api_keys
  id                    uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id                uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  owner_user_id         uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT
  created_by_user_id    uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT
  team_id               uuid NOT NULL REFERENCES teams(id) ON DELETE RESTRICT
  name                  text NOT NULL
  key_prefix            varchar(12) NOT NULL
  secret_hash           bytea NOT NULL   -- SHA-256, 32 bytes
  expires_at            timestamptz NULL   -- NULL = no expiration
  revoked_at            timestamptz NULL   -- NULL = active
  created_at            timestamptz NOT NULL DEFAULT now()

  UNIQUE INDEX ix_personal_api_keys_secret_hash (secret_hash)
  INDEX ix_personal_api_keys_owner_user_id (owner_user_id)
  INDEX ix_personal_api_keys_org_id (org_id)
```

Deliberately a **separate table**, not a repurposed `ServiceAccountKey` row (per the
locked architecture decision), but every column-level convention is copied verbatim
from `ServiceAccountKey`: `ON DELETE RESTRICT` on the owning user (same "a live
credential must never silently orphan-delete its owner" rationale from
`phase-1.4-budget-basic-design.md` §1.4/ADR-2, now applied to two owner-shaped columns
instead of one), SHA-256 (not a slow KDF) for the same "256-bit random token, not a
guessable password" reasoning, `revoked_at`-only (no redundant `is_active` boolean),
and no plaintext secret column ever. `team_id` is `NOT NULL` at the schema level (not
service-layer-only like `ServiceAccountKey.team_id` — see §1.7 for why the two differ):
every `PersonalApiKey` is created fresh under Phase 2, so there is no legacy-row
population problem forcing nullability here the way there is for the pre-existing
`service_account_keys` table.

**Auth prefix**: personal keys use `gk_pk_` (distinct from `ServiceAccountKey`'s
`gk_sk_`), so the unified gateway-auth dependency (§2.4) can route by prefix with a
single lookup, never a try-both-tables fallback.

### 1.7 `service_account_keys.team_id` (alter existing table)

```
ALTER TABLE service_account_keys
  ADD COLUMN team_id uuid NULL REFERENCES teams(id) ON DELETE RESTRICT;
CREATE INDEX ix_service_account_keys_team_id ON service_account_keys (team_id);
```

`NULL` = legacy row (created before Phase 2, or an org that never adopts teams for a
given service account) — resolves against the owner `User.budget_usd`/
`current_spend_usd`, byte-for-byte the existing Phase 1.4 code path, unchanged.

**Enforcement of "new keys require `team_id` going forward" is service-layer, not a
`NOT NULL`/`CHECK` column constraint** — this directly reuses an already-shipped
precedent in this exact codebase rather than inventing a new mechanism:
`ServiceAccountKeyCreateRequest.user_id` (Phase 1.4) is `NOT NULL` at the Pydantic
schema/API layer while the underlying column had to stay nullable-then-backfilled at
the DB layer for pre-existing rows (see `phase-1.4-budget-basic-design.md` §1.2, ADR-7).
`team_id` follows the identical shape: the migration adds it nullable with **no**
backfill (there is no team an existing key can be correctly, non-guessed attributed to
— unlike Phase 1.4's `user_id` backfill, which had a safe default: one auto-created
unmetered legacy user); `ServiceAccountKeyCreateRequest` gains a required `team_id:
UUID` field for every *new* creation from Phase 2 onward. A column-level `NOT NULL`
can't distinguish "legacy row" from "new row created without a team" without a
brittle, migration-cutover-timestamp-shaped `CHECK` constraint, so the schema stays
permissive and the API contract is what actually closes the gap — consistent with how
this codebase already resolved the exact same tension once.

### 1.8 `users` additions (alter existing table)

```
ALTER TABLE users ADD COLUMN org_role user_org_role NULL;   -- NULL = no org-wide role
ALTER TABLE users ADD COLUMN sso_subject text NULL;          -- OIDC 'sub' claim
ALTER TABLE users ADD COLUMN sso_email text NULL;             -- IdP-asserted email, display only
CREATE UNIQUE INDEX ix_users_sso_subject ON users (sso_subject) WHERE sso_subject IS NOT NULL;
```

`sso_subject` (the OIDC `sub` claim), not email, is the auth-lookup key and the unique
constraint's target — standard OIDC relying-party guidance: `sub` is the IdP's durable
per-user identifier, while email can change or be reassigned. The partial unique index
exempts every pre-Phase-2, admin-created `User` row (`sso_subject IS NULL` — Phase 1's
flat, non-SSO cost-center users), so no backfill/migration conflict there.

`org_role = NULL` is the common case (ordinary member/team_lead — that role lives on
`TeamMembership.role` instead, per the locked RBAC data-model decision); `org_admin`/
`auditor` are org-wide and independent of any specific team.

### 1.9 `sessions` (new)

```
sessions
  id              uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id          uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  user_id         uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE
  token_hash      bytea NOT NULL   -- SHA-256 of the opaque cookie value
  created_at      timestamptz NOT NULL DEFAULT now()
  last_seen_at    timestamptz NOT NULL DEFAULT now()
  expires_at      timestamptz NOT NULL
  revoked_at      timestamptz NULL   -- NULL = active

  UNIQUE INDEX ix_sessions_token_hash (token_hash)
  INDEX ix_sessions_user_id (user_id)
```

Same SHA-256-lookup-hash pattern as `ServiceAccountKey.secret_hash`/`PersonalApiKey
.secret_hash` — the raw opaque token is the httpOnly cookie value, never persisted in
plaintext; a leaked DB dump cannot be replayed as a live session. `ON DELETE CASCADE`
from `users`: a deleted user's sessions die with them (no orphaned, unauthenticatable
session rows to reason about).

### 1.10 `audit_entries` (new)

```
audit_entries
  id               uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id           uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  actor_user_id    uuid NULL REFERENCES users(id) ON DELETE SET NULL
  actor_label      text NOT NULL   -- name/email snapshot, or "system:admin_token" sentinel (A4)
  action           text NOT NULL   -- fixed vocabulary, see §5's action-type table
  target_type      text NOT NULL
  target_id        text NOT NULL   -- stringified id; deliberately not a typed/polymorphic FK
  old_value        jsonb NULL
  new_value        jsonb NULL
  created_at       timestamptz NOT NULL DEFAULT now()

  INDEX ix_audit_entries_org_id_created_at (org_id, created_at)
  INDEX ix_audit_entries_actor_user_id (actor_user_id)
  INDEX ix_audit_entries_action (action)
```

Plain, append-only (AC4.2) — service-layer code only ever `INSERT`s here, never
`UPDATE`/`DELETE`. `actor_label` is a **snapshot**, not a live join to `users.name`, so
a later rename/delete of the acting user never rewrites history. `target_id` is text,
not a real FK, because `target_type` varies row-to-row (a genuine polymorphic
reference, which Postgres has no native typed-FK support for) — this table
deliberately never blocks deletion of anything it references, and nothing ever
deletes a row out from under it either. **Forward-compat note**: Phase 5's
hash-chained ledger is explicitly documented (UI doc §10.3) as adding columns
(`chain_hash`, `prev_hash`) to this same table, not a new one — this schema is written
to make that an additive migration, not a rework.

### 1.11 `usage_logs` additions (alter existing table)

```
ALTER TABLE usage_logs ADD COLUMN team_id uuid NULL REFERENCES teams(id) ON DELETE SET NULL;
ALTER TABLE usage_logs ADD COLUMN personal_api_key_id uuid NULL REFERENCES personal_api_keys(id) ON DELETE SET NULL;
ALTER TABLE usage_logs ADD COLUMN raw_provider_cost_usd numeric(20,10) NULL;
ALTER TABLE usage_logs ADD COLUMN fx_rate_applied numeric(20,10) NOT NULL DEFAULT 1;
CREATE INDEX ix_usage_logs_team_id ON usage_logs (team_id);
```

`team_id`/`personal_api_key_id` follow the exact nullable + `SET NULL` pattern already
used for `user_id`/`service_account_key_id` on this table (module docstring: "a usage
record should outlive the credential/user that generated it"). `cost_usd` (existing
column) continues to mean "the normalized cost charged against the org's budget
currency" — see ADR-9 for why, this phase, `raw_provider_cost_usd == cost_usd` and
`fx_rate_applied == 1` always, and why the columns still exist now rather than being
deferred.

---

## 2. Auth / session design

### 2.1 OIDC authorization-code flow

Standard, provider-agnostic authorization-code flow with PKCE (S256), config-driven via
env vars following `config.py`'s exact `.env.example` pattern:

```
GATEKEY_OIDC_ISSUER_URL          # e.g. https://acme.okta.com or http://keycloak:8080/realms/gatekey-dev
GATEKEY_OIDC_CLIENT_ID
GATEKEY_OIDC_CLIENT_SECRET
GATEKEY_OIDC_REDIRECT_URI        # e.g. http://localhost:8000/v1/auth/sso/callback
GATEKEY_SESSION_COOKIE_SECURE    # default true; settable false only for local http dev
GATEKEY_SESSION_TTL_HOURS        # default 12
```

`Settings` gains `field_validator`s mirroring `GATEKEY_ADMIN_TOKEN`'s: if any one of
`GATEKEY_OIDC_ISSUER_URL`/`CLIENT_ID`/`CLIENT_SECRET`/`REDIRECT_URI` is set, all four
must be (fail-fast at startup, not a confusing runtime 500 on first login attempt) — SSO
stays fully optional (unset = SSO routes 404/disabled, break-glass token remains the
only path), matching the "under-60-minutes `docker-compose up`" promise; nothing about
Phase 2 requires an operator to stand up SSO to keep using Gatekey.

Sequence:

1. `GET /v1/auth/sso/login` — no auth required. Backend fetches (and short-TTL
   in-process-caches) the issuer's `/.well-known/openid-configuration` discovery
   document, builds the authorization URL (`client_id`, `redirect_uri`, `scope=openid
   profile email`, `response_type=code`, `state`, PKCE `code_challenge`, `nonce`),
   stores `state`/PKCE-verifier/`nonce` in a short-lived signed cookie (not server-side
   state — no DB row needed for a value that lives seconds), 302s to the IdP.
2. IdP authenticates the user, redirects to `GET /v1/auth/sso/callback?code=...&state=...`.
3. Backend validates `state` against the signed cookie, exchanges `code` at the IdP's
   token endpoint (server-to-server, `client_secret` included — this is a confidential
   client; the browser never talks to the IdP's token endpoint directly), validates the
   ID token (issuer, audience, expiry, `nonce`), extracts `sub`/`email`/`name`.
4. **Upsert `User`** by `sso_subject = sub`. Not found → create
   `User(org_id=DEFAULT_ORG_ID, name=<claim>, sso_subject=sub, sso_email=email,
   org_role=NULL, budget_usd=NULL)` — the flat `budget_usd` stays `NULL`/unused for
   every SSO-provisioned user by construction (A6: it only ever mattered for Phase 1
   flat users, and a freshly-created SSO user immediately proceeds to either onboarding
   or an existing `TeamMembership`).
5. **Route by state**, computed fresh on every callback/`GET /v1/auth/me` (never cached
   client-side as a one-time decision):
   - `org_role` is set, **or** ≥1 `TeamMembership` row exists → issue a `Session`, set
     the httpOnly cookie, redirect to the app.
   - a `pending` `JoinRequest` exists for this user → redirect to the holding-state
     screen (still issues a session — a pending user is authenticated, just not yet
     authorized for gateway/console access beyond that one screen; see §5's onboarding
     endpoints for how routes stay locked down regardless of session validity).
   - neither → redirect to the profile+team-selection screen (§2.6/AC6.1-6.3).
6. **Session issuance**: `secrets.token_urlsafe(32)` raw token; `sessions.token_hash =
   sha256(raw)`; `Set-Cookie` with the **raw** value, `HttpOnly`, `Secure` (per
   `GATEKEY_SESSION_COOKIE_SECURE`), `SameSite=Lax`, `Max-Age` matching `expires_at`
   (`now() + GATEKEY_SESSION_TTL_HOURS`).

### 2.2 Session validation

```python
@dataclass(frozen=True)
class SessionContext:
    session_id: uuid.UUID
    org_id: uuid.UUID
    user_id: uuid.UUID
    org_role: Literal["org_admin", "auditor"] | None
    display_label: str   # name/email snapshot, for audit actor_label

async def try_get_session_context(request: Request, session: AsyncSession) -> SessionContext | None:
    """Reads the session cookie, hashes it, looks up an active
    (revoked_at IS NULL AND expires_at > now()) sessions row joined to
    users.org_role. Returns None on any failure (no cookie, no matching row,
    expired, revoked) - never raises, so callers decide what "no session"
    means for their own route (require_admin's break-glass fallback vs.
    get_current_session's hard 401)."""

async def get_current_session(request: Request, session: AsyncSession = Depends(get_db_session)) -> SessionContext:
    """Hard-fails (401 UnauthorizedError) if try_get_session_context returns
    None. The base dependency for every non-admin, session-authenticated
    route (My Usage, Model Access, My API Keys, onboarding, etc.)."""
```

`last_seen_at` is updated best-effort (fire-and-forget, never blocks the request or
fails it on a write error) — not load-bearing for auth, just operational visibility.

### 2.3 `require_admin` refactor — break-glass OR org_admin session

```python
@dataclass(frozen=True)
class AdminContext:
    actor_user_id: uuid.UUID | None   # None for break-glass (A4)
    actor_label: str                  # "system:admin_token", or the session user's snapshot
    org_id: uuid.UUID

async def require_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> AdminContext:
    """Break-glass bearer token OR an org_admin session cookie - either
    satisfies this dependency (AC1.4/AC1.5). Checked in that order: the
    break-glass path is a cheap, no-DB constant-time comparison and is tried
    first, exactly preserving Phase 1's original check/timing for that path
    unchanged; only falls through to a session lookup if no valid bearer
    token was presented.
    """
    if credentials is not None and credentials.credentials:
        settings: Settings = request.app.state.settings
        if hmac.compare_digest(
            credentials.credentials.encode("utf-8"), settings.GATEKEY_ADMIN_TOKEN.encode("utf-8")
        ):
            return AdminContext(actor_user_id=None, actor_label="system:admin_token", org_id=DEFAULT_ORG_ID)

    ctx = await try_get_session_context(request, session)
    if ctx is not None and ctx.org_role == "org_admin":
        return AdminContext(actor_user_id=ctx.user_id, actor_label=ctx.display_label, org_id=ctx.org_id)

    raise UnauthorizedError("Missing or invalid admin credential.")
```

**Every existing Phase 1 admin router declares this dependency router-level**
(`dependencies=[Depends(require_admin)]`) and never inspects its return value —
changing the return type from `None` to `AdminContext` is purely additive; not one
existing route needs to change. New Phase 2 admin routes that need the actor identity
for an `AuditEntry` write instead use the parameter form
(`admin: AdminContext = Depends(require_admin)`).

### 2.4 `require_role` / `require_team_role` — resolving role per specific team

A user can be `team_lead` on team A and `member` on team B simultaneously (AC1.2), so
role resolution must be **route-scoped to the `team_id` in that specific request**, not
a single flat "what's my role" fact on the session.

```python
def require_role(*allowed_org_roles: Literal["org_admin", "auditor"]):
    """Factory for ORG-WIDE-role-only routes (Settings, org-level Teams CRUD,
    Identity & Access, org-wide Audit Log). Returns a dependency that checks
    SessionContext.org_role against allowed_org_roles - a member/team_lead
    session (org_role=NULL) is always rejected here regardless of any team
    they lead, by design: leading a team is not an org-wide privilege."""
    async def _dep(ctx: SessionContext = Depends(get_current_session)) -> SessionContext:
        if ctx.org_role not in allowed_org_roles:
            raise ForbiddenError("This action requires an org-wide role.")
        return ctx
    return _dep


def require_team_role(*allowed_team_roles: Literal["team_lead", "member"], org_admin_bypass: bool = True):
    """Factory for TEAM-scoped routes. The returned dependency takes the
    route's own `team_id: uuid.UUID` path parameter (FastAPI resolves this
    automatically - both are Depends-injected into the same route handler)
    and looks up TeamMembership(session.user_id, team_id).role, checking it
    against allowed_team_roles.

    org_admin_bypass=True (the default): a session with org_role=='org_admin'
    always passes, regardless of whether they hold a TeamMembership row for
    this team at all - Org Admin has full control over every team (locked
    architecture decision). Set False only for the rare route that must stay
    strictly team-internal even to an Org Admin (none identified this phase;
    exposed for completeness/future use).

    On failure (no matching membership, or membership role not in
    allowed_team_roles), raises the SAME generic 403 regardless of whether
    the team exists at all - deliberately not distinguishing "team not
    found" from "you're not a member with sufficient role," mirroring
    require_service_account's existing anti-enumeration discipline (never
    give an authenticated-but-unauthorized caller a way to probe which team
    IDs are real).
    """
    async def _dep(
        team_id: uuid.UUID,
        ctx: SessionContext = Depends(get_current_session),
        session: AsyncSession = Depends(get_db_session),
    ) -> TeamRoleContext:
        if org_admin_bypass and ctx.org_role == "org_admin":
            return TeamRoleContext(session=ctx, team_id=team_id, role="org_admin", via_bypass=True)
        membership = await get_team_membership(session, team_id=team_id, user_id=ctx.user_id)
        if membership is None or membership.role not in allowed_team_roles:
            raise ForbiddenError("You do not have the required role for this team.")
        return TeamRoleContext(session=ctx, team_id=team_id, role=membership.role, via_bypass=False)
    return _dep
```

`ForbiddenError` (new, 403, `code="forbidden"`) is added to `errors.py` alongside the
existing `UnauthorizedError`/`NotFoundError` — auth failure (no/invalid credential) vs.
authorization failure (valid credential, insufficient role) are kept as the standard
401-vs-403 distinction this codebase already draws (`UnauthorizedError` is 401;
`ModelDeniedError`/`ForbiddenError` are 403).

Composition examples used throughout §5's API contract:
- `Depends(require_role("org_admin"))` — team CRUD, org ceiling, Identity & Access.
- `Depends(require_role("org_admin", "auditor"))` — org-wide read-only views (Audit Log,
  Policy Viewer, Org Usage).
- `Depends(require_team_role("team_lead"))` — member/budget/model-restriction mutation
  within one team (org-admin-bypassed automatically).
- `Depends(require_team_role("team_lead", "member"))` — any team participant, read-only
  (Team Dashboard, own team's Model Restrictions view).
- Plain `Depends(get_current_session)` — "My Usage," "My API Keys," "Model Access": any
  authenticated user, authorized by construction because every query is scoped to
  `ctx.user_id` server-side, never to a caller-supplied id.

### 2.5 Unified gateway credential dependency

`require_service_account` is extended (not replaced in shape — kept as the concrete
handler for `gk_sk_` credentials) behind one new dispatching dependency,
`require_gateway_credential`, which becomes the auth dependency on all three gateway
routes in place of `require_service_account`:

```python
@dataclass(frozen=True)
class GatewayCallerContext:
    org_id: uuid.UUID
    credential_id: uuid.UUID
    credential_type: Literal["service_account", "personal"]
    user_id: uuid.UUID          # budget/policy identity - the owning human or app's user
    team_id: uuid.UUID | None   # resolved team context (A6/AC5.5) - None = legacy flat-budget path
    name: str

async def require_gateway_credential(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    session: AsyncSession = Depends(get_db_session),
) -> GatewayCallerContext:
    """Dispatches on the bearer token's prefix - gk_sk_ -> ServiceAccountKey
    lookup (existing hash/lookup logic, unchanged), gk_pk_ -> PersonalApiKey
    lookup (new: same hash-lookup shape, PLUS an expires_at freshness check
    a ServiceAccountKey lookup doesn't need). Any other/missing prefix ->
    the same generic UnauthorizedError message as today (never distinguishes
    "wrong prefix" from "not found" in the response, same anti-enumeration
    posture already established)."""
```

This keeps "one shared helper, one call site per route" (Phase 1.3's own stated
precedent) intact — `chat.py`/`completions.py`/`embeddings.py` each swap their existing
`ctx: ServiceAccountContext = Depends(require_service_account)` parameter for `ctx:
GatewayCallerContext = Depends(require_gateway_credential)`; every downstream use of
`ctx.user_id` is unchanged, `ctx.team_id` is new and threads into `check_model_policy`/
`check_budget_available` (§3, §4).

---

## 3. Budget enforcement design

### 3.1 Which counter gets decremented (A6, made concrete)

Given `GatewayCallerContext.team_id`:

- **`team_id is not None`** (every new personal key; a `ServiceAccountKey` with
  `team_id` set): resolve/charge against **that `TeamMembership`'s** `budget_usd`/
  `current_spend_usd` — looked up by `(team_id, user_id)`, which is guaranteed to exist
  because (a) a `PersonalApiKey`/team-attributed `ServiceAccountKey` can only be created
  if the owner already holds that `TeamMembership` (service-layer check at key-creation
  time), and (b) membership removal is blocked while such a key still exists (ADR-4) —
  so this lookup is never "should be unreachable," it's guaranteed by construction, the
  same discipline `check_budget_available`'s existing `AssertionError` comment already
  documents for the Phase 1.4 case.
- **`team_id is None`** (every pre-Phase-2 `ServiceAccountKey`): resolve/charge against
  the owning `User.budget_usd`/`current_spend_usd` — byte-for-byte the unmodified
  Phase 1.4 code path.

The org ceiling and team ceiling are **never** independently re-checked or decremented
at spend time — they are allocation constraints, enforced only when someone *writes* a
`budget_usd`/`budget_ceiling_usd` value (AC2.2's own framing: "enforced at assignment
time, not just at spend time"). This is a deliberate simplification directly licensed by
the product spec's own wording, and it is what keeps the spend-time hot path an
unchanged extension of `services/budget.py`'s existing single-counter atomic pattern
rather than a three-level cascade of writes on every request.

### 3.2 Spend-time atomic deduction (AC2.3) — direct extension of `budget.py`

```python
async def record_team_membership_usage_charge(
    session: AsyncSession, *, membership_id: uuid.UUID, team_id: uuid.UUID,
    model: str, prompt_tokens: int, completion_tokens: int | None,
) -> Decimal:
    """Same shape as services.budget.record_usage_charge - single UPDATE ...
    RETURNING statement, no read-modify-write. Updates BOTH the membership's
    own current_spend_usd (the spend-cutoff counter) AND the team's
    denormalized aggregate current_spend_usd (ADR-7, for threshold-alert
    detection) in the SAME transaction, via two single-row UPDATE ...
    RETURNING statements - never a SUM() aggregate query."""
    cost = compute_cost(model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)

    membership_stmt = (
        update(TeamMembership)
        .where(TeamMembership.id == membership_id)
        .values(current_spend_usd=TeamMembership.current_spend_usd + cost)
        .returning(TeamMembership.current_spend_usd)
    )
    new_membership_total = (await session.execute(membership_stmt)).scalar_one()

    team_stmt = (
        update(Team)
        .where(Team.id == team_id)
        .values(current_spend_usd=Team.current_spend_usd + cost)
        .returning(Team.current_spend_usd, (Team.current_spend_usd).label("new_total"))
    )
    # RETURNING also exposes the pre-charge total via `Team.current_spend_usd - cost`
    # (computed in the RETURNING clause itself, no extra read) - see §3.4.
    ...
    await session.commit()
    return cost
```

`check_budget_available` (pre-call gate) is extended identically to `is_budget_exhausted
`'s existing logic, just reading `TeamMembership` instead of `User` when `team_id` is
present — `NULL budget_usd` = unmetered, `current_spend_usd >= budget_usd` = exhausted,
same `>=`-not-`>` semantics, same `402 BudgetExhaustedError`.

### 3.3 Assignment-time ceiling enforcement — a genuinely different concurrency pattern

**ADR-5: assignment-time ceiling checks use `SELECT ... FOR UPDATE` row-locking on the
constraining parent row (team or org), not a single `UPDATE ... RETURNING` statement.**

- **Decision**: every write that must satisfy "sum of children ≤ parent's ceiling"
  (creating/editing a `TeamMembership.budget_usd`, approving a `JoinRequest` with a
  budget, reassigning budget between two members, editing a `Team.budget_ceiling_usd`
  against the org ceiling) runs inside an explicit transaction that first issues
  `SELECT budget_ceiling_usd FROM teams WHERE id = :team_id FOR UPDATE` (or the
  equivalent `org_settings` row for the org-ceiling-vs-team-ceilings check, A3),
  computes the current aggregate (`SUM` over sibling rows) and the new proposed total
  inside that same locked transaction, and only then performs the `INSERT`/`UPDATE`,
  before `COMMIT` releases the lock.
- **Why this isn't just another `budget.py`-style single statement**: `record_usage_
  charge`'s atomicity works because the invariant is "increment *this one row's own*
  counter" — a single `UPDATE ... RETURNING` is inherently race-free for that shape. The
  assignment-time invariant is different in kind: "the *sum across a whole set of sibling
  rows* must not exceed a value read from a *different* row." A single statement with
  correlated subqueries (`INSERT ... WHERE (SELECT SUM(...) ...) + :new <= (SELECT
  ceiling ...)`) is *not* sufficient under Postgres's default READ COMMITTED isolation:
  two concurrent transactions can both evaluate the same subqueries against the same
  pre-write snapshot and both see "headroom available," both write, and jointly
  over-allocate — exactly the failure mode the phase's own success criteria calls out
  explicitly ("a team's allocated total never exceeds its ceiling even when exercised
  concurrently by multiple pending requests"). `SELECT ... FOR UPDATE` closes this by
  serializing all budget-assignment writes **for the same team** through a row lock (a
  second transaction's `SELECT ... FOR UPDATE` on the same team row blocks until the
  first commits) — concurrent writes to *different* teams are entirely unaffected, so
  this only adds contention exactly where contention is real.
- **Alternative considered**: `SERIALIZABLE` isolation with app-level retry-on-conflict.
  Rejected — correct, but introduces a retry-loop pattern with no existing precedent
  anywhere in this codebase, for a code path (admin/team-lead budget writes) that is
  low-frequency and where a lock-wait of a few milliseconds is imperceptible; pessimistic
  locking is simpler to reason about and test deterministically (a blocking-then-succeed
  integration test is straightforward; a serialization-failure-then-retry test is not).
- **Scope of the lock**: held only for the duration of the read-check-write, never spans
  an `await` on anything other than the DB itself (no outbound HTTP, no notifier dispatch
  inside the locked section) — keeps lock hold time to a single round trip's worth of
  latency.

Concrete shape for join-request approval (AC6.7's "atomic with budget allocation, no
intermediate approved-but-unbudgeted state" — the `INSERT` and the `JoinRequest` status
update happen in the same locked transaction):

```python
async def approve_join_request(session, *, request_id, team_id, budget_usd, approved_by) -> TeamMembership:
    async with session.begin():
        team = (await session.execute(
            select(Team.budget_ceiling_usd).where(Team.id == team_id).with_for_update()
        )).one()
        allocated = (await session.execute(
            select(func.coalesce(func.sum(TeamMembership.budget_usd), 0)).where(TeamMembership.team_id == team_id)
        )).scalar_one()
        if team.budget_ceiling_usd is not None and allocated + budget_usd > team.budget_ceiling_usd:
            raise BudgetCeilingExceededError(
                headroom=team.budget_ceiling_usd - allocated, requested=budget_usd
            )
        membership = TeamMembership(team_id=team_id, user_id=..., role="member", budget_usd=budget_usd)
        session.add(membership)
        await session.execute(
            update(JoinRequest).where(JoinRequest.id == request_id).values(
                status="approved", resolved_at=func.now(), resolved_by_user_id=approved_by,
                approved_budget_usd=budget_usd,
            )
        )
    # AuditEntry write happens in the same request handler, same DB transaction
    # boundary via the shared session - see §7's audit-write convention.
    return membership
```

`BudgetCeilingExceededError` (422, `code="budget_ceiling_exceeded"`) carries the live
headroom figure in its message, matching the UI's "Max: $190 (team has $190 unallocated)"
clamping display — the frontend can also pre-fetch headroom via a plain `GET` to avoid a
round-trip failure in the common case, but the write path never trusts a client-computed
headroom figure; it always re-derives it inside the lock.

**A3 (org ceiling ≥ sum of team ceilings, and a ceiling reduction that would retroactively
over-allocate is blocked)** uses the identical pattern one level up: editing `Team
.budget_ceiling_usd` locks `org_settings` (`FOR UPDATE`) and checks the new team-ceiling
sum against `org_settings.budget_ceiling_usd`; editing `org_settings.budget_ceiling_usd`
itself checks the new org ceiling against `SUM(teams.budget_ceiling_usd)`. A *reduction*
of either ceiling below its current allocated/child-ceiling total is rejected with the
specific inline reason (422, e.g. `"Cannot reduce ceiling to $X — teams are currently
allocated $Y in total."`) rather than silently leaving something over its own ceiling,
per A3's ratified resolution.

### 3.4 Threshold-alert detection — free from the RETURNING clause, not a SUM query

**ADR-7: `teams.current_spend_usd` is a denormalized, transactionally-maintained
running total**, kept in lockstep with `SUM(team_memberships.current_spend_usd)` at
every mutation site rather than computed on demand.

- **Why**: threshold alerts (80%/100%) are evaluated against the **team's** aggregate
  spend vs. its ceiling (the product spec's user story is "a *team* crosses 80%/100% of
  *its* budget," not an individual member crossing their own). Computing that via a live
  `SUM()` aggregate on every charged request would add a second, more expensive query to
  the gateway hot path (aggregating over every member row of a potentially large team),
  which the phase's own <10ms RBAC/policy-resolution NFR budget doesn't have headroom
  for once you also account for the existing budget/policy queries. Maintaining a
  denormalized counter, updated by the same single-row `UPDATE ... RETURNING` shape
  already used everywhere else in this codebase, keeps the hot path at two cheap
  single-row writes instead of one write plus one aggregate read.
- **Consistency obligation, stated explicitly**: exactly two code paths ever mutate
  `team_memberships.current_spend_usd`, and both must update `teams.current_spend_usd`
  by the identical delta in the same transaction: (1) `record_team_membership_usage_
  charge` (§3.2 — adds `cost` to both), and (2) period rollover/reset (§3.5 — resets a
  membership's spend to 0, so the team aggregate is decremented by that membership's
  pre-reset `current_spend_usd`). Because both paths are funneled through exactly two
  shared service functions (never scattered ad hoc `UPDATE`s elsewhere), drift risk is
  low without needing a periodic reconciliation job — but a reconciliation job (`SUM`
  membership spend, compare to the team's cached total, alert/correct on drift) is a
  reasonable low-effort hardening item to schedule for a later phase, not required to
  ship Phase 2.
- **Detecting a crossed threshold, with zero extra queries**: `record_team_membership_
  usage_charge`'s `team_stmt` above computes the pre-charge total via arithmetic in the
  same `RETURNING` clause (`Team.current_spend_usd - cost` for the old value, `Team
  .current_spend_usd` for the new value, both already fetched from the one `UPDATE`).
  The service layer compares `old_total / ceiling` and `new_total / ceiling` against
  0.8/1.0: a threshold is "just crossed" only on a `false -> true` transition, which is
  how repeated over-threshold requests are prevented from re-firing a notification on
  every single subsequent charge.

### 3.5 Period boundary — lazy, touch-based rollover/reset (no scheduler daemon)

**ADR-10: period rollover/reset is evaluated lazily on next touch, not via a cron/
background scheduler.** This codebase has no existing job-runner/scheduler
infrastructure (Phase 1's entire deployment story is `docker-compose up` with Postgres +
backend + frontend, no worker/cron container), and introducing one for a single
low-frequency, per-team-per-period event would be disproportionate infrastructure for
what it buys, at odds with the self-hosted "no external dependencies beyond Postgres and
the container runtime" framing that has held since Phase 1.7.

Design: `teams.period_type`/`current_period_started_at` determine the currently active
period's boundary (`compute_period_end(period_type, current_period_started_at)`). A
single shared function, `ensure_current_period(session, team_id)`, is called at the
start of **every** code path that reads or writes team/membership spend state —
`check_budget_available` (gateway hot path), the Team detail/Members admin+team-lead
`GET` endpoints, and the Team Dashboard usage endpoint — so a boundary crossing is
applied the moment *anything* touches that team, whether that's real gateway traffic or
just an admin opening the console.

- **Hot-path cost**: the common case (not yet past the boundary) is a single, already-
  in-hand comparison (`now() >= period_end`) against a row already fetched as part of
  the existing budget-state query — no extra round trip. Only the rare boundary-crossing
  call engages the expensive path below.
- **On a crossing** (locked via the same `SELECT ... FOR UPDATE` on the `teams` row
  §3.3 already uses, so this composes with, rather than duplicates, that locking
  discipline): advance `current_period_started_at` to the correct current period (looped
  to the right boundary, not single-stepped, so a long-dormant team catches up in one
  pass rather than one period at a time); for every `TeamMembership` row in the team,
  apply the rollover/reset rule below; adjust `teams.current_spend_usd` by the sum of
  deltas applied (maintaining ADR-7's invariant).
- **Known, accepted limitation**: a team with zero traffic and nobody viewing its console
  page will not visibly roll over until the next touch — acceptable (nothing depends on
  the boundary firing at an exact wall-clock instant), and explicitly preferable to
  adding scheduler infrastructure for a self-hosted, single-container reference
  deployment.

**ADR-6: rollover/reset arithmetic.** The product spec specifies the *choice*
(rollover vs. reset, default reset) but not the exact mechanic; this is this design's own
call, flagged since it isn't dictated anywhere upstream:

- `current_spend_usd` **always** resets to `0` at a period boundary, regardless of
  `on_period_end` — reporting stays simple and non-negative either way ("spend this
  period" always means "since the period started").
- The distinguishing mechanic lives entirely in `budget_usd`: on `reset`, `budget_usd` is
  left unchanged (the admin's originally configured nominal per-period figure holds
  indefinitely). On `rollover`, `budget_usd` is incremented by that membership's unused
  amount from the just-ended period (`leftover = max(0, budget_usd - current_spend_usd)`;
  `new_budget_usd = budget_usd + leftover`) — so an unspent allowance compounds into the
  next period's effective ceiling, and keeps compounding indefinitely if left unspent
  across further periods.
- This compounding is **not** re-checked against the team's own `budget_ceiling_usd`
  (AC2.2's assignment-time check) — that check is explicitly scoped to *assignment*
  (an admin/team-lead's deliberate write), and a system-driven rollover credit is not an
  assignment. This is a deliberate, spec-consistent exemption: the product spec's own
  stated reason for defaulting to `reset` is that "rollover accumulating indefinitely...
  risks silent, unintended budget growth" — rollover growing a member's effective
  ceiling over time **is the documented, chosen consequence** of opting into it, not a
  bug this design needs to prevent.
- A membership with `budget_usd = NULL` (unmetered) skips this arithmetic entirely —
  nothing to roll over.

---

## 4. Nested model policy design

Extends `services/model_policy.py` per that module's own Phase-2 rework flag, without
touching `ModelPolicyCache`/`ModelPolicySnapshot` (the org-baseline layer stays exactly
as Phase 1.3 shipped it — zero changes, zero risk to existing behavior for orgs that
never adopt teams).

```python
class TeamModelPolicyCache:
    """Process-local cache of every team's model-restriction overlay, keyed
    by team_id. Same lock-free, GIL-atomic 'replace the whole snapshot,
    never mutate in place' contract as ModelPolicyCache - a full org
    realistically has low hundreds of teams at most, so caching and
    wholesale-replacing the entire dict on any write is cheap and avoids
    partial-update races, the same simplicity trade ModelPolicyCache itself
    already makes. Warmed at startup with the identical bounded, fail-open
    pattern as ADR-3 in phase-1.3-model-governance.md (absence of a row for
    a team = no restriction, which is also the safe/permissive default)."""

    def get(self, team_id: uuid.UUID) -> frozenset[str] | None: ...   # None = no restriction row
    def set_all(self, snapshot: dict[uuid.UUID, frozenset[str]]) -> None: ...   # full replace
```

```python
@dataclass(frozen=True)
class ModelAccessDecision:
    allowed: bool
    blocking_layer: Literal["org", "team"] | None   # None only when allowed=True
                                                       # - extensible: Phase 3/5 adds "content_classification"
                                                       #   here without reshaping this type (AC3.4)

def resolve_model_access(
    model: str, *, org_cache: ModelPolicyCache, team_cache: TeamModelPolicyCache, team_id: uuid.UUID | None,
) -> ModelAccessDecision:
    if not org_cache.get().is_allowed(model):
        return ModelAccessDecision(allowed=False, blocking_layer="org")
    if team_id is not None:
        team_restriction = team_cache.get(team_id)
        if team_restriction is not None and model not in team_restriction:
            return ModelAccessDecision(allowed=False, blocking_layer="team")
    return ModelAccessDecision(allowed=True, blocking_layer=None)
```

`check_model_policy` (gateway route call site) is extended to call `resolve_model_access`
instead of `org_cache.get().is_allowed(model)` directly, threading `ctx.team_id` from
`GatewayCallerContext` — same call-site ordering (`resolve_route -> check_model_policy ->
...`), same "one shared helper, not reimplemented per route" shape. `ModelDeniedError`'s
message gains the blocking layer for consistency with the non-admin Model Access screen's
plain-language requirement, without changing its `code`/`status_code`.

**AC3.2 defense-in-depth** (server-side reject of a team restriction that would re-enable
an org-banned model): `set_team_model_policy(session, team_id, models)` re-fetches the
**current** org baseline directly from the DB (not the cache — same "control-plane reads
through to DB" precedent as `get_policy()`), computes `unknown_or_org_denied = {m for m in
models if not org_snapshot.is_allowed(m)}`, and rejects (422,
`code="team_model_restricts_org_denied_model"`) with the offending model names if
non-empty — no DB write in that case, mirroring `set_policy`'s existing
`UnknownModelInPolicyError` shape exactly.

**Self-view endpoint** (`GET /v1/model-access`, non-admin Model Access screen, AC3.3):
iterates every `MODEL_REGISTRY` key, calls `resolve_model_access` for each against the
caller's resolved `team_id` (see §5 for how the endpoint picks *which* team context when
a user belongs to more than one — mirrors A1's exact resolution: auto-select if exactly
one active `TeamMembership`, otherwise require an explicit `?team_id=` query param),
returns `{model, allowed, blocking_layer}` per entry — the frontend renders the
plain-language sentence per `blocking_layer` (AC3.3's explicit "not a bare 'blocked'"
requirement), with the exact copy owned by frontend-developer, not this design.

---

## 5. API contract

Base path `/v1`. Every route not explicitly listed as "no auth" requires at least
`get_current_session`. `team_id` path params are consumed by `require_team_role(...)`,
which resolves org-admin bypass automatically — routes below are not duplicated into
separate `/admin/...` and `/team-lead/...` trees; one resource tree, per-route auth
dependency, matching the UI doc's own explicit "don't build two independent
implementations of the same [...] logic" instruction, applied consistently across the
whole Teams surface (not just join requests, where the UI doc says it outright).

### 5.1 Auth & session

| Method & path | Auth | Notes |
|---|---|---|
| `GET /v1/auth/sso/login` | none | 404 if SSO env vars unset |
| `GET /v1/auth/sso/callback` | none | sets session cookie, 302s per §2.1 step 5 |
| `POST /v1/auth/logout` | session | revokes session row, clears cookie |
| `GET /v1/auth/me` | session | `{user_id, name, email, org_role, teams: [{team_id, team_name, role}], onboarding_status: "resolved"\|"pending_profile"\|"pending_approval"}` |

### 5.2 Onboarding (§2.6)

| Method & path | Auth | Request | Response |
|---|---|---|---|
| `GET /v1/onboarding/teams` | session | — | `[{id, name}]` — name-only, no budget/member data (avoids leaking sensitive figures to a pre-onboarding user) |
| `POST /v1/onboarding/join-requests` | session | `{full_name, team_id}` | `201 JoinRequest`; `409 join_request_already_pending` if one exists (AC6.4) |
| `GET /v1/onboarding/status` | session | — | current/most-recent `JoinRequest` for the caller, including `routed_to`-derived copy for the holding screen (AC6.10) |

### 5.3 Team Lead / Org Admin join-request queues (§2.6)

| Method & path | Auth | Request | Response |
|---|---|---|---|
| `GET /v1/teams/{team_id}/join-requests?status=` | `require_team_role(team_lead)` | — | list, filterable by status |
| `POST /v1/teams/{team_id}/join-requests/{id}/approve` | `require_team_role(team_lead)` | `{budget_usd}` | `201 TeamMembership`; `422 budget_ceiling_exceeded` with live headroom (AC6.7) |
| `POST /v1/teams/{team_id}/join-requests/{id}/reject` | `require_team_role(team_lead)` | `{reason?}` | `200 JoinRequest` |
| `GET /v1/admin/join-requests/queue` | `require_role(org_admin)` | — | requests where the target team currently has zero `team_lead` memberships **or** the request has been pending ≥5 business days (A7's Mon–Fri, org-timezone rule) — computed live at query time, not solely from the stored `routed_to` snapshot (see §1.5's schema note for why) |

### 5.4 Teams

| Method & path | Auth | Notes |
|---|---|---|
| `POST /v1/teams` | `require_role(org_admin)` | `{name, budget_ceiling_usd?}` |
| `GET /v1/teams` | session | org_admin/auditor see all; team_lead/member see only their own teams |
| `GET /v1/teams/{team_id}` | session, org-admin/auditor unconditional, else must belong to the team | full detail incl. members, model restrictions, alert config |
| `PATCH /v1/teams/{team_id}` | `require_role(org_admin)` | `{name?, budget_ceiling_usd?}` — ceiling edits run the §3.3 locked check; `422 budget_ceiling_below_current_allocation` on a would-be-retroactive reduction (A3) |
| `PATCH /v1/teams/{team_id}/period-config` | `require_team_role(team_lead)` | `{period_type?, on_period_end?}` — per the phase doc's own "Org Admin **or** Team Lead configures period boundary" story |
| `DELETE /v1/teams/{team_id}` | `require_role(org_admin)` | `409 team_has_members` / `409 team_has_join_requests` if not empty |
| `GET /v1/teams/{team_id}/members` | `require_team_role(team_lead, member)` | |
| `POST /v1/teams/{team_id}/members` | `require_team_role(team_lead)` | `{user_id, role: "member"\|"team_lead", budget_usd}` — `role` is structurally restricted to these two literal values at the schema level (org_admin/auditor are never assignable here — see AC1.5 note below) |
| `PATCH /v1/teams/{team_id}/members/{user_id}` | `require_team_role(team_lead)` | `{role?, budget_usd?}` — budget edits run the §3.3 locked check |
| `DELETE /v1/teams/{team_id}/members/{user_id}` | `require_team_role(team_lead)` | `409 member_has_active_keys` if the member holds ≥1 active personal key or team-attributed service-account key scoped to this team (ADR-4) |
| `POST /v1/teams/{team_id}/reassign-budget` | `require_team_role(team_lead)` | `{from_user_id, to_user_id, amount_usd}`; one `AuditEntry` recording both old→new (AC2.4) |
| `GET /v1/teams/{team_id}/model-restrictions` | `require_team_role(team_lead, member)` | `{org_baseline: [...], team_restriction: [...] | null}` |
| `PUT /v1/teams/{team_id}/model-restrictions` | `require_team_role(team_lead)` | `{models: [...]}`; `422` per §4's AC3.2 defense-in-depth |
| `GET /v1/teams/{team_id}/alert-config` | `require_role(org_admin)` | see design note below |
| `PUT /v1/teams/{team_id}/alert-config` | `require_role(org_admin)` | `{threshold_80_enabled, threshold_100_enabled, webhook_enabled, webhook_url?, email_enabled}` |
| `GET /v1/teams/{team_id}/usage?range=` | `require_team_role(team_lead, member)` | Team Dashboard |

**AC1.5 note (schema-level, not just app-checked)**: `POST/PATCH .../members`'s `role`
field is typed `Literal["member", "team_lead"]` — `org_admin`/`auditor` are not
expressible values on this endpoint at all, so a Team Lead (whose access to this route
is already scoped to a team they administer, via `require_team_role`) attempting to
grant Org Admin/Auditor is rejected by request validation before any authorization logic
even runs. This mirrors Phase 1.3's ADR-2 "let the type system guarantee it" style
exactly. Org-wide roles are granted only via `PATCH /v1/admin/users/{id}/org-role`
(§5.5), which is `require_role(org_admin)`-only.

**Alert-config note**: scoped `require_role(org_admin)`, not `require_team_role(team_lead)`
— resolves an apparent tension between the phase doc's story-level phrasing ("As a Team
Lead/Org Admin, I receive a webhook... alert") and `ui-requirements-non-admin.md` §3's
Team Lead nav, which lists Join Requests / Team Dashboard / Members & Budgets / Model
Restrictions / Access Schedule / Budget Marketplace but **no** Alert Thresholds screen.
The nav (the more specific, concrete signal) is treated as authoritative over the general
story-level phrasing, which is about *receiving* alerts, not *configuring* them — a Team
Lead is still a notification recipient (their email/identity is looked up and included
whenever a team they lead crosses a threshold), they just don't get a config screen this
phase. Flagged explicitly since it's a genuine resolution of a spec/UI-doc conflict, not
a restatement of either.

### 5.5 Org RBAC

| Method & path | Auth | Request |
|---|---|---|
| `PATCH /v1/admin/users/{id}/org-role` | `require_role(org_admin)` | `{org_role: "org_admin"\|"auditor"\|null}` |
| `GET/PUT /v1/admin/org-settings` | `require_role(org_admin)` | ceiling, currency, personal-key settings (§1.1) |

### 5.6 Personal API keys (§2.5)

| Method & path | Auth | Request | Response |
|---|---|---|---|
| `GET /v1/keys` | session | — | caller's own keys |
| `POST /v1/keys` | session | `{name, team_id, expires_at?}` — `team_id` always required in the body; frontend auto-selects it per A1 but the server never infers it | `201` incl. plaintext `secret` once (AC5.2/5.3/5.5) |
| `POST /v1/keys/{id}/regenerate` | session, must own | — | `200` incl. new plaintext `secret` |
| `DELETE /v1/keys/{id}` | session, must own | — | `204` |
| `GET /v1/teams/{team_id}/members/{user_id}/keys` | `require_team_role(team_lead)` | — | delegated view (AC5.8) |
| `POST /v1/teams/{team_id}/members/{user_id}/keys` | `require_team_role(team_lead)` | `{name, expires_at?}` (`team_id` implied by path) | delegated create |
| `POST /v1/teams/{team_id}/members/{user_id}/keys/{id}/regenerate` | `require_team_role(team_lead)` | — | |
| `DELETE /v1/teams/{team_id}/members/{user_id}/keys/{id}` | `require_team_role(team_lead)` | — | |
| `GET /v1/admin/keys?type=app\|personal\|all` | `require_role(org_admin)` | — | org-wide oversight, extends the existing service-accounts admin listing with a `key_type`/`owner` discriminator |
| `POST /v1/admin/keys/{id}/regenerate` | `require_role(org_admin)` | — | stronger-confirm is a frontend-only concern; backend behavior identical to the delegated path |
| `DELETE /v1/admin/keys/{id}` | `require_role(org_admin)` | — | works on either key type |

Every mutation on this list writes an `AuditEntry` (AC5.10).

### 5.7 Model access (self-view)

| Method & path | Auth | Notes |
|---|---|---|
| `GET /v1/model-access?team_id=` | session | `team_id` optional if exactly one active `TeamMembership` (A1's pattern, reused); `400` requiring it explicitly if 2+ |

### 5.8 Audit & usage

| Method & path | Auth |
|---|---|
| `GET /v1/admin/audit-entries?action=&actor=&from=&to=&page=` | `require_role(org_admin, auditor)` |
| `GET /v1/me/usage?range=` | session |
| `GET /v1/admin/usage/summary?range=&team_id=` | `require_role(org_admin, auditor)` — extends the existing Phase 1 endpoint with team breakdown |

### 5.9 Identity & Access (read-only this phase — see ADR-8)

| Method & path | Auth | Notes |
|---|---|---|
| `GET /v1/admin/identity/sso-config` | `require_role(org_admin)` | env-derived; `client_secret` reported as `{configured: bool}`, never the value |
| `POST /v1/admin/identity/sso-config/test-connection` | `require_role(org_admin)` | live discovery-document fetch against the configured issuer; same three-structured-error-state pattern as provider-key validation |

---

## 6. Notifier interface design

```python
@dataclass(frozen=True)
class ThresholdAlertEvent:
    team_id: uuid.UUID
    team_name: str
    threshold_pct: Literal[80, 100]
    current_spend_usd: Decimal
    budget_ceiling_usd: Decimal
    currency: str
    recipients: list[NotifyRecipient]   # every team_lead of the team + every org_admin

class Notifier(Protocol):
    async def send(self, event: ThresholdAlertEvent) -> None: ...   # never raises - see dispatch note below

class WebhookNotifier(Notifier):
    """Generic JSON POST + a Slack-compatible payload variant (detected by
    URL shape or an explicit `webhook_format` team setting - default
    generic). Delivered via the shared httpx.AsyncClient already on
    app.state (same pooled-client precedent as provider calls) - no new
    connection-pool setup."""

class EmailNotifier(Notifier):
    """SMTP, config via GATEKEY_SMTP_HOST/PORT/USERNAME/PASSWORD/
    FROM_ADDRESS/USE_TLS env vars (Settings field_validator pattern - if any
    is set, host+from_address are required; unset entirely = email notifier
    is a no-op regardless of any team's email_alert_enabled toggle, logged
    once at startup as an informational note, never a hard failure).
    UNVERIFIED-LIVE (A8-equivalent): implemented and spec-compliant, no real
    SMTP credentials available in this build environment - QA must not mark
    this verified without a real mailbox test."""

class NotifierDispatcher:
    """Fans out a ThresholdAlertEvent to every enabled channel for the
    team (webhook_alert_enabled, email_alert_enabled). Each channel's
    failure is caught and logged independently - one channel failing never
    blocks or is masked by another's success/failure."""
```

**Dispatch timing**: the crossed-threshold check itself (§3.4) is cheap and synchronous
(arithmetic on values already in hand from the charge's own `RETURNING` clause), but
actual delivery (outbound HTTP to a webhook, SMTP handshake) is scheduled via FastAPI
`BackgroundTasks`, running **after** the gateway response has already been sent to the
caller. This is a design decision worth stating explicitly since nothing upstream
mandates it: a notifier failure or a slow webhook target must never add latency to, or
risk failing, the actual gateway request that triggered it — directly in the spirit of
this codebase's existing "never let a non-essential side effect threaten the primary
request" posture (e.g. `record_usage_charge`'s own failure handling in the streaming
path), and of the self-hosted/no-mandatory-phone-home ethos generally: an unreachable
webhook endpoint must degrade to "logged, not delivered," never to "the gateway request
itself failed."

Testing story: a mock HTTP receiver fixture for the webhook path (unit/integration,
asserting exact payload shape for both generic and Slack-compatible formats) plus one
live webhook target for the phase's own acceptance criterion (AC2.6) — QA-owned, not
this design's concern beyond making sure the interface is real-target-agnostic (it just
POSTs to whatever URL is configured).

---

## 7. Audit-entry write convention

Every mutation this phase introduces writes exactly one `AuditEntry`, in the **same DB
transaction** as the mutation itself (never a separate, best-effort write after commit —
an audit entry that silently failed to write would be worse than not having the feature).
A small shared helper centralizes this:

```python
async def write_audit_entry(
    session: AsyncSession, *, actor: AdminContext | SessionContext, action: str,
    target_type: str, target_id: str, old_value: dict | None, new_value: dict | None,
) -> None:
    """actor_label is derived once here (AdminContext.actor_label or
    SessionContext.display_label) so every call site doesn't re-derive the
    A4 sentinel/snapshot logic independently."""
```

Fixed action-type vocabulary (populates the Audit Log filter dropdown per UI doc §10.3's
"list every action type this doc defines... not a hardcoded few"):

`team.create`, `team.update`, `team.delete`, `team.period_config.update`,
`team.model_restrictions.update`, `team.alert_config.update`, `team.member.add`,
`team.member.update`, `team.member.remove`, `team.budget.reassign`, `join_request.submit`,
`join_request.approve`, `join_request.reject`, `user.org_role.update`,
`personal_key.create`, `personal_key.regenerate`, `personal_key.revoke`,
`service_account_key.create` *(existing Phase 1 action, now also logged — was previously
un-audited since Phase 1 had no audit trail at all)*, `service_account_key.revoke`,
`org_settings.update`.

---

## 8. Non-functional requirements — explicit accounting

- **AC1.7 (<10ms added RBAC/policy-resolution latency)**: `require_gateway_credential`
  adds no new DB round trips vs. today's `require_service_account` (same one indexed
  lookup, now against one of two tables by prefix). `check_model_policy`'s team-overlay
  check is a second in-process dict lookup (`TeamModelPolicyCache.get`), zero I/O — same
  order of magnitude as the existing org-baseline `frozenset` check. `check_budget_
  available`'s team-aware form is the *same* single query as Phase 1.4's, broadened by a
  join to pull `team_id`/period fields already needed for `ensure_current_period`'s cheap
  comparison — not an additional round trip. Net: the steady-state hot path adds
  in-process comparisons only, no new queries, consistent with hitting the NFR — actual
  verification is a load-test acceptance check (QA-owned, flagged so it isn't silently
  dropped as "the design says it's fine").
- **AC2.3 / phase NFR (atomic spend-check-and-deduct under concurrency)**: satisfied by
  §3.2's direct extension of `budget.py`'s existing single-statement pattern.
- **Success criteria (team allocation never exceeds ceiling under concurrent approvals)**:
  satisfied by §3.3's `SELECT ... FOR UPDATE` design (ADR-5) — this is the one place this
  phase's concurrency story is *not* a literal reuse of the Phase 1.4 pattern, and is
  called out as such.
- **AC1.3 (`docker-compose up` alone never starts Keycloak / stays under an hour)**:
  satisfied by the `--profile sso` gate (§9) and by SSO env vars being fully optional
  (§2.1) — a fresh clone with only `GATEKEY_ADMIN_TOKEN`/`GATEKEY_MASTER_KEY` set
  behaves exactly as it does today, break-glass-only, no SSO routes reachable.
- **No plaintext secrets at rest (cross-phase non-negotiable)**: extended in this phase
  to session tokens (hashed, §1.9), personal-key secrets (hashed, §1.6, same discipline
  as `ServiceAccountKey`), and team webhook URLs (AES-256-GCM envelope, §1.2 — flagged
  explicitly since a Slack webhook URL is bearer-equivalent and it would have been easy
  to treat it as "just a URL" and store it in plaintext).

---

## 9. Keycloak `docker-compose --profile sso` — design for devops-engineer

```yaml
  keycloak:
    image: quay.io/keycloak/keycloak:26.0   # pin an exact tag, not `latest`
    profiles: ["sso"]                        # NEVER starts on plain `docker-compose up`
    command: start-dev --import-realm
    environment:
      KEYCLOAK_ADMIN: admin
      KEYCLOAK_ADMIN_PASSWORD: admin          # dev-only; document as such in README, not a real secret
    volumes:
      - ./devops/keycloak/gatekey-realm.json:/opt/keycloak/data/import/gatekey-realm.json:ro
    ports:
      - "8080:8080"
```

**Realm export approach**: a checked-in `devops/keycloak/gatekey-realm.json` (Keycloak's
native realm-export JSON format), pre-seeded with:
- Realm `gatekey-dev`.
- Client `gatekey-backend` — **confidential** client (client authentication ON), not a
  public/SPA client — the browser never talks to Keycloak's token endpoint directly,
  only the backend does (server-side session model, §2.1), so a confidential client with
  a real `client_secret` is the correct type, not the public-client-plus-PKCE-only shape
  a browser-side SPA would need.
- Redirect URI: `http://localhost:8000/v1/auth/sso/callback` (matches the backend's own
  published host:port in the default compose networking, same convention as the
  existing `NEXT_PUBLIC_API_BASE_URL` browser-facing URL).
- At least one seeded test user with a fixed dev password, so an automated
  authorization-code-flow test can run end-to-end without manual Keycloak admin-console
  interaction.
- `.env.example` gains a new, commented-out "Optional: SSO (Phase 2, `--profile sso`)"
  section documenting `GATEKEY_OIDC_ISSUER_URL=http://localhost:8080/realms/gatekey-dev`,
  `GATEKEY_OIDC_CLIENT_ID=gatekey-backend`, `GATEKEY_OIDC_CLIENT_SECRET=<matches realm
  export>`, `GATEKEY_OIDC_REDIRECT_URI=http://localhost:8000/v1/auth/sso/callback` —
  mirroring the file's existing commented-out-optional-fields convention exactly.

**A8 restated**: this setup is sufficient for spec-compliant automated OIDC testing, not
"a real pilot IdP." Devops-engineer should still sanity-check that the same env-var
surface structurally maps onto a real Okta/Azure AD/Google Workspace tenant (issuer
discovery document shape, standard claims), but there is no live credential in this build
environment to actually exercise that end-to-end — same unverified-live treatment as the
email notifier (§6) and Phase 1's pricing-needs-live-verification gap.

---

## 10. Architectural forks — orchestrator sign-off requested

Collected here for visibility (each also appears inline above, at its point of use):

1. **ADR-8 — SSO configuration is env-var-only, not DB-editable via the admin console.**
   `ui-requirements-admin.md` §14 shows an editable Client ID/Secret/Issuer form with a
   "Save" button; the locked architecture decision #3 says OIDC config is "config-driven
   via env vars following the exact `.env.example` pattern." These conflict. Resolved in
   favor of the locked decision: `GET /v1/admin/identity/sso-config` is read-only
   (env-derived, masked secret), `POST .../test-connection` is a live discovery check,
   and there is no `PUT`. Frontend should render the Identity & Access screen this phase
   as "Configured via environment variables — edit `.env` and restart to change," not a
   working Save button. SCIM fields on that same screen are out of scope entirely (AC1.6)
   regardless.
2. **ADR-9 — cost "normalization" is an identity function this phase, not real FX
   conversion.** `PRICING_TABLE` (Phase 1.4) prices every model in USD only; there is no
   multi-currency provider pricing or live FX-rate source anywhere in this codebase.
   `org_settings.currency`, `usage_logs.raw_provider_cost_usd`, and `fx_rate_applied` are
   real, persisted columns (so AC2.7's auditability requirement has somewhere to render),
   but `fx_rate_applied` is always `1` and `raw_provider_cost_usd == cost_usd` always —
   the schema is forward-compatible with a real FX feature landing later without a
   rework, but building actual currency conversion now would be speculative capability
   ahead of any pilot org actually operating in a non-USD reporting currency, matching
   this codebase's existing "don't build ahead of a real design-partner need" precedent
   (Ollama/OpenRouter, the CLI-passthrough non-decision in §2.5).
3. **ADR-4 — removing a team member (or deleting a legacy `User`) is blocked, not
   auto-revoked, while active keys reference that team context.** Not addressed by the
   product spec at this level of detail; resolved by extending the existing `ServiceAcc
   ountKey`-blocks-user-deletion precedent (`phase-1.4-budget-basic-design.md` §1.4/
   ADR-2) to team-membership removal, rather than silently auto-revoking a user's keys
   as a side effect of an unrelated admin action.
4. **ADR-5 — assignment-time ceiling enforcement uses `SELECT ... FOR UPDATE` row
   locking**, a genuinely different concurrency mechanism from `services/budget.py`'s
   existing single-statement `UPDATE ... RETURNING` pattern (§3.3) — flagged because it's
   a meaningful new pattern being introduced into the codebase, not a copy of an existing
   one, even though it's the correct extension of the same underlying "atomic, not
   read-modify-write" philosophy.
5. **ADR-6 — rollover arithmetic** (unused budget compounds onto `budget_usd` itself;
   `current_spend_usd` always resets to 0 regardless of rollover/reset) — the product
   spec specifies the policy choice, not the mechanic; this design supplies one (§3.5).
6. **ADR-7 — a denormalized, transactionally-maintained `teams.current_spend_usd`
   aggregate**, to make threshold-alert detection free instead of requiring a `SUM()`
   query on the gateway hot path (§3.4) — a real schema decision with an ongoing
   consistency obligation (two, and only two, call sites must maintain it), not dictated
   by the spec.
7. **ADR-10 — lazy, touch-based period rollover instead of a scheduler/cron daemon**
   (§3.5) — the right call for this codebase's no-extra-infrastructure deployment story,
   but has a real, worth-acknowledging behavioral consequence (a dormant team's boundary
   doesn't visibly roll over until next touch).
8. **Alert-threshold configuration is Org-Admin-only, not Team-Lead-editable** — resolves
   a phase-doc-vs-UI-doc conflict (§5.4's design note) in favor of the more specific
   signal (the Team Lead nav in `ui-requirements-non-admin.md` §3, which omits this
   screen).

---

## 11. Task breakdown

Legend: `[P]` = can run in parallel with sibling `[P]` tasks; `[D: X]` = hard dependency
on task `X`. Given the phase's size, tasks are grouped by subsystem; within a subsystem,
apply the same legend.

### database-admin

- **DB-1** `[P]`: Migration(s) for `org_settings`, `teams`, `team_model_policies` (§1.1–
  1.3), plus the new enums shared across this phase (`user_org_role`, `team_role`,
  `team_period_type`, `team_period_end`, `join_request_status`,
  `join_request_routed_to` — can all be created in this same migration even though some
  are consumed by later tables, matching `0001`'s precedent of creating an enum ahead of
  the table that needs it).
- **DB-2** `[D: DB-1]`: Migration for `team_memberships`.
- **DB-3** `[D: DB-1]`: Migration for `users` additions (`org_role`, `sso_subject`,
  `sso_email`) and `sessions`. Independent of DB-2 — `[P]` relative to it.
- **DB-4** `[D: DB-1, DB-2]`: Migration for `join_requests` (references both `teams` and
  `users`).
- **DB-5** `[D: DB-1, DB-2]`: Migration for `personal_api_keys` and `service_account_
  keys.team_id`.
- **DB-6** `[D: DB-3]`: Migration for `audit_entries` (references `users`/`orgs` only).
  `[P]` relative to DB-4/DB-5.
- **DB-7** `[D: DB-2, DB-5]`: Migration for `usage_logs` additions (`team_id`,
  `personal_api_key_id`, `raw_provider_cost_usd`, `fx_rate_applied`).
- **DB-8** `[D: DB-1..DB-7]`: ORM models for every new/altered table (`db/models/team.py`,
  `team_membership.py`, `team_model_policy.py`, `join_request.py`, `personal_api_key.py`,
  `session.py`, `audit_entry.py`, `org_settings.py`, plus edits to `user.py`,
  `service_account_key.py`, `usage_log.py`), registered in `db/models/__init__.py`.

### backend-developer — auth/session subsystem

- **BD-1** `[D: DB-8]`: `services/sessions.py` — session creation/lookup/revocation,
  `try_get_session_context`/`get_current_session` (§2.2).
- **BD-2** `[D: DB-8]`: `config.py` — OIDC/SMTP/session-cookie env vars + `field_
  validator`s (§2.1, §6). `[P]` with BD-1.
- **BD-3** `[D: BD-2]`: `services/oidc.py` — discovery-document fetch/cache, authorization-
  URL construction, code exchange, ID-token validation.
- **BD-4** `[D: BD-1, BD-3]`: `api/v1/auth.py` — `/v1/auth/sso/login`, `/callback`,
  `/logout`, `/me` (§5.1).
- **BD-5** `[D: BD-1]`: `api/deps.py` — refactor `require_admin` (§2.3), add `require_role`/
  `require_team_role` (§2.4), `errors.py` — add `ForbiddenError`.
- **BD-6** `[D: DB-8]`: `errors.py`/`schemas/personal_api_key.py` — `gk_pk_` prefix
  constant, secret hashing (reuse `service_accounts.py`'s `hash_secret`).
- **BD-7** `[D: BD-5, BD-6]`: `api/deps.py` — `GatewayCallerContext`, `require_gateway_
  credential` (§2.5); wire into `chat.py`/`completions.py`/`embeddings.py` replacing
  `require_service_account`.

### backend-developer — budget subsystem

- **BD-8** `[D: DB-8]`: `services/budget.py` extensions — `TeamMembershipBudgetState`,
  `check_budget_available`/`record_team_membership_usage_charge` (§3.1–3.2).
- **BD-9** `[D: DB-8]`: `services/team_budget.py` (new) — `SELECT ... FOR UPDATE`-based
  assignment-time enforcement helpers (§3.3): membership create/update, org/team ceiling
  edits, budget reassignment. `[P]` with BD-8.
- **BD-10** `[D: BD-9]`: `services/team_periods.py` (new) — `ensure_current_period`,
  rollover/reset arithmetic (§3.5, ADR-6/10).
- **BD-11** `[D: BD-8, BD-10]`: wire `check_budget_available`/`record_team_membership_
  usage_charge` (with `ensure_current_period` called first) into the three gateway
  routes via `GatewayCallerContext.team_id`.

### backend-developer — model policy subsystem

- **BD-12** `[D: DB-8]`: `services/model_policy.py` extensions — `TeamModelPolicyCache`,
  `resolve_model_access`, `set_team_model_policy` w/ AC3.2 validation (§4). `[P]` with
  BD-8/BD-9.
- **BD-13** `[D: BD-12, BD-7]`: wire `resolve_model_access` into `check_model_policy`'s
  gateway call sites.

### backend-developer — teams, RBAC, onboarding, keys, audit, notifiers (mostly `[P]` with each other once BD-5/BD-7/BD-9 land)

- **BD-14** `[D: BD-9]`: `services/teams.py` + `api/v1/teams.py` — Team CRUD, members,
  reassign-budget, model-restrictions, alert-config, usage routes (§5.4).
- **BD-15** `[D: BD-9]`: `services/join_requests.py` + `api/v1/onboarding.py` +
  join-request routes on `api/v1/teams.py` + `api/v1/admin/join_requests.py` (§5.2, 5.3) —
  includes the 5-business-day computation (A7).
- **BD-16** `[D: BD-6, BD-9]`: `services/personal_keys.py` + `api/v1/keys.py` (self-serve
  + delegated + admin routes, §5.6).
- **BD-17** `[D: DB-8]`: `services/audit.py` — `write_audit_entry` helper (§7); threaded
  into BD-14/15/16's write paths as they land (each of those tasks includes its own
  audit-write call sites, not a separate follow-up task).
- **BD-18** `[D: BD-14]`: `services/notifiers.py` — `Notifier`/`WebhookNotifier`/
  `EmailNotifier`/`NotifierDispatcher` (§6); wired into BD-11's charge path for
  threshold-crossing dispatch via `BackgroundTasks`.
- **BD-19** `[D: BD-5]`: `api/v1/admin/org_settings.py`, `api/v1/admin/identity.py`
  (§5.5, 5.9). `[P]` with BD-14/15/16.
- **BD-20** `[D: BD-12]`: `api/v1/model_access.py` (§5.7). `[P]` with the above.
- **BD-21** `[D: BD-14..BD-20]`: integration/unit tests — concurrency tests for §3.3
  (N concurrent approvals against headroom for <N) and §3.2 (N concurrent charges), the
  AC3.2 org-denied-model rejection, the AC6.4 partial-unique-index 409 mapping, break-glass
  vs. org_admin-session `require_admin` regression coverage (AC1.4).

### frontend-developer

Can start against the API contract (§5) as soon as it's published — every task below is
`[P]` with the corresponding backend task once route shapes are locked, since the
contract (not the backend implementation) is the shared interface.

- **FE-1** `[P]`: SSO login screen + callback handling (no username/password field,
  cookie-based — `frontend/src/lib/api.ts` gains `credentials: "include"` on every
  request instead of the current bearer-token header pattern for session-authenticated
  routes; admin-token bearer-header auth stays for the existing Phase 1 admin flows).
- **FE-2** `[P]`: Profile+team-selection and holding-state onboarding screens
  (`ui-requirements-non-admin.md` §2.1–2.2).
- **FE-3** `[P]`: `ConsoleShell` role-based nav (Member/Team Lead/Auditor/Org Admin
  variants per `ui-requirements-non-admin.md` §3) — extends the existing shell rather
  than forking it.
- **FE-4** `[P]`: Teams & Users admin screen (`ui-requirements-admin.md` §8) — team
  list/detail, members table, reassign-budget flow, model restrictions, alert config.
- **FE-5** `[P]`: Team Lead "My Team" screens (`ui-requirements-non-admin.md` §7.1–7.5,
  minus §7.6 Budget Marketplace which is Phase 6) — reuses FE-4's components scoped to
  one team, per the UI doc's explicit reuse instruction.
- **FE-6** `[P]`: My API Keys (self-serve) + delegated key management on the Members
  table (`ui-requirements-non-admin.md` §6, minus §6.1 CLI Auto-Sync which is Phase 3.7a).
- **FE-7** `[P]`: Model Access screen (non-admin) + Model Policy team-restriction UI
  extension (admin) — both consume the same `blocking_layer` shape from §4/§5.7.
- **FE-8** `[P]`: Audit Log screen (`ui-requirements-admin.md` §10.3, Phase 2 slice —
  no hash-chain UI).
- **FE-9** `[P]`: Auditor read-only screens (`ui-requirements-non-admin.md` §8.1–8.3,
  minus §8.4 which is Phase 5).
- **FE-10** `[D: FE-1]`: Identity & Access screen — read-only display per ADR-8, not a
  working Save form.
- **FE-11** `[D: FE-4, FE-6, FE-7, FE-8]`: end-to-end smoke pass once backend routes are
  live — not a parallelizable task, sequenced last.

### Parallelization summary

`DB-1` gates almost everything and should start immediately. `DB-2`/`DB-3` fan out in
parallel once `DB-1` lands; `DB-4`–`DB-7` layer on top per their listed dependencies.
Once `DB-8` (ORM models) lands, the backend auth (`BD-1`–`BD-7`), budget (`BD-8`–`BD-11`),
and model-policy (`BD-12`–`BD-13`) subsystems can all proceed in parallel — they touch
disjoint files except for the shared `GatewayCallerContext` type (`BD-7`), which should
land before `BD-11`/`BD-13` wire their route-handler call sites, but nothing blocks
`BD-8`/`BD-9`/`BD-10`/`BD-12` from being built concurrently with `BD-1`–`BD-6`. `BD-14`
through `BD-20` are all `[P]` with each other once their respective single upstream
dependency (`BD-9`, `BD-6`, `BD-5`, or `BD-12`) lands — this is the widest parallelization
window in the whole phase. Frontend work (`FE-1`–`FE-9`) is entirely parallel with backend
work and with itself, gated only on the API contract in §5 being stable (already true as
of this document) — frontend does not need to wait for backend implementation to be
merged, only for the contract to not change out from under it.

---

## 12. Forward-looking rework flags

- **Phase 3 (DLP/residency/hash-chained audit)**: `ModelAccessDecision.blocking_layer`
  is typed to accept a third value without reshaping the type (§4) — Phase 3's
  content-classification layer should be addable inside `resolve_model_access` without
  touching gateway route handlers again, mirroring how this phase itself extended Phase
  1.3's single call site. `audit_entries` (§1.10) is built for Phase 5's `chain_hash`/
  `prev_hash` columns to be an additive migration. Phase 3's holiday calendar should
  extend, not replace, A7's Mon–Fri business-day computation (§1.5/§5.3).
- **Phase 4 (caching, multi-worker scale)**: `TeamModelPolicyCache` and `ModelPolicyCache`
  share the exact same "in-process singleton, no cross-worker convergence" limitation
  already documented in `phase-1.3-model-governance.md` §2.4/ADR-4 — should be revisited
  together, once, alongside whatever shared-state mechanism Phase 4 introduces for its
  own rate-limiter requirement, not solved three times independently.
- **Phase 5/6 (budget marketplace, forecasting)**: the `teams.current_spend_usd`
  denormalized aggregate (ADR-7) is exactly the figure Phase 6's Budget Marketplace and
  Forecasting tabs will want to read — no rework anticipated there, flagging only so a
  future designer doesn't reintroduce a duplicate aggregate.
- **Real FX conversion** (ADR-9): if a pilot org actually needs non-USD reporting, this
  requires (a) a real FX-rate source (live feed or admin-entered static rates) and (b)
  either multi-currency provider pricing or a conversion step applied to the existing
  USD-computed `cost_usd` — the schema (`fx_rate_applied`, `raw_provider_cost_usd`) is
  already shaped for the latter, cheaper option; this is a real, scoped follow-up, not
  speculative infrastructure to build now.
