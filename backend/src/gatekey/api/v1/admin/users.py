"""Admin endpoints for `User` (budget cost-center) management (Phase 1.4 / 1.6).

Follows `api/v1/admin/service_accounts.py`'s exact pattern: router-level
`require_admin` dependency (human-admin trust boundary), no `org_id` param,
`GatekeyError`-based errors, service-layer logic kept out of this route
module.
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import require_admin, require_role
from gatekey.db.models.user import User, UserOrgRole
from gatekey.db.session import get_db_session
from gatekey.errors import GatekeyError, NotFoundError
from gatekey.schemas.user import TeamMembershipSummary, UserCreateRequest, UserResponse, UserUpdateRequest
from gatekey.services.audit import write_audit_entry
from gatekey.services.sessions import SessionContext
from gatekey.services.users import (
    ActiveTeamMembership,
    create_user,
    delete_user,
    get_active_team_memberships,
    get_user,
    list_users,
    update_user,
)

router = APIRouter(prefix="/v1/admin/users", tags=["admin", "users"], dependencies=[Depends(require_admin)])


def _to_user_response(row: User, memberships: list[ActiveTeamMembership]) -> UserResponse:
    return UserResponse(
        id=row.id,
        name=row.name,
        budget_usd=row.budget_usd,
        current_spend_usd=row.current_spend_usd,
        org_role=row.org_role.value if row.org_role is not None else None,
        created_at=row.created_at,
        updated_at=row.updated_at,
        team_memberships=[
            TeamMembershipSummary(
                team_id=m.team_id,
                team_name=m.team_name,
                budget_usd=m.budget_usd,
                current_spend_usd=m.current_spend_usd,
            )
            for m in memberships
        ],
    )


@router.post("", response_model=UserResponse, status_code=201)
async def create_user_endpoint(
    payload: UserCreateRequest, session: AsyncSession = Depends(get_db_session)
) -> UserResponse:
    row = await create_user(session, name=payload.name, budget_usd=payload.budget_usd)
    return _to_user_response(row, [])  # freshly created - cannot have a membership yet


@router.get("", response_model=list[UserResponse])
async def list_users_endpoint(session: AsyncSession = Depends(get_db_session)) -> list[UserResponse]:
    rows = await list_users(session)
    memberships_by_user = await get_active_team_memberships(session, [r.id for r in rows])
    return [_to_user_response(r, memberships_by_user.get(r.id, [])) for r in rows]


@router.get("/{user_id}", response_model=UserResponse)
async def get_user_endpoint(
    user_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
) -> UserResponse:
    row = await get_user(session, user_id)
    if row is None:
        raise NotFoundError(f"No user found with id '{user_id}'.")
    memberships_by_user = await get_active_team_memberships(session, [user_id])
    return _to_user_response(row, memberships_by_user.get(user_id, []))


@router.patch("/{user_id}", response_model=UserResponse)
async def update_user_endpoint(
    user_id: uuid.UUID,
    payload: UserUpdateRequest,
    session: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    row = await update_user(session, user_id, payload.model_dump(exclude_unset=True))
    if row is None:
        raise NotFoundError(f"No user found with id '{user_id}'.")
    memberships_by_user = await get_active_team_memberships(session, [user_id])
    return _to_user_response(row, memberships_by_user.get(user_id, []))


class OrgRoleUpdateRequest(BaseModel):
    """`PATCH /v1/admin/users/{id}/org-role` body (design doc section 5.5).
    `org_role` is required; `null` explicitly clears the org-wide role
    (back to a plain member/team_lead-only user)."""

    model_config = ConfigDict(extra="forbid")

    org_role: Literal["org_admin", "auditor"] | None


class OrgRoleResponse(BaseModel):
    id: uuid.UUID
    name: str
    org_role: Literal["org_admin", "auditor"] | None


@router.patch("/{user_id}/org-role", response_model=OrgRoleResponse)
async def update_org_role_endpoint(
    user_id: uuid.UUID,
    payload: OrgRoleUpdateRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> OrgRoleResponse:
    """Org-wide roles are granted ONLY here (design doc 5.5's AC1.5 note -
    team-member routes structurally cannot express them). Requires an
    org_admin-equivalent caller via `require_role`: an org_admin session,
    or the break-glass bearer token (locked decision #1 - the token keeps
    full Org Admin rights, and it is also the only way to bootstrap the
    FIRST org_admin in a fresh SSO deployment; audited as
    "system:admin_token" per A4).
    Writes a `user.org_role.update` audit entry in the same transaction.
    """
    row = await get_user(session, user_id)
    if row is None:
        raise NotFoundError(f"No user found with id '{user_id}'.")
    old_role = row.org_role.value if row.org_role is not None else None
    row.org_role = UserOrgRole(payload.org_role) if payload.org_role is not None else None
    await session.flush()
    await write_audit_entry(
        session,
        actor=ctx,
        action="user.org_role.update",
        target_type="user",
        target_id=str(user_id),
        old_value={"org_role": old_role},
        new_value={"org_role": payload.org_role},
    )
    await session.commit()
    return OrgRoleResponse(id=row.id, name=row.name, org_role=payload.org_role)


@router.delete("/{user_id}", status_code=204)
async def delete_user_endpoint(
    user_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
) -> Response:
    """204 on delete; 404 if the id doesn't exist; 409 `user_in_use` if one
    or more service-account keys (active or revoked) still reference the
    user - see `services.users.delete_user()`'s docstring.
    """
    result = await delete_user(session, user_id)
    if result is None:
        raise NotFoundError(f"No user found with id '{user_id}'.")
    if result is False:
        raise GatekeyError(
            f"User '{user_id}' is still referenced by one or more service-account keys "
            "and cannot be deleted. Revoke or reassign them first.",
            code="user_in_use",
            status_code=status.HTTP_409_CONFLICT,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
