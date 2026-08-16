"""Admin endpoints for graceful degradation configuration (Phase 4, Reliability & Cost Efficiency).

`router` (org-level, `/v1/admin/degradation-policy`) is gated by
`require_admin` at the router level - Org Admin (or the break-glass token)
only.

`team_router` (`/v1/admin/teams/{team_id}/degradation-policy`) is the
Team-Lead-accessible counterpart the product spec's RBAC table requires
("As an Org Admin or Team Lead, I can configure automatic model
downgrades...", phase-4-product-spec.md section 2/4).

FIX (found in review, fixed here): these team-scoped routes previously
lived on `router` above too, meaning they inherited that router's
router-level `require_admin` dependency - a route-level dependency cannot
override/bypass a router-level one (FastAPI runs both), so despite already
taking `team_id` from the path and looking correctly team-scoped, a real
Team Lead session (non-org_admin) was actually rejected with 401 before
ever reaching the route body. Fixed by moving these two routes onto their
own router with NO router-level dependency, gated per-route instead by
`require_team_role` (Org Admin OR that team's own Team Lead - the exact
same dependency `api/v1/teams.py` uses for every other team-scoped admin
surface, and the one `api/v1/admin/rate_limits.py`'s new team-scoped routes
also use - see that module's docstring for the identical pattern applied to
a second surface).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import TeamRoleContext, get_degradation_policy_cache, require_admin, require_team_role
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.degradation_policy import DegradationPolicy, DegradationScopeType
from gatekey.db.session import get_db_session
from gatekey.errors import GatekeyError
from gatekey.services import model_policy as model_policy_service
from gatekey.services.degradation import DegradationPolicyCache, DegradationPolicySnapshot
from sqlalchemy import select

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin", "degradation"],
    dependencies=[Depends(require_admin)],
)

team_router = APIRouter(
    prefix="/v1/admin/teams",
    tags=["admin", "degradation", "team"],
)


class DegradationPolicyCreate(BaseModel):
    """Request body for creating/updating degradation policy."""

    enabled: bool = False
    threshold_pct_of_budget: float = 10.0
    downgrade_target_model: str


class DegradationPolicyResponse(BaseModel):
    """Response schema for degradation policy."""

    id: uuid.UUID | None
    scope_type: DegradationScopeType
    scope_team_id: uuid.UUID | None
    enabled: bool
    threshold_pct_of_budget: Decimal
    downgrade_target_model: str


@router.get("/degradation-policy", response_model=DegradationPolicyResponse)
async def get_degradation_policy(
    session: AsyncSession = Depends(get_db_session),
) -> DegradationPolicyResponse:
    """Get the current degradation policy for the default org."""
    row = await session.scalar(select(DegradationPolicy).where(
        DegradationPolicy.org_id == DEFAULT_ORG_ID,
        DegradationPolicy.scope_type == DegradationScopeType.ORG,
    ))

    if row is None:
        # Return default values if not configured
        return DegradationPolicyResponse(
            id=None,
            scope_type=DegradationScopeType.ORG,
            scope_team_id=None,
            enabled=False,
            threshold_pct_of_budget=Decimal("10.0"),
            downgrade_target_model="",
        )

    return DegradationPolicyResponse(
        id=row.id,
        scope_type=row.scope_type,
        scope_team_id=row.scope_team_id,
        enabled=row.enabled,
        threshold_pct_of_budget=row.threshold_pct_of_budget,
        downgrade_target_model=row.downgrade_target_model,
    )


@router.put("/degradation-policy", response_model=DegradationPolicyResponse)
async def update_degradation_policy(
    payload: DegradationPolicyCreate = Body(...),
    session: AsyncSession = Depends(get_db_session),
    degradation_policy_cache: DegradationPolicyCache = Depends(get_degradation_policy_cache),
) -> DegradationPolicyResponse:
    """Create or update degradation policy for the default org."""
    from sqlalchemy import select

    # Validate threshold
    if payload.threshold_pct_of_budget < 1.0 or payload.threshold_pct_of_budget > 99.0:
        raise GatekeyError(
            "threshold_pct_of_budget must be between 1.0 and 99.0.",
            code="invalid_threshold",
            status_code=400,
        )

    # Fix 5 (security review finding, config-time half): the fallback
    # model must itself be permitted by the org's own model access policy -
    # otherwise this endpoint would let an Org Admin configure degradation
    # to silently reroute traffic to a model denied elsewhere in this same
    # admin console. See `services.model_policy.
    # validate_downgrade_target_model()`'s docstring; raises 422, no DB
    # write below, if the model isn't actually allowed.
    await model_policy_service.validate_downgrade_target_model(
        session, payload.downgrade_target_model
    )

    row = await session.scalar(select(DegradationPolicy).where(
        DegradationPolicy.org_id == DEFAULT_ORG_ID,
        DegradationPolicy.scope_type == DegradationScopeType.ORG,
    ))

    if row is None:
        # Create new policy
        row = DegradationPolicy(
            org_id=DEFAULT_ORG_ID,
            scope_type=DegradationScopeType.ORG,
            enabled=payload.enabled,
            threshold_pct_of_budget=Decimal(str(payload.threshold_pct_of_budget)),
            downgrade_target_model=payload.downgrade_target_model,
        )
        session.add(row)
    else:
        # Update existing
        row.enabled = payload.enabled
        row.threshold_pct_of_budget = Decimal(str(payload.threshold_pct_of_budget))
        row.downgrade_target_model = payload.downgrade_target_model

    await session.commit()
    # Fix 6 (NFR gap): refresh the process-wide cache immediately after the
    # commit succeeds - same write-then-refresh-cache pattern
    # `caching_settings.py`/`rate_limits.py` use.
    degradation_policy_cache.set_org_policy(
        DegradationPolicySnapshot(
            enabled=row.enabled,
            threshold_pct_of_budget=row.threshold_pct_of_budget,
            downgrade_target_model=row.downgrade_target_model,
        )
    )

    return DegradationPolicyResponse(
        id=row.id,
        scope_type=row.scope_type,
        scope_team_id=row.scope_team_id,
        enabled=row.enabled,
        threshold_pct_of_budget=row.threshold_pct_of_budget,
        downgrade_target_model=row.downgrade_target_model,
    )


# ============================================================================
# Team-level degradation policy endpoints
# ============================================================================


class TeamDegradationPolicyCreate(BaseModel):
    """Request body for creating/updating team degradation policy."""

    enabled: bool = False
    threshold_pct_of_budget: float = 10.0
    downgrade_target_model: str


class TeamDegradationPolicyResponse(BaseModel):
    """Response schema for team degradation policy."""

    team_id: uuid.UUID
    enabled: bool
    threshold_pct_of_budget: Decimal
    downgrade_target_model: str


@team_router.get("/{team_id}/degradation-policy", response_model=TeamDegradationPolicyResponse)
async def get_team_degradation_policy(
    team_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead", "member")),
    session: AsyncSession = Depends(get_db_session),
) -> TeamDegradationPolicyResponse:
    """Get the current degradation policy for a specific team."""
    row = await session.scalar(select(DegradationPolicy).where(
        DegradationPolicy.scope_team_id == team_id,
        DegradationPolicy.scope_type == DegradationScopeType.TEAM,
    ))

    if row is None:
        # Check if org-level policy exists
        org_row = await session.scalar(select(DegradationPolicy).where(
            DegradationPolicy.org_id == DEFAULT_ORG_ID,
            DegradationPolicy.scope_type == DegradationScopeType.ORG,
        ))
        if org_row is None:
            raise GatekeyError(
                f"No degradation policy found for team '{team_id}'.",
                code="not_found",
                status_code=404,
            )
        # Return org policy values
        return TeamDegradationPolicyResponse(
            team_id=team_id,
            enabled=org_row.enabled,
            threshold_pct_of_budget=org_row.threshold_pct_of_budget,
            downgrade_target_model=org_row.downgrade_target_model,
        )

    return TeamDegradationPolicyResponse(
        team_id=row.scope_team_id,
        enabled=row.enabled,
        threshold_pct_of_budget=row.threshold_pct_of_budget,
        downgrade_target_model=row.downgrade_target_model,
    )


@team_router.put("/{team_id}/degradation-policy", response_model=TeamDegradationPolicyResponse)
async def update_team_degradation_policy(
    team_id: uuid.UUID,
    payload: TeamDegradationPolicyCreate = Body(...),
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
    degradation_policy_cache: DegradationPolicyCache = Depends(get_degradation_policy_cache),
) -> TeamDegradationPolicyResponse:
    """Create or update degradation policy for a specific team."""
    from sqlalchemy import select

    # Validate threshold
    if payload.threshold_pct_of_budget < 1.0 or payload.threshold_pct_of_budget > 99.0:
        raise GatekeyError(
            "threshold_pct_of_budget must be between 1.0 and 99.0.",
            code="invalid_threshold",
            status_code=400,
        )

    # Fix 5 (security review finding, config-time half): validate against
    # BOTH the org baseline AND this team's own restriction overlay - see
    # `update_degradation_policy` above and `services.model_policy.
    # validate_downgrade_target_model()`'s docstring. A Team Lead must not
    # be able to configure degradation to a model their own team's model
    # policy (or the org's) has denied.
    await model_policy_service.validate_downgrade_target_model(
        session, payload.downgrade_target_model, team_id=team_id
    )

    row = await session.scalar(select(DegradationPolicy).where(
        DegradationPolicy.scope_team_id == team_id,
        DegradationPolicy.scope_type == DegradationScopeType.TEAM,
    ))

    if row is None:
        # Create new team policy
        row = DegradationPolicy(
            org_id=DEFAULT_ORG_ID,
            scope_type=DegradationScopeType.TEAM,
            scope_team_id=team_id,
            enabled=payload.enabled,
            threshold_pct_of_budget=Decimal(str(payload.threshold_pct_of_budget)),
            downgrade_target_model=payload.downgrade_target_model,
        )
        session.add(row)
    else:
        # Update existing
        row.enabled = payload.enabled
        row.threshold_pct_of_budget = Decimal(str(payload.threshold_pct_of_budget))
        row.downgrade_target_model = payload.downgrade_target_model

    await session.commit()
    degradation_policy_cache.set_team_policy(
        team_id,
        DegradationPolicySnapshot(
            enabled=row.enabled,
            threshold_pct_of_budget=row.threshold_pct_of_budget,
            downgrade_target_model=row.downgrade_target_model,
        ),
    )

    return TeamDegradationPolicyResponse(
        team_id=row.scope_team_id,
        enabled=row.enabled,
        threshold_pct_of_budget=row.threshold_pct_of_budget,
        downgrade_target_model=row.downgrade_target_model,
    )