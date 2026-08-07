---
title: Phase 1.3 — Model Access Governance (Basic) — Architecture Design
status: accepted
author: architect
last_updated: 2026-07-15
---

# Phase 1.3 — Model Access Governance (Basic) — Design

Scope: org-wide static model allow/denylist enforced on `/v1/chat/completions`,
`/v1/completions`, `/v1/embeddings`, plus `GET`/`PUT /v1/admin/model-policy`. Builds
directly on Phase 1.1 (admin auth pattern, `constants.DEFAULT_ORG_ID` single-org
precedent) and Phase 1.2 (`providers/model_registry.py`, `api/v1/gateway/common.py`'s
`resolve_route -> capability-check -> fetch_credential` sequence). This document is the
durable copy of the design (unlike `phase-1.2-gateway-core.md`, which is a short
pointer — this file is the actual content, per this phase's handoff instructions).

Full product spec: `gatekey/phase-1-core-gateway.md` §1.3 + the product-owner build
spec (AC-1 through AC-9, A1/A2). This doc does not re-litigate product decisions; it
designs against them.

---

## 1. Data model & storage

### 1.1 Table shape

New table `model_policies`, one row per org, **keyed directly on `org_id`** (not a
surrogate `id` + unique constraint, unlike `provider_keys`):

```
model_policies
  org_id      uuid PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE
  mode        model_policy_mode NOT NULL   -- Postgres enum: 'allowlist' | 'denylist' ONLY
  models      jsonb NOT NULL DEFAULT '[]'::jsonb
  created_at  timestamptz NOT NULL DEFAULT now()
  updated_at  timestamptz NOT NULL DEFAULT now()
```

**ADR-1: `org_id` as primary key, not a surrogate id.**
- Decision: `org_id` is the PK. There is, by product design (A2), never more than one
  policy row per org — this makes "exactly one policy per org" a schema-level
  invariant instead of an app-enforced one, and gives the full-replace `PUT` (AC-8) a
  natural, single-column `ON CONFLICT (org_id) DO UPDATE` target.
- Alternative considered: mirror `ProviderKey`'s `id` PK + `UNIQUE(org_id, provider)`
  shape. Rejected — that shape exists because `provider_keys` is genuinely
  multi-row-per-org (one per provider); `model_policies` has no such second dimension
  in Phase 1. Forcing the same shape here would just add an unused surrogate key.
- Forward-compat note: this is Phase-1-specific (single org). Phase 2 nested policy
  (§2.3 of the roadmap) adds a team dimension — see §8 (rework flags) for how this PK
  choice needs to evolve.

**ADR-2: "unconfigured" is the absence of a row, not a third enum value.**
- Decision: the Postgres enum `model_policy_mode` has exactly two values,
  `'allowlist'` and `'denylist'`. The product-level `unconfigured` state (A1) is
  represented by *no row existing* for the org. `services.model_policy` is the only
  place that maps "no row" → the `unconfigured` snapshot.
- Rationale: `PUT` can only ever write `allowlist` or `denylist` (AC-7:
  `mode="unconfigured"` must 422). Keeping the enum 2-valued makes that a schema-level
  type constraint — `PUT`'s Pydantic model literally cannot express writing
  `"unconfigured"` — rather than app-level validation code that could drift from the
  DB constraint over time. It also makes `DELETE`-style "go back to unconfigured"
  trivial to add later (just delete the row) without a migration, though no such
  endpoint is in scope for this phase (non-goals).
- Alternative considered: 3-value enum with `unconfigured` as an explicit, writable
  default row seeded per org (mirroring how `orgs` is seeded in `0001`). Rejected: it
  would require app code to defensively reject a client `PUT` of `mode="unconfigured"`
  that the schema would otherwise happily accept, duplicating what the type system can
  already guarantee, and it adds a seed-row dependency this phase doesn't need (no org
  signup flow yet, but no reason to require one for this table either).

**`models` column** stores gateway-facing model identifiers — `MODEL_REGISTRY` keys
(see `providers/model_registry.py`), never `native_model_id`. Enforced only at the
write path (`services.model_policy.set_policy()`, AC-7's recommended
`unknown_model_in_policy` validation) since `MODEL_REGISTRY` is a pure in-memory
Python dict, not a DB table — there is no FK to lean on here.

### 1.2 Migration

New Alembic revision `0003` (`down_revision = "0002"`), following `0001`'s enum-creation
pattern exactly (`create_type=False` on the column, explicit `enum.create(bind,
checkfirst=True)` beforehand — required under the async dialect per `0001`'s own
comment about `checkfirst` not reliably short-circuiting the implicit
`create_table`-triggered enum creation).

`backend/alembic/versions/0003_create_model_policies.py`:

```python
"""create model_policies table

Phase 1.3 (Model Access Governance - Basic). See
gatekey.db.models.model_policy.ModelPolicy for the ORM side and its module
docstring for the "absence-of-row = unconfigured" rationale (ADR-2 in the
Phase 1.3 design doc); this migration is the source of truth for DDL.

Scoped to the single default org (00000000-0000-0000-0000-000000000001)
seeded by 0001_create_orgs_and_provider_keys.py.

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-15
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MODEL_POLICY_MODE_ENUM_NAME = "model_policy_mode"
MODEL_POLICY_MODE_VALUES = ("allowlist", "denylist")


def upgrade() -> None:
    bind = op.get_bind()
    mode_enum = postgresql.ENUM(
        *MODEL_POLICY_MODE_VALUES, name=MODEL_POLICY_MODE_ENUM_NAME, create_type=False
    )
    mode_enum.create(bind, checkfirst=True)

    op.create_table(
        "model_policies",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("mode", mode_enum, nullable=False),
        sa.Column(
            "models",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("model_policies")
    bind = op.get_bind()
    postgresql.ENUM(*MODEL_POLICY_MODE_VALUES, name=MODEL_POLICY_MODE_ENUM_NAME).drop(
        bind, checkfirst=True
    )
```

No seed data — unlike `0001`, there is nothing to seed (absence-of-row is the correct
initial state for every org, matching A1/AC-4 by construction).

### 1.3 ORM model

New file `backend/src/gatekey/db/models/model_policy.py`, following `provider_key.py`'s
structure (`ProviderName` enum + `create_type=False` PGEnum + model class):

```python
class ModelPolicyMode(str, enum.Enum):
    ALLOWLIST = "allowlist"
    DENYLIST = "denylist"

model_policy_mode_enum = PGEnum(
    ModelPolicyMode, name="model_policy_mode",
    values_callable=lambda enum_cls: [m.value for m in enum_cls],
    create_type=False,
)

class ModelPolicy(Base):
    __tablename__ = "model_policies"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), primary_key=True
    )
    mode: Mapped[ModelPolicyMode] = mapped_column(model_policy_mode_enum, nullable=False)
    models: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
```

Register it in `db/models/__init__.py` (`from gatekey.db.models.model_policy import
ModelPolicy, ModelPolicyMode`) so `Base.metadata` is complete for Alembic
autogenerate/`env.py`, matching the existing convention. Optionally add a
`model_policy: Mapped["ModelPolicy | None"]` `relationship(..., uselist=False)` back on
`Org` for symmetry with `provider_keys`/`service_account_keys` — **not required for
correctness** (the FK's `ON DELETE CASCADE` is enforced at the DB level regardless of
any ORM-side relationship), so treat this as a nice-to-have, not a blocker.

---

## 2. In-process cache & invalidation strategy (AC-3a)

AC-3a is a hard requirement: the policy check must add **zero** DB round trips (cached
or not) on the gateway hot path. This section is deliberately concrete.

### 2.1 What's cached, and where

`backend/src/gatekey/services/model_policy.py` defines:

```python
@dataclass(frozen=True)
class ModelPolicySnapshot:
    mode: Literal["unconfigured", "allowlist", "denylist"]
    models: frozenset[str]

    def is_allowed(self, model: str) -> bool:
        if self.mode == "denylist":
            return model not in self.models
        if self.mode == "allowlist":
            return model in self.models
        return True  # unconfigured -> permissive (A1)

_UNCONFIGURED_SNAPSHOT = ModelPolicySnapshot(mode="unconfigured", models=frozenset())

class ModelPolicyCache:
    """Process-local, in-memory holder of the current policy snapshot."""

    def __init__(self, initial: ModelPolicySnapshot | None = None) -> None:
        self._snapshot = initial or _UNCONFIGURED_SNAPSHOT

    def get(self) -> ModelPolicySnapshot:
        return self._snapshot

    def set(self, snapshot: ModelPolicySnapshot) -> None:
        self._snapshot = snapshot
```

`get()`/`set()` are plain attribute read/write — deliberately lock-free. CPython's GIL
makes a single reference assignment atomic, so a concurrent reader observes either the
prior snapshot or the new one in full; it can never see a torn mix of `mode` from one
write and `models` from another. This is sufficient for a config toggle (eventual
consistency across concurrently-in-flight requests is acceptable — nothing in AC-1
through AC-9 requires a policy change to be linearizable with in-flight requests), and
avoids introducing an `asyncio.Lock`/contention point on what would otherwise be a
zero-cost read.

`ModelPolicyCache` is instantiated once per process and stored on `app.state`, mirroring
`app.state.provider_http_client` / `app.state.vertex_token_cache` in `main.py`.

### 2.2 Startup warm — and why it must fail open, bounded

`main.create_app`'s `_lifespan` gets one addition, run **before** `yield` (i.e. before
the app starts accepting traffic):

```python
app.state.model_policy_cache = ModelPolicyCache()  # cheap, zero-I/O: defaults to unconfigured
try:
    async with asyncio.timeout(_MODEL_POLICY_BOOTSTRAP_TIMEOUT_SECONDS):  # e.g. 5.0s
        async with session_factory() as bootstrap_session:
            snapshot = await load_policy_snapshot(bootstrap_session)
    app.state.model_policy_cache.set(snapshot)
except Exception:
    logger.warning("model_policy_bootstrap_failed", exc_info=True)
    # cache stays at its zero-I/O default (unconfigured/permissive) - see ADR-3.
```

**ADR-3: bootstrap failure fails open (permissive), bounded by a short timeout — not
fail-closed, not unbounded.**
- Decision: `ModelPolicyCache()` is constructed with the safe, zero-I/O
  `unconfigured` default *first*; the real DB-backed value is layered on top only if
  the bootstrap load succeeds within a short timeout. Any failure (DB unreachable,
  timeout, unexpected row shape) is caught, logged, and the app continues serving with
  the default. This is a deliberate design choice, not an oversight — flagging for
  security review per the product spec's request.
- Rationale:
  1. **A1 already defines "no policy loaded" as permissive** — a bootstrap failure and
     "no PUT has ever been called" are observationally the same state
     (`ModelPolicySnapshot(mode="unconfigured", ...)`), so this isn't a new failure
     mode being invented, it's reusing an already-specified one.
  2. **Fail-closed here buys no real protection.** If the DB is genuinely down at
     startup, `fetch_credential()` (which runs later in the exact same request, after
     the policy check) will fail with a DB error on the very first real gateway
     request anyway — the request cannot complete either way. Fail-closed on the
     policy check specifically would only add a second, differently-shaped failure
     mode to reason about without changing the overall availability outcome.
  3. **Fail-closed would risk the opposite, worse failure**: a transient DB hiccup at
     process startup (e.g. Postgres still finishing its own startup in
     `docker-compose up`, a brief network blip) would otherwise turn into "every
     model request 403s" for a self-hosted operator with zero indication why — a much
     worse first-hour experience than the one Phase 1.7 targets (`docker-compose up`
     under an hour to first successful request), and directly at odds with the
     self-deploy-without-support non-negotiable.
  4. The timeout bound exists so a hung/unreachable DB can't stall process startup
     indefinitely — bounded degradation, not bounded correctness.
- Alternative considered: fail-closed (deny all models until a successful load).
  Rejected per (2) and (3) above — it trades a cosmetic "safer-looking" default for a
  worse real-world failure mode with no actual security benefit, since the request
  can't complete without DB access moments later regardless.
- This also has a load-bearing side effect for **test isolation**, covered in §6.

**ADR-3 addendum: bounded, in-process self-heal after a failed bootstrap (security
review finding, added post-review).**
- Problem: the original one-shot bootstrap above has no retry. If the DB is merely
  still starting up when the gateway process starts — ADR-3's own primary example,
  "Postgres still finishing its own startup in `docker-compose up`" — the cache
  latches onto the permissive `unconfigured` default for the *entire remaining
  lifetime of the process*, even though every subsequent `fetch_credential()` call on
  the gateway hot path succeeds fine once the DB comes up seconds later. This is worse
  than the multi-worker divergence in §2.4 because it's silent in a misleading way:
  `GET /v1/admin/model-policy` reads straight from the DB (§4.2), so an admin checking
  "is my policy in effect?" sees their correct allow/denylist reflected and has no
  signal that the actual enforcement path (the in-process cache) is stuck permissive.
- Decision: `main.py`'s `_lifespan`, on bootstrap failure only, schedules a background
  `asyncio.Task` (`_model_policy_self_heal`) that retries the same bounded
  `load_policy_snapshot()` call with exponential backoff — capped at
  `_MODEL_POLICY_SELF_HEAL_MAX_ATTEMPTS` (5) attempts, starting at
  `_MODEL_POLICY_SELF_HEAL_INITIAL_BACKOFF_SECONDS` (2.0s) and doubling up to a
  `_MODEL_POLICY_SELF_HEAL_BACKOFF_CEILING_SECONDS` (30.0s) ceiling. On first success it
  replaces the cache's snapshot and stops; if every attempt fails it logs once and
  gives up, leaving the cache at its permissive default (i.e. today's pre-self-heal
  behavior, as the final fallback — not a new failure mode). The task is stored on
  `app.state.model_policy_self_heal_task` and is cancelled (with the cancellation
  awaited) in the lifespan's shutdown `finally` block, so it never outlives the app.
- Why these numbers: five attempts spread over roughly 2+4+8+16+30 ≈ 60s of elapsed
  time is enough to ride out a several-seconds DB startup-ordering hiccup (the
  documented scenario) without hammering a genuinely-down DB at high frequency
  indefinitely. This stays purely in-process — no new cross-process infra, and
  doesn't touch ADR-4's explicitly-deferred multi-worker problem (§2.4): each worker
  still only self-heals its own cache.
- Test-harness interaction (§6 still holds, with one extra property): the self-heal
  task spends essentially all of its time inside `asyncio.sleep`, which responds to
  cancellation immediately. Existing gateway unit tests build the app, issue one or
  two requests, and tear it down well inside the first 2s backoff window, so the task
  is cancelled while still asleep and never gets a chance to retry against the fake,
  unreachable DSN those tests use — no added flakiness, hangs, or unawaited-task
  warnings. `tests/unit/test_main.py` covers both the self-heal-succeeds path (via a
  monkeypatched, shortened backoff) and the cancel-cleanly-on-shutdown path directly.

**ADR-3 addendum, second round: self-heal must not clobber a concurrent admin `PUT`
(security review finding, added post-review of the first addendum above).**
- Problem: the first addendum changed a load-bearing invariant without calling it out.
  Before it, `ModelPolicyCache` had exactly one non-`PUT` writer — the initial
  bootstrap — which always ran to completion *before* `yield`, i.e. strictly before the
  app began serving traffic, so it could never race a `PUT`. `_model_policy_self_heal`
  breaks that invariant: it runs as a background task *concurrently* with live traffic,
  including live admin `PUT`s, for up to roughly the ~60s backoff window described
  above. `ModelPolicyCache.set()` was (and, for `PUT`, still is — see below) an
  unconditional, unversioned attribute write with no compare-and-set. Concretely: if a
  self-heal retry's `load_policy_snapshot()` SELECT is in flight when an admin `PUT`
  commits a new, more-restrictive policy and calls `cache.set()`, the self-heal
  attempt's stale read can resume afterward and call `cache.set()` again with its
  pre-`PUT` value — silently reverting the admin's just-committed policy — and then log
  `model_policy_bootstrap_self_healed` (misleadingly suggesting success) and stop
  retrying, leaving the cache stuck on the wrong value until another `PUT` or a
  restart.
- Decision: `ModelPolicyCache` (`services/model_policy.py`) gains a monotonically
  increasing `_generation: int` counter, exposed via `get_generation()`, that every
  write bumps. `set()` stays exactly as it was — unconditional, always wins — but a new
  `set_if_current(snapshot, expected_generation) -> bool` method only applies its write
  if `expected_generation` still equals the current generation (comparison-then-write
  with no `await` in between, so this is atomic under the same GIL/single-threaded-
  event-loop reasoning as `get()`/`set()` themselves — no lock introduced).
  `_model_policy_self_heal` captures `cache.get_generation()` immediately before each
  attempt's `_load_model_policy_snapshot_bounded()` call, and applies the result via
  `set_if_current()` instead of `set()`. If that reports it was superseded, the loop
  logs `model_policy_bootstrap_self_heal_superseded_by_put` at info level and stops —
  the cache is already correct via the `PUT`, so this is treated as "someone else
  already fixed it, my job here is superseded," not a failure that should retry again.
- Why `PUT`'s `cache.set()` call is *not* also generation-guarded: an admin `PUT` has
  just committed the authoritative row via `set_policy()`'s atomic
  `INSERT ... ON CONFLICT DO UPDATE` upsert — it is this system's source of truth for
  the policy, not a "best-effort" writer racing to catch up like self-heal is. Guarding
  it with a CAS would invert the priority this fix establishes: a `PUT` could then lose
  to a self-heal attempt that happened to still be "current" by generation but was
  reading (and about to apply) genuinely older data. The generation guard belongs only
  on the side that is racing to catch up (self-heal), not on the side that is the
  authoritative writer (`PUT`). Two concurrent `PUT`s racing each other on
  `cache.set()` ordering (as opposed to the DB row itself, which is protected by the
  single-statement upsert) is a separate, narrower, pre-existing concern not raised by
  this finding and out of scope here — nothing about this fix makes it better or worse.
- Test: `tests/integration/test_model_policy_api.py::
  test_self_heal_does_not_clobber_a_put_that_lands_while_its_read_is_in_flight`
  reproduces the interleaving directly against a real Postgres — a self-heal retry's
  (monkeypatched) read blocks on an `asyncio.Event` to simulate "already in flight",
  the test drives a real `PUT` through underneath it, then releases the blocked read
  (returning a stale, pre-`PUT` snapshot) and asserts the cache ends up holding the
  `PUT`'s value, not self-heal's. Placed as an integration test rather than a unit
  test because driving a real `PUT` to completion needs a real DB session — the unit
  harness's `get_db_session` override is an exploding sentinel (§6) that makes that
  impossible there. `tests/unit/test_model_policy_service.py` separately covers the
  `ModelPolicyCache` generation counter/CAS mechanics in isolation (no app/DB
  involved): generation starts at 0, `set()` bumps it, `set_if_current()` applies and
  bumps on a matching generation and is a no-op (cache untouched, generation
  unchanged beyond the winning write) on a stale one.

### 2.3 Write-path invalidation (no re-query)

`PUT /v1/admin/model-policy`'s handler already knows the exact `mode`/`models` it just
committed (it built the upsert statement from the validated request body and the
`RETURNING` row). It calls `cache.set(ModelPolicySnapshot(mode=..., models=...))`
directly, in the same request, immediately after `session.commit()` succeeds — **no
second DB read to "invalidate"; the cache is *replaced*, not invalidated-and-refetched.**
This keeps the admin write path to exactly the one DB round trip it already needed.

### 2.4 Multi-worker / horizontal-scale limitation — explicit, not silent

`ModelPolicyCache` lives on a single process's `app.state`. If Gatekey is ever run with
more than one worker process (`uvicorn --workers N`, or multiple containers behind a
load balancer), a `PUT` handled by worker A updates **only worker A's** cache; workers
B..N keep serving their stale in-process snapshot until they are restarted or
independently re-warmed.

This is called out explicitly rather than silently accepted:

- **Phase 1 assumption**: the reference deployment (Phase 1.7, `docker-compose up`) is
  a single container running a single uvicorn worker process. Devops/deployment docs
  for Phase 1.7 must state this assumption explicitly (task flagged in §9).
- **Known limitation if an operator runs multiple workers anyway**: policy changes
  converge only on a bounded/eventual basis (worst case: until each worker restarts),
  not immediately. This is the same class of problem Phase 4 already names for its own
  in-process rate-limit counters ("no naive in-process counters if Gatekey is
  horizontally scaled") — see §8 for why this phase's cache should be revisited
  alongside whatever shared-state mechanism (Redis pub/sub, Postgres `LISTEN`/`NOTIFY`)
  Phase 4 introduces, rather than solved twice independently.
- **Not solved now**: introducing a cross-process invalidation channel (Redis, or
  Postgres `LISTEN`/`NOTIFY` requiring a held connection/listener task) is out of
  proportion for a Phase 1.3 "basic" governance slice and isn't required by any AC —
  AC-3a's bar is "no *per-request* DB round trip," which a purely in-process cache
  already satisfies regardless of worker count; it's the *cross-worker convergence
  time* that's the open item, not the latency target.

**ADR-4: in-process singleton cache vs. shared/external cache from day one.**
- Decision: in-process only, for Phase 1.
- Alternatives considered: (a) Redis-backed cache with pub/sub invalidation — rejected
  as introducing a new required infra dependency ahead of Phase 4, where caching
  infrastructure is actually scoped in (§4.3) and where the self-hosted "no external
  dependencies beyond Postgres + the container runtime" (Phase 1.7) constraint would
  otherwise be broken a phase early. (b) Postgres `LISTEN`/`NOTIFY` — technically
  Postgres-only (no new dependency), but requires a long-lived listener connection/task
  per worker, which is real additional lifecycle complexity for a Phase 1 slice whose
  reference deployment is single-worker anyway. Both are reasonable Phase 4 candidates
  (see §8) but disproportionate now.

---

## 3. `check_model_policy()` call-site design

### 3.1 The AC-4-flagged bypass concern — resolved

The product spec flags this explicitly: the policy check's model-identity comparison
must use *the same* string, compared with *the same* semantics, as
`resolve_route()`/`resolve_model()`'s exact-match dict lookup — not a second,
independently-normalized comparison against the raw request body.

**Resolution**: `check_model_policy()` takes the *exact same* `model` variable the
route handler already passed to `resolve_route()` — never a second read of
`body.model`, never `route.native_model_id`, never anything re-derived or normalized.
Concretely, at every call site:

```python
route = resolve_route(body.model)      # unchanged from Phase 1.2
check_model_policy(body.model, cache)  # same variable, not re-read/re-normalized
```

This is provably bypass-proof, not just "probably fine," because of what
`resolve_route()` already guarantees: it succeeded only because
`MODEL_REGISTRY[body.model]` (exact-match, case-sensitive `dict.__getitem__`) did not
raise `KeyError` (see `providers/model_registry.py`'s `resolve_model()` and its module
docstring: "no fuzzy/prefix/alias matching"). By the time control reaches
`check_model_policy()`, `body.model` is therefore **provably a literal `MODEL_REGISTRY`
key** — not merely close to one. Separately, every string ever stored in a policy's
`models` list is validated at `PUT` time (`services.model_policy.set_policy()`, AC-7)
to also be a literal `MODEL_REGISTRY` key. Both sides of the `in` check therefore draw
from the same closed, exact-string universe (`MODEL_REGISTRY.keys()`); no
normalization step exists on either side for a case/whitespace/alias variant to slip
through. A plain Python `in` test on a `frozenset[str]` is sufficient and is the whole
check — deliberately no `.lower()`, `.strip()`, or alias table anywhere near this code
path, because adding one would be the bug, not a hardening.

This is independently verifiable (for a security reviewer) by inspecting exactly two
things: (1) `check_model_policy()`'s parameter is never anything other than the
identical `model` argument passed to `resolve_route()` in the same function, and (2)
`set_policy()` rejects any `models` entry not in `MODEL_REGISTRY` before writing. It's
independently testable (for QA) with a single adversarial case: `PUT` an allowlist
containing `"gpt-4o"`, then request `model="GPT-4o"` (or `" gpt-4o"`) — the expected
result is **404 `model_not_found`** (rejected by `resolve_route()` before the policy
check ever runs), never a 200 (would mean the policy check is too permissive on
variants) and never a 403 with a different message shape (would mean a second,
inconsistent notion of model identity exists somewhere).

### 3.2 Signature and behavior

`backend/src/gatekey/api/v1/gateway/common.py` gains:

```python
def check_model_policy(model: str, cache: ModelPolicyCache) -> None:
    """Enforce the org's model access policy for `model`.

    `model` MUST be the exact same string already passed to `resolve_route()`
    in this same request — never re-read from the request body and never
    normalized — see design doc §3.1 for why that's what makes this
    bypass-proof. Raises `errors.ModelDeniedError` (403) if denied. Pure,
    synchronous, zero I/O — reads only `cache.get()` (AC-3a); never touches
    the database. Call this only *after* `resolve_route()` has already
    succeeded, and *before* any capability/provider check or credential
    fetch — see module docstring for the full ordering.
    """
    snapshot = cache.get()
    if not snapshot.is_allowed(model):
        raise ModelDeniedError(model)
```

`ModelPolicyCache` is threaded in exactly like `key_provider`/`http_client`/
`token_cache` today — via a new FastAPI dependency, not a hidden module-global:

`backend/src/gatekey/api/deps.py`:
```python
def get_model_policy_cache(request: Request) -> ModelPolicyCache:
    """Fetch the shared, in-process ModelPolicyCache stashed on app.state.
    Built once per process in main.create_app's lifespan - see
    services.model_policy.ModelPolicyCache / design doc §2."""
    return request.app.state.model_policy_cache
```

### 3.3 Wiring into the three route handlers — one call site, one place, per AC-3

Per the module docstring's existing invariant ("every gateway route handler is
expected to call [`resolve_route`, ...] in that order") and AC-3's explicit ordering
(`resolve_route → POLICY CHECK → capability check → fetch_credential`), each of
`chat.py`, `completions.py`, `embeddings.py` adds exactly two lines: a new
`cache: ModelPolicyCache = Depends(get_model_policy_cache)` parameter, and one call to
`check_model_policy(body.model, cache)` immediately after `resolve_route()` and before
the endpoint's existing capability/provider check(s). E.g. in `chat.py`:

```python
route = resolve_route(body.model)
check_model_policy(body.model, cache)          # <-- new, AC-3
if route.capability != ModelCapability.CHAT:    # unchanged
    raise HttpUnsupportedRequestError(...)
credential = await fetch_credential(...)        # unchanged
```

No branching logic is duplicated per route — the shared helper is the entire
implementation; each route file only supplies the call site, identical in shape across
all three. This satisfies AC-3's "one shared helper... not reimplemented per route" and
automatically covers both the streaming and non-streaming branches of
`create_chat_completion` (the check runs before the `if body.stream:` branch, exactly
like the existing capability check).

`completions.py` note: its early `if body.stream: raise ...` check runs *before*
`resolve_route()` (it's a cheap static body check that never touches the model
registry — see that file's existing docstring) and is unaffected; `check_model_policy`
is inserted after `resolve_route()`, same as the other two files.

### 3.4 What it raises

`errors.ModelDeniedError` (see §5). No `try`/`except` needed at any call site —
`GatekeyError` subclasses are already caught by the app-wide exception handler
(`errors.register_exception_handlers`), which logs `gatekey_error` with `code` +
`path` and emits the structured `{"error": {...}}` envelope. This also satisfies the
product spec's "a lightweight `logger.info` at the rejection site is optional" —
that generic handler already logs on every `GatekeyError`, including this one, so no
additional logging code is needed at the call site.

---

## 4. Admin API contract

Follows `api/v1/admin/providers.py`'s exact pattern: `require_admin` router-level
dependency, no `org_id` param, `GatekeyError`-based errors, service-layer logic kept
out of the route module.

### 4.1 Schemas — `backend/src/gatekey/schemas/model_policy.py`

```python
class ModelPolicyPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["allowlist", "denylist"]   # "unconfigured" is not a member -> AC-7's
                                              # 422 falls out of ordinary Pydantic/FastAPI
                                              # request validation, no custom code needed.
    models: list[str] = []

    @field_validator("models")
    @classmethod
    def _entries_non_empty_strings(cls, value: list[str]) -> list[str]:
        for entry in value:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError("models entries must be non-empty strings.")
        return value


class ModelPolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    mode: Literal["unconfigured", "allowlist", "denylist"]
    models: list[str]
```

A single `models: list[str]` field (no `allowlist_models`/`denylist_models` pair)
makes "both lists populated" structurally unrepresentable (AC-9) — there is only ever
one list, and `mode` says how to interpret it.

### 4.2 Routes — new `backend/src/gatekey/api/v1/admin/model_policy.py`

```python
router = APIRouter(
    prefix="/v1/admin/model-policy",
    tags=["admin", "model-policy"],
    dependencies=[Depends(require_admin)],
)

@router.get("", response_model=ModelPolicyResponse)
async def get_model_policy(session: AsyncSession = Depends(get_db_session)) -> ModelPolicyResponse:
    """Always 200 - default {"mode": "unconfigured", "models": []} if no row (AC-7).
    Reads the DB directly (not the in-process cache): this is a control-plane read,
    not the AC-3a hot path, and should reflect the latest committed row even on a
    worker whose own cache happens to be stale (see design doc §2.4)."""
    snapshot = await get_policy(session)
    return ModelPolicyResponse(mode=snapshot.mode, models=sorted(snapshot.models))

@router.put("", response_model=ModelPolicyResponse)
async def put_model_policy(
    payload: ModelPolicyPutRequest,
    session: AsyncSession = Depends(get_db_session),
    cache: ModelPolicyCache = Depends(get_model_policy_cache),
) -> ModelPolicyResponse:
    """Full-replace upsert (AC-8). 422 unknown_model_in_policy if any `models` entry
    isn't a known MODEL_REGISTRY id - no DB write in that case (AC-7). Pushes the new
    snapshot straight into this process's cache after commit (design doc §2.3)."""
    try:
        snapshot = await set_policy(session, payload.mode, payload.models)
    except UnknownModelInPolicyError as exc:
        raise GatekeyError(exc.message, code="unknown_model_in_policy", status_code=422) from None
    cache.set(snapshot)
    return ModelPolicyResponse(mode=snapshot.mode, models=sorted(snapshot.models))
```

### 4.3 Service — new `backend/src/gatekey/services/model_policy.py`

In addition to `ModelPolicySnapshot`/`ModelPolicyCache` (§2.1):

```python
class UnknownModelInPolicyError(Exception):
    def __init__(self, unknown_models: list[str]) -> None:
        message = (
            "Unknown model id(s) in policy: " + ", ".join(sorted(unknown_models))
            + ". Every entry must be a model id known to Gatekey's model registry."
        )
        super().__init__(message)
        self.message = message
        self.unknown_models = unknown_models


async def load_policy_snapshot(session: AsyncSession) -> ModelPolicySnapshot:
    """Query the org's policy row. Used at process startup only (to warm
    ModelPolicyCache) and by get_policy() below - NEVER call this from a gateway
    route handler (AC-3a)."""
    stmt = select(ModelPolicy).where(ModelPolicy.org_id == DEFAULT_ORG_ID)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return _UNCONFIGURED_SNAPSHOT
    return ModelPolicySnapshot(mode=row.mode.value, models=frozenset(row.models))


async def get_policy(session: AsyncSession) -> ModelPolicySnapshot:
    return await load_policy_snapshot(session)


async def set_policy(
    session: AsyncSession, mode: Literal["allowlist", "denylist"], models: list[str]
) -> ModelPolicySnapshot:
    unknown = set(models) - MODEL_REGISTRY.keys()
    if unknown:
        raise UnknownModelInPolicyError(sorted(unknown))

    dedup_models = sorted(set(models))
    insert_stmt = postgresql.insert(ModelPolicy).values(
        org_id=DEFAULT_ORG_ID, mode=mode, models=dedup_models
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[ModelPolicy.org_id],
        set_={"mode": insert_stmt.excluded.mode, "models": insert_stmt.excluded.models,
              "updated_at": func.now()},
    ).returning(ModelPolicy)
    row = (await session.execute(upsert_stmt)).scalar_one()
    await session.commit()
    return ModelPolicySnapshot(mode=row.mode.value, models=frozenset(row.models))
```

`set_policy()`'s upsert is a single `INSERT ... ON CONFLICT (org_id) DO UPDATE`
statement — same pattern as `services.provider_keys.add_or_replace_key` — so it is a
true full-replace, not a read-then-write: two concurrent `PUT`s cannot interleave into
a mixed `mode`/`models` pair, and there is no window where a partially-applied policy
is visible (AC-8).

---

## 5. `errors.py` additions

Placed next to `ModelNotFoundError`/`ProviderNotConfiguredError`, in the existing
"Phase 1.2 (BD-10): gateway-specific errors" section:

```python
class ModelDeniedError(GatekeyError):
    """The requested model is not permitted by the org's model access policy.

    Raised by api.v1.gateway.common.check_model_policy() after resolve_route()
    has already succeeded for the same `model` string - see that function's
    docstring and design doc §3.1 for why this guarantees no case/alias/
    whitespace bypass of the policy's `models` list. The model name is safe
    to include in `message` for the same reason ModelNotFoundError's is
    (caller input, not secret material).
    """

    status_code = status.HTTP_403_FORBIDDEN
    code = "model_denied"

    def __init__(self, model: str) -> None:
        super().__init__(
            f"Model '{model}' is not permitted by this organization's model access policy."
        )
```

Matches AC-2's exact required message text and envelope shape.

---

## 6. Interaction with the existing test harness — a real constraint, not incidental

This surfaced while designing §2.2 and materially shaped ADR-3, so it's worth stating
explicitly rather than leaving implicit.

`tests/unit/gateway_test_support.py` builds the real `create_app()` and every existing
`tests/unit/test_gateway_*.py` test drives it via `with TestClient(app) as client:` —
which **does** run `main.py`'s lifespan (startup and shutdown), even though these are
unit tests whose `DATABASE_URL` (`make_settings()`'s default) points at nothing real,
and whose `get_db_session` dependency is overridden to an exploding sentinel that
raises on any attribute access. That override only intercepts the *FastAPI-Depends*
path, not a direct `session_factory()` call made inside the lifespan function itself.

Given that, an eager, unguarded DB query in the lifespan bootstrap (§2.2) would attempt
a real connection against `postgresql+asyncpg://user:pass@localhost:5432/gatekey`
(and, worse, could silently succeed/behave unpredictably on a developer machine that
happens to have a local Postgres listening on the default port) on **every** existing
gateway unit test, breaking or destabilizing all of them without any test itself
having anything to do with model policy.

ADR-3's fail-open-with-bounded-timeout design resolves this as a side effect, not by
coincidence:
- `ModelPolicyCache()` is constructed with the safe, zero-I/O default *before* the
  bootstrap attempt, so the attempt's outcome (success, timeout, or connection
  failure) never blocks startup or leaves the cache unset.
- Against the unit tests' fake DSN, the connection attempt fails fast (connection
  refused, or an auth/DB-name error against a stray local Postgres) well within the
  bootstrap timeout, is caught, and logged — the cache lands on `unconfigured`
  (permissive), which is exactly the behavior every existing gateway unit test already
  assumes (none of them configure a policy, all of them expect registry-known models
  to route successfully).
- `tests/integration/conftest.py`'s `app`/`client` fixtures build against a real,
  migrated Postgres container and explicitly drive
  `app.router.lifespan_context(app)` per test function, so integration tests get a
  correctly-warmed cache from real (initially-empty) DB state each time, with no
  special-casing needed for AC-4 ("fresh org, no PUT ever called").

No changes to the existing test-support fixtures are required for this to hold; this
section exists so a future maintainer sees the reasoning, not just the behavior.

---

## 7. Non-functional requirements — explicit accounting

Per Phase 1's NFRs and this feature's own AC-3a:

- **p99 gateway overhead < 150ms (Phase 1 NFR) / no per-request DB round trip
  (AC-3a)**: satisfied by construction — `check_model_policy()` is synchronous, does
  one `frozenset`/dict-shaped membership test against an in-memory snapshot, and
  performs no I/O. The only DB traffic this feature ever adds is (a) once at process
  startup (bootstrap warm, bounded and fail-open, §2.2) and (b) on admin `PUT`/`GET`
  calls, which are not gateway-hot-path traffic.
- **No lost/double-charged requests on retry (Phase 1 NFR, idempotent cost
  accounting)**: not implicated by this feature — a denial happens strictly before
  `fetch_credential()` and therefore strictly before any provider call or cost/usage
  side effect (AC-2). There is nothing to retry or double-count; a denied request
  never reaches the code paths that would incur cost.
- **No undocumented single point of failure**: this feature does not introduce a new
  SPOF. Postgres is already the system's SPOF (every other Phase 1 feature depends on
  it); the in-process cache does not add a second one — if anything, ADR-3's fail-open
  behavior makes this feature specifically *more* tolerant of a transient DB outage
  than the rest of the request path, not less. The one genuinely new limitation is the
  multi-worker cache-convergence gap (§2.4), which is called out as a known limitation
  rather than silently shipped, per the Phase 1 NFR's own requirement to document (not
  eliminate) any non-eliminated single point of failure / consistency gap.

---

## 8. Forward-looking rework flags

- **Phase 2, §2.3 (Nested Model Policy)**: team-level allow/deny lists that can only
  further restrict the org baseline, with deterministic, visible precedence. This
  phase's `check_model_policy(model, cache)` call site is deliberately the *only*
  place in the gateway route handlers that knows about model policy at all — Phase 2
  should be able to extend what happens *inside* that one function (resolve org
  baseline AND team restriction, in a defined precedence order) without touching
  `chat.py`/`completions.py`/`embeddings.py` again. What **will** need rework:
  `ModelPolicyCache`/`ModelPolicySnapshot` are single-snapshot, single-org shaped
  (matching `DEFAULT_ORG_ID`'s Phase 1 single-org constraint); Phase 2 needs a
  cache keyed by `(org_id, team_id)` (or org_id + a per-team overlay), and
  `ModelPolicy`'s `org_id`-as-PK table shape (ADR-1) will need a team dimension added
  — likely a new `team_model_policies` table alongside this one (org baseline) rather
  than reshaping this table, so Phase 1 policy data and behavior are preserved
  unchanged for orgs that never adopt teams.
- **Phase 4 (multi-worker / horizontal scale)**: §2.4's known limitation (in-process
  cache doesn't converge across worker processes) should be revisited alongside
  whatever shared-state mechanism Phase 4 introduces for its own explicitly-flagged
  "no naive in-process counters if horizontally scaled" rate-limiter requirement —
  solving cross-process invalidation once, for both features, is preferable to two
  independent bolt-ons.

---

## 9. Task breakdown

Legend: [P] = can run in parallel with sibling [P] tasks; [D: X] = hard dependency on
task X.

### database-admin

- **DB-1**: Write and apply Alembic migration `0003_create_model_policies.py` per §1.2
  (enum + table). [P] (no dependency on backend code; can start immediately from this
  doc).
- **DB-2**: Add `ModelPolicy`/`ModelPolicyMode` ORM model at
  `db/models/model_policy.py` per §1.3, and register it in `db/models/__init__.py`.
  [D: DB-1] (needs the migration's exact column/enum names to match).

### backend-developer

- **BD-1**: `errors.py` — add `ModelDeniedError` (§5). [P] (no dependencies).
- **BD-2**: `schemas/model_policy.py` — `ModelPolicyPutRequest`/`ModelPolicyResponse`
  (§4.1). [P] (no dependencies).
- **BD-3**: `services/model_policy.py` — `ModelPolicySnapshot`, `ModelPolicyCache`,
  `UnknownModelInPolicyError`, `load_policy_snapshot`/`get_policy`/`set_policy` (§2.1,
  §4.3). [D: DB-2] (needs the ORM model).
- **BD-4**: `api/deps.py` — add `get_model_policy_cache` (§3.2). [D: BD-3].
- **BD-5**: `main.py` — wire `app.state.model_policy_cache`, add the bounded, fail-open
  bootstrap warm in `_lifespan` per §2.2/ADR-3 (including the timeout constant and the
  broad `except Exception` + `logger.warning`). [D: BD-3].
- **BD-6**: `api/v1/gateway/common.py` — add `check_model_policy()` (§3.2); update the
  module docstring's documented call ordering to mention the new step. [D: BD-1, BD-3].
- **BD-7**: Wire `check_model_policy()` into `chat.py`, `completions.py`,
  `embeddings.py` per §3.3 (new `cache` dependency param + one call site each, inserted
  between `resolve_route()` and the existing capability check). [D: BD-4, BD-6].
- **BD-8**: New `api/v1/admin/model_policy.py` — `GET`/`PUT /v1/admin/model-policy`
  per §4.2; register the router in `main.py`. [D: BD-2, BD-3, BD-4].
- **BD-9**: Tests: unit tests for `check_model_policy()`/`ModelPolicyCache` in
  isolation (AC-4, AC-5, AC-6, the case-sensitivity/bypass adversarial case from
  §3.1), unit tests for the three gateway routes' denial path (AC-2, AC-3 ordering —
  assert no credential-fetch/provider-call occurs), unit tests for the admin
  schemas/validation (AC-7, AC-9), integration tests for `PUT` full-replace semantics
  (AC-8) and fresh-org default-permissive (AC-4) against a real migrated DB. [D: BD-7,
  BD-8].

### Parallelization summary

`DB-1` and `BD-1`/`BD-2` can start immediately and in parallel. `DB-2` depends only on
`DB-1`. `BD-3` depends on `DB-2` (needs the ORM model) — this is the critical-path
dependency that gates everything else (`BD-4` through `BD-8` all transitively depend on
`BD-3`). `BD-7` and `BD-8` can proceed in parallel once their shared prerequisites
(`BD-3`/`BD-4`/`BD-6`) land. `BD-9` is last, after both route-wiring tasks.

### Devops / docs (flagged, not a task owned by this design doc's roles)

- Phase 1.7 deployment docs must state the single-worker-process assumption from §2.4
  explicitly once that phase is built — flagging here so it isn't silently dropped
  between this design and whoever picks up 1.7.
