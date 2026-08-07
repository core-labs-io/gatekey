"""Admin endpoints for the org-wide residency rule (Phase 3, BD-4) - design
doc section 9.3. The team-scoped route lives in `api/v1/teams.py`
(`require_team_role(team_lead)`, AC3.3 narrowing-only), mirroring where the
team model-restriction route lives relative to this one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import get_cache_invalidator, get_residency_rule_cache, get_source_ip, require_role
from gatekey.db.session import get_db_session
from gatekey.errors import NotFoundError
from gatekey.schemas.residency_rule import ResidencyRulePutRequest, ResidencyRuleResponse
from gatekey.services.audit import write_audit_entry
from gatekey.services.residency import (
    ResidencyRuleCache,
    delete_org_residency_rule,
    get_org_residency_rule,
    set_org_residency_rule,
)
from gatekey.services.response_cache import CacheInvalidator
from gatekey.services.sessions import SessionContext

router = APIRouter(prefix="/v1/admin/residency-rules", tags=["admin", "residency"])


@router.get("", response_model=ResidencyRuleResponse | None)
async def get_org_residency_rule_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> ResidencyRuleResponse | None:
    """`null` (not 404) when no org-wide rule exists yet - "no rule
    configured" is a normal, common state, not an error."""
    row = await get_org_residency_rule(session)
    return ResidencyRuleResponse.model_validate(row) if row is not None else None


@router.put("", response_model=ResidencyRuleResponse)
async def put_org_residency_rule_endpoint(
    payload: ResidencyRulePutRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    cache: ResidencyRuleCache = Depends(get_residency_rule_cache),
    cache_invalidator: CacheInvalidator = Depends(get_cache_invalidator),
    source_ip: str | None = Depends(get_source_ip),
) -> ResidencyRuleResponse:
    """AC3.2: an explicit save to `violation_behavior="warn"` on a rule that
    was (or defaults to) `hard_block` is recorded with the distinct
    `residency_rule.weakened` action name (a downgrade an auditor shouldn't
    have to diff JSON to notice) rather than the generic `.update` - see
    the design doc's exact wording. `set_org_residency_rule` commits
    internally, so the audit entry (which needs no not-yet-known id - the
    org-wide rule's `target_id` is the stable `org_id`) is written first,
    same "audit-before-because-the-service-call-commits" pattern as
    `api/v1/teams.py`'s `put_model_restrictions_endpoint`.
    """
    old_row = await get_org_residency_rule(session)
    old_behavior = old_row.violation_behavior.value if old_row is not None else "hard_block"
    weakened = payload.violation_behavior == "warn" and old_behavior == "hard_block"
    await write_audit_entry(
        session,
        actor=ctx,
        action="residency_rule.weakened" if weakened else "residency_rule.update",
        target_type="residency_rule",
        target_id=str(ctx.org_id),
        old_value={"allowed_regions": sorted(old_row.allowed_regions), "violation_behavior": old_behavior}
        if old_row is not None
        else None,
        new_value=payload.model_dump(),
        source_ip=source_ip,
    )
    row = await set_org_residency_rule(
        session,
        allowed_regions=payload.allowed_regions,
        violation_behavior=payload.violation_behavior,
        cache=cache,
        cache_invalidator=cache_invalidator,
    )
    return ResidencyRuleResponse.model_validate(row)


@router.delete("", status_code=204)
async def delete_org_residency_rule_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    cache: ResidencyRuleCache = Depends(get_residency_rule_cache),
    cache_invalidator: CacheInvalidator = Depends(get_cache_invalidator),
    source_ip: str | None = Depends(get_source_ip),
) -> Response:
    row = await get_org_residency_rule(session)
    if row is None:
        raise NotFoundError("No org-wide residency rule is configured.")
    await write_audit_entry(
        session,
        actor=ctx,
        action="residency_rule.delete",
        target_type="residency_rule",
        target_id=str(ctx.org_id),
        old_value={"allowed_regions": sorted(row.allowed_regions), "violation_behavior": row.violation_behavior.value},
        new_value=None,
        source_ip=source_ip,
    )
    await delete_org_residency_rule(session, cache=cache, cache_invalidator=cache_invalidator)
    return Response(status_code=204)
