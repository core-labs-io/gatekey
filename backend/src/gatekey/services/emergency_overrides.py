"""Time-boxed, human-granted bypass of a service-account key's resolved
access schedule (Phase 3, BD-16/BD-18). See
`docs/design/phase-3-security-compliance-design.md` section 5.3 and the
product spec's AC9.6-AC9.9.

Checked only on the access-schedule REJECTION path
(`api.v1.gateway.common.check_access_schedule`) - zero extra I/O in the
common allowed case. `reason` is required and non-empty, enforced
server-side here (AC9.7) - the DB `CHECK (length(reason) > 0)` on
`emergency_overrides` (see `db/models/emergency_override.py`) is
defense-in-depth, not the primary guard.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.emergency_override import EmergencyOverride
from gatekey.errors import GatekeyError


class EmergencyOverrideReasonRequiredError(GatekeyError):
    """AC9.7: a blank/whitespace-only reason is rejected before any write -
    same "reject before writing" discipline as every other service-layer
    validation error in this codebase. 422,
    `code="emergency_override_reason_required"`."""

    status_code = 422
    code = "emergency_override_reason_required"

    def __init__(self) -> None:
        super().__init__("An emergency override requires a non-empty reason.")


class EmergencyOverrideRequiresUserSessionError(GatekeyError):
    """`emergency_overrides.granted_by_user_id` is `NOT NULL` (design doc
    section 1.12 - "a grant record must never be silently orphan-deleted
    via the granter's own deletion", `ON DELETE RESTRICT`) - unlike most
    mutations in this codebase, the break-glass `GATEKEY_ADMIN_TOKEN` actor
    (`user_id=None`, see `api.deps.AdminContext`/`services.sessions.
    BREAK_GLASS_SESSION_CONTEXT`) structurally cannot satisfy this FK.
    Rejected here with a clear, structured error rather than letting a raw
    Postgres `NotNullViolationError` surface as an unhandled 500 - a
    self-hosted operator relying solely on the break-glass token (no SSO
    configured) must grant overrides through a real org_admin session
    instead. 400, `code="emergency_override_requires_user_session"`."""

    status_code = 400
    code = "emergency_override_requires_user_session"

    def __init__(self) -> None:
        super().__init__(
            "Granting an emergency override requires a real user session (not the "
            "break-glass admin token) - the grant is attributed to a specific person."
        )


async def grant_emergency_override(
    session: AsyncSession,
    *,
    service_account_id: uuid.UUID,
    granted_by_user_id: uuid.UUID | None,
    reason: str,
    expires_at: datetime,
) -> EmergencyOverride:
    """Commits internally (same "second-commit deviation" documented on
    `services.service_accounts.revoke_service_account` - the route writes
    its `AuditEntry` afterward, using this call's returned, now-known row
    id, then commits again)."""
    if granted_by_user_id is None:
        raise EmergencyOverrideRequiresUserSessionError()
    stripped_reason = reason.strip()
    if not stripped_reason:
        raise EmergencyOverrideReasonRequiredError()
    row = EmergencyOverride(
        org_id=DEFAULT_ORG_ID,
        service_account_id=service_account_id,
        granted_by_user_id=granted_by_user_id,
        reason=stripped_reason,
        expires_at=expires_at,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def revoke_emergency_override(
    session: AsyncSession, override_id: uuid.UUID, *, revoked_by_user_id: uuid.UUID | None
) -> EmergencyOverride | None:
    """Returns `None` if the id doesn't exist OR was already revoked
    (idempotent no-op in both cases, same disambiguation-by-caller
    convention as `services.service_accounts.revoke_service_account`).
    Commits internally - same second-commit deviation as `grant_emergency_
    override` above.

    `revoked_by_user_id` is nullable (unlike `grant_emergency_override`'s
    required `granted_by_user_id`, `ON DELETE RESTRICT`) - deliberately:
    `require_team_role`'s org-admin/break-glass bypass lets the break-glass
    bearer revoke an override for any team (revoking reduces risk, unlike
    granting), and a break-glass caller has no real `user_id` to record
    (see the audit trail's identical `actor_user_id=None` /
    `actor_label="system:admin_token"` treatment elsewhere). This parameter
    was previously typed non-nullable, mismatching the DB column
    (`db/models/emergency_override.py`), which was the real gap - not the
    call site passing `UUID | None`."""
    row = (
        await session.execute(select(EmergencyOverride).where(EmergencyOverride.id == override_id))
    ).scalar_one_or_none()
    if row is None or row.revoked_at is not None:
        return None
    row.revoked_at = datetime.now(timezone.utc)
    row.revoked_by_user_id = revoked_by_user_id
    await session.commit()
    await session.refresh(row)
    return row


async def get_active_override(
    session: AsyncSession, service_account_id: uuid.UUID, *, now: datetime | None = None
) -> EmergencyOverride | None:
    """Active = non-revoked AND not yet expired as of `now`. Checked only
    on the access-schedule rejection path (design doc section 5.3) - a
    single indexed lookup on `service_account_id`, not a candidate for
    in-process caching (a live, mutable, security-sensitive grant/revoke
    state that must always read the current DB row, same rationale as
    `services.budget`'s per-user spend state)."""
    now = now or datetime.now(timezone.utc)
    stmt = select(EmergencyOverride).where(
        EmergencyOverride.service_account_id == service_account_id,
        EmergencyOverride.revoked_at.is_(None),
        EmergencyOverride.expires_at > now,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_overrides_for_service_account(
    session: AsyncSession, service_account_id: uuid.UUID
) -> list[EmergencyOverride]:
    stmt = (
        select(EmergencyOverride)
        .where(EmergencyOverride.service_account_id == service_account_id)
        .order_by(EmergencyOverride.granted_at.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def get_override(session: AsyncSession, override_id: uuid.UUID) -> EmergencyOverride | None:
    stmt = select(EmergencyOverride).where(EmergencyOverride.id == override_id)
    return (await session.execute(stmt)).scalar_one_or_none()
