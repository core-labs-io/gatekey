"""Admin endpoints for `sensitivity_label_mappings` (Phase 5 -
Differentiators, 5.3 Content-Classification-Aware Routing, AC5.3.5/AC5.3.8).

Org Admin only (AC5.3.8 - matches the existing static Model Policy tab's and
`content_aware_rules`' own RBAC, `require_role("org_admin")` for every
verb, including `GET` - no Auditor read on this surface per the design
doc's API contract table, section 3.1).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import get_source_ip, require_role
from gatekey.db.session import get_db_session
from gatekey.errors import NotFoundError
from gatekey.schemas.sensitivity_label_mapping import (
    SensitivityLabelMappingRequest,
    SensitivityLabelMappingResponse,
)
from gatekey.services.audit import write_audit_entry
from gatekey.services.sensitivity_label_mappings import (
    create_sensitivity_label_mapping,
    delete_sensitivity_label_mapping,
    get_sensitivity_label_mapping,
    list_sensitivity_label_mappings,
    update_sensitivity_label_mapping,
)
from gatekey.services.sessions import SessionContext

router = APIRouter(
    prefix="/v1/admin/content-aware-rules/sensitivity-label-mappings",
    tags=["admin", "content-aware"],
)


@router.get("", response_model=list[SensitivityLabelMappingResponse])
async def list_sensitivity_label_mappings_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> list[SensitivityLabelMappingResponse]:
    rows = await list_sensitivity_label_mappings(session)
    return [SensitivityLabelMappingResponse.model_validate(row) for row in rows]


@router.post("", response_model=SensitivityLabelMappingResponse, status_code=201)
async def create_sensitivity_label_mapping_endpoint(
    payload: SensitivityLabelMappingRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> SensitivityLabelMappingResponse:
    """409 `sensitivity_label_mapping_conflict` if `external_label` already
    has a mapping for this org - no DB write in that case. `create_
    sensitivity_label_mapping` flushes (not commits) so `row.id` is
    available for the audit entry's `target_id`, written in the SAME
    transaction, committed once here (mirrors `dlp_policy.custom_pattern.
    create`'s exact shape)."""
    row = await create_sensitivity_label_mapping(
        session, external_label=payload.external_label, gatekey_category=payload.gatekey_category
    )
    await write_audit_entry(
        session,
        actor=ctx,
        action="sensitivity_label_mapping.create",
        target_type="sensitivity_label_mapping",
        target_id=str(row.id),
        old_value=None,
        new_value=payload.model_dump(),
        source_ip=source_ip,
    )
    await session.commit()
    return SensitivityLabelMappingResponse.model_validate(row)


@router.put("/{mapping_id}", response_model=SensitivityLabelMappingResponse)
async def update_sensitivity_label_mapping_endpoint(
    mapping_id: uuid.UUID,
    payload: SensitivityLabelMappingRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> SensitivityLabelMappingResponse:
    """The audit entry is written BEFORE the service call because `update_
    sensitivity_label_mapping` commits internally (mirrors `dlp_policy.
    custom_pattern.update`'s identical reasoning: its commit persists both
    in one transaction, and a 404/409 leaves the pending audit row
    uncommitted, rolled back with the session)."""
    existing = await get_sensitivity_label_mapping(session, mapping_id)
    if existing is None:
        raise NotFoundError(f"No sensitivity-label mapping with id '{mapping_id}'.")
    await write_audit_entry(
        session,
        actor=ctx,
        action="sensitivity_label_mapping.update",
        target_type="sensitivity_label_mapping",
        target_id=str(mapping_id),
        old_value={
            "external_label": existing.external_label,
            "gatekey_category": existing.gatekey_category,
        },
        new_value=payload.model_dump(),
        source_ip=source_ip,
    )
    row = await update_sensitivity_label_mapping(
        session,
        mapping_id,
        external_label=payload.external_label,
        gatekey_category=payload.gatekey_category,
    )
    assert row is not None  # existence already confirmed above
    return SensitivityLabelMappingResponse.model_validate(row)


@router.delete("/{mapping_id}", status_code=204)
async def delete_sensitivity_label_mapping_endpoint(
    mapping_id: uuid.UUID,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> Response:
    existing = await get_sensitivity_label_mapping(session, mapping_id)
    if existing is None:
        raise NotFoundError(f"No sensitivity-label mapping with id '{mapping_id}'.")
    await write_audit_entry(
        session,
        actor=ctx,
        action="sensitivity_label_mapping.delete",
        target_type="sensitivity_label_mapping",
        target_id=str(mapping_id),
        old_value={
            "external_label": existing.external_label,
            "gatekey_category": existing.gatekey_category,
        },
        new_value=None,
        source_ip=source_ip,
    )
    await delete_sensitivity_label_mapping(session, mapping_id)
    return Response(status_code=204)
