"""In-process cache and DB-backed service for data-residency rules (Phase 3
- Security & Compliance Hardening).

See `docs/design/phase-3-security-compliance-design.md` section 3 for the
full design rationale. Residency is a routing-eligibility concern (can this
request reach this endpoint's region at all) - a deliberately SEPARATE check
from `services.model_policy.resolve_model_access`, not a fourth
`ModelAccessDecision.blocking_layer` value (section 3.2).

Every function in this module operates against `constants.DEFAULT_ORG_ID`
only - see that module's docstring (mirrors `services/model_policy.py` and
every other single-org service in this codebase).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from sqlalchemy import CursorResult, delete, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.residency_rule import ResidencyRule
from gatekey.errors import GatekeyError
from gatekey.providers.model_registry import ModelRoute

if TYPE_CHECKING:
    # Local, TYPE_CHECKING-only import to avoid a circular import at runtime
    # (`services.response_cache` itself imports `services.residency` at
    # module level, for `resolve_model_region` - see that module's
    # docstring) - see `set_org_residency_rule`/`set_team_residency_rule`/
    # `delete_org_residency_rule`/`delete_team_residency_rule`'s new
    # `cache_invalidator` parameter (Fix 3).
    from gatekey.services.response_cache import CacheInvalidator

logger = logging.getLogger("gatekey")

ViolationBehavior = Literal["hard_block", "warn"]

# Design doc section 3.1. Deliberately a small, fixed set (not a DB table -
# no per-org region catalog exists) - `allowed_regions` on every rule is
# validated against this at write time.
SUPPORTED_REGIONS = frozenset({"us", "eu", "apac"})

# Static, non-admin-configurable regions for multi-tenant cloud APIs whose
# hosting region Gatekey has no way to change per-org. `openrouter` is
# deliberately absent - it aggregates arbitrary backend providers/regions
# with no single knowable region (see `resolve_model_region` below).
_PROVIDER_STATIC_REGION: dict[str, str] = {
    "openai": "us",
    "anthropic": "us",
}

# Vertex AI location prefix (the part before the first '-', e.g. "us" from
# "us-central1") -> coarse region. Deliberately narrow: only prefixes this
# codebase can confidently map into one of `SUPPORTED_REGIONS` are listed;
# anything else (e.g. "me-west1") falls through to `None` ("unknown") rather
# than guessing.
_GCP_LOCATION_PREFIX_TO_REGION: dict[str, str] = {
    "us": "us",
    "northamerica": "us",
    "southamerica": "us",
    "europe": "eu",
    "asia": "apac",
    "australia": "apac",
}


def coarsen_gcp_location(location: str) -> str | None:
    """Map a Vertex AI location (e.g. 'us-central1', 'europe-west4',
    'asia-southeast1') to one of `SUPPORTED_REGIONS`. Unrecognized prefixes
    return None ('unknown') rather than guessing - an unrecognized location
    is treated exactly like a provider with no configured region at all
    (see `resolve_model_region`): it satisfies no allowlist and is blocked
    by any active hard-block rule, never silently passed through."""
    prefix = location.split("-", 1)[0].lower()
    return _GCP_LOCATION_PREFIX_TO_REGION.get(prefix)


def resolve_model_region(route: ModelRoute, provider_key_metadata: dict | None) -> str | None:
    """Region resolution, by provider (design doc section 1.13):

    - vertex_ai: `provider_key_metadata["location"]`, coarsened. None if no
      key is configured yet (nothing to route to anyway).
    - ollama: `provider_key_metadata["region"]` (the admin-settable field
      added to the same non-secret `key_metadata` column, see
      `schemas.provider_key.OllamaKeyRequest`) verbatim, if it is one of
      `SUPPORTED_REGIONS`. None if the operator never set it - a residency
      rule blocks self-hosted traffic by default until an admin explicitly
      tags its region, matching hard-block-by-default's own intent.
    - openrouter: always None - an aggregator with no single knowable
      region (deliberate, not a gap - see design doc section 12's
      forward-looking flag).
    - openai/anthropic: the static lookup above.
    """
    if route.provider == "vertex_ai":
        if provider_key_metadata is None:
            return None
        location = provider_key_metadata.get("location")
        return coarsen_gcp_location(location) if location else None
    if route.provider == "ollama":
        if provider_key_metadata is None:
            return None
        region = provider_key_metadata.get("region")
        return region if region in SUPPORTED_REGIONS else None
    if route.provider == "openrouter":
        return None
    return _PROVIDER_STATIC_REGION.get(route.provider)


# ---------------------------------------------------------------------------
# Enforcement (design doc section 3.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResidencyDecision:
    allowed: bool  # False only when violation_behavior == "hard_block"
    violated: bool  # True on ANY rule violation, hard_block or warn
    behavior: ViolationBehavior | None
    region: str | None  # the resolved region, for the audit-entry write


@dataclass(frozen=True)
class ResidencyRuleSnapshot:
    allowed_regions: frozenset[str]
    violation_behavior: ViolationBehavior


class ResidencyRuleCache:
    """Process-local cache of the org-wide rule + every team's rule.

    Same lock-free, GIL-atomic "replace the whole snapshot, never mutate in
    place" contract as `services.model_policy.ModelPolicyCache`/
    `TeamModelPolicyCache` - see those classes' docstrings for the full
    rationale. Instantiated once per process and stored on `app.state` -
    never construct a second instance and thread it through separately.
    """

    def __init__(
        self,
        org_rule: ResidencyRuleSnapshot | None = None,
        team_rules: dict[uuid.UUID, ResidencyRuleSnapshot] | None = None,
    ) -> None:
        self._org_rule = org_rule
        self._team_rules: dict[uuid.UUID, ResidencyRuleSnapshot] = dict(team_rules or {})

    def get_org_rule(self) -> ResidencyRuleSnapshot | None:
        return self._org_rule

    def get_team_rule(self, team_id: uuid.UUID) -> ResidencyRuleSnapshot | None:
        return self._team_rules.get(team_id)

    def set_all(
        self,
        org_rule: ResidencyRuleSnapshot | None,
        team_rules: dict[uuid.UUID, ResidencyRuleSnapshot],
    ) -> None:
        """Full replace - the startup-warm write."""
        self._org_rule = org_rule
        self._team_rules = dict(team_rules)

    def set_org_rule(self, rule: ResidencyRuleSnapshot | None) -> None:
        self._org_rule = rule

    def set_team_rule(self, team_id: uuid.UUID, rule: ResidencyRuleSnapshot | None) -> None:
        """Refresh one team's entry after a committed write - still a
        whole-dict replace (new dict, single reference assignment), never an
        in-place mutation of the live dict. `rule=None` removes the entry
        (a deleted team rule)."""
        replacement = dict(self._team_rules)
        if rule is None:
            replacement.pop(team_id, None)
        else:
            replacement[team_id] = rule
        self._team_rules = replacement


def resolve_residency(
    region: str | None, *, cache: ResidencyRuleCache, team_id: uuid.UUID | None
) -> ResidencyDecision:
    """Cumulative check: the ORG rule (if configured) AND the team rule (if
    one exists) are BOTH evaluated on every read - mirrors `services.
    model_policy.resolve_model_access`'s org-then-team cumulative pattern,
    not "checked once at write time, trusted forever after."

    This module used to check only the innermost configured rule (team if
    present, else org), on the theory that `set_team_residency_rule`'s
    write-time narrowing check made that provably equivalent to checking
    both. That equivalence silently breaks the moment the ORG rule is
    tightened AFTER a team rule was already validated as a narrower subset
    of the OLD org rule: e.g. org allows [us, eu] -> team narrows to [eu]
    (valid then) -> org is later tightened to [us] only. Under
    innermost-only resolution the team's now-too-wide `eu` rule is never
    re-checked against the new org value, so the team keeps routing to `eu`
    indefinitely with zero error and zero audit signal - silently defeating
    the org's tightened policy (security review finding, Phase 3). Checking
    every enabled layer on every read closes that staleness gap; the
    write-time narrowing check (`set_team_residency_rule`'s AC3.3) stays in
    place as cheap defense-in-depth, it just isn't relied on ALONE for
    correctness anymore.

    A layer that is satisfied never downgrades a violation an earlier layer
    already recorded; a `hard_block` violation from either layer always
    outranks a `warn` from the other, regardless of check order (a narrower
    team rule enforces even when the org rule alone would have passed, and
    vice versa). `region=None` ('unknown', e.g. an unconfigured Ollama
    instance or OpenRouter) satisfies no allowlist - an active rule always
    treats it as a violation. Pure, synchronous, zero I/O - two in-process
    dict lookups, same cost class as the innermost-only version this
    replaces."""
    worst_behavior: ViolationBehavior | None = None
    for rule in (cache.get_org_rule(), cache.get_team_rule(team_id) if team_id is not None else None):
        if rule is None:
            continue
        if region is not None and region in rule.allowed_regions:
            continue  # this layer is satisfied - does not clear a prior violation
        if worst_behavior != "hard_block":
            worst_behavior = rule.violation_behavior

    if worst_behavior is None:
        return ResidencyDecision(allowed=True, violated=False, behavior=None, region=region)
    return ResidencyDecision(
        allowed=worst_behavior == "warn", violated=True, behavior=worst_behavior, region=region
    )


# ---------------------------------------------------------------------------
# DB-backed CRUD (admin API + cache warmup)
# ---------------------------------------------------------------------------


class InvalidResidencyRegionError(GatekeyError):
    """`allowed_regions` contains an entry outside `SUPPORTED_REGIONS`, or is
    empty. Region names are caller input, not secret material - safe in
    `message`."""

    status_code = 422
    code = "invalid_residency_region"

    def __init__(self, offending: frozenset[str]) -> None:
        if offending:
            detail = "unrecognized region(s): " + ", ".join(sorted(offending))
        else:
            detail = "allowed_regions must not be empty"
        super().__init__(
            f"{detail}. Supported regions: {', '.join(sorted(SUPPORTED_REGIONS))}."
        )


class ResidencyRuleWidensOrgRuleError(GatekeyError):
    """AC3.3 defense-in-depth: a team rule may only ever narrow the org-wide
    rule's `allowed_regions` - listing a region the org rule doesn't allow
    is rejected with no DB write, mirroring `services.model_policy.
    TeamModelRestrictsOrgDeniedModelError`."""

    status_code = 422
    code = "residency_rule_widens_org_rule"

    def __init__(self, offending: frozenset[str]) -> None:
        super().__init__(
            "A team residency rule can only narrow the org-wide rule - these "
            "region(s) are not allowed by the org rule: " + ", ".join(sorted(offending)) + "."
        )


def _validate_regions(allowed_regions: list[str]) -> frozenset[str]:
    regions = frozenset(allowed_regions)
    offending = regions - SUPPORTED_REGIONS
    if offending or not regions:
        raise InvalidResidencyRegionError(offending)
    return regions


def _snapshot_from_row(row: ResidencyRule) -> ResidencyRuleSnapshot:
    return ResidencyRuleSnapshot(
        allowed_regions=frozenset(row.allowed_regions), violation_behavior=row.violation_behavior.value
    )


async def load_residency_rule_snapshot(
    session: AsyncSession,
) -> tuple[ResidencyRuleSnapshot | None, dict[uuid.UUID, ResidencyRuleSnapshot]]:
    """Query every residency rule row - used at process startup only (to
    warm `ResidencyRuleCache`, see `main.py`'s lifespan). NEVER call this
    from a gateway route handler (same zero-DB hot-path rule as
    `services.model_policy.load_policy_snapshot`)."""
    rows = (await session.execute(select(ResidencyRule))).scalars().all()
    org_rule: ResidencyRuleSnapshot | None = None
    team_rules: dict[uuid.UUID, ResidencyRuleSnapshot] = {}
    for row in rows:
        if row.scope_team_id is None:
            org_rule = _snapshot_from_row(row)
        else:
            team_rules[row.scope_team_id] = _snapshot_from_row(row)
    return org_rule, team_rules


async def get_org_residency_rule(session: AsyncSession) -> ResidencyRule | None:
    stmt = select(ResidencyRule).where(
        ResidencyRule.org_id == DEFAULT_ORG_ID, ResidencyRule.scope_team_id.is_(None)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_team_residency_rule(session: AsyncSession, team_id: uuid.UUID) -> ResidencyRule | None:
    stmt = select(ResidencyRule).where(ResidencyRule.scope_team_id == team_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def set_org_residency_rule(
    session: AsyncSession,
    *,
    allowed_regions: list[str],
    violation_behavior: ViolationBehavior,
    cache: ResidencyRuleCache | None = None,
    cache_invalidator: "CacheInvalidator | None" = None,
) -> ResidencyRule:
    """Validate then atomically full-replace-upsert the org-wide rule.

    AC3.2: `violation_behavior` is a required, explicit argument - there is
    no "create with the column default" path here, so a client cannot
    accidentally omit it (see `schemas` for the request-body-level
    enforcement of this too).

    Fix 3 (security review, BLOCKING): an org-wide residency rule change can
    make a response that was cached under the OLD (more permissive) policy
    no longer valid to serve - `api.v1.gateway.common.check_response_cache()`
    returns a cache HIT before `check_residency()` ever runs, on the
    assumption that a cached entry was only ever written for a request that
    already passed residency at write time. That assumption breaks the
    moment policy is tightened after the write, for up to `cache_ttl_
    minutes` (max 24h). Rather than re-running residency on every cache hit
    (which would defeat the point of caching), an org-level policy change
    invalidates every cached entry ORG-WIDE (`cache_invalidator.clear_all()`
    - a residency rule has no narrower "just this team" blast radius to
    scope to, since it can affect any team). Best-effort: `CacheInvalidator`
    methods already fail open (catch, log, return 0) on a Redis-unreachable
    store - never raise, never block this write itself.

    Hardening pass item 1 (found while writing a real end-to-end test of
    this exact wiring, not by code reading): `execution_options={
    "populate_existing": True}` on the upsert's own `execute()` call is
    REQUIRED, not decorative. `api.v1.admin.residency_rules.py`'s PUT
    handler reads the CURRENT row (`get_org_residency_rule(session)`, for
    its audit-entry `old_value`) into this SAME session's identity map
    BEFORE calling this function. SQLAlchemy 2.0's ORM-enabled `INSERT
    ... RETURNING` then matches the returned row's primary key against
    that already-identity-mapped (stale, pre-update) object and - by
    documented default - returns THAT stale object unchanged rather than
    the fresh post-update values, unless `populate_existing` is set.
    Without this fix, `cache.set_org_rule(_snapshot_from_row(row))` below
    would silently re-arm the in-process `ResidencyRuleCache` with the
    OLD, pre-tightening rule on every UPDATE (not the first-ever INSERT,
    which has no pre-existing identity-mapped object to collide with) -
    meaning a tightened residency rule would correctly invalidate the
    Redis response cache (so no stale cached RESPONSE is ever served) but
    the very next live request's `check_residency()` call would still
    silently enforce the OLD, more permissive rule, for the rest of this
    process's lifetime (until the next full cache warm). A real
    enforcement-correctness bug, not just a cache-staleness one - caught
    only because this hardening pass's new test drives a real SECOND write
    through the real HTTP endpoint (not just a bare service-function unit
    test with no pre-existing identity-mapped row).
    """
    regions = _validate_regions(allowed_regions)
    insert_stmt = postgresql.insert(ResidencyRule).values(
        org_id=DEFAULT_ORG_ID,
        scope_team_id=None,
        allowed_regions=sorted(regions),
        violation_behavior=violation_behavior,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[ResidencyRule.org_id],
        index_where=text("scope_team_id IS NULL"),
        set_={
            "allowed_regions": insert_stmt.excluded.allowed_regions,
            "violation_behavior": insert_stmt.excluded.violation_behavior,
            "updated_at": text("now()"),
        },
    ).returning(ResidencyRule)
    row = (
        await session.execute(upsert_stmt, execution_options={"populate_existing": True})
    ).scalar_one()
    await session.commit()
    if cache is not None:
        cache.set_org_rule(_snapshot_from_row(row))
    if cache_invalidator is not None:
        await cache_invalidator.clear_all()
    return row


async def set_team_residency_rule(
    session: AsyncSession,
    team_id: uuid.UUID,
    *,
    allowed_regions: list[str],
    violation_behavior: ViolationBehavior,
    cache: ResidencyRuleCache | None = None,
    cache_invalidator: "CacheInvalidator | None" = None,
) -> ResidencyRule:
    """Validate (including AC3.3 narrowing-only defense-in-depth) then
    atomically full-replace-upsert one team's rule.

    Re-reads the CURRENT org rule directly from the DB (not the cache - same
    "control-plane reads through to DB" precedent as `services.model_policy.
    set_team_model_policy`) and rejects any region not allowed by it with
    `ResidencyRuleWidensOrgRuleError` - no DB write in that case. Absence of
    an org rule means "unrestricted", so any (validated) region set is
    accepted.

    Fix 3 (security review, BLOCKING) - see `set_org_residency_rule`'s
    docstring for the full rationale; a TEAM rule change only needs to
    invalidate that team's own cached entries (`cache_invalidator.
    clear_team(team_id)`), not every team's.

    Hardening pass item 1: `execution_options={"populate_existing": True}`
    on the upsert below is REQUIRED for the same reason as `set_org_
    residency_rule`'s identical fix (see that function's docstring) -
    `api/v1/teams.py`'s PUT handler for this route also pre-reads the
    current row (`get_team_residency_rule`) into this session's identity
    map before calling this function.
    """
    regions = _validate_regions(allowed_regions)
    org_row = await get_org_residency_rule(session)
    if org_row is not None:
        offending = regions - frozenset(org_row.allowed_regions)
        if offending:
            raise ResidencyRuleWidensOrgRuleError(offending)

    insert_stmt = postgresql.insert(ResidencyRule).values(
        org_id=DEFAULT_ORG_ID,
        scope_team_id=team_id,
        allowed_regions=sorted(regions),
        violation_behavior=violation_behavior,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[ResidencyRule.scope_team_id],
        index_where=text("scope_team_id IS NOT NULL"),
        set_={
            "allowed_regions": insert_stmt.excluded.allowed_regions,
            "violation_behavior": insert_stmt.excluded.violation_behavior,
            "updated_at": text("now()"),
        },
    ).returning(ResidencyRule)
    row = (
        await session.execute(upsert_stmt, execution_options={"populate_existing": True})
    ).scalar_one()
    await session.commit()
    if cache is not None:
        cache.set_team_rule(team_id, _snapshot_from_row(row))
    if cache_invalidator is not None:
        await cache_invalidator.clear_team(team_id)
    return row


async def delete_org_residency_rule(
    session: AsyncSession,
    *,
    cache: ResidencyRuleCache | None = None,
    cache_invalidator: "CacheInvalidator | None" = None,
) -> bool:
    stmt = delete(ResidencyRule).where(
        ResidencyRule.org_id == DEFAULT_ORG_ID, ResidencyRule.scope_team_id.is_(None)
    )
    result = cast(CursorResult, await session.execute(stmt))
    await session.commit()
    deleted = result.rowcount > 0
    if deleted and cache is not None:
        cache.set_org_rule(None)
    # Fix 3: removing a residency rule can also change which cached
    # responses are still valid to serve (a rule's absence means
    # "unrestricted", so this specific direction is actually safe - but
    # invalidating unconditionally on ANY residency-rule write, add or
    # remove, is the simpler and more robust posture than trying to reason
    # about which direction of change is "safe" - see `set_org_residency_
    # rule`'s docstring for the full rationale).
    if deleted and cache_invalidator is not None:
        await cache_invalidator.clear_all()
    return deleted


async def delete_team_residency_rule(
    session: AsyncSession,
    team_id: uuid.UUID,
    *,
    cache: ResidencyRuleCache | None = None,
    cache_invalidator: "CacheInvalidator | None" = None,
) -> bool:
    stmt = delete(ResidencyRule).where(ResidencyRule.scope_team_id == team_id)
    result = cast(CursorResult, await session.execute(stmt))
    await session.commit()
    deleted = result.rowcount > 0
    if deleted and cache is not None:
        cache.set_team_rule(team_id, None)
    if deleted and cache_invalidator is not None:
        await cache_invalidator.clear_team(team_id)
    return deleted
