"""Onboarding routes (Phase 2, BD-15) - design doc section 5.2 / 2.6.

Every route requires only a valid session (`get_current_session`): a
pre-onboarding SSO user is authenticated but holds no membership/role yet.
`GET /teams` is deliberately id+name only - no budget/member data leaks to
a pre-onboarding user.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.team import Team
from gatekey.db.session import get_db_session
from gatekey.errors import NotFoundError
from gatekey.schemas.join_request import (
    JoinRequestCreateRequest,
    JoinRequestResponse,
    OnboardingTeamResponse,
)
from gatekey.services.audit import write_audit_entry
from gatekey.services.join_requests import (
    get_latest_join_request,
    submit_join_request,
)
from gatekey.services.sessions import SessionContext, get_current_session

router = APIRouter(prefix="/v1/onboarding", tags=["onboarding"])


def _response(row, team_name: str | None = None) -> JoinRequestResponse:
    return JoinRequestResponse(
        id=row.id,
        team_id=row.team_id,
        team_name=team_name,
        requester_user_id=row.requester_user_id,
        requester_name=row.requester_name,
        status=row.status.value,
        routed_to=row.routed_to.value,
        requested_at=row.requested_at,
        resolved_at=row.resolved_at,
        resolved_by_user_id=row.resolved_by_user_id,
        approved_budget_usd=row.approved_budget_usd,
        rejection_reason=row.rejection_reason,
    )


@router.get("/teams", response_model=list[OnboardingTeamResponse])
async def list_onboarding_teams_endpoint(
    ctx: SessionContext = Depends(get_current_session),
    session: AsyncSession = Depends(get_db_session),
) -> list[OnboardingTeamResponse]:
    stmt = (
        select(Team.id, Team.name)
        .where(Team.org_id == DEFAULT_ORG_ID)
        .order_by(Team.name)
    )
    return [
        OnboardingTeamResponse(id=row[0], name=row[1])
        for row in (await session.execute(stmt)).all()
    ]


@router.post("/join-requests", response_model=JoinRequestResponse, status_code=201)
async def submit_join_request_endpoint(
    payload: JoinRequestCreateRequest,
    ctx: SessionContext = Depends(get_current_session),
    session: AsyncSession = Depends(get_db_session),
) -> JoinRequestResponse:
    """409 `join_request_already_pending` if one exists (AC6.4 - enforced by
    the partial unique index, mapped in `submit_join_request`)."""
    row = await submit_join_request(
        session,
        requester_user_id=ctx.require_user_id(),
        requester_name=payload.full_name,
        team_id=payload.team_id,
    )
    await write_audit_entry(
        session,
        actor=ctx,
        action="join_request.submit",
        target_type="join_request",
        target_id=str(row.id),
        old_value=None,
        new_value={
            "team_id": payload.team_id,
            "requester_name": payload.full_name,
            "routed_to": row.routed_to,
        },
    )
    await session.commit()
    await session.refresh(row)  # server defaults (status/requested_at)
    return _response(row)


@router.get("/status", response_model=JoinRequestResponse)
async def onboarding_status_endpoint(
    ctx: SessionContext = Depends(get_current_session),
    session: AsyncSession = Depends(get_db_session),
) -> JoinRequestResponse:
    """The caller's current/most-recent request (AC6.10's holding screen) -
    404 if they have never submitted one."""
    latest = await get_latest_join_request(session, ctx.require_user_id())
    if latest is None:
        raise NotFoundError("No join request found for the current user.")
    row, team_name = latest
    return _response(row, team_name)
