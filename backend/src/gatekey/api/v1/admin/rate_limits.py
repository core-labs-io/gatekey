"""Admin endpoints for rate limiting configuration (Phase 4, Reliability & Cost Efficiency).

`router` (org-level collection, `/v1/admin/rate-limit-rules[/{id}]`) is
gated by `require_admin` at the router level (see `gatekey/api/deps.py`) -
same posture as `caching_settings.py`. Org Admin only - the scope of a rule
created here is chosen via the request body (`scope_team_id`/
`scope_user_id`), including `org_default_per_user`, which only an Org Admin
may ever configure.

`team_router` (`/v1/admin/teams/{team_id}/rate-limit-rules[/{id}]`) is the
Team-Lead-accessible counterpart the product spec's RBAC table also
requires ("As an Org Admin or Team Lead, I can configure per-team ... rate
limits", phase-4-product-spec.md section 2/4) - deliberately a SEPARATE
router with NO router-level `require_admin` dependency (a route-level
dependency cannot override/bypass a router-level one - FastAPI runs both -
so this had to be a distinct router, not an extra dependency bolted onto
`router` above), gated per-route instead by `require_team_role` (Org Admin
OR that team's own Team Lead - the SAME dependency `api/v1/teams.py` uses
for every other team-scoped admin surface, and the one `api/v1/admin/
degradation_policy.py`'s team-scoped routes now also use - see that
module's docstring for why its team routes previously did NOT correctly
enforce this despite living under `/v1/admin/teams/{team_id}/...`). Every
route here forces `scope_team_id={team_id}`/`scope_type=team` from the URL
path, never from the request body, so a Team Lead can never target a
different team's rule through this surface (see `TeamRateLimitRuleCreate`'s
docstring) - and `PUT`/`DELETE` additionally verify the target rule is
actually owned by `{team_id}` before touching it (`_get_team_owned_rule_or_
404`), returning the same generic 404 whether the rule doesn't exist at all
or belongs to someone else's team/scope (anti-enumeration, mirroring
`require_team_role`'s own posture).

Both routers share the row-creation/update/delete logic
(`_insert_rate_limit_rule`/`_update_rate_limit_rule_row`/
`_delete_rate_limit_rule_row` below) - the team routes are thin wrappers
that resolve/authorize the target team+rule and then delegate, never a
second independent implementation of the CRUD mechanics.

Fix 6 (NFR gap, AC4.3.4): the gateway hot path (`api.v1.gateway.common.
check_rate_limit()`) now reads `RateLimitCache` off `app.state` instead of
`load_effective_rate_limit_rules()`'s live DB read - every mutation path
below (`_insert_rate_limit_rule`/`_update_rate_limit_rule_row`/`_delete_
rate_limit_rule_row`) refreshes (or clears) the relevant cache slot
immediately after its own commit, branching on the row's own `scope_type`
(`services.rate_limit.snapshot_from_rule`/the cache's
`set_org_rule`/`set_team_rule`/`set_user_rule`), same write-then-refresh-
cache pattern `caching_settings.py`/`degradation_policy.py` use.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, model_validator
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import (
    TeamRoleContext,
    get_rate_limit_cache,
    get_shared_state_store,
    require_admin,
    require_team_role,
)
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.rate_limit_rule import RateLimitOnLimit, RateLimitRule, RateLimitScopeType
from gatekey.db.session import get_db_session
from gatekey.errors import GatekeyError, NotFoundError
from gatekey.services import rate_limit as rate_limit_service
from gatekey.services.rate_limit import RateLimitCache
from gatekey.services.shared_state import SharedStateStore

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin", "rate_limits"],
    dependencies=[Depends(require_admin)],
)

team_router = APIRouter(
    prefix="/v1/admin/teams",
    tags=["admin", "rate_limits", "team"],
)

# ============================================================================
# Request/Response Schemas
# ============================================================================

# `on_limit` on the wire uses `reject`/`queue_and_retry` (AC4.2.3's exact
# naming) while the DB enum's second value is `queue_retry`
# (`RateLimitOnLimit.QUEUE_RETRY`, migration `0026`) - translate both ways
# at the API boundary rather than changing the DB enum (schema is frozen).
_ON_LIMIT_WIRE_TO_MODEL = {
    "reject": RateLimitOnLimit.REJECT,
    "queue_and_retry": RateLimitOnLimit.QUEUE_RETRY,
}
_ON_LIMIT_MODEL_TO_WIRE = {
    RateLimitOnLimit.REJECT: "reject",
    RateLimitOnLimit.QUEUE_RETRY: "queue_and_retry",
}


def _parse_on_limit(value: str) -> RateLimitOnLimit:
    try:
        return _ON_LIMIT_WIRE_TO_MODEL[value]
    except KeyError:
        raise GatekeyError(
            f"Invalid on_limit value '{value}'. Must be one of: "
            f"{sorted(_ON_LIMIT_WIRE_TO_MODEL)}.",
            code="invalid_on_limit",
            status_code=400,
        ) from None


def _render_on_limit(value: RateLimitOnLimit) -> str:
    return _ON_LIMIT_MODEL_TO_WIRE[value]


class RateLimitRuleCreate(BaseModel):
    """Request body for creating/updating a rate limit rule.

    Scope is selected by which of `scope_team_id`/`scope_user_id` is set:
    neither -> org-wide default (`org_default_per_user`), `scope_team_id`
    only -> team-scoped, `scope_user_id` only -> user-scoped. Setting both
    is rejected (422) - mirrors the model's
    `ck_rate_limit_rules_scope_type_matches_scope_id` CHECK constraint
    (migration `0034`) so a bad request fails clean instead of as a DB
    constraint violation.
    """

    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None
    on_limit: str = "reject"
    max_queue_wait_seconds: int = 30
    scope_team_id: uuid.UUID | None = None
    scope_user_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _check_exclusive_scope(self) -> "RateLimitRuleCreate":
        if self.scope_team_id is not None and self.scope_user_id is not None:
            raise ValueError(
                "Only one of scope_team_id or scope_user_id may be set (not both)."
            )
        return self


class TeamRateLimitRuleCreate(BaseModel):
    """Request body for `POST/PUT /v1/admin/teams/{team_id}/rate-limit-
    rules[/{id}]` - the Team-Lead-accessible counterpart of
    `RateLimitRuleCreate` above.

    Deliberately has NO `scope_team_id`/`scope_user_id` fields: the target
    team is ALWAYS `{team_id}` from the URL path (the same id
    `require_team_role` authorizes against), never body-controlled - a
    malicious/confused Team Lead cannot smuggle a different team's id into
    this payload the way an Org Admin's `scope_team_id` field on
    `RateLimitRuleCreate` intentionally can (that field is fine there
    because only an Org Admin, who already has full cross-team access,
    reaches it).
    """

    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None
    on_limit: str = "reject"
    max_queue_wait_seconds: int = 30


class RateLimitRuleResponse(BaseModel):
    """Response schema for a rate limit rule."""

    id: uuid.UUID
    scope_type: RateLimitScopeType
    scope_team_id: uuid.UUID | None
    scope_user_id: uuid.UUID | None
    requests_per_minute: int | None
    tokens_per_minute: int | None
    on_limit: str
    max_queue_wait_seconds: int


class RateLimitRuleListResponse(BaseModel):
    """Response schema for rate limit rules list."""

    rules: list[RateLimitRuleResponse]


class RateLimitRuleStatusResponse(BaseModel):
    """Response schema for `GET /rate-limit-rules/{id}/status` (AC4.2.8) -
    real current-utilization state read from the same Redis counter the
    live gateway pipeline gates requests against. See `services.rate_limit.
    get_rule_current_status`'s docstring for exactly how the key is
    resolved and why `available` can be `False`."""

    rule_id: uuid.UUID
    scope_type: RateLimitScopeType
    available: bool
    reason: str | None
    requests_limit: int | None
    requests_used_last_60s: int | None
    requests_remaining: int | None
    queue_depth: int | None
    queue_depth_tracked: bool


def _to_response(row: RateLimitRule) -> RateLimitRuleResponse:
    return RateLimitRuleResponse(
        id=row.id,
        scope_type=row.scope_type,
        scope_team_id=row.scope_team_id,
        scope_user_id=row.scope_user_id,
        requests_per_minute=row.requests_per_min,
        tokens_per_minute=row.tokens_per_min,
        on_limit=_render_on_limit(row.on_limit),
        max_queue_wait_seconds=row.max_queue_wait_seconds,
    )


def _resolve_scope(payload: RateLimitRuleCreate) -> RateLimitScopeType:
    if payload.scope_team_id is not None:
        return RateLimitScopeType.TEAM
    if payload.scope_user_id is not None:
        return RateLimitScopeType.USER
    return RateLimitScopeType.ORG_DEFAULT_PER_USER


def _validate_payload(payload: RateLimitRuleCreate | TeamRateLimitRuleCreate) -> None:
    if payload.requests_per_minute is None and payload.tokens_per_minute is None:
        raise GatekeyError(
            "At least one of requests_per_minute or tokens_per_minute must be set.",
            code="missing_limit",
            status_code=400,
        )
    if payload.max_queue_wait_seconds < 10 or payload.max_queue_wait_seconds > 300:
        raise GatekeyError(
            "max_queue_wait_seconds must be between 10 and 300 seconds.",
            code="invalid_queue_wait",
            status_code=400,
        )


# ============================================================================
# Shared row-mutation helpers - see module docstring's last paragraph.
# ============================================================================


def _refresh_rate_limit_cache(cache: RateLimitCache, row: RateLimitRule) -> None:
    """Push `row`'s current state into the correct `RateLimitCache` slot,
    branching on its own `scope_type` (Fix 6) - the same three-way branch
    `load_rate_limit_cache_snapshot()`'s startup warm already uses."""
    snapshot = rate_limit_service.snapshot_from_rule(row)
    if row.scope_type == RateLimitScopeType.ORG_DEFAULT_PER_USER:
        cache.set_org_rule(row.org_id, snapshot)
    elif row.scope_type == RateLimitScopeType.TEAM:
        assert row.scope_team_id is not None
        cache.set_team_rule(row.scope_team_id, snapshot)
    elif row.scope_type == RateLimitScopeType.USER:
        assert row.scope_user_id is not None
        cache.set_user_rule(row.scope_user_id, snapshot)


def _clear_rate_limit_cache_for(cache: RateLimitCache, row: RateLimitRule) -> None:
    """Drop `row`'s entry from `RateLimitCache` (a delete) - same
    scope_type branch as `_refresh_rate_limit_cache` above, just clearing
    instead of setting."""
    if row.scope_type == RateLimitScopeType.ORG_DEFAULT_PER_USER:
        cache.set_org_rule(row.org_id, None)
    elif row.scope_type == RateLimitScopeType.TEAM:
        assert row.scope_team_id is not None
        cache.set_team_rule(row.scope_team_id, None)
    elif row.scope_type == RateLimitScopeType.USER:
        assert row.scope_user_id is not None
        cache.set_user_rule(row.scope_user_id, None)


async def _insert_rate_limit_rule(
    session: AsyncSession,
    *,
    scope_type: RateLimitScopeType,
    scope_team_id: uuid.UUID | None,
    scope_user_id: uuid.UUID | None,
    requests_per_minute: int | None,
    tokens_per_minute: int | None,
    on_limit: str,
    max_queue_wait_seconds: int,
    cache: RateLimitCache,
) -> RateLimitRule:
    row = RateLimitRule(
        org_id=DEFAULT_ORG_ID,
        scope_type=scope_type,
        scope_team_id=scope_team_id,
        scope_user_id=scope_user_id,
        requests_per_min=requests_per_minute,
        tokens_per_min=tokens_per_minute,
        on_limit=_parse_on_limit(on_limit),
        max_queue_wait_seconds=max_queue_wait_seconds,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise GatekeyError(
            "A rate limit rule already exists for this scope.",
            code="rate_limit_rule_conflict",
            status_code=409,
        ) from None
    _refresh_rate_limit_cache(cache, row)
    return row


async def _update_rate_limit_rule_row(
    session: AsyncSession,
    rule_id: uuid.UUID,
    payload: RateLimitRuleCreate | TeamRateLimitRuleCreate,
    *,
    cache: RateLimitCache,
) -> RateLimitRule:
    """Caller is responsible for confirming `rule_id` is one the caller may
    touch (the org-level route always may; the team-scoped route checks via
    `_get_team_owned_rule_or_404` first) - this function itself does not
    re-check scope, only that the row exists at all."""
    row = await session.scalar(select(RateLimitRule).where(RateLimitRule.id == rule_id))
    if row is None:
        raise GatekeyError(
            f"No rate limit rule found with id '{rule_id}'.", code="not_found", status_code=404
        )
    await session.execute(
        update(RateLimitRule)
        .where(RateLimitRule.id == rule_id)
        .values(
            requests_per_min=payload.requests_per_minute,
            tokens_per_min=payload.tokens_per_minute,
            on_limit=_parse_on_limit(payload.on_limit),
            max_queue_wait_seconds=payload.max_queue_wait_seconds,
        )
    )
    await session.commit()
    await session.refresh(row)
    _refresh_rate_limit_cache(cache, row)
    return row


async def _delete_rate_limit_rule_row(
    session: AsyncSession, rule_id: uuid.UUID, *, cache: RateLimitCache
) -> None:
    """Same "caller already authorized `rule_id`" contract as
    `_update_rate_limit_rule_row` above."""
    row = await session.scalar(select(RateLimitRule).where(RateLimitRule.id == rule_id))
    if row is None:
        raise GatekeyError(
            f"No rate limit rule found with id '{rule_id}'.", code="not_found", status_code=404
        )
    await session.execute(delete(RateLimitRule).where(RateLimitRule.id == rule_id))
    await session.commit()
    _clear_rate_limit_cache_for(cache, row)


async def _get_team_owned_rule_or_404(
    session: AsyncSession, team_id: uuid.UUID, rule_id: uuid.UUID
) -> RateLimitRule:
    """Ownership gate for the team-scoped `PUT`/`DELETE` routes: the rule
    must exist, be TEAM-scoped, and belong to exactly `team_id` - a Team
    Lead must never be able to mutate the org default, a per-user rule, or
    another team's rule by guessing/enumerating a `rule_id`. Deliberately
    the SAME generic 404 whether the rule doesn't exist at all or belongs to
    someone else (anti-enumeration, mirroring `require_team_role`'s own
    "don't distinguish not-found from forbidden" posture)."""
    row = await session.scalar(
        select(RateLimitRule).where(
            RateLimitRule.id == rule_id,
            RateLimitRule.scope_team_id == team_id,
            RateLimitRule.scope_type == RateLimitScopeType.TEAM,
        )
    )
    if row is None:
        raise GatekeyError(
            f"No rate limit rule found with id '{rule_id}'.", code="not_found", status_code=404
        )
    return row


# ============================================================================
# Admin API Endpoints
# ============================================================================


@router.get("/rate-limit-rules", response_model=RateLimitRuleListResponse)
async def get_rate_limit_rules(
    session: AsyncSession = Depends(get_db_session),
) -> RateLimitRuleListResponse:
    """List all rate limit rules for the default org.

    Returns org-wide default rules plus team- and user-scoped rules.
    """
    rows = (
        (await session.execute(select(RateLimitRule).where(RateLimitRule.org_id == DEFAULT_ORG_ID)))
        .scalars()
        .all()
    )
    return RateLimitRuleListResponse(rules=[_to_response(row) for row in rows])


@router.get("/rate-limit-rules/{rule_id}/status", response_model=RateLimitRuleStatusResponse)
async def get_rate_limit_rule_status(
    rule_id: uuid.UUID,
    user_id: uuid.UUID | None = Query(
        default=None,
        description=(
            "Required to read current utilization for an org-default-per-user rule "
            "(the live limiter tracks that counter per user, not one org-wide "
            "aggregate). Not needed for a team-scoped rule (Fix 2: the team pool is "
            "a genuinely shared, team-wide counter) or a user-scoped rule."
        ),
    ),
    session: AsyncSession = Depends(get_db_session),
    store: SharedStateStore = Depends(get_shared_state_store),
) -> RateLimitRuleStatusResponse:
    """AC4.2.8: current utilization (requests in the last 60 seconds) for
    one rate limit rule, read live from the real Redis/shared-state counter
    the gateway pipeline itself gates requests against - never estimated or
    computed a different way. `available=False` (with `reason` set) if the
    store is unreachable, or if an org-default-per-user rule's per-user
    counter can't be resolved without a `user_id`. `queue_depth` is always
    `null` (`queue_depth_tracked=False`) - see `services.rate_limit.
    get_rule_current_status`'s docstring for why the shipped `queue_and_retry`
    implementation has no real persisted queue-depth number to read.
    """
    row = await session.scalar(
        select(RateLimitRule).where(
            RateLimitRule.id == rule_id, RateLimitRule.org_id == DEFAULT_ORG_ID
        )
    )
    if row is None:
        raise NotFoundError(f"No rate limit rule found with id '{rule_id}'.")

    status = await rate_limit_service.get_rule_current_status(store, rule=row, user_id=user_id)
    return RateLimitRuleStatusResponse(
        rule_id=row.id,
        scope_type=row.scope_type,
        available=status.available,
        reason=status.reason,
        requests_limit=status.requests_limit,
        requests_used_last_60s=status.requests_used_last_60s,
        requests_remaining=status.requests_remaining,
        queue_depth=status.queue_depth,
        queue_depth_tracked=status.queue_depth_tracked,
    )


@router.post("/rate-limit-rules", response_model=RateLimitRuleResponse, status_code=201)
async def create_rate_limit_rule(
    payload: RateLimitRuleCreate = Body(...),
    session: AsyncSession = Depends(get_db_session),
    rate_limit_cache: RateLimitCache = Depends(get_rate_limit_cache),
) -> RateLimitRuleResponse:
    """Create a new rate limit rule.

    Scope is derived from the body - see `RateLimitRuleCreate` docstring.
    """
    _validate_payload(payload)
    row = await _insert_rate_limit_rule(
        session,
        scope_type=_resolve_scope(payload),
        scope_team_id=payload.scope_team_id,
        scope_user_id=payload.scope_user_id,
        requests_per_minute=payload.requests_per_minute,
        tokens_per_minute=payload.tokens_per_minute,
        on_limit=payload.on_limit,
        max_queue_wait_seconds=payload.max_queue_wait_seconds,
        cache=rate_limit_cache,
    )
    return _to_response(row)


@router.put("/rate-limit-rules/{rule_id}", response_model=RateLimitRuleResponse)
async def update_rate_limit_rule(
    rule_id: uuid.UUID,
    payload: RateLimitRuleCreate = Body(...),
    session: AsyncSession = Depends(get_db_session),
    rate_limit_cache: RateLimitCache = Depends(get_rate_limit_cache),
) -> RateLimitRuleResponse:
    """Update an existing rate limit rule. Scope is not changed by this call
    (only the limit/behavior fields) - use delete+create to re-scope a rule."""
    _validate_payload(payload)
    row = await _update_rate_limit_rule_row(session, rule_id, payload, cache=rate_limit_cache)
    return _to_response(row)


@router.delete("/rate-limit-rules/{rule_id}", status_code=204)
async def delete_rate_limit_rule(
    rule_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    rate_limit_cache: RateLimitCache = Depends(get_rate_limit_cache),
) -> None:
    """Delete a rate limit rule."""
    await _delete_rate_limit_rule_row(session, rule_id, cache=rate_limit_cache)


# ============================================================================
# Team-scoped API Endpoints (Org Admin OR that team's own Team Lead) - see
# module docstring.
# ============================================================================


@team_router.get("/{team_id}/rate-limit-rules", response_model=RateLimitRuleListResponse)
async def list_team_rate_limit_rules(
    team_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead", "member")),
    session: AsyncSession = Depends(get_db_session),
) -> RateLimitRuleListResponse:
    """List this team's own rate limit rule(s) only - never another team's
    or the org default (a Team Lead/member has no legitimate reason to see
    those through this surface; they'd use `/v1/admin/rate-limit-rules` as
    an Org Admin for that)."""
    rows = (
        (
            await session.execute(
                select(RateLimitRule).where(
                    RateLimitRule.org_id == DEFAULT_ORG_ID,
                    RateLimitRule.scope_team_id == team_id,
                    RateLimitRule.scope_type == RateLimitScopeType.TEAM,
                )
            )
        )
        .scalars()
        .all()
    )
    return RateLimitRuleListResponse(rules=[_to_response(row) for row in rows])


@team_router.post("/{team_id}/rate-limit-rules", response_model=RateLimitRuleResponse, status_code=201)
async def create_team_rate_limit_rule(
    team_id: uuid.UUID,
    payload: TeamRateLimitRuleCreate = Body(...),
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
    rate_limit_cache: RateLimitCache = Depends(get_rate_limit_cache),
) -> RateLimitRuleResponse:
    """Create this team's own rate limit rule - scope is always forced to
    `team` / `{team_id}` from the path, never body-controlled (see
    `TeamRateLimitRuleCreate`'s docstring)."""
    _validate_payload(payload)
    row = await _insert_rate_limit_rule(
        session,
        scope_type=RateLimitScopeType.TEAM,
        scope_team_id=team_id,
        scope_user_id=None,
        requests_per_minute=payload.requests_per_minute,
        tokens_per_minute=payload.tokens_per_minute,
        on_limit=payload.on_limit,
        max_queue_wait_seconds=payload.max_queue_wait_seconds,
        cache=rate_limit_cache,
    )
    return _to_response(row)


@team_router.put("/{team_id}/rate-limit-rules/{rule_id}", response_model=RateLimitRuleResponse)
async def update_team_rate_limit_rule(
    team_id: uuid.UUID,
    rule_id: uuid.UUID,
    payload: TeamRateLimitRuleCreate = Body(...),
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
    rate_limit_cache: RateLimitCache = Depends(get_rate_limit_cache),
) -> RateLimitRuleResponse:
    """Update `rule_id` - only if it's actually this team's own rule (see
    `_get_team_owned_rule_or_404`); a different team's/the org's rule 404s
    exactly as if it didn't exist."""
    await _get_team_owned_rule_or_404(session, team_id, rule_id)
    _validate_payload(payload)
    row = await _update_rate_limit_rule_row(session, rule_id, payload, cache=rate_limit_cache)
    return _to_response(row)


@team_router.delete("/{team_id}/rate-limit-rules/{rule_id}", status_code=204)
async def delete_team_rate_limit_rule(
    team_id: uuid.UUID,
    rule_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
    rate_limit_cache: RateLimitCache = Depends(get_rate_limit_cache),
) -> None:
    """Same ownership gate as `update_team_rate_limit_rule` above."""
    await _get_team_owned_rule_or_404(session, team_id, rule_id)
    await _delete_rate_limit_rule_row(session, rule_id, cache=rate_limit_cache)
