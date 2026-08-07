"""Org-admin join-request escalation queue (Phase 2, BD-15) - design doc
section 5.3 / A7.

`require_role(org_admin)` (session-based), matching the design's contract
for this route. Membership in the queue is computed live at query time -
zero current `team_lead` memberships on the target team, OR pending >= 5
business days (Mon-Fri) - never solely from the stored `routed_to`
snapshot (design doc 1.5's schema note).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import require_role
from gatekey.db.session import get_db_session
from gatekey.schemas.join_request import AdminJoinRequestQueueEntryResponse
from gatekey.services.join_requests import list_admin_queue
from gatekey.services.sessions import SessionContext

router = APIRouter(prefix="/v1/admin/join-requests", tags=["admin", "join-requests"])


@router.get("/queue", response_model=list[AdminJoinRequestQueueEntryResponse])
async def get_admin_queue_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> list[AdminJoinRequestQueueEntryResponse]:
    entries = await list_admin_queue(session)
    return [
        AdminJoinRequestQueueEntryResponse(
            id=e.request.id,
            team_id=e.request.team_id,
            team_name=e.team_name,
            requester_user_id=e.request.requester_user_id,
            requester_name=e.request.requester_name,
            status=e.request.status.value,
            routed_to=e.request.routed_to.value,
            requested_at=e.request.requested_at,
            resolved_at=e.request.resolved_at,
            resolved_by_user_id=e.request.resolved_by_user_id,
            approved_budget_usd=e.request.approved_budget_usd,
            rejection_reason=e.request.rejection_reason,
            escalation_reason=e.escalation_reason,
        )
        for e in entries
    ]
