"""CRUD for `rotation_policies` rows (Phase 3, BD-15). See
`docs/design/phase-3-security-compliance-design.md` sections 1.11/9.6.

Same one-row-per-scope upsert shape as `services.residency.
set_org_residency_rule`/`set_team_residency_rule` (partial-unique-index
`ON CONFLICT`, not a plain PK), extended to three scope levels instead of
two.

**Org-default cascade, explicitly scoped**: the org-wide row
(`get_org_rotation_policy`/`set_org_rotation_policy`) is a TEMPLATE only -
enabling it does not retroactively create or enable a per-key row for
every existing `ServiceAccountKey` (that would be a bulk fan-out operation
with its own edge cases - new-key-creation wiring, a "reset to org default"
action - deliberately not built this pass). What it DOES do: a per-key
`PUT` that omits `interval_days` inherits the org default's `interval_days`
at write time (AC7.1's "any key can override it"), materialized into the
per-key row rather than resolved live on every scheduler tick. Flagged as a
known, deliberate scope boundary, not an oversight - see this task's
handoff notes for the suggested follow-up (auto-provision a per-key row,
inheriting the org default, in `services.service_accounts.
create_service_account` when the org default is enabled).
"""

from __future__ import annotations

import uuid
from datetime import datetime, time as time_type, timezone

from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.rotation_policy import RotationMode, RotationPolicy, RotationScopeType
from gatekey.services.compliance_settings import get_effective_compliance_settings
from gatekey.services.rotation import compute_next_rotation


async def get_org_rotation_policy(session: AsyncSession) -> RotationPolicy | None:
    return (
        await session.execute(
            select(RotationPolicy).where(
                RotationPolicy.org_id == DEFAULT_ORG_ID,
                RotationPolicy.scope_type == RotationScopeType.ORG,
            )
        )
    ).scalar_one_or_none()


async def set_org_rotation_policy(
    session: AsyncSession,
    *,
    enabled: bool,
    interval_days: int | None,
    rotate_at_local_time: time_type | None,
    overlap_buffer_minutes: int,
) -> RotationPolicy:
    """The org-wide default template. `mode` is fixed to `automatic` on
    this row - see module docstring; it is never itself polled by the
    scheduler (only per-key rows are - see `services.scheduler.
    run_due_rotations`), so the value is a placeholder satisfying the
    NOT NULL column, not a live automation switch at this scope."""
    insert_stmt = postgresql.insert(RotationPolicy).values(
        id=uuid.uuid4(),
        org_id=DEFAULT_ORG_ID,
        scope_type=RotationScopeType.ORG,
        enabled=enabled,
        interval_days=interval_days,
        rotate_at_local_time=rotate_at_local_time,
        overlap_buffer_minutes=overlap_buffer_minutes,
        mode=RotationMode.AUTOMATIC,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[RotationPolicy.org_id],
        index_where=text("scope_type = 'org'"),
        set_={
            "enabled": insert_stmt.excluded.enabled,
            "interval_days": insert_stmt.excluded.interval_days,
            "rotate_at_local_time": insert_stmt.excluded.rotate_at_local_time,
            "overlap_buffer_minutes": insert_stmt.excluded.overlap_buffer_minutes,
            "updated_at": text("now()"),
        },
    ).returning(RotationPolicy)
    row = (await session.execute(upsert_stmt)).scalar_one()
    await session.commit()
    return row


async def get_service_account_rotation_policy(
    session: AsyncSession, key_id: uuid.UUID
) -> RotationPolicy | None:
    return (
        await session.execute(
            select(RotationPolicy).where(RotationPolicy.scope_service_account_id == key_id)
        )
    ).scalar_one_or_none()


async def set_service_account_rotation_policy(
    session: AsyncSession,
    *,
    key_id: uuid.UUID,
    enabled: bool,
    interval_days: int | None,
    rotate_at_local_time: time_type | None,
    overlap_buffer_minutes: int,
) -> RotationPolicy:
    """AC7.1: `mode` is always `automatic` for this scope - never a client
    input. `interval_days=None` inherits the org default's `interval_days`
    (module docstring); if neither resolves to a value, `enabled=True` is
    rejected by the caller (route-level validation) before this is called.
    `next_rotation_at` is (re)computed here whenever the row is enabled -
    this is the row the scheduler's due-work query polls."""
    resolved_interval_days = interval_days
    if resolved_interval_days is None:
        org_policy = await get_org_rotation_policy(session)
        resolved_interval_days = org_policy.interval_days if org_policy else None

    compliance = await get_effective_compliance_settings(session)
    next_rotation_at = None
    if enabled and resolved_interval_days is not None:
        next_rotation_at = compute_next_rotation(
            now=datetime.now(timezone.utc),
            interval_days=resolved_interval_days,
            rotate_at_local_time=rotate_at_local_time,
            timezone_name=compliance.access_schedule_timezone,
        )

    insert_stmt = postgresql.insert(RotationPolicy).values(
        id=uuid.uuid4(),
        org_id=DEFAULT_ORG_ID,
        scope_type=RotationScopeType.SERVICE_ACCOUNT,
        scope_service_account_id=key_id,
        enabled=enabled,
        interval_days=resolved_interval_days,
        rotate_at_local_time=rotate_at_local_time,
        overlap_buffer_minutes=overlap_buffer_minutes,
        next_rotation_at=next_rotation_at,
        mode=RotationMode.AUTOMATIC,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[RotationPolicy.scope_service_account_id],
        index_where=text("scope_service_account_id IS NOT NULL"),
        set_={
            "enabled": insert_stmt.excluded.enabled,
            "interval_days": insert_stmt.excluded.interval_days,
            "rotate_at_local_time": insert_stmt.excluded.rotate_at_local_time,
            "overlap_buffer_minutes": insert_stmt.excluded.overlap_buffer_minutes,
            "next_rotation_at": insert_stmt.excluded.next_rotation_at,
            "updated_at": text("now()"),
        },
    ).returning(RotationPolicy)
    row = (await session.execute(upsert_stmt)).scalar_one()
    await session.commit()
    return row


async def get_provider_key_rotation_policy(
    session: AsyncSession, provider_key_id: uuid.UUID
) -> RotationPolicy | None:
    return (
        await session.execute(
            select(RotationPolicy).where(RotationPolicy.scope_provider_key_id == provider_key_id)
        )
    ).scalar_one_or_none()


async def set_provider_key_rotation_policy(
    session: AsyncSession,
    *,
    provider_key_id: uuid.UUID,
    enabled: bool,
    interval_days: int | None,
    overlap_buffer_minutes: int,
) -> RotationPolicy:
    """AC7.1/AC7.7: `mode` is always `manual_guided` - never auto-fired by
    the scheduler (see `services.scheduler.run_due_rotations`'s explicit
    `scope_type == SERVICE_ACCOUNT` filter). `next_rotation_at` here is
    informational only (a suggested-rotation-date indicator for the admin
    console), fixed at the org off-hours anchor rather than any
    access-schedule (AC7.7: a provider key backs potentially many
    teams/apps, "no single off-hours")."""
    compliance = await get_effective_compliance_settings(session)
    next_rotation_at = None
    if enabled and interval_days is not None:
        next_rotation_at = compute_next_rotation(
            now=datetime.now(timezone.utc),
            interval_days=interval_days,
            rotate_at_local_time=None,
            timezone_name=compliance.access_schedule_timezone,
        )

    insert_stmt = postgresql.insert(RotationPolicy).values(
        id=uuid.uuid4(),
        org_id=DEFAULT_ORG_ID,
        scope_type=RotationScopeType.PROVIDER_KEY,
        scope_provider_key_id=provider_key_id,
        enabled=enabled,
        interval_days=interval_days,
        overlap_buffer_minutes=overlap_buffer_minutes,
        next_rotation_at=next_rotation_at,
        mode=RotationMode.MANUAL_GUIDED,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[RotationPolicy.scope_provider_key_id],
        index_where=text("scope_provider_key_id IS NOT NULL"),
        set_={
            "enabled": insert_stmt.excluded.enabled,
            "interval_days": insert_stmt.excluded.interval_days,
            "overlap_buffer_minutes": insert_stmt.excluded.overlap_buffer_minutes,
            "next_rotation_at": insert_stmt.excluded.next_rotation_at,
            "updated_at": text("now()"),
        },
    ).returning(RotationPolicy)
    row = (await session.execute(upsert_stmt)).scalar_one()
    await session.commit()
    return row
