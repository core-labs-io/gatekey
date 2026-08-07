"""Admin endpoint for the graceful-degradation event log (Phase 4,
Reliability & Cost Efficiency - AC4.4.5, AC4.4.8, technical design
section 3.1).

`GET /v1/admin/degradation-events?team_id=&from=&to=&limit=` -
`require_role(org_admin, auditor)` per the technical design's endpoint
table (matches `audit_entries.py`'s RBAC exactly, the closest existing
precedent for an audit/cost-savings read surface). Reads
`degradation_events` (migration `0032`), written by the degradation-policy
enforcement layer each time a request's model is substituted for a cheaper
fallback (AC4.4.3-4.4.6) - this endpoint is read-only.

`degradation_events` has no `org_id` column of its own (see that model's
docstring) - this codebase is single-org per deployment
(`constants.DEFAULT_ORG_ID`), so team/date filters are sufficient without a
join to `teams` for org scoping.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import require_role
from gatekey.db.models.degradation_event import DegradationEvent
from gatekey.db.session import get_db_session
from gatekey.services.sessions import SessionContext

router = APIRouter(prefix="/v1/admin", tags=["admin", "degradation"])

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000


class DegradationEventResponse(BaseModel):
    id: uuid.UUID
    team_id: uuid.UUID
    user_id: uuid.UUID
    request_id: uuid.UUID | None
    original_model: str
    degraded_model: str
    original_cost: Decimal
    degraded_cost: Decimal
    cost_saved: Decimal
    created_at: datetime


@router.get("/degradation-events", response_model=list[DegradationEventResponse])
async def list_degradation_events_endpoint(
    team_id: uuid.UUID | None = Query(default=None),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    ctx: SessionContext = Depends(require_role("org_admin", "auditor")),
    session: AsyncSession = Depends(get_db_session),
) -> list[DegradationEventResponse]:
    filters = []
    if team_id is not None:
        filters.append(DegradationEvent.team_id == team_id)
    if from_ is not None:
        filters.append(DegradationEvent.created_at >= from_)
    if to is not None:
        filters.append(DegradationEvent.created_at < to)

    rows = (
        (
            await session.execute(
                select(DegradationEvent)
                .where(*filters)
                .order_by(DegradationEvent.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        DegradationEventResponse(
            id=row.id,
            team_id=row.team_id,
            user_id=row.user_id,
            request_id=row.request_id,
            original_model=row.original_model,
            degraded_model=row.degraded_model,
            original_cost=row.original_cost,
            degraded_cost=row.degraded_cost,
            cost_saved=row.original_cost - row.degraded_cost,
            created_at=row.created_at,
        )
        for row in rows
    ]
