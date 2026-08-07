---
title: Phase 4 — Reliability & Cost Efficiency — Architecture Design
status: accepted
author: architect
last_updated: 2026-08-05
---

# Phase 4 — Reliability & Cost Efficiency — Design

Scope: multi-key-per-provider with same-provider automatic failover (org/key default +
narrowing-only team override), passively-derived key health status, per-user + per-team
rate limiting (two independent axes) with reject-or-queue behavior, exact-match response
caching with policy-generation-stamped keys, graceful cost degradation with a single
configured downgrade target and response-header signaling, and the dashboard/admin
surfacing for all four new metrics. Introduces this codebase's first optional
non-Postgres infrastructure (Redis, behind `--profile cache`) with an in-process fallback
that is the correct, accurate default for the actual shipped single-instance topology.

Source of truth for scope/ACs/ratified ambiguities:
`backend/docs/design/phase-4-reliability-cost-efficiency-product-spec.md` (§0–§8) plus
the orchestrator's explicit ratification of the 10 flagged ambiguities in the handoff
brief. This document designs against those ratified decisions, not around them. Builds
directly on Phase 3's cumulative-every-enabled-layer-checked precedence (the corrected
`check_access_schedule`/residency pattern — see `api/v1/gateway/common.py`'s
`check_access_schedule` docstring, which documents the security-review fix that replaced
an earlier, buggy innermost-only model), Phase 3's `asyncio` scheduler (`services/
scheduler.py`), and the existing gateway pipeline (`api/v1/gateway/common.py`).

Migration numbering: Phase 3's last migration is `0022_create_cli_refresh_credentials.py`;
Phase 4 starts at `0023`. Migration ownership follows Phase 2/3's convention exactly —
this section specifies column/constraint/index/FK shape, database-admin owns the actual
Alembic revision files.

---

## 1. Schema design

### 1.1 New enums (`create_type=False`, DDL owned by migrations, existing convention)

| Enum | Values |
|---|---|
| `rate_limit_scope_type` | `org_default_per_user`, `team` |
| `rate_limit_on_limit` | `reject`, `queue_retry` |
| `rate_limit_rejection_outcome` | `reject`, `queue_timeout` |
| `degradation_scope_type` | `org`, `team` |

### 1.2 `provider_keys` — multi-key columns (alter existing table, migration `0023`)

```
ALTER TABLE provider_keys
  ADD COLUMN label            text NOT NULL DEFAULT '__pending__',  -- backfilled below, DEFAULT dropped after
  ADD COLUMN is_primary        boolean NOT NULL DEFAULT false,
  ADD COLUMN failover_enabled  boolean NOT NULL DEFAULT false,
  ADD COLUMN failover_target_id uuid NULL REFERENCES provider_keys(id) ON DELETE SET NULL;

-- Backfill for every pre-existing row (at most one per (org, provider) today):
UPDATE provider_keys SET label = 'Default', is_primary = true WHERE label = '__pending__';
ALTER TABLE provider_keys ALTER COLUMN label DROP DEFAULT;

ALTER TABLE provider_keys DROP CONSTRAINT uq_provider_keys_org_id_provider;
ALTER TABLE provider_keys ADD CONSTRAINT uq_provider_keys_org_id_provider_label
  UNIQUE (org_id, provider, label);

CREATE UNIQUE INDEX uq_provider_keys_one_primary_per_provider
  ON provider_keys (org_id, provider) WHERE is_primary;

CREATE INDEX ix_provider_keys_failover_target_id ON provider_keys (failover_target_id);
```

Per product spec §0.2 (locked): the AES-256-GCM envelope columns and the Phase 3
rotation-overlap columns (`previous_ciphertext`/etc.) are untouched — this is purely an
additive/relaxation change to the existing table, not a redesign. `label` is required
going forward (AC1.1); every pre-existing single-key row is backfilled to
`label='Default'`, `is_primary=true` in the same migration, so no org's existing
configuration silently breaks.

**`is_primary` — an architectural decision the spec doesn't make explicitly, flagged in
§10 fork #1.** Neither the phase doc nor the buildable spec's ACs describe how Gatekey
picks which of N keys for one provider serves *fresh* (non-retry) traffic once more than
one exists — AC1.6 commits scope to "same-provider, multi-key **failover** only," not
load-balancing/traffic-spreading (despite the original phase doc's "traffic can spread
across keys" framing, which none of the ratified ACs actually build). This design
resolves that gap with the simplest mechanic consistent with the ratified scope: exactly
one key per `(org, provider)` is flagged `is_primary` (DB-enforced via the partial unique
index above) and used for all normal routing; `failover_enabled`/`failover_target_id` are
only meaningfully read off the **primary** key at request time (see §3.2). The first key
ever added for a provider becomes primary automatically (byte-for-byte today's
one-key-per-provider behavior for every org that never adds a second key — zero
behavior change for the common case). Additional keys are non-primary by default,
reachable only as a configured failover target or via an explicit "set as primary" admin
action (§9.1).

### 1.3 `team_failover_overrides` (new, migration `0024`)

```
team_failover_overrides
  team_id            uuid PRIMARY KEY REFERENCES teams(id) ON DELETE CASCADE
  failover_disabled  boolean NOT NULL DEFAULT false
  created_at, updated_at
```

Per ratified #1: the org/key-level `failover_enabled` (§1.2) is the org default; this
table is the team-scoped, **narrowing-only** override. The column can only ever *disable*
failover for a team — there is structurally no "enable" value, so unlike
`residency_rules`/`set_team_model_policy`'s write-time subset-check pattern, no
write-time narrowing validation is needed here at all: the schema itself makes widening
impossible to express (§3.2's `resolve_failover_opt_in` reads this cumulatively —
`key.failover_enabled AND NOT (team_override.failover_disabled)` — matching the
cumulative-every-enabled-layer-checked precedent this design was directed to reuse, not
an innermost-only read).

### 1.4 `failover_events` (new, migration `0025`)

```
failover_events
  id                     uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id                 uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  from_provider_key_id   uuid NULL REFERENCES provider_keys(id) ON DELETE SET NULL
  to_provider_key_id     uuid NULL REFERENCES provider_keys(id) ON DELETE SET NULL
  request_id             text NOT NULL
  detected_at            timestamptz NOT NULL   -- when the primary's failing call returned
  switched_at            timestamptz NOT NULL   -- when the backup call succeeded
  created_at             timestamptz NOT NULL DEFAULT now()

  INDEX ix_failover_events_org_id_created_at (org_id, created_at)
```

`ON DELETE SET NULL` (not `CASCADE`) on both key references — a failover event is history
that must survive a later key deletion (same "never lose history" posture as
`audit_entries.target_id`). `detected_at`/`switched_at` are stored as two timestamps, not
a precomputed `duration_ms` column — the admin API (§9.1) computes the
detection-to-switch duration at read time, avoiding a derived value that could drift from
its source columns (same "compute, don't store, when it's cheap to compute" instinct
`services/budget.py`'s `compute_cost` already follows for pricing).

### 1.5 `rate_limit_rules` + `rate_limit_rejection_events` (new, migration `0026`)

```
rate_limit_rules
  id                       uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id                   uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  scope_type               rate_limit_scope_type NOT NULL
  scope_team_id            uuid NULL REFERENCES teams(id) ON DELETE CASCADE
  requests_per_min         integer NULL
  tokens_per_min           integer NULL
  on_limit                 rate_limit_on_limit NOT NULL DEFAULT 'reject'
  max_queue_wait_seconds   integer NOT NULL DEFAULT 30   -- ratified #8's default, admin-configurable
  created_at, updated_at

  UNIQUE INDEX uq_rate_limit_rules_org_default ON rate_limit_rules (org_id) WHERE scope_type = 'org_default_per_user'
  UNIQUE INDEX uq_rate_limit_rules_team_scoped ON rate_limit_rules (scope_team_id) WHERE scope_team_id IS NOT NULL
  CHECK (
    (scope_type = 'org_default_per_user' AND scope_team_id IS NULL) OR
    (scope_type = 'team' AND scope_team_id IS NOT NULL)
  )
```

Same one-row-per-scope partial-unique-index pattern as `residency_rules`/
`rotation_policies`/`access_schedules` — a direct reuse, the fourth application of this
exact pattern in this codebase, not a new one. This is a **Postgres config table**, not
the hot-path counter store — the actual per-minute counters live in the shared-state
store (§4.1), never in this table.

```
rate_limit_rejection_events
  id             uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id         uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  rule_id        uuid NULL REFERENCES rate_limit_rules(id) ON DELETE SET NULL
  scope_type     rate_limit_scope_type NOT NULL
  scope_team_id  uuid NULL
  user_id        uuid NULL
  outcome        rate_limit_rejection_outcome NOT NULL
  occurred_at    timestamptz NOT NULL DEFAULT now()

  INDEX ix_rate_limit_rejection_events_org_id_occurred_at (org_id, occurred_at)
  INDEX ix_rate_limit_rejection_events_rule_id_occurred_at (rule_id, occurred_at)
```

Feeds AC2.9/AC5.2's per-rule rejection-count column (§7).

### 1.6 `caching_settings` + `teams.cache_opt_out` + `cache_lookup_events` (new, migration `0027`)

```
caching_settings
  org_id       uuid PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE
  enabled      boolean NOT NULL DEFAULT true    -- org-wide-on default (AC3.5)
  ttl_seconds  integer NOT NULL DEFAULT 3600    -- no number given by either source doc; 1h chosen and documented here
  created_at, updated_at
```

Mirrors `compliance_settings`/`dlp_policies`'s "absence of row = default state" ADR
exactly — an org that never touches this config gets caching on with a 1-hour TTL, not an
inert feature.

```
ALTER TABLE teams ADD COLUMN cache_opt_out boolean NOT NULL DEFAULT false;
```

Same per-team-toggle-column style as `alert_threshold_80_enabled`/`webhook_alert_enabled`
— matches the product spec's own touchpoints note (§7) verbatim.

```
cache_lookup_events
  id                 uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id             uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  team_id            uuid NULL
  hit                boolean NOT NULL
  provider           text NOT NULL
  model              text NOT NULL   -- native_model_id actually looked up (post-resolve_route)
  prompt_tokens      integer NULL    -- populated on a hit only, copied from the cache entry
  completion_tokens  integer NULL
  occurred_at        timestamptz NOT NULL DEFAULT now()

  INDEX ix_cache_lookup_events_org_id_occurred_at (org_id, occurred_at)
```

No prompt/response content stored here at all (less sensitive than `dlp_scan_results`,
which at least stores redacted findings) — purely a hit/miss/token-count event log for
dashboard aggregation (§7).

### 1.7 `degradation_policies` (new, migration `0028`)

```
degradation_policies
  id                       uuid PRIMARY KEY DEFAULT (app-side uuid4)
  org_id                   uuid NOT NULL REFERENCES orgs(id) ON DELETE CASCADE
  scope_type               degradation_scope_type NOT NULL
  scope_team_id            uuid NULL REFERENCES teams(id) ON DELETE CASCADE
  enabled                  boolean NOT NULL DEFAULT false
  threshold_pct_of_budget  numeric(5,2) NOT NULL DEFAULT 10.0
  downgrade_target_model   text NOT NULL   -- validated as a MODEL_REGISTRY key at write time, mirroring ModelPolicy.models

  UNIQUE INDEX uq_degradation_policies_org ON degradation_policies (org_id) WHERE scope_type = 'org'
  UNIQUE INDEX uq_degradation_policies_team ON degradation_policies (scope_team_id) WHERE scope_team_id IS NOT NULL
  CHECK (
    (scope_type = 'org' AND scope_team_id IS NULL) OR
    (scope_type = 'team' AND scope_team_id IS NOT NULL)
  )
```

Fifth application of the same one-row-per-scope partial-unique pattern. Resolution
semantics are covered in §6.2 — this is a deliberate, explicit exception to a literal
read of "always check every layer cumulatively": see §6.2 for why the `enabled` flag
specifically (not the threshold/target fields) is what gets cumulative treatment here.

### 1.8 `usage_logs.original_model` (alter existing table, migration `0029`)

```
ALTER TABLE usage_logs ADD COLUMN original_model text NULL;
```

`NULL` on every non-degraded request (the overwhelming majority); populated with the
originally-requested model only when `check_degradation` substituted a different one
(AC4.7). `model` (existing column) always holds the model actually used/charged. This is
also what §7's "cost saved via degradation" aggregation queries against — no new table
needed for degradation history, this single nullable column is sufficient.

### 1.9 ORM models

Every table/column above gets a corresponding SQLAlchemy model (or column addition),
registered in `db/models/__init__.py`, following the exact conventions already
established (`create_type=False` enums owned by the migration, `Mapped`/`mapped_column`
shapes matching column types 1:1).

---

## 2. Non-functional requirements — explicit accounting

- **<2s deployment-wide failover-switch NFR (AC1.9)**: satisfied by (a) health state
  living in the shared-state store (§4.1), readable by every worker, not per-process
  cache, and (b) the proactive-skip mechanic in §3.3 — once a key trips to Down, the
  *next* request routes straight to the backup without even attempting the known-dead
  primary, rather than waiting for a live retry-then-fail cycle. Under the default
  in-process store and this project's actual single-instance-per-container topology
  (§4.1), "every worker" is trivially satisfied — there is exactly one process observing
  and one process reading. Under `--profile cache` (Redis), the same property holds
  across a genuinely multi-worker/horizontally-scaled deployment, which is the scenario
  the NFR is actually written for.
- **Rate-limit cross-worker accuracy (AC2.8, hard constraint)**: satisfied by construction
  — §4.1's shared-state interface is the only place rate-limit counters live; there is no
  in-process-`Counter` code path that could be reached by accident. The in-process
  *implementation* of that same interface is accurate for the shipped single-instance
  topology (documented limitation identical in kind to Phase 1–3's own in-process-cache
  caveats) and inaccurate under an operator-added horizontally-scaled deployment absent
  `--profile cache` — this is the explicit, ratified trade-off (§4.1), not a silent gap.
- **Cache-miss overhead ≤ ~10ms (AC3.9)**: the cache lookup itself is a single
  shared-state `get_json` call (an in-process dict read, or one Redis round trip over a
  loopback/compose-network hop) plus a SHA-256 hash over already-in-memory request data —
  no additional DB round trip. §5.6 calls out the one load-test acceptance check this
  still needs (Redis network latency under real load), matching Phase 3's DLP-NFR
  treatment (design accounts for it; a load test confirms it, not a proof).
- **Rate-limit queue bound (AC2.6/ratified #8)**: `max_queue_wait_seconds` (default 30,
  §1.5) is enforced as a hard ceiling on the poll loop (§4.3) — never an unbounded await;
  this is a correctness property of the poll loop's own termination condition, not a
  configuration operators could accidentally disable.
- **Self-hosted/no-mandatory-phone-home (cross-phase non-negotiable)**: Redis is
  optional, profile-gated (`--profile cache`, §9.3), never started by plain
  `docker-compose up`, and — like Keycloak — is entirely operator-initiated
  infrastructure, not a Gatekey-initiated outbound dependency. The in-process fallback
  keeps every Phase 4 feature (failover, rate limiting, caching, degradation)
  functionally complete on the default topology with zero new services.
- **Passive health checks spend no provider budget (AC1.3, ratified #5)**: satisfied by
  construction — health state is derived exclusively from the outcome of calls the
  gateway was already making for real user traffic (§3.1); no new outbound call is ever
  made for the sole purpose of a health probe.

---

## 3. Multi-key & failover design

### 3.1 Health-state derivation (ratified #5)

**Storage**: one small JSON blob per `provider_key_id`, in the shared-state store (§4.1),
under key `health:{provider_key_id}`. Deliberately **not** a database table — health
state is fully re-derivable from live traffic within a 60-second window, so losing it on
a process restart is harmless (a restart is, functionally, as good as an immediate
recovery) and this avoids putting a write on every single provider call's hot path
against Postgres.

```python
@dataclass
class KeyHealthState:
    consecutive_failures: int
    window_started_at: float       # time.monotonic(), not wall-clock (immune to clock adjustment)
    status: Literal["healthy", "degraded", "down"]
    last_error_summary: str | None  # e.g. ProviderUpstreamError.message - already safe to log (errors.py)
```

**State machine** (ratified #5's concrete defaults, admin-configurable — see
`GET/PUT /v1/admin/reliability-settings`, §9.1):

- A **success** immediately resets `consecutive_failures = 0`, `status = "healthy"`,
  clears `last_error_summary` — "1 success immediately recovers it," applied literally.
- A **failure**: if `now - window_started_at > 60s`, treat the window as expired
  (`consecutive_failures = 1`, `window_started_at = now`); else
  `consecutive_failures += 1`. Then: `1-2` consecutive failures within the window →
  `status = "degraded"` (admin-visible only, does **not** trigger proactive rerouting —
  see §3.3); `≥3` → `status = "down"` (trips failover eligibility).

This is recorded by the failover-aware credential/provider-call wrapper (§3.3) after
every outbound provider call, success or failure — not a separate polling job.

### 3.2 Failover opt-in resolution — cumulative, narrowing-only (ratified #1)

```python
def resolve_failover_opt_in(
    primary_key: ProviderKeyRow, *, team_id: uuid.UUID | None, team_override_cache: TeamFailoverOverrideCache,
) -> bool:
    """Both layers checked every time - not an innermost-only shortcut. See
    api/v1/gateway/common.py's check_access_schedule docstring for why this
    codebase now requires every enabled layer be checked at read time, not
    validated-narrower-at-write-then-innermost-only-at-read (the pattern a
    security review already found buggy for access schedules)."""
    if not primary_key.failover_enabled:
        return False
    if team_id is not None:
        override = team_override_cache.get(team_id)
        if override is not None and override.failover_disabled:
            return False
    return True
```

`TeamFailoverOverrideCache` is a new, small process-local cache with the identical
lock-free, GIL-atomic, full-replace-snapshot contract as `TeamModelPolicyCache` (§4.1
covers why this one specific cache — unlike rate limits/health/cache-entries — stays
in-process rather than moving to the shared-state store: it is genuinely process-startup
config, not per-request mutable state, exactly the same category as
`ResidencyRuleCache`/`AccessScheduleCache`, which Phase 4 does not retrofit — see §12).

### 3.3 Selection algorithm

```python
async def select_provider_key(
    session: AsyncSession, provider: str, *, team_id: uuid.UUID | None,
    health_store: SharedStateStore, team_override_cache: TeamFailoverOverrideCache,
) -> tuple[ProviderKeyRow, bool]:  # (selected key, failover_applies)
    """Proactive half of failover (AC1.9): if the primary is already known
    Down and failover applies, route straight to the backup - never attempt
    a call known to be doomed. Called once, before fetch_credential's
    decrypt step."""
    primary = await provider_keys_service.get_primary_key(session, provider)
    if primary is None:
        raise ProviderNotConfiguredError(provider)
    failover_applies = resolve_failover_opt_in(primary, team_id=team_id, team_override_cache=team_override_cache)
    if failover_applies and primary.failover_target_id is not None:
        state = await health_store.get_json(f"health:{primary.id}")
        if state is not None and state["status"] == "down":
            backup = await provider_keys_service.get_key_by_id(session, primary.failover_target_id)
            if backup is not None:
                return backup, failover_applies
    return primary, failover_applies
```

The **reactive** half (AC1.4/1.7/1.8 — a single in-flight request's own call fails before
the health state has even tripped to Down) lives in the route handler's provider-call
wrapper, not `select_provider_key`:

1. Call the provider with the selected key's decrypted credential.
2. On success: record success into `health_store` for that key; done.
3. On `ProviderCallError`/timeout, **and** this was the primary (not already a retry
   against the backup), **and** `failover_applies`, **and** `failover_target_id` is set:
   record the failure into `health_store` for the primary; fetch + decrypt the backup
   credential; retry the call **exactly once** (AC1.7 — never a loop over every key for
   the provider).
   - Backup succeeds: record success for the backup; write a `failover_events` row
     (`detected_at` = the primary's failure timestamp, `switched_at` = now); return the
     backup's response, identical OpenAI-compatible shape, no trace of the primary's
     failure surfaced to the caller (AC1.8).
   - Backup also fails: record the failure for the backup too; **re-raise the primary's
     original error**, unchanged (AC1.7) — never the backup's error, never a generic
     failure.
4. On failure with no failover applicable: record the failure, raise as today
   (`ProviderUpstreamError`), unchanged Phase 1–3 behavior.

### 3.4 Backward compatibility

An org that never adds a second key for any provider sees byte-for-byte today's
behavior: one `is_primary=true` row, `failover_enabled=false` by default (AC1.5's
off-by-default), no `failover_target_id`, `select_provider_key` always returns that same
row. Zero migration risk for the common case.

---

## 4. Rate limiting design

### 4.1 Shared-state interface (ratified #2)

One interface, one pair of concrete implementations, three consumers (rate-limit
counters, key health state §3.1, cache entries §5.4) — a single mechanism, deliberately
not solved independently per feature (the exact gap Phase 2's design doc §12 and Phase
3's design doc §12 both flagged as needing to happen "once, not three times").

```python
class SharedStateStore(Protocol):
    async def try_consume(self, key: str, *, window_seconds: int, limit: int) -> tuple[bool, int]:
        """Atomically: if the current window's count < limit, increments and
        returns (True, new_count); else leaves the counter untouched and
        returns (False, current_count). The one primitive that implements
        BOTH pre-emptive reject (AC2.3) and queue-and-poll (AC2.6) - queueing
        is just calling this again without ever inflating a rejected
        attempt's count."""

    async def incr_by(self, key: str, *, window_seconds: int, amount: int) -> int:
        """Unconditional atomic add - used for AC2.4's retrospective
        tokens/min accounting (added post-response, never gates the request
        that generated the tokens)."""

    async def get_int(self, key: str) -> int:
        """Current window count, 0 if absent/expired - the retrospective
        tokens/min gate reads this without incrementing (AC2.4: never
        pre-charge/estimate)."""

    async def get_json(self, key: str) -> Any | None: ...
    async def set_json(self, key: str, value: Any, *, ttl_seconds: int | None) -> None: ...
```

**In-process implementation** (default, no configuration needed): a process-local dict,
every method body a single dict read+write with no `await` between them — the identical
"CPython GIL makes this atomic, no lock needed" discipline `ModelPolicyCache`/
`DeviceAuthStore` already rely on. Rate-limit keys are naturally bounded (one entry per
distinct user/team × axis × recent window — grows with org size, not with request
volume), so no LRU/eviction is needed for this consumer; health-state keys are bounded by
the number of configured provider keys. **Accurate for this project's actual shipped
topology** — `docker-compose.yml`'s `backend` service is a single container/process (see
Phase 3's design doc §11's "confirm the scheduler loop's behavior under docker-compose's
default single-replica deployment" — the same topology fact applies here), so "in-process"
and "cross-worker-consistent" are the same thing today, not a compromise.

**Redis implementation** (`--profile cache`, §9.3): `try_consume`/`incr_by` are a single
Lua script each (atomic `GET`+conditional `INCR`+`EXPIRE`-if-new-key — the standard
Redis rate-limiting idiom), `get_int` a plain `GET`, `get_json`/`set_json` a plain
`GET`/`SETEX`. This is what actually satisfies AC2.8's hard cross-worker-accuracy
constraint under a real horizontally-scaled deployment.

Both implementations are exercised by the same self-check (`test_shared_state_store.py`
— see task BD-8) so a future third backend (unlikely to be needed, not built) would have
a contract test to satisfy.

### 4.2 Window bucketing

Fixed 60-second windows, keyed `rl:{axis}:{scope_id}:{floor(now/60)}` — not a sliding
window or token bucket. This is a deliberate, documented simplification (`ponytail:`-class
trade-off): a fixed window can admit up to ~2x the configured limit right at a window
boundary, but the AC text is itself phrased as "requests **per minute**," which a fixed
per-minute window satisfies literally and is the simplest primitive that is atomic under
both backends with zero extra bookkeeping. Upgrade path if the boundary-burst behavior
ever proves a real problem: a sliding-window-log (timestamps-per-key) or leaky-bucket
implementation behind the exact same `SharedStateStore` interface — no call site changes
needed.

### 4.3 Two independent axes, both cumulative (ratified #4)

```python
async def check_rate_limit(
    *, store: SharedStateStore, rules: RateLimitRuleCache,
    org_id: uuid.UUID, team_id: uuid.UUID | None, user_id: uuid.UUID,
) -> None:
    checks: list[tuple[RateLimitRuleSnapshot, str]] = [(rules.get_org_default(), f"user:{user_id}")]
    if team_id is not None and (team_rule := rules.get_team(team_id)) is not None:
        checks.append((team_rule, f"team:{team_id}"))

    tripped: list[RateLimitRuleSnapshot] = []
    for rule, scope_key in checks:
        if rule.requests_per_min is not None:
            ok, _ = await store.try_consume(f"rl:req:{scope_key}", window_seconds=60, limit=rule.requests_per_min)
            if not ok:
                tripped.append(rule)
        if rule.tokens_per_min is not None:
            current = await store.get_int(f"rl:tok:{scope_key}")
            if current >= rule.tokens_per_min:
                tripped.append(rule)

    if not tripped:
        return

    # Reject wins over queue_retry when rules disagree (§4.3 note below).
    if any(r.on_limit == "reject" for r in tripped):
        await _record_rejection(tripped, outcome="reject")
        raise RateLimitExceededError(retry_after_seconds=60)

    max_wait = max(r.max_queue_wait_seconds for r in tripped)
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        await asyncio.sleep(1)
        if not await _any_still_tripped(store, checks):
            return
    await _record_rejection(tripped, outcome="queue_timeout")
    raise RateLimitExceededError(retry_after_seconds=max_wait)
```

Both the individual-user check and the team-aggregate check run unconditionally, every
request (AC2.2's two-independent-axes reading, ratified) — never short-circuited once
one passes. "Most-restrictive-wins" (AC2.2's own phrase) is applied twice: once to
*whether* a limit trips (either axis tripping is enough), and — a small, undictated but
low-stakes mechanic this design supplies — once more to *how* a simultaneous trip on both
axes with disagreeing `on_limit` settings behaves: if either tripped rule says `reject`,
the request rejects even if the other configured `queue_retry` (the more conservative
outcome wins), never the reverse.

Request-count enforcement (`requests_per_min`) is pre-emptive via `try_consume`
(AC2.3); token-count enforcement (`tokens_per_min`) is a pure read (`get_int`, never
incremented here) — the actual increment happens post-response (§4.4), matching AC2.4's
explicit "never estimate/pre-charge" requirement.

`RateLimitExceededError` — new `GatekeyError` subclass, 429, `code="rate_limit_exceeded"`,
`Retry-After` header set from the computed wait hint (AC2.5).

### 4.4 Post-response token accounting

`record_usage_charge`'s existing call sites (§8, pipeline integration) gain one
additional line after a successful charge: `await store.incr_by(f"rl:tok:{scope_key}", window_seconds=60, amount=prompt_tokens + completion_tokens)`
for both the individual-user key and the team key (if applicable) — this is the only
place token counts are added to the rate-limit window, satisfying AC2.4 by construction
(a request that never reaches a successful provider response never adds tokens to
anyone's window).

### 4.5 Pipeline placement

`check_rate_limit` runs immediately after `check_content_classification` and before
`check_cache` — per product spec §0.4 (locked, not re-derived here): a request already
denied by policy shouldn't consume a rate-limit slot, matching the existing DLP-scan
placement precedent. See §5.3 for why a cache **hit** still consumes a rate-limit slot
despite AC3.6's prose reading (at a skim) as if it wouldn't — rate limiting protects the
gateway's own request-handling capacity, not provider spend, so it is not one of the
checks a free cache hit is meant to bypass.

### 4.6 Rejection events

`_record_rejection` writes a `rate_limit_rejection_events` row **synchronously**, before
raising — mirroring Phase 3's established residency/DLP/schedule-block convention
exactly (a raised exception has no live response for `BackgroundTasks` to run after, so
deferring is not an option on this path — direct reuse of that precedent, not a new one).

---

## 5. Caching design

### 5.1 Cache key composition (AC3.1/3.2, fully specified by the product spec — implemented, not re-derived)

```python
def build_cache_key(
    *, org_id: uuid.UUID, team_id: uuid.UUID | None, route: ModelRoute,
    redacted_texts: list[str], params: dict[str, Any], policy_generation: int,
) -> str:
    canonical = json.dumps(
        {
            "org_id": str(org_id),
            "team_id": str(team_id) if team_id is not None else None,
            "provider": route.provider,
            "native_model_id": route.native_model_id,
            "texts": redacted_texts,          # POST-DLP-redaction, per AC3.1
            "params": {k: params[k] for k in sorted(params)},  # every response-affecting param
            "policy_generation": policy_generation,
        },
        sort_keys=True, separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"cache:{org_id}:{digest}"
```

`team_id` in the key structurally prevents a cross-team hit (AC3.2); `policy_generation`
structurally prevents a hit surviving a policy config change (§5.2). `params` covers
every field that affects the response shape/content (`temperature`, `max_tokens`,
`top_p`, etc. — the full set an org's supported request schema exposes), never just
`model`+`messages`.

### 5.2 Policy-generation stamp (ratified #9a)

A single, org-wide, in-process monotonic counter (`app.state.policy_generation`), same
GIL-atomic "read/increment an int, no lock" pattern as `ModelPolicyCache._generation`.
Bumped by every write path that currently refreshes `ModelPolicyCache`,
`TeamModelPolicyCache`, `ResidencyRuleCache`, or `ContentAwareRuleCache`, **plus** the DLP
config write paths that today have no cache to refresh at all (`dlp_policies`,
`dlp_custom_patterns`, `team_dlp_action_overrides` — loaded fresh every request per
`run_dlp_scan`'s existing docstring, so this is a genuinely new integration point, not a
place that already calls `.set()` on something). A single shared helper,
`bump_policy_generation(app)`, is called from every one of these admin write handlers
(§11, task BD-9) — one function, one counter, not a per-feature ad hoc version number.

Not persisted to Postgres. Consequence, stated explicitly rather than left implicit: a
backend restart resets the counter to 0 while any Redis-backed cache entries survive the
restart (their TTL is independent of the backend process) — post-restart requests compute
keys with `policy_generation=0`, which will not match any pre-restart entry
(`policy_generation` was ≥1 before restart in any org that has ever made a config
change), so every pre-restart entry becomes permanently unreachable after a restart. This
**fails safe** (a restart can only ever cause extra, harmless cache misses — it can never
cause a stale-policy hit) at the cost of a full effective cache flush on every backend
restart. Given caching is a pure optimization (never load-bearing for correctness) and
restarts are infrequent relative to a typical TTL, this is an accepted, documented
trade-off — the alternative (a Postgres-persisted generation counter) would add a
synchronous DB write to every policy-mutation admin call for a benefit (warm cache across
restarts) this design doesn't consider worth the complexity.

### 5.3 Why a cache hit still passes every requester-side gate — the crux design point

`check_cache` is the eighth step in the pipeline (§8), reached only after
`check_access_schedule`, `check_model_policy`, `check_residency`, `run_dlp_scan`,
`check_content_classification`, and `check_rate_limit` have **all already evaluated
against the requesting credential's own resolved team/org/model state for this specific
request**. A cache hit therefore never needs its own redundant policy check: the gates
already ran, upstream of this step, against the actual requester — not the original
cacher.

What must still be true is that the cached response was legitimately reachable *for a
request identical to this one*, and that is exactly what `build_cache_key` guarantees:

- **Team boundary**: the key embeds `team_id` verbatim, so a hit is only possible when the
  requester's own `team_id` matches the original cacher's exactly — a different team's
  request computes a different key by construction, never an accidental cross-team hit.
- **Policy-change-over-time boundary**: the key embeds `policy_generation`, so any
  DLP/residency/model-policy/content-classification mutation since the entry was written
  makes it permanently unreachable — never served under a since-tightened or
  since-changed policy.
- **Content-classification determinism**: `check_content_classification`'s outcome is a
  pure function of `(model, pii_detected)`, and `pii_detected` is itself a pure function
  of the identical post-redaction text already embedded in the key — so two requests that
  produce the same cache key are, by construction, guaranteed to have received the same
  content-classification outcome, without needing to separately verify it at hit time.

Put together: a cache hit is provably no more permissive than a fresh miss-then-provider-
call would have been for the identical `(org, team, model, post-redaction text, params,
policy_generation)` tuple, because that exact tuple is what the key *is*. This is what
makes "a cached response was produced under a specific policy state" safe without an
active invalidation sweep for the per-request case (§3.3 of the product spec) — only the
config-change-over-time case needed the generation stamp at all.

Budget (ratified #7 — a hit is not spend, so it is deliberately not gated on budget) is
the one exception, by explicit product decision, not an oversight; degradation (§6) is
irrelevant to a hit for the same reason (nothing to downgrade when nothing is called).

### 5.4 Storage backend

Same `SharedStateStore` split as rate limiting (§4.1) — `set_json`/`get_json` with a TTL.

**In-process**: an `OrderedDict` (stdlib, zero new dependency), LRU via
`move_to_end`-on-access + `popitem(last=False)`-eviction when over a configurable max
entry count (default 10,000 per process — genuinely unbounded traffic could otherwise
grow this dict indefinitely, unlike the naturally-bounded rate-limit/health-state key
spaces in §4.1). Lazy TTL check on `get`: an expired entry is treated as absent and
evicted on access, no background sweep needed.

**Redis**: native `SETEX key ttl_seconds value` / `GET key` — Redis's own memory-eviction
policy (`maxmemory-policy`, standard Redis operator configuration, documented in the
README's Redis section, §9.3) is what bounds memory under sustained load; Gatekey does
not need to implement its own eviction against this backend.

### 5.5 Cache entry value

```json
{"response_body": {...}, "prompt_tokens": 123, "completion_tokens": 45}
```

`response_body` is the full OpenAI-compatible non-streaming response shape. **Streaming
requests**: a cache hit for a request with `stream=true` synthesizes a single-chunk SSE
stream from the stored `response_body` (one `data: {...}` frame carrying the full
content, then `data: [DONE]`) rather than storing/replaying raw SSE frames from the
original miss — this is a small, undictated mechanic this design supplies (the spec
dictates "transparent regardless of implementation," not the mechanic), analogous in kind
to Phase 3's "every fetch rotates the key" mechanic-level call (fork #3 there) — flagged
here as a documented, low-risk mechanic, not escalated to §10 (it has no security/policy
consequence, only a client-visible "one big chunk instead of incremental token-by-token"
behavior on a cache hit, which is inherent to caching a complete response at all).

### 5.6 Pipeline placement and write

`check_cache` runs after `check_rate_limit`, before `check_budget_available` (§8, per
product spec §0.4, locked). On a **miss**, the computed cache key and
`policy_generation` are carried forward on the request-scoped pipeline context (no
recomputation needed later) — except for the **write**, which deliberately recomputes the
key using the actually-used `route` (post-`check_degradation`, §6), not the original
lookup's route (AC3.10): a degraded response caches under the downgrade target's own
model, never under the originally-requested model's namespace, with zero special-casing
— `build_cache_key` is simply called twice per request (once for the lookup, once for
the write, potentially with a different `route`), never coupled to "was this
degraded."

`cache_store` runs after `record_usage_charge`, on a miss only, via `store.set_json` with
the org/team's configured TTL. A hit-or-miss `cache_lookup_events` row (§1.6) is written
via `BackgroundTasks` (deferred, after the response is already on the wire) for both
outcomes — the same async-recording mechanism Phase 3's log-only DLP path already
established (`_deliver_async_dlp_scan`), reused directly, not reinvented — so recording
never adds to the synchronous critical path on either a hit or a miss (AC3.9's ~10ms
budget applies to the *lookup*, not to this deferred bookkeeping write).

Load-test acceptance check (mirrors Phase 3's DLP <50ms treatment): confirm cache-miss
overhead stays ≤~10ms under both backends, including one Redis round trip over the
compose network — flagged as an explicit task (BD-14).

---

## 6. Graceful cost degradation design

### 6.1 Threshold and re-validation mechanics (AC4.1–AC4.8, fully specified — implemented here)

Reuses `check_budget_available()`'s already-fetched `UserBudgetState`/
`TeamMembershipBudgetState` (`current_spend_usd`/`budget_usd`) — zero second query
(AC4.1). Condition: `current_spend_usd >= budget_usd * (1 - threshold_pct/100) AND
current_spend_usd < budget_usd` (AC4.2) — evaluated only once `check_budget_available`
has already succeeded (a request already over budget never reaches this step at all).

If triggered: re-resolve `downgrade_target_model` via `resolve_route()` (existing,
zero-I/O), then re-run `check_model_policy` (existing, zero-I/O, org+team layers) against
it — **this is the ratified #9b live re-validation**, reusing the already-built function
verbatim rather than inventing a parallel check. If the downgrade target is denied
(misconfigured, or since-restricted), degradation is **skipped** and the original
request proceeds under its already-resolved route (AC4.4) — never a hard-fail because the
cheaper fallback became invalid. Per the ratified resolution, residency/DLP are **not**
re-run against the substitute model this phase (explicitly scoped, not silently omitted —
AC4.4's model-policy skip-if-denied is the stated safety net, and the ratified brief names
only "the CURRENTLY-resolved model policy," not residency/DLP, as what must be
re-validated).

### 6.2 Org/team scope resolution — cumulative on the enabled flag only

Unlike a restriction-type policy (DLP/residency/rate-limits/failover-opt-in), a
degradation policy is a single *substitution* decision — there is no well-defined "more
restrictive" ordering between two different `(threshold_pct, downgrade_target_model)`
pairs to check cumulatively the way two boolean gates can be ANDed. This design applies
the cumulative-check requirement precisely where the analogous staleness risk actually
lives — the on/off switch — and nowhere else:

```python
def resolve_degradation_policy(
    *, org_policy: DegradationPolicySnapshot | None, team_policy: DegradationPolicySnapshot | None,
) -> DegradationPolicySnapshot | None:
    """A team's policy only ever applies if BOTH the team's own `enabled`
    flag AND the org's `enabled` flag are true - an org disabling
    degradation always propagates down immediately, closing the same class
    of staleness risk the access-schedule security-review fix closed (a
    team's stale 'still enabled' row can never silently outlive an org's
    later kill-switch). threshold_pct/downgrade_target_model themselves are
    NOT merged/compared across layers - whichever layer is effectively
    enabled (team, if both are on; else org) supplies its own values
    wholesale, since there is no meaningful 'narrower' ordering between two
    distinct (threshold, model) configurations to check cumulatively."""
    if team_policy is not None and team_policy.enabled and (org_policy is None or org_policy.enabled):
        return team_policy
    if org_policy is not None and org_policy.enabled:
        return org_policy
    return None
```

This is a deliberate, explicit design call (not left to inference) — flagged in prose
here rather than escalated to §10, since it is narrowly scoped, has a stated security
rationale directly parallel to an already-accepted precedent, and the actual security
boundary (AC4.4's live model-policy re-validation) is independent of which layer
triggered the substitution in the first place.

### 6.3 Response header contract (AC4.5, exact)

```
X-Gatekey-Degraded: true
X-Gatekey-Degraded-Model: <downgrade_target_model>
```

Both set on a degraded response; **both entirely absent** (never `X-Gatekey-Degraded:
false`) on a non-degraded one. For streaming (AC4.8): both headers are set on the
`StreamingResponse` object's `headers=` argument before the generator begins — HTTP
headers are already sent before a streaming body under this stack's existing
Starlette/FastAPI usage, so this needs no special-casing beyond passing the two headers
through at construction time.

### 6.4 Usage-log/cost-saved accounting (AC4.6/4.7)

`record_usage_charge` charges the actual model used (the downgrade target), unchanged —
`usage_logs.model` is what was really used/priced; `usage_logs.original_model` (§1.8) is
set to the originally-requested model only on a degraded request. "Cost saved via
degradation" (§7) is computed at aggregation-query time as
`pricing.compute_cost(original_model, tokens) - cost_usd`, never stored redundantly —
same "compute, don't store" instinct as `failover_events`' duration (§1.4).

---

## 7. Dashboard/metrics design (ratified #6)

### 7.1 What's tracked, and where

| Metric | Source | Aggregation |
|---|---|---|
| Cache hit rate % | `cache_lookup_events` | `COUNT(*) FILTER (WHERE hit) / COUNT(*)`, time-range-scoped |
| Failover event count | `failover_events` | `COUNT(*)`, time-range-scoped |
| Cost saved (cache) | `cache_lookup_events` (hits only) | `SUM(pricing.compute_cost(model, prompt_tokens, completion_tokens))` per hit row, in-process pricing lookup, zero I/O |
| Cost saved (degradation) | `usage_logs` where `original_model IS NOT NULL` | `SUM(pricing.compute_cost(original_model, prompt_tokens, completion_tokens) - cost_usd)` |
| Cost saved (combined) | both of the above | summed, per AC5.4's single-figure framing |
| Rate-limit rejection count | `rate_limit_rejection_events` | `COUNT(*)` grouped by `rule_id`, time-range-scoped |

All four respect the existing Dashboard time-range selector (`since`/`until`, identical
shape to `usage.py`'s existing `range`/`start`/`end` query params, AC5.3) and the
existing empty-state convention (AC5.1): `cache_hit_rate`/`cost_saved_usd`/
`failover_count` are `None` (not `0`) when the underlying feature has never been enabled
for the org (no rows exist at all for that org in the relevant table), distinguishing
"never turned on" from "turned on, currently zero."

### 7.2 Retention

`cache_lookup_events`, `rate_limit_rejection_events`, and `failover_events` are all
folded into Phase 3's existing `run_log_prompt_purge_if_due` job (`services/
scheduler.py`) as three additional tables purged against
`compliance_settings.log_prompt_retention_days` — the same batched
`_purge_rows_older_than` helper Phase 3 already built for `usage_logs`/
`dlp_scan_results`, extended with three more call sites. These are operational event
logs, not audit-grade records with their own retention semantics — reusing the existing
job avoids inventing a fourth purge mechanism for what is, mechanically, the same
"bounded-growth event log" problem Phase 3 already solved twice.

### 7.3 Admin API shape

Per AC5.2's own recommendation (adopted as-is, not re-derived): the four metrics are
**not** a single new endpoint — `cache_hit_rate`/`failover_count`/`cost_saved_usd` extend
the existing `GET /v1/admin/usage/summary` response (already time-range-scoped,
already team-filterable, already the Dashboard's one data source — extending it avoids
the frontend making a second round trip), while `rate_limit_rejection_count` is a new
field on each row of the Rate Limits admin list response (§9.2), since it's inherently
per-rule, not a single org-wide number, matching the existing Rate Limits tab table shape.

---

## 8. Pipeline integration

Exact ordering (product spec §0.4, locked — this section states where each new step's
*implementation* lives, not a re-derivation of the order itself):

```
check_access_schedule          (unchanged, Phase 3)
  -> resolve_route              (unchanged, Phase 1)
  -> check_model_policy         (unchanged, Phase 1/2)
  -> check_residency            (unchanged, Phase 3)
  -> run_dlp_scan                (unchanged, Phase 3)
  -> check_content_classification (unchanged, Phase 3)
  -> check_rate_limit            (NEW, §4) — after policy denial, before spend; a request
                                    already going to be denied shouldn't consume a slot
  -> check_cache                 (NEW, §5) — hit short-circuits everything below; still
                                    consumes the rate-limit slot already taken above (§4.5)
  -> check_budget_available      (unchanged, Phase 1/2) — skipped entirely on a cache hit
  -> check_degradation           (NEW, §6) — only reached once budget confirms the caller
                                    is under budget; may substitute the route
  -> select_provider_key          (NEW, §3.3) — failover-aware key selection, proactive
                                    skip-if-Down
  -> fetch_credential             (extended, §3 — now keyed by provider_key_id, not just
                                    provider)
  -> provider call                 — wrapped with the reactive one-retry failover mechanic
                                    (§3.3) and health-state recording (§3.1)
  -> record_usage_charge          (unchanged Phase 1/2, extended: §4.4's post-hoc
                                    tokens/min accounting is one new call at this point)
  -> cache_store                  (NEW, §5, miss only)
```

On a cache hit, the route handler returns immediately after `check_cache` — none of
`check_budget_available`, `check_degradation`, `select_provider_key`, `fetch_credential`,
the provider call, `record_usage_charge`, or `cache_store` execute. Every gateway route
handler (`api/v1/gateway/chat.py`/`completions.py`/`embeddings.py`) is updated to the new
call order in one coordinated change (mirroring how Phase 3's BD-6 concentrated its own
pipeline rewiring into a single task to avoid two changes racing each other — see §11).

---

## 9. API contract

Base path `/v1` unless noted. Every route requires at least `require_admin`/
`require_team_role`, matching Phase 2/3's convention.

### 9.1 Multi-key & failover

| Method & path | Auth | Notes |
|---|---|---|
| `PUT /v1/admin/providers/{provider}/key` | `require_admin` | extended (breaking, admin-API-only — see note below): body now requires `label`; upserts by `(provider, label)`, not just `provider` |
| `GET /v1/admin/providers/{provider}` | `require_admin` | unchanged — returns the primary key only (backward-compatible single-object shape) |
| `GET /v1/admin/providers/{provider}/keys` | `require_admin` | NEW — full list of keys for the provider |
| `DELETE /v1/admin/providers/{provider}/keys/{key_id}` | `require_admin` | NEW, replaces provider-scoped `DELETE` for multi-key orgs |
| `POST /v1/admin/providers/{provider}/keys/{key_id}/set-primary` | `require_admin` | NEW — promotes a key; DB partial-unique index enforces exactly one primary |
| `PATCH /v1/admin/providers/{provider}/keys/{key_id}/failover` | `require_admin` | NEW — `failover_enabled`, `failover_target_id` (must be same provider, app-validated) |
| `GET/PUT /v1/teams/{team_id}/failover-override` | `require_team_role(team_lead)` | NEW — `failover_disabled` only (narrowing-only by construction, §1.3) |
| `GET /v1/admin/providers/health` | `require_admin` | NEW — live Healthy/Degraded/Down + `last_error_summary` per key, reads the shared-state store directly |
| `GET /v1/admin/failover-events` | `require_admin` | NEW — timeline, time-range-scoped, `duration_ms` computed at read time |
| `GET/PUT /v1/admin/reliability-settings` | `require_admin` | NEW — health-status thresholds (consecutive-failure count, window seconds), admin-configurable per ratified #5 |

**Admin-API breaking-change note**: unlike the gateway's OpenAI-compatible surface
(protected by the cross-phase non-negotiable), these are admin-console-only endpoints —
Phase 2/3 precedent already shows admin contracts evolving freely across phases (new
required fields, new routes replacing old ones). `label` becoming required on the
existing `PUT .../key` endpoint needs documentation (README/CHANGELOG), not special
justification.

### 9.2 Rate limiting

| Method & path | Auth | Notes |
|---|---|---|
| `GET/PUT /v1/admin/rate-limit-rules` | `require_admin` | org-default-per-user rule; list response includes each rule's `rejection_count` over the current dashboard time range |
| `GET/PUT /v1/teams/{team_id}/rate-limit-rule` | `require_team_role(team_lead)` | team-aggregate rule |

### 9.3 Caching

| Method & path | Auth | Notes |
|---|---|---|
| `GET/PUT /v1/admin/caching-settings` | `require_admin` | `enabled`, `ttl_seconds` |
| `GET/PUT /v1/teams/{team_id}/cache-opt-out` | `require_team_role(team_lead)` | single boolean |

### 9.4 Degradation

| Method & path | Auth | Notes |
|---|---|---|
| `GET/PUT /v1/admin/degradation-policy` | `require_admin` | org scope |
| `GET/PUT /v1/teams/{team_id}/degradation-policy` | `require_team_role(team_lead)` | team scope; `downgrade_target_model` validated against `MODEL_REGISTRY` at write time |

### 9.5 Dashboard

| Method & path | Auth | Notes |
|---|---|---|
| `GET /v1/admin/usage/summary` | `require_admin_or_auditor` | extended (additive) — `cache_hit_rate`, `failover_count`, `cost_saved_usd` fields added; existing fields unchanged |

### 9.6 Infrastructure (devops-facing, no HTTP route)

`GATEKEY_REDIS_URL` — new optional backend environment variable, unset by default (=
in-process store), pass-through style identical to the existing `GATEKEY_OIDC_*`/
`GATEKEY_SMTP_*` optional-integration variables (§9.3 of `docker-compose.yml` design).

---

## 10. Architectural forks — orchestrator sign-off requested

1. **`is_primary` — which key serves fresh (non-failover) traffic when a provider has
   more than one key configured (§1.2).** Neither source doc's ratified ACs build
   load-balancing/traffic-spreading across keys (AC1.6 explicitly scopes to
   "same-provider, multi-key **failover** only"), but the original phase doc's own
   framing ("traffic can spread across keys") could be read as expecting more than a
   pure primary+backup relationship. This design resolves the gap with the simplest
   mechanic consistent with the ratified, narrower scope — exactly one flagged primary
   key per provider, DB-enforced, auto-assigned to the first key added — but this is a
   genuine behavioral choice a future admin-side "traffic spreading" feature would need
   to revisit, not an obvious consequence of the ACs. Flagging for the same class of
   sign-off Phase 3's fork #3 (`GET /v1/me/current-key` rotates every call) received —
   spec dictates behavior (failover only), this design supplies the missing mechanic
   (which key is "the" key otherwise).
2. **Degradation's org/team resolution is cumulative on the `enabled` flag only, not on
   `threshold_pct`/`downgrade_target_model` (§6.2).** The task brief's instruction to
   apply the cumulative-every-enabled-layer pattern to "any new nested org/team policy"
   is followed for the security-relevant part (an org kill-switch always propagates down
   immediately) but deliberately *not* extended to merging/comparing the two layers'
   threshold or target-model values, since there is no well-defined "narrower" ordering
   between two distinct model substitutions to check cumulatively the way day/hour
   schedules or model allowlists have. Flagging since this is a partial, not full,
   application of an explicit instruction, and the reasoning for the split is worth
   explicit confirmation rather than silent inference.
3. **Streaming cache hits synthesize a single-chunk SSE response rather than replaying
   original incremental frames (§5.5).** A small, low-risk mechanic (no policy/security
   consequence — only a client-visible "one big chunk vs. token-by-token" difference on
   a cache hit), flagged for consistency with how this codebase has previously treated
   mechanic-level gaps (Phase 3 fork #3) rather than because it's genuinely contentious.

---

## 11. Task breakdown

Legend: `[P]` = can run in parallel with sibling `[P]` tasks; `[D: X]` = hard dependency
on task `X`.

### database-admin

- **DB-1** `[P]`: Migration `0023` — `provider_keys` multi-key columns
  (`label`/`is_primary`/`failover_enabled`/`failover_target_id`), backfill, relaxed
  unique constraint, partial-unique primary index.
- **DB-2** `[P]`: Migration `0024` — `team_failover_overrides`.
- **DB-3** `[D: DB-1]`: Migration `0025` — `failover_events` (FKs `provider_keys.id`).
- **DB-4** `[P]`: Migration `0026` — `rate_limit_rules`, `rate_limit_rejection_events`,
  enums `rate_limit_scope_type`/`rate_limit_on_limit`/`rate_limit_rejection_outcome`.
- **DB-5** `[P]`: Migration `0027` — `caching_settings`, `teams.cache_opt_out`,
  `cache_lookup_events`.
- **DB-6** `[P]`: Migration `0028` — `degradation_policies`, enum
  `degradation_scope_type`.
- **DB-7** `[P]`: Migration `0029` — `usage_logs.original_model`.
- **DB-8** `[D: DB-1..DB-7]`: ORM models for every new/altered table, registered in
  `db/models/__init__.py`.

### backend-developer — shared-state store (gates every other track's shared-state usage)

- **BD-1** `[P]`: `services/shared_state.py` — `SharedStateStore` protocol,
  `InProcessSharedStateStore` (§4.1), self-check (`test_shared_state_store.py`,
  exercises both implementations against the same contract). No DB dependency, can start
  immediately.
- **BD-2** `[D: BD-1]`: `RedisSharedStateStore` (`redis.asyncio` client, Lua-scripted
  `try_consume`/`incr_by`), wired to `app.state.shared_state_store` in `main.py`'s
  lifespan (selects Redis when `GATEKEY_REDIS_URL` is set, else in-process — same
  fail-open-to-simpler-default discipline as every other optional-integration check in
  this codebase).

### backend-developer — multi-key & failover

- **BD-3** `[D: DB-8]`: `services/provider_keys.py` extension — `get_primary_key`,
  `get_key_by_id`, `set_primary`, `set_failover_config` (app-level same-provider
  validation for `failover_target_id`), admin route updates (§9.1).
- **BD-4** `[D: DB-8, BD-1]`: `services/provider_key_health.py` — `KeyHealthState`
  state machine (§3.1), `TeamFailoverOverrideCache` (process-local, warmed at startup
  same fail-open discipline as `ResidencyRuleCache`), `resolve_failover_opt_in` (§3.2).
- **BD-5** `[D: BD-3, BD-4]`: `select_provider_key` (§3.3) + the reactive one-retry
  wrapper around the provider call in `api/v1/gateway/*.py`, `failover_events` write,
  `errors.py` unchanged (reuses existing `ProviderUpstreamError`).
- **BD-6** `[D: BD-4]`: `GET /v1/admin/providers/health`, `GET
  /v1/admin/failover-events`, `GET/PUT /v1/admin/reliability-settings` (§9.1).
- **BD-7** `[D: BD-5]`: game-day/chaos test for AC1.9 (<2s deployment-wide switch,
  measured under both the in-process default and `--profile cache`).

### backend-developer — rate limiting

- **BD-8** `[D: BD-1, DB-8]`: `services/rate_limit.py` — `RateLimitRuleCache`,
  `check_rate_limit` (§4.3), `_record_rejection`, `RateLimitExceededError` in
  `errors.py`. `[P]` with the failover track (BD-3..BD-7) once BD-1/DB-8 land.
- **BD-9** `[D: BD-8]`: post-response token accounting hook in `record_usage_charge`
  call sites (§4.4); admin routes (§9.2).

### backend-developer — caching

- **BD-10** `[D: BD-1, DB-8]`: `services/policy_generation.py` —
  `bump_policy_generation`, wired into every DLP/residency/model-policy/content-aware
  admin write handler (§5.2) — touches `api/v1/admin/dlp_policy.py`,
  `api/v1/admin/residency_rules.py`, `api/v1/admin/model_policy.py` (org + team
  routes), `api/v1/admin/content_aware_rules.py`. `[D: BD-1]` only (no dependency on
  the failover/rate-limit tracks).
- **BD-11** `[D: BD-10]`: `services/response_cache.py` — `build_cache_key` (§5.1),
  `check_cache`/`cache_store` (§5.6), `CachingSettingsCache` (process-local, same
  pattern as `caching_settings`'s absence-of-row default), streaming-hit SSE synthesis
  (§5.5).
- **BD-12** `[D: BD-11]`: `cache_lookup_events` `BackgroundTasks` write path (§5.6);
  admin routes (§9.3).
- **BD-13** `[D: BD-8, BD-11]`: pipeline rewiring in `api/v1/gateway/common.py` and
  every route handler to the new §8 order — concentrated into one task, same reasoning
  Phase 3's BD-6 used (two subsystems touching the same pipeline file must not race
  each other).
- **BD-14** `[D: BD-13]`: load-test acceptance check for AC3.9 (~10ms cache-miss
  overhead, both backends).

### backend-developer — degradation

- **BD-15** `[D: DB-8]`: `services/degradation.py` — `resolve_degradation_policy`
  (§6.2), `check_degradation` (§6.1, reuses `check_model_policy`/`resolve_route`
  verbatim). `[D: BD-13]` for its actual pipeline insertion point, but its own logic has
  no dependency on the caching/rate-limit tracks — buildable in parallel, wired in by
  BD-13.
- **BD-16** `[D: BD-15]`: `X-Gatekey-Degraded`/`X-Gatekey-Degraded-Model` header
  wiring in every route handler (streaming + non-streaming); admin routes (§9.4).

### backend-developer — dashboard/metrics

- **BD-17** `[D: DB-8]`: `services/usage_logs.py` extension — cost-saved aggregation
  queries (§7.1) against `cache_lookup_events`/`usage_logs.original_model`;
  `api/v1/admin/usage.py` response extension (§9.5). `[D: DB-8]` only; can start once
  the schema lands, independent of the feature tracks whose events it aggregates
  (nothing to aggregate yet in dev, but the query/response shape doesn't need real data
  to build against).
- **BD-18** `[D: BD-6, BD-9]`: `run_log_prompt_purge_if_due` extension — three new
  `_purge_rows_older_than` call sites (§7.2).

### frontend-developer

Can start against §9's contract as soon as it's stable — `[P]` with corresponding
backend tasks.

- **FE-1** `[P]`: Providers screen — multi-key add/list/remove, primary-promotion,
  per-key failover toggle + backup-key picker (same-provider only), Health-dot component
  per key — `ui-requirements-admin.md` §6.
- **FE-2** `[P]`: Failover & Health tab — live status grid, timeline view — §11.
- **FE-3** `[P]`: Rate Limits tab — org-default-per-user row, per-team rows,
  reject/queue toggle, rejection-count column — §11.
- **FE-4** `[P]`: Caching tab — org toggle/TTL, per-team opt-out — §11.
- **FE-5** `[P]`: Graceful Degradation tab — threshold slider, target-model picker, org
  + team scope — §11.
- **FE-6** `[P]`: Dashboard — Cache and Failovers stat tiles (§7.1), Cost Saved tile
  (Spend-tile-level prominence per AC5.2), all respecting the existing time-range
  selector and empty-state convention.
- **FE-7** `[D: FE-1..FE-6]`: end-to-end smoke pass once backend routes are live —
  sequenced last.

### devops-engineer

- **DO-1** `[P]`: `docker-compose.yml` — new `redis` service behind `profiles: ["cache"]`
  (mirrors the `keycloak`/`--profile sso` pattern exactly — never starts on plain
  `docker-compose up`), `GATEKEY_REDIS_URL` pass-through env var on `backend`, README
  section documenting `docker compose --profile cache up` + setting `GATEKEY_REDIS_URL`
  together (same two-step pattern already documented for SSO/Keycloak).
- **DO-2** `[D: BD-2]`: confirm the Redis client's connection-pool/timeout behavior is
  safe under the backend's existing single-event-loop-per-process model, document the
  in-process-vs-Redis accuracy trade-off (§4.1) for operators considering horizontal
  scaling — same documentation obligation Phase 3's DO-2 discharged for the scheduler
  loop.

### Parallelization summary

`DB-1` through `DB-7` are `[P]` with each other except `DB-3`, which needs `DB-1`'s
`provider_keys` columns to exist for its FK. `DB-8` (ORM models) gates every backend
task. `BD-1` (the shared-state store) is the one shared dependency the failover,
rate-limiting, and caching tracks all need before their own logic can be built — it has
zero DB dependency itself and should start immediately, in parallel with `DB-1..DB-7`.
Once `BD-1`/`DB-8` land, four backend tracks (failover, rate limiting, caching,
degradation) proceed largely in parallel; the caching and degradation tracks both need
their own DLP/policy-write integration points (BD-10) and pipeline slot (BD-13) touched
carefully so they don't race the failover/rate-limit track's own pipeline edits — BD-13
concentrates all of §8's pipeline rewiring into one task, the same "one file, one task"
discipline Phase 3's BD-6 established. The dashboard/metrics track (BD-17/18) only needs
the schema, not the feature tracks' own logic, to build its query/response shape.
Frontend work is fully parallel with backend and with itself, gated only on §9's contract
being stable. `DO-1` (compose profile) has zero backend dependency and should start
immediately.

---

## 12. Forward-looking rework flags

- **The `SharedStateStore` mechanism (§4.1) is the exact "one shared-state mechanism, not
  solved three times" resolution Phase 2's design doc §12 and Phase 3's design doc §12
  both anticipated — but `ModelPolicyCache`/`TeamModelPolicyCache`/`ResidencyRuleCache`/
  `ContentAwareRuleCache`/`AccessScheduleCache`/the new `TeamFailoverOverrideCache` (§3.2)
  are deliberately NOT retrofitted onto it this phase.** Those remain in-process-only
  singletons with no cross-worker convergence story, same limitation as before. This was
  a scope call, not an oversight: those five/six caches are genuinely process-startup
  config (warmed once, refreshed on admin writes), a different shape from the
  per-request-mutable-counter/ephemeral-entry data `SharedStateStore` was built for, and
  retrofitting them was not requested by this phase's ratified scope. If a future phase
  needs those caches to converge across workers too (e.g. a true horizontal-scaling
  story), `SharedStateStore`'s `get_json`/`set_json` primitives are already the right
  shape to hold them — this is the natural, low-risk consolidation point when that need
  actually arrives, not before.
- **`policy_generation`'s reset-on-restart cache-flush behavior (§5.2)** is fine at
  today's scale/restart-frequency; if a future phase needs cache warmth to survive a
  restart (e.g. because Redis-backed caching becomes the norm and restarts become
  frequent under some rolling-deploy story this codebase doesn't have yet), the fix is
  persisting the counter in Postgres (one row, one column) — a small, contained change,
  not a redesign.
- **`is_primary`/no-load-balancing (§10 fork #1)**: if a future phase is asked to build
  genuine traffic-spreading across multiple keys for the same provider (not just
  failover), this phase's schema (`is_primary`, `failover_enabled`, `failover_target_id`)
  does not need to change to support it — a load-balancing *selection algorithm* would
  layer on top of `select_provider_key` (§3.3) as an additional mode, not a schema
  rework.
- **Fixed 60-second rate-limit windows (§4.2)**: if boundary-burst behavior (up to ~2x
  the configured limit at a window edge) ever proves a real operational problem, the fix
  is a sliding-window-log or leaky-bucket implementation behind the same
  `SharedStateStore` interface — no call-site changes needed in `check_rate_limit`.
- **Semantic caching (ratified #10, explicitly out of scope this phase)**: nothing in
  this design forecloses adding it later — `build_cache_key`'s exact-match hash and
  `SharedStateStore`'s key-value shape are both orthogonal to whatever a future
  embedding-similarity lookup would need (a genuinely different storage shape, e.g. a
  vector index, not an extension of this phase's `SharedStateStore`).
- **Cross-provider failover (ratified #3, out of scope)**: `select_provider_key`'s single
  `failover_target_id` FK is same-provider-constrained at the app layer, not the schema
  layer — a future cross-provider failover feature would need real design work
  (capability/format compatibility, re-validating policy/DLP/residency for a genuinely
  different model, different pricing), not just relaxing this FK's validation.
