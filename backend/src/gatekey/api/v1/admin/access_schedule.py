"""Admin endpoints for the org-wide access schedule and the flat org-wide
holiday-date list (Phase 3, BD-17) - design doc section 9.7.

The team-scoped route lives in `api/v1/teams.py` (`require_team_role(
team_lead)`, AC9.2 narrowing-only), the per-service-account-key route and
the AC9.10 effective-schedule listing live in `api/v1/keys.py`'s existing
`admin_router` - mirroring exactly where the per-key rotation-policy routes
live relative to `api/v1/admin/rotation_policy.py` (see that module's
docstring for the precedent this follows).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import get_access_schedule_cache, get_source_ip, require_role
from gatekey.db.session import get_db_session
from gatekey.errors import NotFoundError
from gatekey.schemas.access_schedule import (
    AccessSchedulePutRequest,
    AccessScheduleResponse,
    HolidayDateCreateRequest,
    HolidayDateResponse,
)
from gatekey.services.access_schedules import (
    AccessScheduleCache,
    add_holiday_date,
    delete_holiday_date,
    delete_org_access_schedule,
    get_org_access_schedule,
    list_holiday_dates,
    set_org_access_schedule,
)
from gatekey.services.audit import write_audit_entry
from gatekey.services.sessions import SessionContext

router = APIRouter(prefix="/v1/admin", tags=["admin", "access-schedule"])


# --- org-wide access schedule (AC9.1-AC9.3) -----------------------------------


@router.get("/access-schedule", response_model=AccessScheduleResponse | None)
async def get_org_access_schedule_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> AccessScheduleResponse | None:
    """`null` (not 404) when no org-wide schedule exists yet - "unrestricted"
    is the normal default state (AC9.3), not an error."""
    row = await get_org_access_schedule(session)
    return AccessScheduleResponse.model_validate(row) if row is not None else None


@router.put("/access-schedule", response_model=AccessScheduleResponse)
async def put_org_access_schedule_endpoint(
    payload: AccessSchedulePutRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    cache: AccessScheduleCache = Depends(get_access_schedule_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> AccessScheduleResponse:
    """`set_org_access_schedule` commits internally, so (mirroring `services.
    residency`'s org-rule PUT route) the audit entry is written first,
    using the stable `org_id` as `target_id`."""
    old_row = await get_org_access_schedule(session)
    await write_audit_entry(
        session,
        actor=ctx,
        action="access_schedule.update",
        target_type="access_schedule",
        target_id=str(ctx.org_id),
        old_value=AccessScheduleResponse.model_validate(old_row).model_dump(mode="json")
        if old_row is not None
        else None,
        new_value=payload.model_dump(mode="json"),
        source_ip=source_ip,
    )
    row = await set_org_access_schedule(
        session,
        enabled=payload.enabled,
        allowed_days=payload.allowed_days,
        allowed_hours_start=payload.allowed_hours_start,
        allowed_hours_end=payload.allowed_hours_end,
        cache=cache,
    )
    return AccessScheduleResponse.model_validate(row)


@router.delete("/access-schedule", status_code=204)
async def delete_org_access_schedule_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    cache: AccessScheduleCache = Depends(get_access_schedule_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> Response:
    row = await get_org_access_schedule(session)
    if row is None:
        raise NotFoundError("No org-wide access schedule is configured.")
    await write_audit_entry(
        session,
        actor=ctx,
        action="access_schedule.delete",
        target_type="access_schedule",
        target_id=str(ctx.org_id),
        old_value=AccessScheduleResponse.model_validate(row).model_dump(mode="json"),
        new_value=None,
        source_ip=source_ip,
    )
    await delete_org_access_schedule(session, cache=cache)
    return Response(status_code=204)


# --- holiday dates (AC9.5, flat org-wide list, no calendar-ref indirection) --


@router.get("/holiday-dates", response_model=list[HolidayDateResponse])
async def list_holiday_dates_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> list[HolidayDateResponse]:
    rows = await list_holiday_dates(session)
    return [HolidayDateResponse.model_validate(row) for row in rows]


@router.post("/holiday-dates", response_model=HolidayDateResponse, status_code=201)
async def create_holiday_date_endpoint(
    payload: HolidayDateCreateRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    cache: AccessScheduleCache = Depends(get_access_schedule_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> HolidayDateResponse:
    """`add_holiday_date` commits internally (409 `holiday_date_already_
    exists` on a duplicate date, no DB write in that case) - the audit entry
    is written after, using the now-known row id, same second-commit
    deviation `services.service_accounts.create_service_account`'s route
    already documents."""
    row = await add_holiday_date(
        session, holiday_date=payload.holiday_date, label=payload.label, cache=cache
    )
    await write_audit_entry(
        session,
        actor=ctx,
        action="holiday_date.create",
        target_type="holiday_date",
        target_id=str(row.id),
        old_value=None,
        new_value={"holiday_date": row.holiday_date, "label": row.label},
        source_ip=source_ip,
    )
    await session.commit()
    return HolidayDateResponse.model_validate(row)


@router.delete("/holiday-dates/{holiday_date_id}", status_code=204)
async def delete_holiday_date_endpoint(
    holiday_date_id: uuid.UUID,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    cache: AccessScheduleCache = Depends(get_access_schedule_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> Response:
    row = await delete_holiday_date(session, holiday_date_id, cache=cache)
    if row is None:
        raise NotFoundError(f"No holiday date found with id '{holiday_date_id}'.")
    await write_audit_entry(
        session,
        actor=ctx,
        action="holiday_date.delete",
        target_type="holiday_date",
        target_id=str(holiday_date_id),
        old_value={"holiday_date": row.holiday_date, "label": row.label},
        new_value=None,
        source_ip=source_ip,
    )
    await session.commit()
    return Response(status_code=204)
