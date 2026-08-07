"""Admin endpoint for the failover-event audit log (Phase 4, Reliability &
Cost Efficiency - AC4.1.8, AC4.5.7, technical design section 3.1).

`GET /v1/admin/failover-events?from=&to=&limit=` - same `from`/`to` half-
open date-range-filter convention as `api/v1/admin/audit_entries.py`
(reused verbatim, not reinvented). Reads `failover_events` (migration
`0025`), written once per successful reactive failover switch by
`api.v1.gateway.common.call_provider_with_failover` (see that table's model
docstring) - AC4.5.7's "one request that retries across multiple backup
keys counts as ONE failover event" is a property of that writer (one row
per successful switch, never one per retry attempt), not something this
read-only endpoint needs to de-duplicate itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import require_admin
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.failover_event import FailoverEvent
from gatekey.db.session import get_db_session

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin", "failover"],
    dependencies=[Depends(require_admin)],
)

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000


class FailoverEventResponse(BaseModel):
    id: uuid.UUID
    from_provider_key_id: uuid.UUID | None
    to_provider_key_id: uuid.UUID | None
    request_id: str
    detected_at: datetime
    switched_at: datetime
    created_at: datetime


@router.get("/failover-events", response_model=list[FailoverEventResponse])
async def list_failover_events_endpoint(
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    session: AsyncSession = Depends(get_db_session),
) -> list[FailoverEventResponse]:
    filters = [FailoverEvent.org_id == DEFAULT_ORG_ID]
    if from_ is not None:
        filters.append(FailoverEvent.created_at >= from_)
    if to is not None:
        filters.append(FailoverEvent.created_at < to)

    rows = (
        (
            await session.execute(
                select(FailoverEvent)
                .where(*filters)
                .order_by(FailoverEvent.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        FailoverEventResponse(
            id=row.id,
            from_provider_key_id=row.from_provider_key_id,
            to_provider_key_id=row.to_provider_key_id,
            request_id=row.request_id,
            detected_at=row.detected_at,
            switched_at=row.switched_at,
            created_at=row.created_at,
        )
        for row in rows
    ]
