"""Personal API keys - self-serve, delegated, and admin oversight routes
(Phase 2, BD-16) - design doc section 5.6.

Three routers, one module:

- self-serve (`/v1/keys`): session auth, callers operate on their OWN keys
  only. "Must own" failures are a generic 404 `NotFoundError` - a caller
  probing someone else's key id learns nothing about whether it exists
  (anti-enumeration; the ownership filter lives in the SQL lookup itself,
  so "not yours" and "never existed" are indistinguishable by construction).
- delegated (`/v1/teams/{team_id}/members/{user_id}/keys`):
  `require_team_role(team_lead)` (org-admin bypass built in); the target
  user must hold a membership on that team, and key lookups are scoped to
  (owner, team) so a lead can never reach a key outside their team.
- admin (`/v1/admin/keys`): `require_role(org_admin)` session auth per the
  design contract (deliberately NOT the break-glass `require_admin` - the
  design's 5.6 table specifies the session-role dependency). Unified
  listing over service-account ("app") + personal keys with a
  `key_type`/owner discriminator; regenerate/revoke work on either type by
  trying personal first, then service-account, by id.

Every mutation writes exactly one `AuditEntry` in the same DB transaction
(service functions flush; this module audits, then commits) - AC5.10.
Plaintext secrets appear exactly once, in create/regenerate responses.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import (
    TeamRoleContext,
    get_access_schedule_cache,
    get_source_ip,
    require_role,
    require_team_role,
)
from gatekey.db.models.personal_api_key import PersonalApiKey
from gatekey.db.models.service_account_key import ServiceAccountKey
from gatekey.db.models.user import User
from gatekey.db.session import get_db_session
from gatekey.errors import ForbiddenError, GatekeyError, NotFoundError
from gatekey.schemas.access_schedule import (
    AccessSchedulePutRequest,
    AccessScheduleResponse,
    EffectiveScheduleEntry,
)
from gatekey.schemas.personal_api_key import (
    PersonalApiKeyCreateRequest,
    PersonalApiKeyCreateResponse,
    PersonalApiKeyResponse,
)
from gatekey.schemas.rotation_policy import (
    RotateNowResponse,
    RotationPolicyPutRequest,
    RotationPolicyResponse,
)
from gatekey.services.access_schedules import (
    AccessScheduleCache,
    delete_service_account_access_schedule,
    get_service_account_access_schedule,
    list_effective_schedules,
    set_service_account_access_schedule,
)
from gatekey.services.audit import write_audit_entry
from gatekey.services.personal_keys import (
    create_personal_key,
    get_personal_key,
    list_personal_keys_for_owner,
    regenerate_personal_key,
    revoke_personal_key,
)
from gatekey.services.rotation import (
    DEFAULT_OVERLAP_BUFFER_MINUTES,
    deliver_service_account_rotation_notification,
)
from gatekey.services.rotation_policy import (
    get_service_account_rotation_policy,
    set_service_account_rotation_policy,
)
from gatekey.services.service_accounts import (
    get_service_account,
    regenerate_service_account_key,
    revoke_service_account_row,
    rotate_service_account_key,
)
from gatekey.services.sessions import SessionContext, get_current_session
from gatekey.services.teams import get_membership

# One generic message for every "not yours / doesn't exist" rejection.
_KEY_NOT_FOUND_MESSAGE = "No API key found."

router = APIRouter(prefix="/v1/keys", tags=["keys"])
delegated_router = APIRouter(
    prefix="/v1/teams/{team_id}/members/{user_id}/keys", tags=["keys", "teams"]
)
admin_router = APIRouter(prefix="/v1/admin/keys", tags=["admin", "keys"])


def _create_response(row: PersonalApiKey, secret: str) -> PersonalApiKeyCreateResponse:
    return PersonalApiKeyCreateResponse(
        id=row.id,
        name=row.name,
        owner_user_id=row.owner_user_id,
        team_id=row.team_id,
        key_prefix=row.key_prefix,
        secret=secret,
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


async def _audited_create(
    session: AsyncSession,
    *,
    actor: SessionContext,
    owner_user_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    team_id: uuid.UUID,
    name: str,
    expires_at: datetime | None,
) -> PersonalApiKeyCreateResponse:
    """Shared by the self-serve and delegated create routes - one create
    path, one rule set (soft cap / expiry limits enforced in the service)."""
    row, secret = await create_personal_key(
        session,
        owner_user_id=owner_user_id,
        created_by_user_id=created_by_user_id,
        team_id=team_id,
        name=name,
        expires_at=expires_at,
    )
    await write_audit_entry(
        session,
        actor=actor,
        action="personal_key.create",
        target_type="personal_api_key",
        target_id=str(row.id),
        old_value=None,
        new_value={
            "name": row.name,
            "owner_user_id": row.owner_user_id,
            "team_id": row.team_id,
            "key_prefix": row.key_prefix,
            "expires_at": row.expires_at,
        },
    )
    await session.commit()
    await session.refresh(row)  # server default created_at
    return _create_response(row, secret)


async def _audited_regenerate(
    session: AsyncSession, *, actor: SessionContext, row: PersonalApiKey
) -> PersonalApiKeyCreateResponse:
    old_prefix = row.key_prefix
    row, secret = await regenerate_personal_key(session, row)
    await write_audit_entry(
        session,
        actor=actor,
        action="personal_key.regenerate",
        target_type="personal_api_key",
        target_id=str(row.id),
        old_value={"key_prefix": old_prefix},
        new_value={"key_prefix": row.key_prefix},
    )
    await session.commit()
    return _create_response(row, secret)


async def _audited_revoke(
    session: AsyncSession, *, actor: SessionContext, row: PersonalApiKey
) -> None:
    """Idempotent: already-revoked -> 204 with no new audit entry (nothing
    changed), matching the service-accounts DELETE convention."""
    changed = await revoke_personal_key(session, row)
    if changed:
        await write_audit_entry(
            session,
            actor=actor,
            action="personal_key.revoke",
            target_type="personal_api_key",
            target_id=str(row.id),
            old_value={"name": row.name, "key_prefix": row.key_prefix},
            new_value={"revoked_at": row.revoked_at},
        )
        await session.commit()


# --- self-serve (5.6) --------------------------------------------------------


@router.get("", response_model=list[PersonalApiKeyResponse])
async def list_own_keys(
    ctx: SessionContext = Depends(get_current_session),
    session: AsyncSession = Depends(get_db_session),
) -> list[PersonalApiKeyResponse]:
    rows = await list_personal_keys_for_owner(session, ctx.user_id)
    return [PersonalApiKeyResponse.model_validate(row) for row in rows]


@router.post("", response_model=PersonalApiKeyCreateResponse, status_code=201)
async def create_own_key(
    payload: PersonalApiKeyCreateRequest,
    ctx: SessionContext = Depends(get_current_session),
    session: AsyncSession = Depends(get_db_session),
) -> PersonalApiKeyCreateResponse:
    """`team_id` is always required in the body (the server never infers it,
    design doc 5.6) and the caller must hold a membership on that team - the
    rejection is the same generic 403 whether or not the team exists,
    mirroring `require_team_role`'s anti-enumeration posture."""
    if await get_membership(session, team_id=payload.team_id, user_id=ctx.user_id) is None:
        raise ForbiddenError("You do not have the required role for this team.")
    return await _audited_create(
        session,
        actor=ctx,
        owner_user_id=ctx.user_id,
        created_by_user_id=ctx.user_id,
        team_id=payload.team_id,
        name=payload.name,
        expires_at=payload.expires_at,
    )


@router.post("/{key_id}/regenerate", response_model=PersonalApiKeyCreateResponse)
async def regenerate_own_key(
    key_id: uuid.UUID,
    ctx: SessionContext = Depends(get_current_session),
    session: AsyncSession = Depends(get_db_session),
) -> PersonalApiKeyCreateResponse:
    row = await get_personal_key(session, key_id, owner_user_id=ctx.user_id)
    if row is None:
        raise NotFoundError(_KEY_NOT_FOUND_MESSAGE)
    return await _audited_regenerate(session, actor=ctx, row=row)


@router.delete("/{key_id}", status_code=204)
async def revoke_own_key(
    key_id: uuid.UUID,
    ctx: SessionContext = Depends(get_current_session),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    row = await get_personal_key(session, key_id, owner_user_id=ctx.user_id)
    if row is None:
        raise NotFoundError(_KEY_NOT_FOUND_MESSAGE)
    await _audited_revoke(session, actor=ctx, row=row)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- delegated team-lead routes (5.6, AC5.8) ---------------------------------


class DelegatedKeyCreateRequest(BaseModel):
    """`team_id` is implied by the path on the delegated create."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=256)
    # AwareDatetime for the same L-3 reason as PersonalApiKeyCreateRequest.
    expires_at: AwareDatetime | None = None

    @field_validator("name")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank.")
        return value


async def _require_target_membership(
    session: AsyncSession, *, team_id: uuid.UUID, user_id: uuid.UUID
) -> None:
    if await get_membership(session, team_id=team_id, user_id=user_id) is None:
        raise NotFoundError("Team membership not found.")


@delegated_router.get("", response_model=list[PersonalApiKeyResponse])
async def list_member_keys(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
) -> list[PersonalApiKeyResponse]:
    """Delegated view - only the member's keys scoped to THIS team; a lead
    never sees keys the member holds on other teams."""
    await _require_target_membership(session, team_id=team_id, user_id=user_id)
    rows = await list_personal_keys_for_owner(session, user_id, team_id=team_id)
    return [PersonalApiKeyResponse.model_validate(row) for row in rows]


@delegated_router.post("", response_model=PersonalApiKeyCreateResponse, status_code=201)
async def create_member_key(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: DelegatedKeyCreateRequest,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
) -> PersonalApiKeyCreateResponse:
    await _require_target_membership(session, team_id=team_id, user_id=user_id)
    return await _audited_create(
        session,
        actor=team_ctx.session,
        owner_user_id=user_id,
        # `created_by_user_id` is NOT NULL (FK to users) and the break-glass
        # sentinel has no user row (session.user_id is None) - fall back to
        # the owning member; the audit entry written alongside carries the
        # real actor ("system:admin_token", A4) and is the durable record of
        # who actually minted the key.
        created_by_user_id=team_ctx.session.user_id or user_id,
        team_id=team_id,
        name=payload.name,
        expires_at=payload.expires_at,
    )


@delegated_router.post("/{key_id}/regenerate", response_model=PersonalApiKeyCreateResponse)
async def regenerate_member_key(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    key_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
) -> PersonalApiKeyCreateResponse:
    await _require_target_membership(session, team_id=team_id, user_id=user_id)
    row = await get_personal_key(session, key_id, owner_user_id=user_id, team_id=team_id)
    if row is None:
        raise NotFoundError(_KEY_NOT_FOUND_MESSAGE)
    return await _audited_regenerate(session, actor=team_ctx.session, row=row)


@delegated_router.delete("/{key_id}", status_code=204)
async def revoke_member_key(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    key_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await _require_target_membership(session, team_id=team_id, user_id=user_id)
    row = await get_personal_key(session, key_id, owner_user_id=user_id, team_id=team_id)
    if row is None:
        raise NotFoundError(_KEY_NOT_FOUND_MESSAGE)
    await _audited_revoke(session, actor=team_ctx.session, row=row)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- admin oversight (5.6) ---------------------------------------------------


class AdminKeyResponse(BaseModel):
    """One row of the unified admin listing - service-account ("app") and
    personal keys share this shape via the `key_type` discriminator. No
    secret material can appear here (the fields don't exist)."""

    id: uuid.UUID
    key_type: Literal["app", "personal"]
    name: str
    key_prefix: str
    owner_user_id: uuid.UUID
    owner_name: str
    team_id: uuid.UUID | None
    expires_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None
    active: bool


class AdminKeyRegenerateResponse(BaseModel):
    """The ONLY admin-keys schema with a `secret` field - returned exactly
    once by regenerate, never persisted."""

    id: uuid.UUID
    key_type: Literal["app", "personal"]
    name: str
    key_prefix: str
    secret: str


def _personal_admin_row(row: PersonalApiKey, owner_name: str) -> AdminKeyResponse:
    active = PersonalApiKeyResponse.model_validate(row).active
    return AdminKeyResponse(
        id=row.id,
        key_type="personal",
        name=row.name,
        key_prefix=row.key_prefix,
        owner_user_id=row.owner_user_id,
        owner_name=owner_name,
        team_id=row.team_id,
        expires_at=row.expires_at,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        active=active,
    )


def _app_admin_row(row: ServiceAccountKey, owner_name: str) -> AdminKeyResponse:
    return AdminKeyResponse(
        id=row.id,
        key_type="app",
        name=row.name,
        key_prefix=row.key_prefix,
        owner_user_id=row.user_id,
        owner_name=owner_name,
        team_id=row.team_id,
        expires_at=None,
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        active=row.revoked_at is None,
    )


@admin_router.get("", response_model=list[AdminKeyResponse])
async def list_admin_keys(
    key_type: Literal["app", "personal", "all"] = Query(default="all", alias="type"),
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> list[AdminKeyResponse]:
    entries: list[AdminKeyResponse] = []
    if key_type in ("app", "all"):
        rows = (
            await session.execute(
                select(ServiceAccountKey, User.name).join(
                    User, User.id == ServiceAccountKey.user_id
                )
            )
        ).all()
        entries.extend(_app_admin_row(row, name) for row, name in rows)
    if key_type in ("personal", "all"):
        rows = (
            await session.execute(
                select(PersonalApiKey, User.name).join(
                    User, User.id == PersonalApiKey.owner_user_id
                )
            )
        ).all()
        entries.extend(_personal_admin_row(row, name) for row, name in rows)
    entries.sort(key=lambda e: e.created_at)
    return entries


@admin_router.post("/{key_id}/regenerate", response_model=AdminKeyRegenerateResponse)
async def admin_regenerate_key(
    key_id: uuid.UUID,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> AdminKeyRegenerateResponse:
    """Works on either key type - personal is tried first, then
    service-account, by id (design doc 5.6). Revoked keys cannot be
    regenerated (409 for personal, 404-indistinguishable is NOT used here
    since the admin view already lists revocation state)."""
    personal = await get_personal_key(session, key_id)
    if personal is not None:
        result = await _audited_regenerate(session, actor=ctx, row=personal)
        return AdminKeyRegenerateResponse(
            id=result.id,
            key_type="personal",
            name=result.name,
            key_prefix=result.key_prefix,
            secret=result.secret,
        )
    sa_row = await get_service_account(session, key_id)
    if sa_row is None or sa_row.revoked_at is not None:
        raise NotFoundError(_KEY_NOT_FOUND_MESSAGE)
    old_prefix = sa_row.key_prefix
    sa_row, secret = await regenerate_service_account_key(session, sa_row)
    # `service_account_key.regenerate` extends the design's fixed action
    # vocabulary (which predates the unified regenerate surface) - flagged
    # as a deliberate addition, not an ad hoc string.
    await write_audit_entry(
        session,
        actor=ctx,
        action="service_account_key.regenerate",
        target_type="service_account_key",
        target_id=str(sa_row.id),
        old_value={"key_prefix": old_prefix},
        new_value={"key_prefix": sa_row.key_prefix},
    )
    await session.commit()
    return AdminKeyRegenerateResponse(
        id=sa_row.id,
        key_type="app",
        name=sa_row.name,
        key_prefix=sa_row.key_prefix,
        secret=secret,
    )


@admin_router.delete("/{key_id}", status_code=204)
async def admin_revoke_key(
    key_id: uuid.UUID,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Works on either key type (personal first, then service-account).
    Idempotent 204 if already revoked; 404 only when the id matches
    neither table."""
    personal = await get_personal_key(session, key_id)
    if personal is not None:
        await _audited_revoke(session, actor=ctx, row=personal)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    sa_row = await get_service_account(session, key_id)
    if sa_row is None:
        raise NotFoundError(_KEY_NOT_FOUND_MESSAGE)
    changed = await revoke_service_account_row(session, sa_row)
    if changed:
        await write_audit_entry(
            session,
            actor=ctx,
            action="service_account_key.revoke",
            target_type="service_account_key",
            target_id=str(sa_row.id),
            old_value={"name": sa_row.name, "key_prefix": sa_row.key_prefix},
            new_value={"revoked_at": sa_row.revoked_at},
        )
        await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- rotation (Phase 3, BD-15, design doc section 9.6) -----------------------


@admin_router.get("/{key_id}/rotation-policy", response_model=RotationPolicyResponse)
async def get_key_rotation_policy(
    key_id: uuid.UUID,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> RotationPolicyResponse:
    sa_row = await get_service_account(session, key_id)
    if sa_row is None:
        raise NotFoundError(_KEY_NOT_FOUND_MESSAGE)
    row = await get_service_account_rotation_policy(session, key_id)
    if row is None:
        return RotationPolicyResponse(
            enabled=False,
            interval_days=None,
            rotate_at_local_time=None,
            overlap_buffer_minutes=DEFAULT_OVERLAP_BUFFER_MINUTES,
            next_rotation_at=None,
            last_rotated_at=None,
            mode="automatic",
        )
    return RotationPolicyResponse.model_validate(row)


@admin_router.put("/{key_id}/rotation-policy", response_model=RotationPolicyResponse)
async def put_key_rotation_policy(
    key_id: uuid.UUID,
    payload: RotationPolicyPutRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> RotationPolicyResponse:
    """AC7.1: this scope is always `mode="automatic"` - `payload` never
    carries a `mode` field at all (see `RotationPolicyPutRequest`), so
    there is no client input to reject here, only to ignore-by-absence.
    `interval_days=None` while `enabled=True` inherits the org default
    (`services.rotation_policy` module docstring); if neither resolves,
    the written row simply stays enabled with no `next_rotation_at` (never
    fires) rather than erroring - an admin can set the org default
    afterward and this row picks it up on its next edit."""
    sa_row = await get_service_account(session, key_id)
    if sa_row is None:
        raise NotFoundError(_KEY_NOT_FOUND_MESSAGE)
    old = await get_service_account_rotation_policy(session, key_id)
    old_response = (
        RotationPolicyResponse.model_validate(old)
        if old
        else RotationPolicyResponse(
            enabled=False,
            interval_days=None,
            rotate_at_local_time=None,
            overlap_buffer_minutes=DEFAULT_OVERLAP_BUFFER_MINUTES,
            next_rotation_at=None,
            last_rotated_at=None,
            mode="automatic",
        )
    )
    row = await set_service_account_rotation_policy(
        session,
        key_id=key_id,
        enabled=payload.enabled,
        interval_days=payload.interval_days,
        rotate_at_local_time=payload.rotate_at_local_time,
        overlap_buffer_minutes=payload.overlap_buffer_minutes,
    )
    await write_audit_entry(
        session,
        actor=ctx,
        action="rotation_policy.update",
        target_type="service_account_key",
        target_id=str(key_id),
        old_value=old_response.model_dump(mode="json"),
        new_value=payload.model_dump(mode="json"),
        source_ip=source_ip,
    )
    await session.commit()
    return RotationPolicyResponse.model_validate(row)


@admin_router.post("/{key_id}/rotate-now", response_model=RotateNowResponse)
async def rotate_key_now(
    key_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> RotateNowResponse:
    """AC7.6: the SAME short-overlap mechanism as scheduled rotation - never
    an instant swap, and a visibly distinct action/endpoint from `DELETE
    /v1/admin/keys/{id}` (immediate, zero-overlap revoke). Returns the new
    plaintext secret exactly once (one-time-reveal, admin is present in
    this request/response, unlike the fully-automatic scheduled path - see
    `services.rotation` module docstring's AC7.5 gap note)."""
    sa_row = await get_service_account(session, key_id)
    if sa_row is None:
        raise NotFoundError(_KEY_NOT_FOUND_MESSAGE)
    if sa_row.revoked_at is not None:
        raise GatekeyError(
            "Cannot rotate a revoked service-account key.",
            code="service_account_key_revoked",
            status_code=409,
        )
    policy = await get_service_account_rotation_policy(session, key_id)
    overlap_buffer_minutes = policy.overlap_buffer_minutes if policy else DEFAULT_OVERLAP_BUFFER_MINUTES

    rotated = await rotate_service_account_key(
        session, key_id=key_id, overlap_buffer_minutes=overlap_buffer_minutes
    )
    if rotated is None:
        raise NotFoundError(_KEY_NOT_FOUND_MESSAGE)
    row, secret = rotated
    await write_audit_entry(
        session,
        actor=ctx,
        action="service_account_key.rotate_now",
        target_type="service_account_key",
        target_id=str(row.id),
        old_value={"key_prefix": sa_row.key_prefix},
        new_value={"key_prefix": row.key_prefix, "overlap_buffer_minutes": overlap_buffer_minutes},
        source_ip=source_ip,
    )
    await session.commit()

    background_tasks.add_task(
        deliver_service_account_rotation_notification,
        request.app,
        service_account_id=row.id,
        key_name=row.name,
        rotated_at=datetime.now(timezone.utc),
        overlap_expires_at=row.previous_secret_valid_until,
    )
    return RotateNowResponse(
        id=row.id,
        key_prefix=row.key_prefix,
        secret=secret,
        overlap_expires_at=row.previous_secret_valid_until,
    )


# --- access schedule (Phase 3, BD-16/BD-17, design doc section 5/9.7) --------


@admin_router.get("/schedules", response_model=list[EffectiveScheduleEntry])
async def list_key_effective_schedules_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    cache: AccessScheduleCache = Depends(get_access_schedule_cache),
) -> list[EffectiveScheduleEntry]:
    """AC9.10: the fully-resolved EFFECTIVE schedule per service-account
    key ("Mon-Fri 9:00-18:00" / "Always"), not merely has-an-override:
    yes/no. Declared BEFORE `/{key_id}/access-schedule` below so FastAPI's
    literal-first route-matching doesn't treat `schedules` as a `key_id`
    path value."""
    entries = await list_effective_schedules(session, cache=cache)
    return [
        EffectiveScheduleEntry(
            service_account_id=e.service_account_id,
            name=e.name,
            team_id=e.team_id,
            effective=e.effective,
        )
        for e in entries
    ]


@admin_router.get("/{key_id}/access-schedule", response_model=AccessScheduleResponse | None)
async def get_key_access_schedule_endpoint(
    key_id: uuid.UUID,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> AccessScheduleResponse | None:
    sa_row = await get_service_account(session, key_id)
    if sa_row is None:
        raise NotFoundError(_KEY_NOT_FOUND_MESSAGE)
    row = await get_service_account_access_schedule(session, key_id)
    return AccessScheduleResponse.model_validate(row) if row is not None else None


@admin_router.put("/{key_id}/access-schedule", response_model=AccessScheduleResponse)
async def put_key_access_schedule_endpoint(
    key_id: uuid.UUID,
    payload: AccessSchedulePutRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    cache: AccessScheduleCache = Depends(get_access_schedule_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> AccessScheduleResponse:
    """422 `access_schedule_widens_parent` passes straight through from
    `set_service_account_access_schedule` (AC9.2 defense-in-depth against
    the key's resolved team/org parent) - no DB write in that case. The
    key's OWN `team_id` (not a client-supplied value) resolves which parent
    to narrow against."""
    sa_row = await get_service_account(session, key_id)
    if sa_row is None:
        raise NotFoundError(_KEY_NOT_FOUND_MESSAGE)
    old_row = await get_service_account_access_schedule(session, key_id)
    await write_audit_entry(
        session,
        actor=ctx,
        action="service_account_access_schedule.update",
        target_type="service_account_key",
        target_id=str(key_id),
        old_value=AccessScheduleResponse.model_validate(old_row).model_dump(mode="json")
        if old_row is not None
        else None,
        new_value=payload.model_dump(mode="json"),
        source_ip=source_ip,
    )
    row = await set_service_account_access_schedule(
        session,
        key_id,
        team_id=sa_row.team_id,
        enabled=payload.enabled,
        allowed_days=payload.allowed_days,
        allowed_hours_start=payload.allowed_hours_start,
        allowed_hours_end=payload.allowed_hours_end,
        cache=cache,
    )
    return AccessScheduleResponse.model_validate(row)


@admin_router.delete("/{key_id}/access-schedule", status_code=204)
async def delete_key_access_schedule_endpoint(
    key_id: uuid.UUID,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    cache: AccessScheduleCache = Depends(get_access_schedule_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> Response:
    sa_row = await get_service_account(session, key_id)
    if sa_row is None:
        raise NotFoundError(_KEY_NOT_FOUND_MESSAGE)
    row = await get_service_account_access_schedule(session, key_id)
    if row is None:
        raise NotFoundError(f"No access schedule is configured for key '{key_id}'.")
    await write_audit_entry(
        session,
        actor=ctx,
        action="service_account_access_schedule.delete",
        target_type="service_account_key",
        target_id=str(key_id),
        old_value=AccessScheduleResponse.model_validate(row).model_dump(mode="json"),
        new_value=None,
        source_ip=source_ip,
    )
    await delete_service_account_access_schedule(session, key_id, cache=cache)
    return Response(status_code=204)
