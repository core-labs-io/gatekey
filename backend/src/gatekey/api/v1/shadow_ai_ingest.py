"""The Shadow AI Discovery ingestion endpoint (Phase 5 - Differentiators,
5.1 Shadow AI Discovery) - `POST /v1/admin/shadow-ai/ingest`.

**On its OWN router, dedicated auth dependency declared on the ROUTE
itself, not the router** - see `gatekey/phase-5-technical-design.md`
section 2.5's "Router placement - an explicit warning for backend-developer"
and `api.deps.require_shadow_ai_ingest_token`'s own docstring. This router
does NOT declare `dependencies=[Depends(require_admin)]` (or any other
admin/gateway dependency) at the `APIRouter(...)` level - despite sharing
the `/v1/admin/shadow-ai` URL prefix with `api/v1/admin/shadow_ai.py` (the
separate config/report/token-gen/hostname-CRUD router, which DOES use the
normal `require_admin_or_auditor`/`require_role`/`require_team_role`
dependencies), this one route is reachable ONLY by a caller holding a valid
`gk_sai_...` ingestion token - never by an admin session or the break-glass
token. See `api.deps.require_shadow_ai_ingest_token` for the full non-overlap
proof this design deliberately verifies, not just asserts.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import ShadowAiIngestContext, require_shadow_ai_ingest_token
from gatekey.db.session import get_db_session
from gatekey.schemas.shadow_ai import ShadowAiIngestBatchRequest, ShadowAiIngestResponse
from gatekey.services.shadow_ai import (
    ShadowAiIngestEventInput,
    ingest_events,
    schedule_shadow_ai_enforcement,
)

router = APIRouter(prefix="/v1/admin/shadow-ai", tags=["shadow-ai-ingest"])


@router.post("/ingest", response_model=ShadowAiIngestResponse, status_code=202)
async def ingest_shadow_ai_events_endpoint(
    payload: ShadowAiIngestBatchRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: ShadowAiIngestContext = Depends(require_shadow_ai_ingest_token),
    session: AsyncSession = Depends(get_db_session),
) -> ShadowAiIngestResponse:
    """AC5.1.1's data-minimization gate is enforced entirely inside
    `services.shadow_ai.ingest_events` - this handler never inspects
    per-event outcomes beyond the aggregate counts it returns. `202
    Accepted` (not `201`) - a batch may persist zero, some, or all of its
    events (dropped rows are a normal, expected outcome, not an error), and
    enforcement delivery (if configured) happens asynchronously after this
    response, via `BackgroundTasks`."""
    events = [
        ShadowAiIngestEventInput(
            user_identifier=event.user_identifier,
            destination_host=event.destination_host,
            occurred_at=event.occurred_at,
            source=event.source,
            raw_metadata=event.raw_metadata,
        )
        for event in payload.events
    ]
    result = await ingest_events(session, org_id=ctx.org_id, events=events)
    schedule_shadow_ai_enforcement(background_tasks, request.app, event_ids=result.persisted_event_ids)
    return ShadowAiIngestResponse(
        received=result.received, persisted=result.persisted, dropped=result.dropped
    )
