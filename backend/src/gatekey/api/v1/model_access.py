"""Model-access self-view (Phase 2, BD-20) - design doc section 5.7.

`GET /v1/model-access?team_id=` - session auth. Resolves the caller's team
context (A1's auto-select pattern), then evaluates every `MODEL_REGISTRY`
entry through `resolve_model_access` - the exact same layered org-then-team
resolution the gateway hot path enforces, so what this screen shows is by
construction what the gateway will do.

Team resolution rules (`select_team_id`, pure/unit-tested):
- explicit `team_id`: must be one of the caller's own memberships unless the
  caller holds an org-wide role (org_admin/auditor may view any team's
  resolution); a non-member's explicit team_id gets the same generic 403 as
  `require_team_role` (anti-enumeration).
- omitted: exactly one membership -> auto-selected; two or more -> 400
  `team_id_required`; zero -> org baseline only (the team layer is skipped
  entirely - a user with no team yet still sees the org's policy, matching
  the gateway's own `team_id=None` legacy path).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import get_model_policy_cache, get_team_model_policy_cache
from gatekey.db.models.team_membership import TeamMembership
from gatekey.db.session import get_db_session
from gatekey.errors import ForbiddenError, GatekeyError
from gatekey.providers.model_registry import MODEL_REGISTRY
from gatekey.services.model_policy import (
    ModelPolicyCache,
    TeamModelPolicyCache,
    resolve_model_access,
)
from gatekey.services.sessions import SessionContext, get_current_session

router = APIRouter(prefix="/v1/model-access", tags=["model-access"])

_ORG_WIDE_ROLES = ("org_admin", "auditor")


class ModelAccessEntry(BaseModel):
    model: str
    allowed: bool
    blocking_layer: str | None


class ModelAccessResponse(BaseModel):
    team_id: uuid.UUID | None
    models: list[ModelAccessEntry]


def select_team_id(
    requested_team_id: uuid.UUID | None,
    membership_team_ids: list[uuid.UUID],
    *,
    org_wide_role: bool,
) -> uuid.UUID | None:
    """Pure team-resolution rules - see module docstring. Raises the
    module's 403/400 errors; returns None for the zero-membership
    org-baseline-only case."""
    if requested_team_id is not None:
        if org_wide_role or requested_team_id in membership_team_ids:
            return requested_team_id
        raise ForbiddenError("You do not have the required role for this team.")
    if len(membership_team_ids) == 1:
        return membership_team_ids[0]
    if len(membership_team_ids) >= 2:
        raise GatekeyError(
            "You belong to more than one team - pass ?team_id= to select one.",
            code="team_id_required",
            status_code=400,
        )
    return None


@router.get("", response_model=ModelAccessResponse)
async def get_model_access_endpoint(
    team_id: uuid.UUID | None = Query(default=None),
    ctx: SessionContext = Depends(get_current_session),
    session: AsyncSession = Depends(get_db_session),
    org_cache: ModelPolicyCache = Depends(get_model_policy_cache),
    team_cache: TeamModelPolicyCache = Depends(get_team_model_policy_cache),
) -> ModelAccessResponse:
    membership_team_ids = list(
        (
            await session.execute(
                select(TeamMembership.team_id).where(TeamMembership.user_id == ctx.user_id)
            )
        )
        .scalars()
        .all()
    )
    resolved_team_id = select_team_id(
        team_id, membership_team_ids, org_wide_role=ctx.org_role in _ORG_WIDE_ROLES
    )
    entries = []
    for model in sorted(MODEL_REGISTRY):
        decision = resolve_model_access(
            model, org_cache=org_cache, team_cache=team_cache, team_id=resolved_team_id
        )
        entries.append(
            ModelAccessEntry(
                model=model, allowed=decision.allowed, blocking_layer=decision.blocking_layer
            )
        )
    return ModelAccessResponse(team_id=resolved_team_id, models=entries)
