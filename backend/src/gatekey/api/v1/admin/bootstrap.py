"""One-call onboarding bootstrap (Tier 4 ops/DX polish).

`POST /v1/admin/bootstrap` collapses the four-entity first-key chain
(user -> team -> membership+budget -> service-account key) - previously
three console screens with strict ordering, the most error-prone step of
onboarding - into a single atomic admin call.

Atomicity, deliberately built around the existing services' commit
semantics rather than changing them:
- the `User` row is added/flushed directly (NOT via `services.users.
  create_user`, which commits internally - a mid-chain commit would leave
  a stray user behind on later failure);
- `create_team` flushes; `create_team_membership` flushes under its
  `SELECT ... FOR UPDATE` teams lock; the team.create / team.member.add
  audit entries are written BEFORE that lock is taken (the codebase-wide
  lock-ordering rule from the CMR-14 security review: audit-chain lock
  first);
- `create_service_account` runs LAST - it commits internally (Phase 1
  semantics, kept for its other callers), and that one commit finalizes
  everything above atomically. Any earlier failure means nothing was ever
  committed.
- the `service_account_key.create` audit entry rides a second commit
  immediately after, exactly the accepted deviation the standalone
  create-key endpoint documents.

The response includes the key's plaintext `secret` - returned here only,
never retrievable again, same contract as the standalone endpoint.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import AdminContext, get_source_ip, require_admin
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.team_membership import TeamRole
from gatekey.db.models.user import User
from gatekey.db.session import get_db_session
from gatekey.errors import GatekeyError
from gatekey.services.audit import write_audit_entry
from gatekey.services.service_accounts import create_service_account
from gatekey.services.team_budget import create_team_membership
from gatekey.services.teams import create_team

router = APIRouter(
    prefix="/v1/admin/bootstrap",
    tags=["admin", "bootstrap"],
    dependencies=[Depends(require_admin)],
)


class BootstrapRequest(BaseModel):
    user_name: str = Field(min_length=1, max_length=255)
    team_name: str = Field(min_length=1, max_length=255)
    # None = unmetered member (same semantics as adding a member by hand).
    budget_usd: Decimal | None = Field(default=None, gt=0)
    # Defaults to "<team_name> key" when omitted.
    key_name: str | None = Field(default=None, min_length=1, max_length=255)


class BootstrapUser(BaseModel):
    id: uuid.UUID
    name: str


class BootstrapTeam(BaseModel):
    id: uuid.UUID
    name: str


class BootstrapMembership(BaseModel):
    role: str
    budget_usd: Decimal | None


class BootstrapServiceAccountKey(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    secret: str
    created_at: datetime


class BootstrapResponse(BaseModel):
    user: BootstrapUser
    team: BootstrapTeam
    membership: BootstrapMembership
    service_account_key: BootstrapServiceAccountKey


@router.post("", response_model=BootstrapResponse, status_code=201)
async def bootstrap_endpoint(
    payload: BootstrapRequest,
    session: AsyncSession = Depends(get_db_session),
    admin_ctx: AdminContext = Depends(require_admin),
    source_ip: str | None = Depends(get_source_ip),
) -> BootstrapResponse:
    # 1. User - direct add+flush (see module docstring for why not
    #    `create_user`). The flat per-user budget stays NULL: a team-bound
    #    key charges the membership budget, never this legacy field.
    user = User(org_id=DEFAULT_ORG_ID, name=payload.user_name, budget_usd=None)
    session.add(user)
    await session.flush()

    # 2. Team - flush-only; duplicate name -> clean 409 from the service.
    team = await create_team(session, name=payload.team_name)

    # 3. Audit entries FIRST (lock-ordering rule), with pre-generated ids
    #    where the target row doesn't exist yet.
    membership_id = uuid.uuid4()
    await write_audit_entry(
        session,
        actor=admin_ctx,
        action="team.create",
        target_type="team",
        target_id=str(team.id),
        old_value=None,
        new_value={"name": team.name, "bootstrap": True},
        source_ip=source_ip,
    )
    await write_audit_entry(
        session,
        actor=admin_ctx,
        action="team.member.add",
        target_type="team_membership",
        target_id=str(membership_id),
        old_value=None,
        new_value={
            "team_id": team.id,
            "user_id": user.id,
            "role": "member",
            "budget_usd": payload.budget_usd,
            "bootstrap": True,
        },
        source_ip=source_ip,
    )

    # 4. Membership (takes the teams FOR UPDATE lock).
    try:
        membership = await create_team_membership(
            session,
            team_id=team.id,
            user_id=user.id,
            role=TeamRole.MEMBER,
            budget_usd=payload.budget_usd,
            membership_id=membership_id,
        )
    except IntegrityError:
        await session.rollback()
        raise GatekeyError(
            "Bootstrap failed creating the team membership.",
            code="bootstrap_membership_failed",
            status_code=409,
        ) from None

    # 5. Service-account key LAST - its internal commit finalizes the whole
    #    chain atomically (module docstring).
    key_name = payload.key_name or f"{payload.team_name} key"
    key_row, secret = await create_service_account(
        session, key_name, user.id, team_id=team.id
    )

    # 6. Key audit entry - second commit, same accepted deviation as the
    #    standalone create-key endpoint.
    await write_audit_entry(
        session,
        actor=admin_ctx,
        action="service_account_key.create",
        target_type="service_account_key",
        target_id=str(key_row.id),
        old_value=None,
        new_value={
            "name": key_row.name,
            "user_id": key_row.user_id,
            "team_id": key_row.team_id,
            "key_prefix": key_row.key_prefix,
            "bootstrap": True,
        },
        source_ip=source_ip,
    )
    await session.commit()

    return BootstrapResponse(
        user=BootstrapUser(id=user.id, name=user.name),
        team=BootstrapTeam(id=team.id, name=team.name),
        membership=BootstrapMembership(
            role=membership.role
            if isinstance(membership.role, str)
            else membership.role.value,
            budget_usd=membership.budget_usd,
        ),
        service_account_key=BootstrapServiceAccountKey(
            id=key_row.id,
            name=key_row.name,
            key_prefix=key_row.key_prefix,
            secret=secret,
            created_at=key_row.created_at,
        ),
    )
