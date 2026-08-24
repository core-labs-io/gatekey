"""Admin endpoints for the response cache (Phase 4, Reliability & Cost
Efficiency - AC4.3.8, AC4.3.9, technical design section 3.1).

`GET /v1/admin/cache/entries` - team-filtered, teaser-only metadata (never
the cached prompt/response body - AC4.3.9's explicit privacy requirement).
`POST /v1/admin/cache/clear` - `team_id: null` (org-wide) is Org-Admin-only;
a specific `team_id` is available to that team's Team Lead too (AC4.3.8) -
unlike `rate_limits.py`/`caching_settings.py`'s router-level `require_admin`
(where the scope-selecting field made a path-based `require_team_role`
awkward), this endpoint's RBAC genuinely branches on the request body, so
the check is done inline against `get_privileged_session`'s context rather
than skipped/flagged - see `_authorize_clear` below.

Cache data itself lives in Redis/the in-process `SharedStateStore`
(`services/response_cache.py`), not Postgres - `cache_lookup_events` is a
separate hit/miss AUDIT log (Postgres), not the cache data. If no shared
state store is configured/reachable, both endpoints degrade to an empty
list / a "0 entries cleared" response rather than erroring (matches
`services/response_cache.py`'s own fail-open discipline for cache
operations).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, Depends, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import get_privileged_session
from gatekey.db.models.team_membership import TeamMembership
from gatekey.db.session import get_db_session
from gatekey.errors import ForbiddenError
from gatekey.services.response_cache import CacheInvalidator, ResponseCache
from gatekey.services.sessions import SessionContext

router = APIRouter(prefix="/v1/admin/cache", tags=["admin", "cache"])

_LIST_ENTRIES_LIMIT = 200


class CacheEntryTeaser(BaseModel):
    key_preview: str
    team_id: str | None
    user_id: str | None
    provider: str | None
    model: str | None
    input_tokens: int
    output_tokens: int
    created_at: str | None
    expires_at: str | None


class CacheClearRequest(BaseModel):
    team_id: uuid.UUID | None = None


class CacheClearResponse(BaseModel):
    team_id: uuid.UUID | None
    entries_cleared: int


async def _authorize_org_admin_or_team_lead(
    session: AsyncSession, ctx: SessionContext, team_id: uuid.UUID | None, *, action: str
) -> None:
    """Shared RBAC for both endpoints (AC4.3.8/AC4.3.9): org-wide access
    (`team_id=None`) is Org-Admin-only; a specific team's access is
    available to that team's Team Lead too (Org Admin always passes, same
    bypass semantics as `require_team_role`)."""
    if ctx.org_role == "org_admin":
        return
    if team_id is None:
        raise ForbiddenError(f"Only an Org Admin may {action} the cache org-wide.")
    # `removed_at IS NULL` (added by `0049`) - same RBAC-must-cut-off-
    # immediately reasoning as `api.deps._get_team_membership`.
    membership = (
        await session.execute(
            select(TeamMembership).where(
                TeamMembership.team_id == team_id,
                TeamMembership.user_id == ctx.user_id,
                TeamMembership.removed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if membership is None or membership.role.value != "team_lead":
        raise ForbiddenError("You do not have the required role for this team.")


@router.get("/entries", response_model=list[CacheEntryTeaser])
async def list_cache_entries_endpoint(
    request: Request,
    team_id: uuid.UUID | None = None,
    ctx: SessionContext = Depends(get_privileged_session),
    session: AsyncSession = Depends(get_db_session),
) -> list[CacheEntryTeaser]:
    await _authorize_org_admin_or_team_lead(session, ctx, team_id, action="view")

    store = getattr(request.app.state, "shared_state_store", None)
    if store is None:
        return []
    cache = ResponseCache(store)
    raw_entries = await cache.list_entries(team_id, limit=_LIST_ENTRIES_LIMIT)
    return [CacheEntryTeaser(**entry) for entry in raw_entries]


@router.post("/clear", response_model=CacheClearResponse)
async def clear_cache_endpoint(
    request: Request,
    payload: CacheClearRequest | None = Body(default=None),
    ctx: SessionContext = Depends(get_privileged_session),
    session: AsyncSession = Depends(get_db_session),
) -> CacheClearResponse:
    payload = payload or CacheClearRequest()
    await _authorize_org_admin_or_team_lead(session, ctx, payload.team_id, action="clear")

    store = getattr(request.app.state, "shared_state_store", None)
    if store is None:
        return CacheClearResponse(team_id=payload.team_id, entries_cleared=0)

    invalidator = CacheInvalidator(store)
    if payload.team_id is None:
        cleared = await invalidator.clear_all()
    else:
        cleared = await invalidator.clear_team(payload.team_id)
    return CacheClearResponse(team_id=payload.team_id, entries_cleared=cleared)
