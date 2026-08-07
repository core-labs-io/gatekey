"""Admin endpoints for caching configuration (Phase 4, Reliability & Cost Efficiency).

`router` (org-level, `/v1/admin/caching-settings`) is gated by
`require_admin` at the router level - Org Admin (or the break-glass token)
only.

`team_router` (`/v1/admin/teams/{team_id}/cache-settings`) is the
Team-Lead-accessible counterpart for `teams.cache_enabled`/`teams.
cache_ttl_minutes` (AC4.3.2/AC4.3.3, migration `0035` - see `db/models/
team.py`'s "cache_enabled / cache_ttl_minutes" docstring) - a schema/code-
drift gap where those two columns were added and are already READ by the
gateway pipeline (`services.response_cache.load_effective_caching_config`)
but had no admin write surface at all. Deliberately a SEPARATE router with
NO router-level `require_admin` dependency (a route-level dependency cannot
override/bypass a router-level one - FastAPI runs both), gated per-route
instead by `require_team_role` (Org Admin OR that team's own Team Lead -
the SAME dependency `api/v1/admin/degradation_policy.py`'s and `api/v1/
admin/rate_limits.py`'s team-scoped routes already use - see those modules'
docstrings for the identical pattern applied to earlier surfaces).

Unlike `degradation_policy.py`/`rate_limits.py`, there is no separate table
row to upsert here - `cache_enabled`/`cache_ttl_minutes` are plain columns
on `teams` itself, so the team-scoped handlers below read/write the `Team`
row directly.

Fix 6 (NFR gap, AC4.3.4): the gateway hot path (`api.v1.gateway.common.
check_response_cache()`) now reads `CachingSettingsCache` off `app.state`
instead of `load_effective_caching_config()`'s live DB read (see that
function's own docstring, and `services.response_cache.
resolve_effective_caching_config()`) - every write endpoint below refreshes
the relevant cache entry immediately after its commit (same
write-then-refresh-cache pattern `services.model_policy.set_team_model_
policy()` already established), so an admin change is visible on the very
next gateway request, not just after the next process restart.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import TeamRoleContext, get_caching_settings_cache, require_admin, require_team_role
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.caching_settings import CachingSettings
from gatekey.db.models.team import Team
from gatekey.db.session import get_db_session
from gatekey.errors import GatekeyError, NotFoundError
from gatekey.services.response_cache import (
    CachingSettingsCache,
    CachingSettingsSnapshot,
    TeamCachingSettingsSnapshot,
)
from sqlalchemy import select

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin", "caching"],
    dependencies=[Depends(require_admin)],
)

team_router = APIRouter(
    prefix="/v1/admin/teams",
    tags=["admin", "caching", "team"],
)


class CachingSettingsCreate(BaseModel):
    """Request body for creating/updating caching settings."""

    enabled: bool = True
    ttl_seconds: int = 3600


class CachingSettingsResponse(BaseModel):
    """Response schema for caching settings."""

    org_id: uuid.UUID
    enabled: bool
    ttl_seconds: int


@router.get("/caching-settings", response_model=CachingSettingsResponse)
async def get_caching_settings(
    session: AsyncSession = Depends(get_db_session),
) -> CachingSettingsResponse:
    """Get the current caching settings for the default org."""
    row = await session.scalar(select(CachingSettings).where(CachingSettings.org_id == DEFAULT_ORG_ID))
    if row is None:
        # Return default values if not configured
        return CachingSettingsResponse(
            org_id=DEFAULT_ORG_ID,
            enabled=True,  # Default per design doc
            ttl_seconds=3600,  # 1 hour default
        )

    return CachingSettingsResponse(
        org_id=row.org_id,
        enabled=row.enabled,
        ttl_seconds=row.ttl_seconds,
    )


@router.put("/caching-settings", response_model=CachingSettingsResponse)
async def update_caching_settings(
    payload: CachingSettingsCreate = Body(...),
    session: AsyncSession = Depends(get_db_session),
    caching_settings_cache: CachingSettingsCache = Depends(get_caching_settings_cache),
) -> CachingSettingsResponse:
    """Create or update caching settings for the default org."""
    from sqlalchemy import select, insert, update

    # Validate TTL
    if payload.ttl_seconds < 60 or payload.ttl_seconds > 86400:
        raise GatekeyError(
            "ttl_seconds must be between 60 (1 minute) and 86400 (24 hours).",
            code="invalid_ttl",
            status_code=400,
        )

    row = await session.scalar(select(CachingSettings).where(CachingSettings.org_id == DEFAULT_ORG_ID))

    if row is None:
        # Create new settings
        row = CachingSettings(
            org_id=DEFAULT_ORG_ID,
            enabled=payload.enabled,
            ttl_seconds=payload.ttl_seconds,
        )
        session.add(row)
    else:
        # Update existing
        row.enabled = payload.enabled
        row.ttl_seconds = payload.ttl_seconds

    await session.commit()
    # Fix 6 (NFR gap): refresh the process-wide cache immediately after the
    # commit succeeds - see module docstring.
    caching_settings_cache.set_org_settings(
        DEFAULT_ORG_ID, CachingSettingsSnapshot(enabled=row.enabled, ttl_seconds=row.ttl_seconds)
    )

    return CachingSettingsResponse(
        org_id=row.org_id,
        enabled=row.enabled,
        ttl_seconds=row.ttl_seconds,
    )


@router.post("/caching-settings/clear", status_code=200)
async def clear_caching_settings(
    session: AsyncSession = Depends(get_db_session),
    caching_settings_cache: CachingSettingsCache = Depends(get_caching_settings_cache),
) -> dict[str, str]:
    """Clear all caching settings (reset to defaults)."""
    from sqlalchemy import delete

    await session.execute(delete(CachingSettings).where(CachingSettings.org_id == DEFAULT_ORG_ID))
    await session.commit()
    # Fix 6: drop the cached entry too - absence resolves to the documented
    # `enabled=True` default (see `resolve_effective_caching_config()`),
    # matching `load_effective_caching_config()`'s own "no row" behavior.
    caching_settings_cache.set_org_settings(DEFAULT_ORG_ID, None)

    return {"message": "Caching settings cleared, defaults will be used."}


# ============================================================================
# Team-level cache settings endpoints (AC4.3.2/AC4.3.3) - see module
# docstring.
# ============================================================================


class TeamCacheSettingsUpdate(BaseModel):
    """Request body for `PUT /v1/admin/teams/{team_id}/cache-settings`."""

    cache_enabled: bool = False
    cache_ttl_minutes: int = 5


class TeamCacheSettingsResponse(BaseModel):
    """Response schema for a team's cache settings."""

    team_id: uuid.UUID
    cache_enabled: bool
    cache_ttl_minutes: int


def _validate_cache_ttl_minutes(cache_ttl_minutes: int) -> None:
    # Mirrors the DB `CHECK` in migration `0035`
    # (`chk_teams_cache_ttl_minutes_bounds`) so a bad request fails clean
    # (400) instead of as a DB constraint violation.
    if cache_ttl_minutes < 1 or cache_ttl_minutes > 1440:
        raise GatekeyError(
            "cache_ttl_minutes must be between 1 and 1440 (1 minute to 24 hours).",
            code="invalid_cache_ttl",
            status_code=400,
        )


@team_router.get("/{team_id}/cache-settings", response_model=TeamCacheSettingsResponse)
async def get_team_cache_settings(
    team_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead", "member")),
    session: AsyncSession = Depends(get_db_session),
) -> TeamCacheSettingsResponse:
    """Get this team's own `cache_enabled`/`cache_ttl_minutes` settings."""
    row = await session.scalar(select(Team).where(Team.id == team_id))
    if row is None:
        raise NotFoundError(f"No team found with id '{team_id}'.")
    return TeamCacheSettingsResponse(
        team_id=row.id,
        cache_enabled=row.cache_enabled,
        cache_ttl_minutes=row.cache_ttl_minutes,
    )


@team_router.put("/{team_id}/cache-settings", response_model=TeamCacheSettingsResponse)
async def update_team_cache_settings(
    team_id: uuid.UUID,
    payload: TeamCacheSettingsUpdate = Body(...),
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
    caching_settings_cache: CachingSettingsCache = Depends(get_caching_settings_cache),
) -> TeamCacheSettingsResponse:
    """Update this team's own `cache_enabled`/`cache_ttl_minutes` settings.

    Fix 6: refreshes `CachingSettingsCache`'s team entry immediately after
    the commit - see module docstring.
    """
    _validate_cache_ttl_minutes(payload.cache_ttl_minutes)

    row = await session.scalar(select(Team).where(Team.id == team_id))
    if row is None:
        raise NotFoundError(f"No team found with id '{team_id}'.")

    row.cache_enabled = payload.cache_enabled
    row.cache_ttl_minutes = payload.cache_ttl_minutes
    await session.commit()
    await session.refresh(row)
    caching_settings_cache.set_team_settings(
        team_id,
        TeamCachingSettingsSnapshot(
            cache_enabled=row.cache_enabled, cache_ttl_minutes=row.cache_ttl_minutes
        ),
    )

    return TeamCacheSettingsResponse(
        team_id=row.id,
        cache_enabled=row.cache_enabled,
        cache_ttl_minutes=row.cache_ttl_minutes,
    )