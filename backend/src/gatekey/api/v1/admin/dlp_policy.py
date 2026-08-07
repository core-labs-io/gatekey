"""Admin endpoints for DLP policy (Phase 3, BD-2) - design doc section 9.2.

`require_role(org_admin)` for the org-wide policy + custom patterns; the
team-scoped action-override route lives in `api/v1/teams.py`
(`require_team_role(team_lead)`), mirroring where the team model-restriction
route lives relative to `api/v1/admin/model_policy.py`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import get_cache_invalidator, get_source_ip, require_role
from gatekey.db.models.dlp_policy import DlpAction
from gatekey.db.session import get_db_session
from gatekey.errors import NotFoundError
from gatekey.schemas.dlp_policy import (
    DlpCustomPatternRequest,
    DlpCustomPatternResponse,
    DlpPolicyPutRequest,
    DlpPolicyResponse,
)
from gatekey.services.audit import write_audit_entry
from gatekey.services.dlp import (
    create_custom_pattern,
    delete_custom_pattern,
    get_custom_pattern,
    get_dlp_policy_row,
    list_custom_patterns,
    set_dlp_policy,
    update_custom_pattern,
)
from gatekey.services.response_cache import CacheInvalidator
from gatekey.services.sessions import SessionContext

router = APIRouter(prefix="/v1/admin/dlp-policy", tags=["admin", "dlp"])

_DEFAULT_POLICY_RESPONSE = DlpPolicyResponse(
    ssn_detector_enabled=False,
    credit_card_detector_enabled=False,
    email_detector_enabled=False,
    phone_detector_enabled=False,
    default_action="log",
    store_raw_flagged_content=False,
    scan_inbound_responses=False,
)


def _policy_response(row: object | None) -> DlpPolicyResponse:
    if row is None:
        return _DEFAULT_POLICY_RESPONSE
    return DlpPolicyResponse.model_validate(row)


@router.get("", response_model=DlpPolicyResponse)
async def get_dlp_policy_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> DlpPolicyResponse:
    """Always 200 - defaults (every detector off, `default_action="log"`) if
    no row exists yet, mirroring `ModelPolicy`'s absence-of-row contract."""
    return _policy_response(await get_dlp_policy_row(session))


@router.put("", response_model=DlpPolicyResponse)
async def put_dlp_policy_endpoint(
    payload: DlpPolicyPutRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
    cache_invalidator: CacheInvalidator = Depends(get_cache_invalidator),
) -> DlpPolicyResponse:
    """422 `inbound_scanning_not_implemented` if `scan_inbound_responses` is
    set true - that field is NOT YET IMPLEMENTED (see its schema
    description); no code path scans provider responses, so accepting
    `true` here would silently do nothing. Leave it false/absent."""
    old = _policy_response(await get_dlp_policy_row(session))
    await write_audit_entry(
        session,
        actor=ctx,
        action="dlp_policy.update",
        target_type="dlp_policy",
        target_id=str(ctx.org_id),
        old_value=old.model_dump(),
        new_value=payload.model_dump(),
        source_ip=source_ip,
    )
    row = await set_dlp_policy(
        session,
        ssn_detector_enabled=payload.ssn_detector_enabled,
        credit_card_detector_enabled=payload.credit_card_detector_enabled,
        email_detector_enabled=payload.email_detector_enabled,
        phone_detector_enabled=payload.phone_detector_enabled,
        default_action=DlpAction(payload.default_action),
        store_raw_flagged_content=payload.store_raw_flagged_content,
        scan_inbound_responses=payload.scan_inbound_responses,
        cache_invalidator=cache_invalidator,
    )
    return _policy_response(row)


@router.get("/custom-patterns", response_model=list[DlpCustomPatternResponse])
async def list_custom_patterns_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> list[DlpCustomPatternResponse]:
    rows = await list_custom_patterns(session)
    return [DlpCustomPatternResponse.model_validate(row) for row in rows]


@router.post("/custom-patterns", response_model=DlpCustomPatternResponse, status_code=201)
async def create_custom_pattern_endpoint(
    payload: DlpCustomPatternRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
    cache_invalidator: CacheInvalidator = Depends(get_cache_invalidator),
) -> DlpCustomPatternResponse:
    """422 `invalid_dlp_custom_pattern_regex` / 409
    `dlp_custom_pattern_name_conflict` pass straight through from
    `create_custom_pattern` - no DB write in either case. That call flushes
    (not commits) so `row.id` is available for the audit entry's
    `target_id`, written in the SAME transaction, committed once here.

    Fix 3 (security review, BLOCKING): a new custom pattern can newly flag
    content a cached response was written before it existed - invalidate
    org-wide (a custom pattern applies to every team, no narrower blast
    radius), same rationale as `set_dlp_policy`'s. Hardening pass item 2:
    the invalidation call itself now lives inside `services.dlp.create_
    custom_pattern` (see that function's docstring for the exact timing),
    not here - this handler just threads `cache_invalidator` through.
    """
    row = await create_custom_pattern(
        session,
        name=payload.name,
        pattern=payload.pattern,
        action=DlpAction(payload.action),
        cache_invalidator=cache_invalidator,
    )
    await write_audit_entry(
        session,
        actor=ctx,
        action="dlp_policy.custom_pattern.create",
        target_type="dlp_custom_pattern",
        target_id=str(row.id),
        old_value=None,
        new_value=payload.model_dump(),
        source_ip=source_ip,
    )
    await session.commit()
    return DlpCustomPatternResponse.model_validate(row)


@router.patch("/custom-patterns/{pattern_id}", response_model=DlpCustomPatternResponse)
async def update_custom_pattern_endpoint(
    pattern_id: uuid.UUID,
    payload: DlpCustomPatternRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
    cache_invalidator: CacheInvalidator = Depends(get_cache_invalidator),
) -> DlpCustomPatternResponse:
    """The audit entry is written BEFORE the service call because `update_
    custom_pattern` commits internally (mirrors `api/v1/teams.py`'s `put_
    model_restrictions_endpoint` - same reasoning: its commit persists both
    in one transaction, and 404/422 leave the pending audit row uncommitted,
    rolled back with the session). Fix 3: cache invalidation (org-wide, see
    `create_custom_pattern_endpoint`'s docstring) happens AFTER the service
    call actually commits - hardening pass item 2: that invalidation call now
    lives inside `services.dlp.update_custom_pattern` itself, not here."""
    existing = await get_custom_pattern(session, pattern_id)
    if existing is None:
        raise NotFoundError(f"No custom DLP pattern with id '{pattern_id}'.")
    await write_audit_entry(
        session,
        actor=ctx,
        action="dlp_policy.custom_pattern.update",
        target_type="dlp_custom_pattern",
        target_id=str(pattern_id),
        old_value={"name": existing.name, "pattern": existing.pattern, "action": existing.action.value},
        new_value=payload.model_dump(),
        source_ip=source_ip,
    )
    row = await update_custom_pattern(
        session,
        pattern_id,
        name=payload.name,
        pattern=payload.pattern,
        action=DlpAction(payload.action),
        cache_invalidator=cache_invalidator,
    )
    assert row is not None  # existence already confirmed above
    return DlpCustomPatternResponse.model_validate(row)


@router.delete("/custom-patterns/{pattern_id}", status_code=204)
async def delete_custom_pattern_endpoint(
    pattern_id: uuid.UUID,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
    cache_invalidator: CacheInvalidator = Depends(get_cache_invalidator),
) -> Response:
    existing = await get_custom_pattern(session, pattern_id)
    if existing is None:
        raise NotFoundError(f"No custom DLP pattern with id '{pattern_id}'.")
    await write_audit_entry(
        session,
        actor=ctx,
        action="dlp_policy.custom_pattern.delete",
        target_type="dlp_custom_pattern",
        target_id=str(pattern_id),
        old_value={"name": existing.name, "pattern": existing.pattern, "action": existing.action.value},
        new_value=None,
        source_ip=source_ip,
    )
    await delete_custom_pattern(session, pattern_id, cache_invalidator=cache_invalidator)
    return Response(status_code=204)
