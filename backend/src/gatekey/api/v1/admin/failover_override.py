"""Admin endpoint for the per-team failover narrowing override (Phase 4,
Reliability & Cost Efficiency, AC4.1.3).

`TeamFailoverOverride` (migration `0024`, model `db.models.team_failover_
override.TeamFailoverOverride`) had no admin CRUD surface at all before this
module - it was written/read only by the gateway pipeline (`api.v1.gateway.
chat.py`/`completions.py`/`embeddings.py`/`common.py` via `services.
provider_key_health.resolve_failover_opt_in`) and by `main.py`'s startup
cache warm.

There is deliberately NO org-level counterpart router here (unlike
`caching_settings.py`/`degradation_policy.py`/`rate_limits.py`): failover is
enabled at the org/key level via `provider_keys.failover_enabled` (see
`db/models/provider_key.py`'s module docstring), not at this table - this
table can only ever *narrow* (disable) that default for one team, never
enable it (see `db/models/team_failover_override.py`'s docstring - the
column is structurally a one-way "disabled" boolean, no "enabled" value
exists to widen with). Only a Team Lead's own team, or an Org Admin for any
team, may toggle that narrowing - gated by the SAME `require_team_role`
dependency `caching_settings.py`'s/`degradation_policy.py`'s/`rate_limits.
py`'s team-scoped routes already use.

Precedence (confirmed against `services.provider_key_health.
resolve_failover_opt_in` before writing this docstring - do not restate this
from memory without re-checking that function if it ever changes): failover
actually applies to a request only if BOTH the resolved provider key's own
`failover_enabled` is `true` AND this team's `failover_disabled` override
(if any row exists) is `false`. A Team Lead can therefore only ever turn
failover further OFF for their own team (a compliance opt-out) - flipping
`failover_disabled` to `false` here (or deleting the row) never forces
failover ON if the org/key level already has it disabled. `GET` here simply
reports this team's own override row truthfully; it does not attempt to
resolve or expose the combined effective value (that combination depends on
which provider key would be selected for a given request, which this
team-scoped settings surface has no reason to know).

Refreshes `TeamFailoverOverrideCache` (the process-local, lock-free,
full-replace-snapshot cache `services.provider_key_health` and `main.py`'s
lifespan already define and warm at startup) after every write, via
`TeamFailoverOverrideCache.set_one` - the same
"commit-then-refresh-the-in-process-cache" discipline
`services.residency.set_org_residency_rule`/`set_team_residency_rule`
already establish for `ResidencyRuleCache`, so a gateway request landing on
this process immediately after an admin `PUT` never reads a stale
pre-write value.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import (
    TeamRoleContext,
    get_team_failover_override_cache,
    require_team_role,
)
from gatekey.db.models.team_failover_override import TeamFailoverOverride
from gatekey.db.session import get_db_session
from gatekey.services.provider_key_health import (
    TeamFailoverOverrideCache,
    TeamFailoverOverrideSnapshot,
)

team_router = APIRouter(
    prefix="/v1/admin/teams",
    tags=["admin", "failover", "team"],
)


class TeamFailoverOverrideUpdate(BaseModel):
    """Request body for `PUT /v1/admin/teams/{team_id}/failover-override`.

    See module docstring - `failover_disabled=true` narrows (suppresses)
    failover for this team; `failover_disabled=false` simply removes any
    narrowing this team previously applied, it can never force failover on
    if the org/key level has it off.
    """

    failover_disabled: bool = False


class TeamFailoverOverrideResponse(BaseModel):
    """Response schema for a team's failover override state."""

    team_id: uuid.UUID
    failover_disabled: bool


@team_router.get("/{team_id}/failover-override", response_model=TeamFailoverOverrideResponse)
async def get_team_failover_override(
    team_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead", "member")),
    session: AsyncSession = Depends(get_db_session),
) -> TeamFailoverOverrideResponse:
    """Return this team's own override row, or the default (no narrowing,
    `failover_disabled=false`) if no row has ever been written for it."""
    row = await session.scalar(
        select(TeamFailoverOverride).where(TeamFailoverOverride.team_id == team_id)
    )
    return TeamFailoverOverrideResponse(
        team_id=team_id,
        failover_disabled=row.failover_disabled if row is not None else False,
    )


@team_router.put("/{team_id}/failover-override", response_model=TeamFailoverOverrideResponse)
async def update_team_failover_override(
    team_id: uuid.UUID,
    payload: TeamFailoverOverrideUpdate = Body(...),
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
    cache: TeamFailoverOverrideCache = Depends(get_team_failover_override_cache),
) -> TeamFailoverOverrideResponse:
    """Upsert this team's own override row - `team_id` is always taken from
    the URL path (the same id `require_team_role` authorizes against), never
    body-controlled, so a Team Lead can never target a different team's
    override through this surface."""
    insert_stmt = postgresql.insert(TeamFailoverOverride).values(
        team_id=team_id,
        failover_disabled=payload.failover_disabled,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[TeamFailoverOverride.team_id],
        set_={
            "failover_disabled": insert_stmt.excluded.failover_disabled,
            "updated_at": func.now(),
        },
    ).returning(TeamFailoverOverride)
    row = (await session.execute(upsert_stmt)).scalar_one()
    await session.commit()

    cache.set_one(team_id, TeamFailoverOverrideSnapshot(failover_disabled=row.failover_disabled))

    return TeamFailoverOverrideResponse(team_id=row.team_id, failover_disabled=row.failover_disabled)
