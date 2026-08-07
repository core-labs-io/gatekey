"""Compliance-settings admin endpoints (Phase 3, BD-10) - design doc section
9.1. Phase 5 (5.2 Hash-Chained Audit Ledger) extends the PUT with
`chain_enabled` - see `gatekey/phase-5-technical-design.md` section 3.1.

`compliance_settings` follows `org_settings`'s ADR-1/ADR-2 pattern exactly
(single row keyed by `org_id`, absence of a row = defaults) - see
`services.compliance_settings`. `GET` never writes; `PUT` upserts.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import get_source_ip, require_role
from gatekey.db.session import get_db_session
from gatekey.schemas.compliance_settings import (
    ComplianceSettingsPutRequest,
    ComplianceSettingsResponse,
)
from gatekey.services.audit import write_audit_entry
from gatekey.services.compliance_settings import (
    EffectiveComplianceSettings,
    get_effective_compliance_settings,
    set_compliance_settings,
)
from gatekey.services.sessions import SessionContext

router = APIRouter(prefix="/v1/admin/compliance-settings", tags=["admin", "compliance"])


def _response(settings: EffectiveComplianceSettings) -> ComplianceSettingsResponse:
    return ComplianceSettingsResponse(
        audit_retention_days=settings.audit_retention_days,
        log_prompt_retention_days=settings.log_prompt_retention_days,
        access_schedule_timezone=settings.access_schedule_timezone,
        chain_enabled=settings.chain_enabled,
    )


@router.get("", response_model=ComplianceSettingsResponse)
async def get_compliance_settings_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> ComplianceSettingsResponse:
    return _response(await get_effective_compliance_settings(session))


@router.put("", response_model=ComplianceSettingsResponse)
async def put_compliance_settings_endpoint(
    payload: ComplianceSettingsPutRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> ComplianceSettingsResponse:
    old = _response(await get_effective_compliance_settings(session))
    await write_audit_entry(
        session,
        actor=ctx,
        action="compliance_settings.update",
        target_type="compliance_settings",
        target_id=str(ctx.org_id),
        old_value=old.model_dump(),
        new_value=payload.model_dump(),
        source_ip=source_ip,
    )
    row = await set_compliance_settings(
        session,
        audit_retention_days=payload.audit_retention_days,
        log_prompt_retention_days=payload.log_prompt_retention_days,
        access_schedule_timezone=payload.access_schedule_timezone,
        chain_enabled=payload.chain_enabled,
    )
    return ComplianceSettingsResponse(
        audit_retention_days=row.audit_retention_days,
        log_prompt_retention_days=row.log_prompt_retention_days,
        access_schedule_timezone=row.access_schedule_timezone,
        chain_enabled=row.chain_enabled,
    )
