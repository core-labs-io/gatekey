"""DB-backed service for the onboarding / join-request workflow (Phase 2,
BD-15) - design doc sections 1.5, 5.2, 5.3.

Transaction contract: mutating functions FLUSH but never COMMIT - the route
handler writes its `AuditEntry` on the same session and commits (design doc
section 7). Approval itself lives in
`services.team_budget.approve_join_request` (the ADR-5 locked path); this
module owns submit, reject, listings, and the org-admin escalation queue.

AC6.4 ("one pending request per user") is the partial unique index
`uq_join_requests_one_pending_per_user` - `submit_join_request` catches the
`IntegrityError` and maps it to a 409 `join_request_already_pending`, never
pre-check-then-insert.

A7 (org-admin queue): a pending request needs org-admin attention when its
team currently has zero `team_lead` memberships (computed live, NOT from
the stored `routed_to` snapshot - a lead may have joined/left since submit)
OR it has been pending >= 5 business days (Mon-Fri).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.join_request import (
    JoinRequest,
    JoinRequestRoutedTo,
    JoinRequestStatus,
)
from gatekey.db.models.team import Team
from gatekey.db.models.team_membership import TeamMembership, TeamRole
from gatekey.errors import GatekeyError, NotFoundError

# A7's ratified Mon-Fri escalation threshold.
ESCALATION_BUSINESS_DAYS = 5

EscalationReason = Literal["no_team_lead", "pending_over_5_business_days"]


def business_days_between(start: datetime, end: datetime) -> int:
    """Count Mon-Fri days in `(start.date(), end.date()]` - "how many
    business days has this been pending". Same-day (or end before start)
    is 0. A plain day loop: pending requests measure days, not years.
    Phase 3's holiday calendar extends this function, not replaces it."""
    day = start.date()
    end_day = end.date()
    count = 0
    while day < end_day:
        day += timedelta(days=1)
        if day.weekday() < 5:  # Mon=0 .. Fri=4
            count += 1
    return count


def compute_escalation_reason(
    *, has_team_lead: bool, requested_at: datetime, now: datetime
) -> EscalationReason | None:
    """Why a pending request belongs in the org-admin queue, or None if it
    doesn't (yet). Pure - unit-testable without a DB."""
    if not has_team_lead:
        return "no_team_lead"
    if business_days_between(requested_at, now) >= ESCALATION_BUSINESS_DAYS:
        return "pending_over_5_business_days"
    return None


async def _team_has_lead(session: AsyncSession, team_id: uuid.UUID) -> bool:
    stmt = (
        select(TeamMembership.id)
        .where(
            TeamMembership.team_id == team_id,
            TeamMembership.role == TeamRole.TEAM_LEAD,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def submit_join_request(
    session: AsyncSession,
    *,
    requester_user_id: uuid.UUID,
    requester_name: str,
    team_id: uuid.UUID,
) -> JoinRequest:
    """Insert a pending request. `requester_name` and `routed_to` are
    snapshots at submit time (design doc 1.5): team has >= 1 team_lead ->
    routed to team_lead, else org_admin. Flushes, does not commit."""
    team_exists = (
        await session.execute(
            select(Team.id).where(Team.org_id == DEFAULT_ORG_ID, Team.id == team_id)
        )
    ).scalar_one_or_none()
    if team_exists is None:
        raise NotFoundError("Team not found.")

    routed_to = (
        JoinRequestRoutedTo.TEAM_LEAD
        if await _team_has_lead(session, team_id)
        else JoinRequestRoutedTo.ORG_ADMIN
    )
    row = JoinRequest(
        org_id=DEFAULT_ORG_ID,
        requester_user_id=requester_user_id,
        requester_name=requester_name,
        team_id=team_id,
        routed_to=routed_to,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        # The AC6.4 partial unique index - see module docstring.
        await session.rollback()
        raise GatekeyError(
            "You already have a pending join request.",
            code="join_request_already_pending",
            status_code=409,
        ) from None
    return row


async def get_latest_join_request(
    session: AsyncSession, requester_user_id: uuid.UUID
) -> tuple[JoinRequest, str] | None:
    """The caller's current/most-recent request (+ team name), for the
    onboarding holding screen (AC6.10). None = never submitted one."""
    stmt = (
        select(JoinRequest, Team.name)
        .join(Team, JoinRequest.team_id == Team.id)
        .where(JoinRequest.requester_user_id == requester_user_id)
        .order_by(JoinRequest.requested_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).one_or_none()
    return None if row is None else (row[0], row[1])


async def list_team_join_requests(
    session: AsyncSession,
    *,
    team_id: uuid.UUID,
    status: JoinRequestStatus | None = None,
) -> list[JoinRequest]:
    stmt = select(JoinRequest).where(JoinRequest.team_id == team_id)
    if status is not None:
        stmt = stmt.where(JoinRequest.status == status)
    stmt = stmt.order_by(JoinRequest.requested_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def get_team_join_request(
    session: AsyncSession, *, team_id: uuid.UUID, request_id: uuid.UUID
) -> JoinRequest | None:
    stmt = select(JoinRequest).where(
        JoinRequest.id == request_id, JoinRequest.team_id == team_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def reject_join_request(
    session: AsyncSession,
    *,
    request_id: uuid.UUID,
    team_id: uuid.UUID,
    resolved_by_user_id: uuid.UUID | None,
    reason: str | None,
) -> JoinRequest:
    """Guarded single-statement UPDATE (same shape as
    `team_budget.approve_join_request`'s status flip): only a still-pending
    request for THIS team flips to rejected - a concurrent resolve sees zero
    rows and gets a clean 404 instead of double-resolving. Flushes via the
    UPDATE itself; does not commit."""
    row = (
        await session.execute(
            update(JoinRequest)
            .where(
                JoinRequest.id == request_id,
                JoinRequest.team_id == team_id,
                JoinRequest.status == JoinRequestStatus.PENDING,
            )
            .values(
                status=JoinRequestStatus.REJECTED,
                resolved_at=func.now(),
                resolved_by_user_id=resolved_by_user_id,
                rejection_reason=reason,
            )
            .returning(JoinRequest)
        )
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Join request not found or no longer pending.")
    return row


@dataclass(frozen=True)
class AdminQueueEntry:
    request: JoinRequest
    team_name: str
    escalation_reason: EscalationReason


async def list_admin_queue(
    session: AsyncSession, *, now: datetime | None = None
) -> list[AdminQueueEntry]:
    """Pending requests needing org-admin attention (design doc 5.3 / A7) -
    computed live at query time, never from the stored `routed_to` snapshot
    (see module docstring). The lead-exists test is SQL (`EXISTS`); the
    business-day arithmetic is Python over the (small) pending set."""
    now = now or datetime.now(timezone.utc)
    lead_exists = (
        select(TeamMembership.id)
        .where(
            TeamMembership.team_id == JoinRequest.team_id,
            TeamMembership.role == TeamRole.TEAM_LEAD,
        )
        .exists()
    )
    stmt = (
        select(JoinRequest, Team.name, lead_exists.label("has_lead"))
        .join(Team, JoinRequest.team_id == Team.id)
        .where(
            JoinRequest.org_id == DEFAULT_ORG_ID,
            JoinRequest.status == JoinRequestStatus.PENDING,
        )
        .order_by(JoinRequest.requested_at)
    )
    entries: list[AdminQueueEntry] = []
    for request, team_name, has_lead in (await session.execute(stmt)).all():
        reason = compute_escalation_reason(
            has_team_lead=bool(has_lead), requested_at=request.requested_at, now=now
        )
        if reason is not None:
            entries.append(
                AdminQueueEntry(request=request, team_name=team_name, escalation_reason=reason)
            )
    return entries
