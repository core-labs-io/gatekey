"""Hash-chain verification endpoint (Phase 5 - Differentiators, 5.2
Hash-Chained Audit Ledger, AC5.2.4). See
`gatekey/phase-5-technical-design.md` section 3.1 ("GET /v1/admin/audit/
verify") and section 5's wiring checklist "5.1 (Ledger, 5.2)" row 4 (this
router is registered in `main.py` alongside `admin_audit_entries_router`).

A separate router from `api/v1/admin/audit_entries.py`'s `/v1/admin/
audit-entries` prefix - `GET /v1/admin/audit/verify` is its own path per
AC5.2.4's literal spec, not a sub-route of the existing audit-entries
router.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import AdminContext, require_admin_or_auditor
from gatekey.db.session import get_db_session
from gatekey.services.audit_chain import verify_chain
from gatekey.services.compliance_settings import get_effective_compliance_settings

router = APIRouter(prefix="/v1/admin/audit", tags=["admin", "audit"])


class AuditVerifyResponse(BaseModel):
    """AC5.2.4's exact response shape - `status="not_enabled"` (design doc
    section 7.1) before the org has ever turned chaining on;
    `status="intact"` only ever populates `entries_verified`;
    `status="broken"` always names the specific entry (id + chain_seq),
    never a bare boolean."""

    status: Literal["intact", "broken", "not_enabled"]
    entries_verified: int | None = None
    broken_at_entry_id: str | None = None
    broken_at_chain_seq: int | None = None
    expected_prev_hash: str | None = None
    actual_prev_hash: str | None = None


@router.get("/verify", response_model=AuditVerifyResponse, response_model_exclude_none=True)
async def verify_audit_chain_endpoint(
    ctx: AdminContext = Depends(require_admin_or_auditor),
    session: AsyncSession = Depends(get_db_session),
) -> AuditVerifyResponse:
    """`response_model_exclude_none=True` so each status's response body
    matches AC5.2.4's literal shape exactly - `not_enabled` returns just
    `{"status": "not_enabled"}`, `intact` returns `{"status", "entries_
    verified"}`, `broken` returns `{"status", "broken_at_entry_id",
    "broken_at_chain_seq", "expected_prev_hash", "actual_prev_hash"}` - no
    stray null fields from the shared response model's other branches."""
    compliance = await get_effective_compliance_settings(session)
    if not compliance.chain_enabled:
        return AuditVerifyResponse(status="not_enabled")

    result = await verify_chain(session, ctx.org_id)
    if result.status == "broken":
        return AuditVerifyResponse(
            status=result.status,
            broken_at_entry_id=result.broken_at_entry_id,
            broken_at_chain_seq=result.broken_at_chain_seq,
            expected_prev_hash=result.expected_prev_hash,
            actual_prev_hash=result.actual_prev_hash,
        )
    return AuditVerifyResponse(status=result.status, entries_verified=result.entries_verified)
