"""SCIM 2.0 User resource endpoints (Phase 3, BD-22) - design doc sections
6.1-6.4, API contract section 9.5.

Mounted at `/scim/v2/Users` (see `main.py`) - separate from `/v1/...`
because SCIM clients (IdPs) expect the RFC 7644 resource/error shapes, not
this codebase's generic `{"error": {code, message}}` envelope. See
`services/scim.py`'s module docstring for the full rationale on why this
protocol-boundary difference is deliberate, not an oversight - including why
request bodies here are raw `dict`s (via `Body(...)`), not pydantic models:
a `RequestValidationError` on a malformed body would otherwise escape through
the app-wide generic envelope instead of this router's RFC-shaped one.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import ScimContext, get_source_ip, require_scim_token
from gatekey.db.session import get_db_session
from gatekey.services.audit import write_audit_entry
from gatekey.services.scim import (
    build_system_scim_actor,
    create_scim_user,
    get_scim_user,
    list_response,
    list_scim_users,
    parse_user_patch_active,
    parse_user_payload,
    replace_scim_user,
    scim_not_found,
    set_scim_user_active,
    user_to_scim_resource,
)

router = APIRouter(prefix="/scim/v2/Users", tags=["scim"])


@router.get("")
async def list_users(
    startIndex: int = Query(default=1, ge=1),
    count: int = Query(default=100, ge=1, le=500),
    filter_param: str | None = Query(default=None, alias="filter"),
    ctx: ScimContext = Depends(require_scim_token),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    rows, total = await list_scim_users(
        session, filter_str=filter_param, start_index=startIndex, count=count
    )
    return list_response(
        [user_to_scim_resource(row) for row in rows], total=total, start_index=startIndex, count=count
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: dict = Body(...),
    ctx: ScimContext = Depends(require_scim_token),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> dict:
    """AC5.3: `POST /Users` -> `User(org_id, name, sso_email,
    scim_external_id, org_role=NULL)`. Does not create any `TeamMembership` -
    team assignment only ever comes from Group push (AC5.3): a
    SCIM-provisioned user with no group assignment yet has zero gateway
    access, same "zero access until resolved" principle as the self-service
    onboarding flow."""
    fields = parse_user_payload(payload)
    actor = build_system_scim_actor(ctx.org_id)
    user = await create_scim_user(
        session,
        user_name=fields["user_name"],
        display_name=fields["display_name"],
        external_id=fields["external_id"],
    )
    if not fields["active"]:
        # Rare in practice (most IdPs never POST a pre-deactivated user),
        # but honored for correctness - same cascade as any other
        # deactivation.
        user = await set_scim_user_active(session, user, active=False, actor=actor, source_ip=source_ip)
    await write_audit_entry(
        session,
        actor=actor,
        action="scim_user.create",
        target_type="user",
        target_id=str(user.id),
        old_value=None,
        new_value={"sso_email": user.sso_email, "scim_external_id": user.scim_external_id},
        source_ip=source_ip,
    )
    await session.commit()
    return user_to_scim_resource(user)


@router.get("/{user_id}")
async def get_user(
    user_id: uuid.UUID,
    ctx: ScimContext = Depends(require_scim_token),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    user = await get_scim_user(session, user_id)
    if user is None:
        raise scim_not_found("User")
    return user_to_scim_resource(user)


@router.put("/{user_id}")
async def replace_user(
    user_id: uuid.UUID,
    payload: dict = Body(...),
    ctx: ScimContext = Depends(require_scim_token),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> dict:
    user = await get_scim_user(session, user_id)
    if user is None:
        raise scim_not_found("User")
    fields = parse_user_payload(payload)
    was_active = user.scim_deactivated_at is None
    actor = build_system_scim_actor(ctx.org_id)
    user = await replace_scim_user(
        session,
        user,
        user_name=fields["user_name"],
        display_name=fields["display_name"],
        external_id=fields["external_id"],
        active=fields["active"],
        actor=actor,
        source_ip=source_ip,
    )
    await write_audit_entry(
        session,
        actor=actor,
        action="scim_user.deactivate" if was_active and not fields["active"] else "scim_user.update",
        target_type="user",
        target_id=str(user.id),
        old_value={"active": was_active},
        new_value={"active": fields["active"], "sso_email": user.sso_email},
        source_ip=source_ip,
    )
    await session.commit()
    return user_to_scim_resource(user)


@router.patch("/{user_id}")
async def patch_user(
    user_id: uuid.UUID,
    payload: dict = Body(...),
    ctx: ScimContext = Depends(require_scim_token),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> dict:
    """Scoped subset (AC5.1): only an `active` change is understood - see
    `services.scim.parse_user_patch_active`. Any other operation is a
    documented no-op, not silently mis-applied."""
    user = await get_scim_user(session, user_id)
    if user is None:
        raise scim_not_found("User")
    operations = payload.get("Operations") or payload.get("operations") or []
    active = parse_user_patch_active(operations)
    if active is not None:
        was_active = user.scim_deactivated_at is None
        actor = build_system_scim_actor(ctx.org_id)
        user = await set_scim_user_active(session, user, active=active, actor=actor, source_ip=source_ip)
        if was_active != active:
            await write_audit_entry(
                session,
                actor=actor,
                action="scim_user.deactivate" if not active else "scim_user.update",
                target_type="user",
                target_id=str(user.id),
                old_value={"active": was_active},
                new_value={"active": active},
                source_ip=source_ip,
            )
    await session.commit()
    return user_to_scim_resource(user)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    ctx: ScimContext = Depends(require_scim_token),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> Response:
    """AC5.6: never deletes the `User` row - deactivates it exactly like
    `PATCH active:false` (design doc section 6.4), including the full
    revocation cascade."""
    user = await get_scim_user(session, user_id)
    if user is None:
        raise scim_not_found("User")
    was_active = user.scim_deactivated_at is None
    actor = build_system_scim_actor(ctx.org_id)
    user = await set_scim_user_active(session, user, active=False, actor=actor, source_ip=source_ip)
    if was_active:
        await write_audit_entry(
            session,
            actor=actor,
            action="scim_user.deactivate",
            target_type="user",
            target_id=str(user.id),
            old_value={"active": True},
            new_value={"active": False},
            source_ip=source_ip,
        )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
