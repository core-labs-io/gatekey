"""Org-settings admin endpoints (Phase 2, BD-19) - design doc section 5.5.

`org_settings` follows the ADR-1/ADR-2 pattern: single row keyed by
`org_id`, absence of a row = defaults (`services.org_settings`). `GET`
therefore never writes; `PUT` upserts. Ceiling edits go through
`services.team_budget.set_org_budget_ceiling`'s `SELECT ... FOR UPDATE`
check (A3: never below the current sum of team ceilings) - never a bare
column write. The `org_settings.update` audit entry rides the same
transaction (design doc section 7).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import get_source_ip, require_role
from gatekey.db.session import get_db_session
from gatekey.services.audit import write_audit_entry
from gatekey.services.org_settings import (
    DEFAULT_PERSONAL_KEY_SOFT_CAP,
    EffectiveOrgSettings,
    get_effective_org_settings,
)
from gatekey.services.sessions import SessionContext
from gatekey.services.team_budget import set_org_budget_ceiling

router = APIRouter(prefix="/v1/admin/org-settings", tags=["admin", "org-settings"])


class OrgSettingsResponse(BaseModel):
    budget_ceiling_usd: Decimal | None
    currency: str
    max_self_serve_key_expiration_days: int | None
    personal_key_soft_cap: int
    auto_provision_personal_key_on_approval: bool


class OrgSettingsPutRequest(BaseModel):
    """Full-replace PUT. `currency` is structurally pinned to 'USD' this
    phase (ADR-9's identity normalization - the field exists so real FX
    support later is additive, but no other value is writable yet)."""

    model_config = ConfigDict(extra="forbid")

    budget_ceiling_usd: Decimal | None = Field(default=None, ge=0)
    currency: Literal["USD"] = "USD"
    max_self_serve_key_expiration_days: int | None = Field(default=None, ge=1)
    personal_key_soft_cap: int = Field(default=DEFAULT_PERSONAL_KEY_SOFT_CAP, ge=1)
    auto_provision_personal_key_on_approval: bool = False


def _response(settings: EffectiveOrgSettings) -> OrgSettingsResponse:
    return OrgSettingsResponse(
        budget_ceiling_usd=settings.budget_ceiling_usd,
        currency=settings.currency,
        max_self_serve_key_expiration_days=settings.max_self_serve_key_expiration_days,
        personal_key_soft_cap=settings.personal_key_soft_cap,
        auto_provision_personal_key_on_approval=settings.auto_provision_personal_key_on_approval,
    )


@router.get("", response_model=OrgSettingsResponse)
async def get_org_settings_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> OrgSettingsResponse:
    return _response(await get_effective_org_settings(session))


@router.put("", response_model=OrgSettingsResponse)
async def put_org_settings_endpoint(
    payload: OrgSettingsPutRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> OrgSettingsResponse:
    old = await get_effective_org_settings(session)
    # Upserts + locks the row, and runs the A3 sum-of-team-ceilings check
    # (422 budget_ceiling_below_current_allocation passes through). Flushes,
    # does not commit - the remaining field writes and the audit entry ride
    # the same transaction/lock.
    row = await set_org_budget_ceiling(session, budget_ceiling_usd=payload.budget_ceiling_usd)
    row.currency = payload.currency
    row.max_self_serve_key_expiration_days = payload.max_self_serve_key_expiration_days
    row.personal_key_soft_cap = payload.personal_key_soft_cap
    row.auto_provision_personal_key_on_approval = (
        payload.auto_provision_personal_key_on_approval
    )
    await session.flush()
    new_value = payload.model_dump()
    await write_audit_entry(
        session,
        actor=ctx,
        action="org_settings.update",
        target_type="org_settings",
        target_id=str(row.org_id),
        old_value={
            "budget_ceiling_usd": old.budget_ceiling_usd,
            "currency": old.currency,
            "max_self_serve_key_expiration_days": old.max_self_serve_key_expiration_days,
            "personal_key_soft_cap": old.personal_key_soft_cap,
            "auto_provision_personal_key_on_approval": old.auto_provision_personal_key_on_approval,
        },
        new_value=new_value,
        source_ip=source_ip,
    )
    await session.commit()
    return _response(await get_effective_org_settings(session))
