"""SCIM 2.0 Group resource endpoints (Phase 3, BD-23) - design doc sections
6.1-6.4, API contract section 9.5. Groups map onto `Team`/`TeamMembership`
exactly as `services/scim.py` describes - see that module's docstring for
the RFC-shaped error/response rationale shared with `api/v1/scim/users.py`.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import ScimContext, get_source_ip, require_scim_token
from gatekey.db.session import get_db_session
from gatekey.services.audit import write_audit_entry
from gatekey.services.scim import (
    add_scim_group_members,
    build_system_scim_actor,
    create_scim_group,
    delete_scim_group,
    get_scim_group,
    group_to_scim_resource,
    list_response,
    list_scim_groups,
    parse_group_patch_member_ops,
    parse_group_payload,
    remove_scim_group_members,
    replace_scim_group_members,
    scim_not_found,
)
from gatekey.services.teams import list_team_members

router = APIRouter(prefix="/scim/v2/Groups", tags=["scim"])


async def _resource(session: AsyncSession, team) -> dict:
    members = [(user.id, user.name) for _membership, user in await list_team_members(session, team.id)]
    return group_to_scim_resource(team, members)


@router.get("")
async def list_groups(
    startIndex: int = Query(default=1, ge=1),
    count: int = Query(default=100, ge=1, le=500),
    filter_param: str | None = Query(default=None, alias="filter"),
    ctx: ScimContext = Depends(require_scim_token),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    rows, total = await list_scim_groups(
        session, filter_str=filter_param, start_index=startIndex, count=count
    )
    resources = [await _resource(session, row) for row in rows]
    return list_response(resources, total=total, start_index=startIndex, count=count)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: dict = Body(...),
    ctx: ScimContext = Depends(require_scim_token),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> dict:
    fields = parse_group_payload(payload)
    actor = build_system_scim_actor(ctx.org_id)
    team = await create_scim_group(session, display_name=fields["display_name"], external_id=fields["external_id"])
    await write_audit_entry(
        session,
        actor=actor,
        action="scim_group.create",
        target_type="team",
        target_id=str(team.id),
        old_value=None,
        new_value={"name": team.name, "scim_external_id": team.scim_external_id},
        source_ip=source_ip,
    )
    if fields["member_ids"]:
        await add_scim_group_members(session, team, fields["member_ids"], actor=actor, source_ip=source_ip)
    await session.commit()
    return await _resource(session, team)


@router.get("/{group_id}")
async def get_group(
    group_id: uuid.UUID,
    ctx: ScimContext = Depends(require_scim_token),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    team = await get_scim_group(session, group_id)
    if team is None:
        raise scim_not_found("Group")
    return await _resource(session, team)


@router.put("/{group_id}")
async def replace_group(
    group_id: uuid.UUID,
    payload: dict = Body(...),
    ctx: ScimContext = Depends(require_scim_token),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> dict:
    team = await get_scim_group(session, group_id)
    if team is None:
        raise scim_not_found("Group")
    fields = parse_group_payload(payload)
    actor = build_system_scim_actor(ctx.org_id)
    old_name = team.name
    team.name = fields["display_name"]
    team.scim_external_id = fields["external_id"]
    await session.flush()
    # QA fix: same `updated_at` expiry-after-flush issue as `services.scim.
    # set_scim_user_active` - see that function's comment. `team.updated_at`
    # is read by `group_to_scim_resource` below, after `session.commit()`,
    # outside any awaited context.
    await session.refresh(team)
    await replace_scim_group_members(session, team, fields["member_ids"], actor=actor, source_ip=source_ip)
    await write_audit_entry(
        session,
        actor=actor,
        action="scim_group.update",
        target_type="team",
        target_id=str(team.id),
        old_value={"name": old_name},
        new_value={"name": team.name},
        source_ip=source_ip,
    )
    await session.commit()
    return await _resource(session, team)


@router.patch("/{group_id}")
async def patch_group(
    group_id: uuid.UUID,
    payload: dict = Body(...),
    ctx: ScimContext = Depends(require_scim_token),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> dict:
    """Scoped subset (AC5.1): `add`/`remove` on `members` only - see
    `services.scim.parse_group_patch_member_ops`."""
    team = await get_scim_group(session, group_id)
    if team is None:
        raise scim_not_found("Group")
    operations = payload.get("Operations") or payload.get("operations") or []
    add_ids, remove_ids = parse_group_patch_member_ops(operations)
    actor = build_system_scim_actor(ctx.org_id)
    if add_ids:
        await add_scim_group_members(session, team, add_ids, actor=actor, source_ip=source_ip)
    if remove_ids:
        await remove_scim_group_members(session, team, remove_ids, actor=actor, source_ip=source_ip)
    await session.commit()
    return await _resource(session, team)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(
    group_id: uuid.UUID,
    ctx: ScimContext = Depends(require_scim_token),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> Response:
    team = await get_scim_group(session, group_id)
    if team is None:
        raise scim_not_found("Group")
    await delete_scim_group(session, team, actor=build_system_scim_actor(ctx.org_id), source_ip=source_ip)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
