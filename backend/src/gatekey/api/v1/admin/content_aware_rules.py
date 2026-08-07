"""Admin endpoints for content-aware routing rules (Phase 3, BD-5) - design
doc section 9.4. Org-wide only (AC4.2 - no team-level override exists).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import get_content_aware_rule_cache, get_source_ip, require_role
from gatekey.db.session import get_db_session
from gatekey.schemas.content_aware_rule import (
    CONTENT_AWARE_CATEGORIES,
    ContentAwareRuleResponse,
    ContentAwareRulesPutRequest,
)
from gatekey.services.audit import write_audit_entry
from gatekey.services.model_policy import (
    ContentAwareRuleCache,
    get_content_aware_rules,
    set_content_aware_rule,
)
from gatekey.services.sessions import SessionContext

router = APIRouter(prefix="/v1/admin/content-aware-rules", tags=["admin", "content-aware"])


async def _full_response(session: AsyncSession) -> list[ContentAwareRuleResponse]:
    """Always renders all three fixed categories (ratified #6) - a category
    with no row yet renders as `enabled=False, allowed_models=[]`, same
    absence-of-row-means-default posture as every other Phase 1.3/3 policy
    table."""
    rows_by_category = {row.category: row for row in await get_content_aware_rules(session)}
    return [
        ContentAwareRuleResponse(
            category=category,
            enabled=rows_by_category[category].enabled if category in rows_by_category else False,
            allowed_models=(
                list(rows_by_category[category].allowed_models) if category in rows_by_category else []
            ),
        )
        for category in CONTENT_AWARE_CATEGORIES
    ]


@router.get("", response_model=list[ContentAwareRuleResponse])
async def get_content_aware_rules_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> list[ContentAwareRuleResponse]:
    return await _full_response(session)


@router.put("", response_model=list[ContentAwareRuleResponse])
async def put_content_aware_rules_endpoint(
    payload: ContentAwareRulesPutRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    cache: ContentAwareRuleCache = Depends(get_content_aware_rule_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> list[ContentAwareRuleResponse]:
    """Upserts every category in `payload.rules` (a category omitted from
    the list is left unchanged - not implicitly disabled). One audit entry
    per changed category, `content_aware_rules.update` (AC4.4: an enabled
    rule with zero `allowed_models` is a real, deliberate "block this
    category entirely" write, not a validation error - nothing here rejects
    an empty `allowed_models` list)."""
    old_by_category = {row.category: row for row in await get_content_aware_rules(session)}
    for item in payload.rules:
        old = old_by_category.get(item.category)
        await write_audit_entry(
            session,
            actor=ctx,
            action="content_aware_rules.update",
            target_type="content_aware_rule",
            target_id=item.category,
            old_value=(
                {"enabled": old.enabled, "allowed_models": sorted(old.allowed_models)}
                if old is not None
                else None
            ),
            new_value={"enabled": item.enabled, "allowed_models": sorted(set(item.allowed_models))},
            source_ip=source_ip,
        )
        # `set_content_aware_rule` commits internally (mirrors `services.
        # model_policy.set_team_model_policy`) - each iteration's audit
        # entry rides that same commit.
        await set_content_aware_rule(
            session,
            item.category,
            enabled=item.enabled,
            allowed_models=item.allowed_models,
            cache=cache,
        )
    return await _full_response(session)
