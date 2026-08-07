"""In-process cache and DB-backed service for the org's model access policy.

Phase 1.3 (Model Access Governance - Basic). See
`docs/design/phase-1.3-model-governance.md` sections 2 and 4.3 for the full
design rationale (ADR-2 through ADR-4).

Every function in this module operates against `constants.DEFAULT_ORG_ID`
only - see that module's docstring for why no `org_id` parameter is
accepted here (mirrors `services/provider_keys.py`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.content_aware_rule import ContentAwareRule
from gatekey.db.models.model_policy import ModelPolicy
from gatekey.db.models.team_model_policy import TeamModelPolicy
from gatekey.errors import GatekeyError
from gatekey.providers.model_registry import MODEL_REGISTRY

if TYPE_CHECKING:
    # Local, TYPE_CHECKING-only import to avoid a circular import at runtime
    # (`services.self_hosted_providers` does not import this module, so a
    # real cycle is not actually possible today - but keeping this
    # deferred/TYPE_CHECKING-only mirrors `services.residency`'s own
    # `CacheInvalidator` import discipline for a sibling cross-service type
    # used only as a parameter annotation).
    from gatekey.services.custom_models import CustomModelRouteCache
    from gatekey.services.self_hosted_providers import SelfHostedModelRouteCache

PolicyMode = Literal["unconfigured", "allowlist", "denylist"]


@dataclass(frozen=True)
class ModelPolicySnapshot:
    """An immutable, point-in-time view of the org's model access policy.

    `models` is a `frozenset` (not a `list`) so `is_allowed()` is an O(1)
    membership test and the snapshot itself is hashable/immutable - matching
    `ModelPolicyCache`'s "replace, never mutate" contract (design doc
    section 2.1).
    """

    mode: PolicyMode
    models: frozenset[str] = field(default_factory=frozenset)

    def is_allowed(self, model: str) -> bool:
        """Whether `model` is permitted under this snapshot.

        `model` must already be a literal `MODEL_REGISTRY` key (i.e. it has
        already passed `resolve_route()`/`resolve_model()`) - see
        `api.v1.gateway.common.check_model_policy()`'s docstring for why no
        normalization happens here or at that call site.
        """
        if self.mode == "denylist":
            return model not in self.models
        if self.mode == "allowlist":
            return model in self.models
        return True  # unconfigured -> permissive (A1)


_UNCONFIGURED_SNAPSHOT = ModelPolicySnapshot(mode="unconfigured", models=frozenset())


class ModelPolicyCache:
    """Process-local, in-memory holder of the current policy snapshot.

    `get()`/`set()` are plain attribute read/write - deliberately
    lock-free. CPython's GIL makes a single reference assignment atomic, so
    a concurrent reader observes either the prior snapshot or the new one in
    full, never a torn mix of `mode` from one write and `models` from
    another. This is sufficient for a config toggle (eventual consistency
    across concurrently-in-flight requests is acceptable) and avoids an
    `asyncio.Lock`/contention point on what would otherwise be a zero-cost
    read (design doc section 2.1).

    Instantiated once per process and stored on `app.state` (see
    `main.create_app`'s lifespan) - never construct a second instance and
    thread it through separately.

    Security review finding, second round (design doc section 2.2/ADR-3
    addendum): `_generation` is a monotonically increasing counter bumped by
    every write (`set()` and `set_if_current()` alike). It exists solely so
    `main._model_policy_self_heal` - the one writer that now runs
    concurrently with live admin `PUT` traffic (see that function's
    docstring) - can detect "someone else already wrote a newer value while
    my own DB read was in flight" and avoid clobbering it. `set()` itself
    stays an unconditional, unversioned write (see its docstring for why);
    only `set_if_current()` is compare-and-set. Reading/bumping an `int`
    attribute is exactly as GIL-atomic as the snapshot reference swap above,
    so this adds no lock and no new contention point.
    """

    def __init__(self, initial: ModelPolicySnapshot | None = None) -> None:
        self._snapshot = initial or _UNCONFIGURED_SNAPSHOT
        self._generation = 0

    def get(self) -> ModelPolicySnapshot:
        return self._snapshot

    def get_generation(self) -> int:
        """The current write generation - see class docstring.

        Callers that intend to later call `set_if_current()` must capture
        this *immediately before* starting whatever read they're racing
        against (e.g. the DB query whose result they'll conditionally
        apply), not any earlier - see `main._model_policy_self_heal`.
        """
        return self._generation

    def set(self, snapshot: ModelPolicySnapshot) -> int:
        """Unconditional write - always wins, regardless of generation.

        This is deliberate, not an oversight: the only caller of `set()` is
        the admin `PUT` handler (`api.v1.admin.model_policy.put_model_policy`),
        and an admin write is this system's source of truth for the policy -
        it has just atomically upserted the authoritative row in Postgres
        (`services.set_policy`'s single `ON CONFLICT ... DO UPDATE`) and
        must always be reflected in the cache, never silently dropped
        because some other writer (self-heal) happened to be "further
        along". Guarding `set()` with a generation check would invert the
        priority this fix is trying to establish - see
        `set_if_current()`'s docstring and the design doc addendum for the
        full reasoning, including why two concurrent `PUT`s racing each
        other is a separate, pre-existing, not-in-scope concern.

        Returns the new generation, mirroring `set_if_current()`'s return
        shape for callers that want to chain a subsequent CAS off this
        write - no current caller needs this, but it costs nothing to
        expose.
        """
        self._snapshot = snapshot
        self._generation += 1
        return self._generation

    def set_if_current(self, snapshot: ModelPolicySnapshot, expected_generation: int) -> bool:
        """Compare-and-set: write only if no other write has landed since
        `expected_generation` was captured (via `get_generation()`).

        Returns `True` and applies the write if `expected_generation` still
        matches the current generation; returns `False` and leaves the
        cache untouched otherwise (some other writer - in practice, an
        admin `PUT` via `set()` - already superseded the value this caller
        was about to apply). The sole caller is
        `main._model_policy_self_heal`, closing the race where its own
        `load_policy_snapshot()` SELECT is in flight while a concurrent
        admin `PUT` commits and calls `set()` - without this guard,
        self-heal's stale read could silently overwrite the admin's
        just-committed, newer policy (security review finding, second
        round). Comparison-then-write happens with no `await` in between,
        so - same as `set()` - this is atomic under CPython's GIL/single-
        threaded event loop with no separate lock required.
        """
        if self._generation != expected_generation:
            return False
        self._snapshot = snapshot
        self._generation += 1
        return True


class UnknownModelInPolicyError(Exception):
    """Raised by `set_policy()` when the request's `models` list contains an
    entry that isn't a known `MODEL_REGISTRY` id.

    `message` lists every offending entry - these are caller-supplied model
    ids (not secret material), so this is safe to surface in a 422 response
    and to log.
    """

    def __init__(self, unknown_models: list[str]) -> None:
        message = (
            "Unknown model id(s) in policy: "
            + ", ".join(sorted(unknown_models))
            + ". Every entry must be a model id known to Gatekey's model registry."
        )
        super().__init__(message)
        self.message = message
        self.unknown_models = unknown_models


async def load_policy_snapshot(session: AsyncSession) -> ModelPolicySnapshot:
    """Query the org's policy row and build a `ModelPolicySnapshot` from it.

    Used at process startup only (to warm `ModelPolicyCache`, see
    `main.py`'s lifespan) and by `get_policy()` below - NEVER call this from
    a gateway route handler (AC-3a: the hot path must add zero DB round
    trips).
    """
    stmt = select(ModelPolicy).where(ModelPolicy.org_id == DEFAULT_ORG_ID)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        return _UNCONFIGURED_SNAPSHOT
    return ModelPolicySnapshot(mode=row.mode.value, models=frozenset(row.models))


async def get_policy(session: AsyncSession) -> ModelPolicySnapshot:
    """Fetch the org's current policy directly from the database.

    Used by the admin `GET` route (a control-plane read, not the AC-3a hot
    path) - it deliberately reads through to the DB rather than the
    in-process cache so it reflects the latest committed row even on a
    worker whose own cache happens to be stale (design doc section 2.4).
    """
    return await load_policy_snapshot(session)


async def set_policy(
    session: AsyncSession,
    mode: Literal["allowlist", "denylist"],
    models: list[str],
    *,
    self_hosted_cache: "SelfHostedModelRouteCache | None" = None,
    custom_model_cache: "CustomModelRouteCache | None" = None,
) -> ModelPolicySnapshot:
    """Validate then atomically full-replace-upsert the org's model policy.

    Raises `UnknownModelInPolicyError` if any entry in `models` isn't a
    known `MODEL_REGISTRY` id, isn't a currently-routable self-hosted model
    id, and isn't a currently-routable (verified) custom model name either;
    no database write happens in that case (AC-7).

    Phase 5 (5.5, AC5.5.6/design doc section 2.3(d)): `self_hosted_cache`
    widens the "known model" universe to `MODEL_REGISTRY.keys() |
    self_hosted_cache.known_model_ids()` - a verified self-hosted model id
    is addable/removable from the org baseline with no special-casing,
    exactly like any BYOK model. `self_hosted_cache=None` (every call site
    that doesn't thread it through, e.g. a unit test exercising only the
    static-registry path) preserves the byte-for-byte pre-Phase-5 behavior
    of validating against `MODEL_REGISTRY` alone.

    Custom Model Registry (CMR-5, technical design doc section 5 row 13):
    `custom_model_cache` widens the union a third way, identical mechanism,
    no special-casing - `MODEL_REGISTRY.keys() | self_hosted_cache.
    known_model_ids() | custom_model_cache.known_model_ids()`, each term
    conditional on its cache being non-`None`. `CustomModelRouteCache.
    known_model_ids()` only ever contains `verified=true` rows (see that
    class's docstring), so an unverified custom model's name is still
    rejected here exactly like any other unknown model id.

    The upsert itself is a single `INSERT ... ON CONFLICT (org_id) DO
    UPDATE` statement (not a read-then-write), mirroring
    `services.provider_keys.add_or_replace_key` - so two concurrent `PUT`s
    cannot interleave into a mixed `mode`/`models` pair, and there is no
    window where a partially-applied policy is visible (AC-8).

    Hardening pass item 2 (QA audit of every `on_conflict_do_update(...).
    returning(...)` call site for the same defect fixed in `services.
    residency.set_org_residency_rule`/`services.dlp.set_dlp_policy`, see
    those functions' docstrings for the full mechanism): `execution_options=
    {"populate_existing": True}` on the upsert's own `execute()` call below
    is added defensively, not because a live bug is currently reachable
    through it. `api.v1.admin.model_policy.put_model_policy` (the only
    caller today) does NOT pre-read the current row into this session's
    identity map before calling this function, so SQLAlchemy's ORM-enabled
    `RETURNING` has nothing stale to collide with right now. But that is an
    accident of this one caller's current shape, not a guarantee - a future
    caller that adds a pre-read (for an audit-entry `old_value`, exactly
    like every sibling policy-write route in this codebase already does)
    would silently reintroduce the identical enforcement-breaking bug with
    no test ever catching it until it broke in production, same as
    happened here. Cheap, harmless when unneeded, and closes that latent
    trap for good - same posture this hardening pass applied everywhere
    else this shape appears.
    """
    known_models = (
        MODEL_REGISTRY.keys()
        | (self_hosted_cache.known_model_ids() if self_hosted_cache is not None else frozenset())
        | (custom_model_cache.known_model_ids() if custom_model_cache is not None else frozenset())
    )
    unknown = set(models) - known_models
    if unknown:
        raise UnknownModelInPolicyError(sorted(unknown))

    dedup_models = sorted(set(models))
    insert_stmt = postgresql.insert(ModelPolicy).values(
        org_id=DEFAULT_ORG_ID, mode=mode, models=dedup_models
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[ModelPolicy.org_id],
        set_={
            "mode": insert_stmt.excluded.mode,
            "models": insert_stmt.excluded.models,
            "updated_at": func.now(),
        },
    ).returning(ModelPolicy)
    row = (
        await session.execute(upsert_stmt, execution_options={"populate_existing": True})
    ).scalar_one()
    await session.commit()
    return ModelPolicySnapshot(mode=row.mode.value, models=frozenset(row.models))


# ---------------------------------------------------------------------------
# Phase 2 (BD-12): team-level narrowing overlay (design doc section 4).
# The org-baseline layer above is untouched - zero behavior change for orgs
# that never adopt teams.
# ---------------------------------------------------------------------------


class TeamModelPolicyCache:
    """Process-local cache of every team's model-restriction overlay, keyed
    by `team_id`.

    Same lock-free, GIL-atomic "replace the whole snapshot, never mutate in
    place" contract as `ModelPolicyCache` - a full org realistically has low
    hundreds of teams at most, so building a new dict on any write is cheap
    and avoids partial-update races, the same simplicity trade
    `ModelPolicyCache` itself already makes. Warmed at startup with the
    identical bounded, fail-open pattern (see `main.py`'s lifespan): absence
    of a row for a team = no restriction beyond the org baseline, which is
    also the safe/permissive default an empty cache yields.

    Instantiated once per process and stored on `app.state` - never
    construct a second instance and thread it through separately.
    """

    def __init__(self, initial: dict[uuid.UUID, frozenset[str]] | None = None) -> None:
        self._snapshot: dict[uuid.UUID, frozenset[str]] = dict(initial or {})

    def get(self, team_id: uuid.UUID) -> frozenset[str] | None:
        """The team's allowed-model overlay, or None = no restriction row."""
        return self._snapshot.get(team_id)

    def set_all(self, snapshot: dict[uuid.UUID, frozenset[str]]) -> None:
        """Full replace - the startup-warm/self-heal write."""
        self._snapshot = dict(snapshot)

    def set_team(self, team_id: uuid.UUID, models: frozenset[str]) -> None:
        """Refresh one team's entry after a committed write - still a
        whole-snapshot replace (new dict, single reference assignment),
        never an in-place mutation of the live dict."""
        replacement = dict(self._snapshot)
        replacement[team_id] = models
        self._snapshot = replacement


@dataclass(frozen=True)
class ModelAccessDecision:
    """Outcome of the layered org-then-team model-access resolution.

    `blocking_layer` is None only when `allowed=True`. Phase 3 (AC4.1) adds
    `"content_classification"` here without reshaping this type, exactly the
    extension point Phase 2's design doc section 12 pre-flagged - see
    `resolve_content_classification` below.
    """

    allowed: bool
    blocking_layer: Literal["org", "team", "content_classification"] | None


def resolve_model_access(
    model: str,
    *,
    org_cache: ModelPolicyCache,
    team_cache: TeamModelPolicyCache,
    team_id: uuid.UUID | None,
) -> ModelAccessDecision:
    """Layered check: org baseline first, then the team's narrowing overlay
    (design doc section 4). Pure, synchronous, zero I/O - two in-process
    dict/frozenset lookups. `team_id=None` (legacy flat path) skips the team
    layer entirely."""
    if not org_cache.get().is_allowed(model):
        return ModelAccessDecision(allowed=False, blocking_layer="org")
    if team_id is not None:
        team_restriction = team_cache.get(team_id)
        if team_restriction is not None and model not in team_restriction:
            return ModelAccessDecision(allowed=False, blocking_layer="team")
    return ModelAccessDecision(allowed=True, blocking_layer=None)


class TeamModelRestrictsOrgDeniedModelError(GatekeyError):
    """AC3.2 defense-in-depth: a team restriction may only ever narrow the
    org baseline - listing an org-denied (or unknown) model is rejected with
    no DB write, mirroring `set_policy`'s `UnknownModelInPolicyError` shape.
    Model ids are caller input, not secret material - safe in `message`.
    """

    status_code = 422
    code = "team_model_restricts_org_denied_model"

    def __init__(self, offending_models: list[str]) -> None:
        super().__init__(
            "A team restriction can only narrow the org baseline - these "
            "model(s) are denied by (or unknown to) the org's model access "
            "policy: " + ", ".join(sorted(offending_models)) + "."
        )
        self.offending_models = offending_models


async def load_team_policy_snapshot(
    session: AsyncSession,
) -> dict[uuid.UUID, frozenset[str]]:
    """Query every team's restriction row - used at process startup only (to
    warm `TeamModelPolicyCache`, see `main.py`'s lifespan). NEVER call this
    from a gateway route handler (same zero-DB hot-path rule as
    `load_policy_snapshot`)."""
    rows = (await session.execute(select(TeamModelPolicy))).scalars().all()
    return {row.team_id: frozenset(row.models) for row in rows}


async def get_team_model_policy(
    session: AsyncSession, team_id: uuid.UUID
) -> frozenset[str] | None:
    """Fetch one team's restriction directly from the database (None = no
    restriction row). Control-plane read - deliberately reads through to the
    DB, not the cache, same as `get_policy`."""
    row = (
        await session.execute(
            select(TeamModelPolicy).where(TeamModelPolicy.team_id == team_id)
        )
    ).scalar_one_or_none()
    return None if row is None else frozenset(row.models)


async def set_team_model_policy(
    session: AsyncSession,
    team_id: uuid.UUID,
    models: list[str],
    *,
    cache: TeamModelPolicyCache | None = None,
    self_hosted_cache: "SelfHostedModelRouteCache | None" = None,
    custom_model_cache: "CustomModelRouteCache | None" = None,
) -> frozenset[str]:
    """Validate then atomically full-replace-upsert one team's restriction.

    AC3.2 defense-in-depth: re-fetches the CURRENT org baseline directly
    from the DB (not the cache - same "control-plane reads through to DB"
    precedent as `get_policy`) and rejects any entry that is unknown or
    org-denied with `TeamModelRestrictsOrgDeniedModelError` - no DB write in
    that case. The upsert itself is a single `INSERT ... ON CONFLICT
    (team_id) DO UPDATE`, mirroring `set_policy` exactly. When `cache` is
    provided, the committed value is pushed into it (the same
    write-then-refresh-cache pattern the org admin PUT route uses).

    Phase 5 (5.5, AC5.5.6/design doc section 2.3(d)): `self_hosted_cache`,
    like `set_policy`'s own parameter of the same name, widens "known
    model" to also accept a currently-routable self-hosted model id -
    `self_hosted_cache=None` preserves byte-for-byte pre-Phase-5 behavior.

    Custom Model Registry (CMR-5, technical design doc section 5 row 13):
    `custom_model_cache`, like `set_policy`'s own parameter of the same
    name, widens "known model" a third way to also accept a currently-
    routable (verified) custom model name - `custom_model_cache=None`
    preserves byte-for-byte pre-feature behavior.

    Hardening pass item 2 (QA audit finding the same defect already fixed in
    `services.residency.set_team_residency_rule`/`services.dlp.
    set_team_dlp_override` - see `services.residency.
    set_org_residency_rule`'s docstring for the full mechanism):
    `execution_options={"populate_existing": True}` on the upsert below is
    REQUIRED, not decorative, and IS live-triggered here.
    `api/v1/teams.py`'s `put_model_restrictions_endpoint` pre-reads the
    CURRENT restriction row (`get_team_model_policy(session, team_id)`, for
    its own audit-entry `old_value`) into this SAME session's identity map
    BEFORE calling this function. Without `populate_existing`, SQLAlchemy
    2.0's ORM-enabled `INSERT ... RETURNING` matches the returned row's
    primary key against that already-identity-mapped (stale, pre-update)
    object and returns it unchanged instead of the fresh post-update values
    on every UPDATE (not the first-ever INSERT for a team, which has no
    pre-existing identity-mapped object to collide with). That means
    `cache.set_team(team_id, committed)` below would silently re-arm
    `TeamModelPolicyCache` with the OLD, pre-tightening restriction on every
    subsequent write to an already-restricted team - `resolve_model_access()`
    (read on every single gateway request) would keep enforcing the OLD,
    more permissive restriction for the rest of this process's lifetime, a
    real enforcement-correctness bug (e.g. a model removed from a team's
    allowlist would silently remain reachable through that team).
    """
    known_models = (
        MODEL_REGISTRY.keys()
        | (self_hosted_cache.known_model_ids() if self_hosted_cache is not None else frozenset())
        | (custom_model_cache.known_model_ids() if custom_model_cache is not None else frozenset())
    )
    org_snapshot = await load_policy_snapshot(session)
    offending = sorted(
        {m for m in models if m not in known_models or not org_snapshot.is_allowed(m)}
    )
    if offending:
        raise TeamModelRestrictsOrgDeniedModelError(offending)

    dedup_models = sorted(set(models))
    insert_stmt = postgresql.insert(TeamModelPolicy).values(
        team_id=team_id, models=dedup_models
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[TeamModelPolicy.team_id],
        set_={"models": insert_stmt.excluded.models, "updated_at": func.now()},
    ).returning(TeamModelPolicy)
    row = (
        await session.execute(upsert_stmt, execution_options={"populate_existing": True})
    ).scalar_one()
    await session.commit()
    committed = frozenset(row.models)
    if cache is not None:
        cache.set_team(team_id, committed)
    return committed


# ---------------------------------------------------------------------------
# Phase 3 (BD-5): content-aware routing (design doc section 1.7/3.4 of the
# product spec, AC4.1-AC4.5). Org-wide only (AC4.2 - no team-level override).
#
# Phase 5 (5.3, AC5.3.1-AC5.3.2): all four categories ("pii", "financial_
# data", "source_code", "legal") are now wired to a real classifier signal
# (`services.dlp.py`'s `category_findings`, generalized from the Phase-3-only
# `pii_detected: bool`) - see `resolve_content_classification` below.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContentAwareRuleSnapshot:
    enabled: bool
    allowed_models: frozenset[str]


class ContentAwareRuleCache:
    """Process-local cache of every category's rule, keyed by `category`.

    Same lock-free, GIL-atomic "replace the whole snapshot, never mutate in
    place" contract as `ModelPolicyCache`/`TeamModelPolicyCache`.
    Instantiated once per process and stored on `app.state` - never
    construct a second instance and thread it through separately.
    """

    def __init__(self, initial: dict[str, ContentAwareRuleSnapshot] | None = None) -> None:
        self._snapshot: dict[str, ContentAwareRuleSnapshot] = dict(initial or {})

    def get(self, category: str) -> ContentAwareRuleSnapshot | None:
        return self._snapshot.get(category)

    def set_all(self, snapshot: dict[str, ContentAwareRuleSnapshot]) -> None:
        """Full replace - the startup-warm write."""
        self._snapshot = dict(snapshot)

    def set_category(self, category: str, snapshot: ContentAwareRuleSnapshot) -> None:
        replacement = dict(self._snapshot)
        replacement[category] = snapshot
        self._snapshot = replacement


def resolve_content_classification(
    model: str, *, cache: ContentAwareRuleCache, category_findings: frozenset[str]
) -> ModelAccessDecision:
    """AC4.3/AC5.3.2: applies AFTER the static org/team baseline has already
    allowed `model` - this function only ever further restricts, it cannot
    re-enable a statically-blocked model (the caller only reaches this step
    once `check_model_policy` has already passed for the same model - see
    `api.v1.gateway.common`'s pipeline ordering).

    Phase 5 (5.3, AC5.3.2): generalized from a single `pii_detected: bool`
    parameter to `category_findings: frozenset[str]` - for every ENABLED
    `content_aware_rules` row whose category is in `category_findings`, the
    effective allowed-models set is the INTERSECTION of every matched
    category's `allowed_models`. A request matching multiple enabled
    categories with disjoint allowed-models sets can therefore end up with
    an empty intersection - `model not in frozenset()` is unconditionally
    `True`, so this falls out of the plain membership check with no
    special-casing needed (AC4.4/AC5.3.2's "empty allowed_models blocks
    everything in that category" behavior generalizes for free to "empty
    INTERSECTION blocks everything the matched categories jointly permit").

    This is a strict generalization of the pre-Phase-5 single-category
    check, not a behavior change for any existing caller: called with
    `category_findings=frozenset({"pii"})` (what every pre-Phase-5 caller
    effectively meant by `pii_detected=True`), the loop below degenerates
    to exactly the old `if pii_detected: rule = cache.get("pii"); ...` body.
    `category_findings=frozenset()` (the old `pii_detected=False`) never
    enters the loop, matching the old unconditional "allowed" fall-through.

    Pure, synchronous, zero I/O."""
    effective_allowed: frozenset[str] | None = None
    for category in category_findings:
        rule = cache.get(category)
        if rule is not None and rule.enabled:
            effective_allowed = (
                rule.allowed_models if effective_allowed is None else effective_allowed & rule.allowed_models
            )
    if effective_allowed is None:
        return ModelAccessDecision(allowed=True, blocking_layer=None)
    if model not in effective_allowed:
        return ModelAccessDecision(allowed=False, blocking_layer="content_classification")
    return ModelAccessDecision(allowed=True, blocking_layer=None)


# ---------------------------------------------------------------------------
# Phase 4 (Fix 5, security review finding - config-time half): a graceful
# degradation policy's `downgrade_target_model` must itself be permitted by
# the org's (and, for a team-scoped policy, that team's) model access
# policy - otherwise an Org Admin or Team Lead could configure degradation
# to silently reroute budget-proximity traffic to a model that same admin
# surface has separately denied. See `api.v1.admin.degradation_policy`'s two
# PUT handlers (the only callers) and `api.v1.gateway.chat`'s request-time
# re-validation (the other half - policy can still be tightened AFTER this
# check ran, so this alone is not sufficient).
# ---------------------------------------------------------------------------


class DowngradeTargetModelNotAllowedError(GatekeyError):
    """Raised by `validate_downgrade_target_model()` below - 422, no DB
    write on this path, mirroring `TeamModelRestrictsOrgDeniedModelError`'s
    shape. `model` is caller input, not secret material - safe in
    `message`."""

    status_code = 422
    code = "downgrade_target_model_not_allowed"

    def __init__(self, model: str) -> None:
        super().__init__(
            f"downgrade_target_model '{model}' is not permitted by the current model access "
            "policy - it is either unknown to Gatekey's model registry, denied by the org's "
            "model access policy, or (for a team-scoped degradation policy) excluded by that "
            "team's own model-access restriction. Choose a model your org/team policy actually "
            "allows."
        )
        self.model = model


async def validate_downgrade_target_model(
    session: AsyncSession, model: str, *, team_id: uuid.UUID | None = None
) -> None:
    """Validate that `model` (a degradation policy's `downgrade_target_
    model`) is actually permitted by the org baseline and, when `team_id`
    is given, that team's own narrowing overlay too.

    Reads directly through to the database (not the process-wide cache) -
    same "control-plane write validates against the live DB row" precedent
    `set_team_model_policy()` already uses for the identical narrowing-
    invariant check; this runs on an admin `PUT`, not the hot gateway
    path, so the extra round trip is the correct trade-off here (see
    `services.model_policy` module docstring's `get_policy`/control-plane
    read convention).

    Raises `DowngradeTargetModelNotAllowedError` (422) if `model` is
    unknown to `MODEL_REGISTRY`, denied by the org's policy, or (when
    `team_id` is given) excluded by that team's own restriction overlay.
    No DB write happens in that case - call this BEFORE the caller's own
    upsert.
    """
    org_snapshot = await load_policy_snapshot(session)
    if model not in MODEL_REGISTRY or not org_snapshot.is_allowed(model):
        raise DowngradeTargetModelNotAllowedError(model)
    if team_id is not None:
        team_restriction = await get_team_model_policy(session, team_id)
        if team_restriction is not None and model not in team_restriction:
            raise DowngradeTargetModelNotAllowedError(model)


async def load_content_aware_rule_snapshot(session: AsyncSession) -> dict[str, ContentAwareRuleSnapshot]:
    """Query every content-aware rule row - used at process startup only (to
    warm `ContentAwareRuleCache`, see `main.py`'s lifespan). NEVER call this
    from a gateway route handler (same zero-DB hot-path rule as
    `load_policy_snapshot`)."""
    rows = (
        await session.execute(select(ContentAwareRule).where(ContentAwareRule.org_id == DEFAULT_ORG_ID))
    ).scalars().all()
    return {
        row.category: ContentAwareRuleSnapshot(
            enabled=row.enabled, allowed_models=frozenset(row.allowed_models)
        )
        for row in rows
    }


async def get_content_aware_rules(session: AsyncSession) -> list[ContentAwareRule]:
    """Control-plane read (admin `GET`) - reads the DB directly, not the
    cache, same "reflect the latest committed row" precedent as
    `get_policy`."""
    stmt = (
        select(ContentAwareRule)
        .where(ContentAwareRule.org_id == DEFAULT_ORG_ID)
        .order_by(ContentAwareRule.category)
    )
    return list((await session.execute(stmt)).scalars().all())


async def set_content_aware_rule(
    session: AsyncSession,
    category: str,
    *,
    enabled: bool,
    allowed_models: list[str],
    cache: ContentAwareRuleCache | None = None,
) -> ContentAwareRule:
    """Full-replace upsert of one category's rule (composite `(org_id,
    category)` PK - `db/models/content_aware_rule.py`). `allowed_models` is
    NOT validated against `MODEL_REGISTRY`: unlike `ModelPolicy`/
    `TeamModelPolicy`, an admin listing a model id Gatekey doesn't currently
    know about here is harmless (it just never matches any real route) and
    future-proofs a rule authored before a model is registered - no
    narrowing/subset invariant applies to this table (AC4.2: org-wide only,
    nothing to narrow against).

    Hardening pass item 2 (QA audit finding the same defect already fixed in
    `services.residency.set_org_residency_rule`/`services.dlp.
    set_dlp_policy` - see that function's docstring for the full mechanism):
    `execution_options={"populate_existing": True}` on the upsert below is
    REQUIRED, not decorative, and IS live-triggered here.
    `api/v1/admin/content_aware_rules.py`'s `put_content_aware_rules_
    endpoint` pre-reads every CURRENT rule row (`get_content_aware_rules
    (session)`, for its own per-category audit-entry `old_value`) into this
    SAME session's identity map BEFORE calling this function, once per
    category in the payload. Without `populate_existing`, SQLAlchemy 2.0's
    ORM-enabled `INSERT ... RETURNING` matches the returned row's primary
    key against that already-identity-mapped (stale, pre-update) object and
    returns it unchanged instead of the fresh post-update values on every
    UPDATE to a category that already had a row (not the first-ever INSERT
    for that category). That means `cache.set_category(...)` below would
    silently re-arm `ContentAwareRuleCache` with the OLD, pre-tightening
    rule - `resolve_content_classification()` (read on every single gateway
    request) would keep enforcing the OLD, more permissive allowed-models
    set for the rest of this process's lifetime, a real
    enforcement-correctness bug (e.g. a model removed from a category's
    allowlist would silently remain reachable for content matching that
    category)."""
    dedup_models = sorted(set(allowed_models))
    insert_stmt = postgresql.insert(ContentAwareRule).values(
        org_id=DEFAULT_ORG_ID, category=category, enabled=enabled, allowed_models=dedup_models
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[ContentAwareRule.org_id, ContentAwareRule.category],
        set_={
            "enabled": insert_stmt.excluded.enabled,
            "allowed_models": insert_stmt.excluded.allowed_models,
            "updated_at": func.now(),
        },
    ).returning(ContentAwareRule)
    row = (
        await session.execute(upsert_stmt, execution_options={"populate_existing": True})
    ).scalar_one()
    await session.commit()
    if cache is not None:
        cache.set_category(
            category, ContentAwareRuleSnapshot(enabled=row.enabled, allowed_models=frozenset(row.allowed_models))
        )
    return row
