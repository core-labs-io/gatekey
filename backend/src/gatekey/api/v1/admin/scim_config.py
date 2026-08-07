"""Admin endpoints for `scim_config` (Phase 3, BD-24) - design doc section
6.2, API contract section 9.5. `enabled` toggle + base-URL display:
`GET`/`PUT`. Token issuance/rotation: `POST .../rotate-token`, one-time-
reveal (mirrors the service-account-secret reveal component per AC5.2's
explicit instruction, and immediately invalidates the prior token - no
overlap, unlike the scheduled outbound rotations in `services.rotation`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import get_source_ip, require_role
from gatekey.db.session import get_db_session
from gatekey.schemas.scim_config import (
    ScimConfigPutRequest,
    ScimConfigResponse,
    ScimTokenRotateResponse,
)
from gatekey.services.audit import write_audit_entry
from gatekey.services.scim import get_scim_config, rotate_scim_token, set_scim_enabled
from gatekey.services.sessions import SessionContext

router = APIRouter(prefix="/v1/admin/scim-config", tags=["admin", "scim"])


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/scim/v2"


@router.get("", response_model=ScimConfigResponse)
async def get_scim_config_endpoint(
    request: Request,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> ScimConfigResponse:
    row = await get_scim_config(session)
    return ScimConfigResponse(
        enabled=row.enabled if row is not None else False,
        token_created_at=row.token_created_at if row is not None else None,
        base_url=_base_url(request),
    )


@router.put("", response_model=ScimConfigResponse)
async def put_scim_config_endpoint(
    payload: ScimConfigPutRequest,
    request: Request,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> ScimConfigResponse:
    """AC5.7: org-wide on/off toggle, default off. `set_scim_enabled` commits
    internally, so the audit entry is written first - same "audit-before-
    because-the-service-call-commits" pattern as
    `api/v1/admin/residency_rules.py`'s PUT route."""
    old_row = await get_scim_config(session)
    await write_audit_entry(
        session,
        actor=ctx,
        action="scim_config.update",
        target_type="scim_config",
        target_id=str(ctx.org_id),
        old_value={"enabled": old_row.enabled if old_row is not None else False},
        new_value={"enabled": payload.enabled},
        source_ip=source_ip,
    )
    row = await set_scim_enabled(session, enabled=payload.enabled)
    return ScimConfigResponse(
        enabled=row.enabled, token_created_at=row.token_created_at, base_url=_base_url(request)
    )


@router.post("/rotate-token", response_model=ScimTokenRotateResponse)
async def rotate_scim_token_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> ScimTokenRotateResponse:
    """AC5.2: rotation immediately invalidates the prior token - no overlap
    window. One-time-reveal: `token` is returned in this response body only,
    never persisted, never returned by any other endpoint."""
    await write_audit_entry(
        session,
        actor=ctx,
        action="scim_config.rotate_token",
        target_type="scim_config",
        target_id=str(ctx.org_id),
        old_value=None,
        new_value=None,
        source_ip=source_ip,
    )
    row, token = await rotate_scim_token(session)
    return ScimTokenRotateResponse(token=token, token_created_at=row.token_created_at)
