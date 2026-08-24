"""Assignment-time budget-ceiling enforcement (Phase 2, BD-9).

See `docs/design/phase-2-multi-tenant-governance-design.md` section 3.3
(ADR-5) and A3. Every write that must satisfy "sum of children <= parent's
ceiling" runs inside a transaction that first takes `SELECT ... FOR UPDATE`
on the constraining parent row (team, or `org_settings` one level up),
computes the aggregate and the proposed new total under that lock, and only
then writes - a single-statement correlated-subquery check is NOT
race-free under READ COMMITTED (two concurrent writers can both see the
same pre-write headroom), which is exactly the over-allocation the phase's
success criteria forbids.

Transaction contract
---------------------
Functions here FLUSH but never COMMIT. The caller (route handler) writes
its `AuditEntry` on the same session and commits - design doc section 7's
"audit entry in the same DB transaction as the mutation" rule - which is
also what releases the row lock. Locks are therefore held only across DB
awaits (the audit INSERT + commit), never across outbound HTTP/notifier
work - callers must keep it that way.

Lock ordering (deadlock avoidance): `org_settings` before `teams` before
`team_memberships` - the same parent-then-child order used by
`services.team_periods.ensure_current_period` and
`services.budget.record_team_membership_usage_charge`.

CMR-14 security review addendum: `write_audit_entry` (`services/audit.py`)
takes its own `SELECT ... FOR UPDATE` on `compliance_settings` when the
org's hash chain is enabled - a lock ABOVE `org_settings` in this ordering,
acquired first. Every call site in this codebase that both writes an audit
entry and calls one of the `_lock_team`-based functions here (or
`set_org_budget_ceiling`) must call `write_audit_entry` BEFORE the locking
function, never after - see `api/v1/teams.py`'s `create_team_endpoint`/
`update_team_endpoint`/`add_member_endpoint`/`update_member_endpoint`/
`approve_join_request_endpoint`/`reassign_budget_endpoint`,
`api/v1/admin/org_settings.py::put_org_settings_endpoint`, and
`services/scim.py::add_scim_group_members` for the established pattern
(mirroring `api/v1/admin/custom_models.py`'s/`self_hosted_providers.py`'s
audit-then-`org_settings`-lock convention for the `custom_models`/
`self_hosted_providers` collision guards). Getting this backwards
reproduces a real, previously-shipped Postgres deadlock between opposite
lock-acquisition orderings.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.join_request import JoinRequest, JoinRequestStatus
from gatekey.db.models.org_settings import OrgSettings
from gatekey.db.models.team import Team
from gatekey.db.models.team_membership import TeamMembership, TeamRole
from gatekey.errors import (
    BudgetCeilingBelowAllocationError,
    BudgetCeilingExceededError,
    GatekeyError,
    NotFoundError,
)


async def _lock_team(session: AsyncSession, team_id: uuid.UUID) -> Team:
    """`SELECT ... FOR UPDATE` the constraining team row - the ADR-5 lock."""
    team = (
        await session.execute(select(Team).where(Team.id == team_id).with_for_update())
    ).scalar_one_or_none()
    if team is None:
        raise NotFoundError("Team not found.")
    return team


async def _allocated_member_budget(
    session: AsyncSession, team_id: uuid.UUID, *, exclude_user_id: uuid.UUID | None = None
) -> Decimal:
    """SUM of sibling memberships' `budget_usd` (NULL rows contribute
    nothing - unmetered members are outside the allocation arithmetic).
    Only meaningful while the team row lock is held. `removed_at IS NULL`
    (added by `0049`) - a removed member's allocation must free up that
    headroom for everyone else, not keep counting against the ceiling
    forever."""
    stmt = select(func.coalesce(func.sum(TeamMembership.budget_usd), 0)).where(
        TeamMembership.team_id == team_id, TeamMembership.removed_at.is_(None)
    )
    if exclude_user_id is not None:
        stmt = stmt.where(TeamMembership.user_id != exclude_user_id)
    # `coalesce(sum(...), 0)` guarantees a non-NULL Decimal at the SQL
    # level; the SQLAlchemy stubs still type a SUM's scalar as
    # `Decimal | None` (they don't reason through coalesce), so this is
    # stub imprecision, not a real nullability gap - same category as the
    # `CursorResult.rowcount` casts elsewhere in this codebase.
    return cast(Decimal, (await session.execute(stmt)).scalar_one())


def _check_headroom(
    *, ceiling: Decimal | None, allocated: Decimal, requested: Decimal | None
) -> None:
    """Raise `BudgetCeilingExceededError` if `allocated + requested` would
    exceed `ceiling`. NULL ceiling = unconstrained; NULL requested
    (unmetered assignment) skips the arithmetic, same NULL semantics as
    everywhere else in the budget subsystem."""
    if ceiling is None or requested is None:
        return
    if allocated + requested > ceiling:
        raise BudgetCeilingExceededError(headroom=ceiling - allocated, requested=requested)


async def create_team_membership(
    session: AsyncSession,
    *,
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    role: TeamRole = TeamRole.MEMBER,
    budget_usd: Decimal | None,
    membership_id: uuid.UUID | None = None,
) -> TeamMembership:
    """Add a member with a budget, ceiling-checked under the team lock
    (AC2.2's assignment-time enforcement). Flushes, does not commit - see
    module docstring.

    `membership_id` may be supplied by the caller (a router-generated
    `uuid.uuid4()`, same shape as `custom_models.py`'s `custom_model_id`
    parameter) so a lock-ordering-sensitive audit entry can reference the
    row's id BEFORE this function's own `SELECT ... FOR UPDATE` on `teams`
    runs (CMR-14 security review's broader systemic-lock-ordering finding -
    callers write `write_audit_entry` before calling this function so the
    `compliance_settings` lock, when the org's hash chain is enabled, is
    always acquired before the `teams` lock here, matching the convention
    `custom_models.py`/`self_hosted_providers.py`/`org_settings.py` use for
    the `org_settings` lock). Defaults to a freshly generated id when
    omitted, for callers that don't need the id ahead of time.

    `0049` (soft delete) note: the `(team_id, user_id)` unique constraint
    is deliberately unchanged (still exactly one row, ever, per pair - see
    `db/models/team_membership.py`'s module docstring), so this always
    INSERTs and never transparently restores a previously-removed row over
    itself - that's `restore_team_member`'s job, a clearly separate
    operation, not implicit magic here (a caller-supplied `membership_id`
    for a fresh audit-entry target_id would silently mismatch the real,
    pre-existing row's id if this DID restore-in-place). A conflict here
    means "this user was previously a member and was removed" - surfaced
    as a distinct, actionable 409 telling the caller to use restore
    instead of a generic/confusing constraint-violation error.
    """
    team = await _lock_team(session, team_id)
    allocated = await _allocated_member_budget(session, team_id)
    _check_headroom(ceiling=team.budget_ceiling_usd, allocated=allocated, requested=budget_usd)
    membership = TeamMembership(
        id=membership_id if membership_id is not None else uuid.uuid4(),
        team_id=team_id, user_id=user_id, role=role, budget_usd=budget_usd
    )
    session.add(membership)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise GatekeyError(
            "This user was previously a member of this team and was "
            "removed - restore their membership instead of adding it "
            "again.",
            code="member_previously_removed",
            status_code=409,
        ) from None
    return membership


async def update_team_membership_budget(
    session: AsyncSession,
    *,
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    budget_usd: Decimal | None,
) -> TeamMembership:
    """Edit an existing member's budget, ceiling-checked against the other
    members' allocation under the team lock. Flushes, does not commit."""
    team = await _lock_team(session, team_id)
    # `removed_at IS NULL` (added by `0049`) - editing a removed member's
    # budget through this path should 404, same as it not existing.
    membership = (
        await session.execute(
            select(TeamMembership).where(
                TeamMembership.team_id == team_id,
                TeamMembership.user_id == user_id,
                TeamMembership.removed_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        raise NotFoundError("Team membership not found.")
    allocated_others = await _allocated_member_budget(
        session, team_id, exclude_user_id=user_id
    )
    _check_headroom(
        ceiling=team.budget_ceiling_usd, allocated=allocated_others, requested=budget_usd
    )
    membership.budget_usd = budget_usd
    await session.flush()
    return membership


async def approve_join_request(
    session: AsyncSession,
    *,
    request_id: uuid.UUID,
    team_id: uuid.UUID,
    requester_user_id: uuid.UUID,
    budget_usd: Decimal | None,
    approved_by_user_id: uuid.UUID | None,
) -> TeamMembership:
    """AC6.7: approval is atomic with budget allocation - the ceiling check,
    the `TeamMembership` INSERT, and the `JoinRequest` status update all
    happen in the same locked transaction; there is no intermediate
    approved-but-unbudgeted state. Flushes, does not commit (the caller's
    audit write + commit releases the lock - design doc section 7)."""
    team = await _lock_team(session, team_id)

    # Guarded UPDATE first: only a still-pending request for THIS team flips
    # to approved - a concurrent approve/reject (serialized behind the team
    # lock for same-team requests, but not for a mismatched team_id) sees
    # zero rows and gets a clean 404 instead of double-approving. Runs
    # before the headroom check so a no-longer-pending request is always a
    # 404, never a 422; a headroom failure below rolls this flip back with
    # the rest of the locked transaction.
    resolved_id = (
        await session.execute(
            update(JoinRequest)
            .where(
                JoinRequest.id == request_id,
                JoinRequest.team_id == team_id,
                JoinRequest.status == JoinRequestStatus.PENDING,
            )
            .values(
                status=JoinRequestStatus.APPROVED,
                resolved_at=func.now(),
                resolved_by_user_id=approved_by_user_id,
                approved_budget_usd=budget_usd,
            )
            .returning(JoinRequest.id)
        )
    ).scalar_one_or_none()
    if resolved_id is None:
        raise NotFoundError("Join request not found or no longer pending.")

    allocated = await _allocated_member_budget(session, team_id)
    _check_headroom(ceiling=team.budget_ceiling_usd, allocated=allocated, requested=budget_usd)

    membership = TeamMembership(
        team_id=team_id,
        user_id=requester_user_id,
        role=TeamRole.MEMBER,
        budget_usd=budget_usd,
    )
    session.add(membership)
    try:
        await session.flush()
    except IntegrityError:
        # Same `member_previously_removed` case as `create_team_membership`
        # - a user who was removed, then submitted a fresh join request,
        # is asking to be a member again, not restored into their old
        # role/budget. `rollback()` undoes the JoinRequest status flip to
        # APPROVED above too (same uncommitted transaction), so the
        # request is left genuinely pending again for a retry - never a
        # silently half-approved state.
        await session.rollback()
        raise GatekeyError(
            "This user was previously a member of this team and was "
            "removed - an org admin or team lead must restore their "
            "membership instead of approving this request.",
            code="member_previously_removed",
            status_code=409,
        ) from None
    return membership


@dataclass(frozen=True)
class BudgetReassignment:
    """Old -> new for both sides of a reassignment, in one shape the caller
    can drop straight into the single `AuditEntry` AC2.4 requires."""

    from_user_id: uuid.UUID
    to_user_id: uuid.UUID
    amount_usd: Decimal
    from_old_budget_usd: Decimal
    from_new_budget_usd: Decimal
    to_old_budget_usd: Decimal
    to_new_budget_usd: Decimal


async def reassign_budget(
    session: AsyncSession,
    *,
    team_id: uuid.UUID,
    from_user_id: uuid.UUID,
    to_user_id: uuid.UUID,
    amount_usd: Decimal,
) -> BudgetReassignment:
    """Move `amount_usd` of budget between two members of the same team in
    one atomic transaction (AC2.4). The total allocation is unchanged, so
    no ceiling check applies - but the team lock is still taken so this
    serializes with every other assignment write (every `budget_usd` writer
    goes through the same lock). Flushes, does not commit."""
    if amount_usd <= 0:
        raise GatekeyError(
            "Reassignment amount must be greater than zero.",
            code="budget_reassignment_invalid",
            status_code=422,
        )
    if from_user_id == to_user_id:
        raise GatekeyError(
            "Cannot reassign budget from a member to themselves.",
            code="budget_reassignment_invalid",
            status_code=422,
        )
    await _lock_team(session, team_id)
    # `removed_at IS NULL` (added by `0049`) - can't reassign budget
    # to/from a removed member.
    rows = (
        (
            await session.execute(
                select(TeamMembership).where(
                    TeamMembership.team_id == team_id,
                    TeamMembership.user_id.in_([from_user_id, to_user_id]),
                    TeamMembership.removed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    by_user = {row.user_id: row for row in rows}
    source = by_user.get(from_user_id)
    target = by_user.get(to_user_id)
    if source is None or target is None:
        raise NotFoundError("Team membership not found.")
    if source.budget_usd is None or target.budget_usd is None:
        raise GatekeyError(
            "Budget can only be reassigned between members with a set "
            "(non-unmetered) budget.",
            code="budget_reassignment_invalid",
            status_code=422,
        )
    if source.budget_usd - amount_usd < 0:
        raise GatekeyError(
            f"Cannot reassign ${amount_usd:,.2f} USD - the source member only "
            f"has ${source.budget_usd:,.2f} USD allocated.",
            code="budget_reassignment_invalid",
            status_code=422,
        )
    result = BudgetReassignment(
        from_user_id=from_user_id,
        to_user_id=to_user_id,
        amount_usd=amount_usd,
        from_old_budget_usd=source.budget_usd,
        from_new_budget_usd=source.budget_usd - amount_usd,
        to_old_budget_usd=target.budget_usd,
        to_new_budget_usd=target.budget_usd + amount_usd,
    )
    source.budget_usd = result.from_new_budget_usd
    target.budget_usd = result.to_new_budget_usd
    await session.flush()
    return result


async def set_team_budget_ceiling(
    session: AsyncSession, *, team_id: uuid.UUID, budget_ceiling_usd: Decimal | None
) -> Team:
    """Edit a team's ceiling, checked both downward (A3: never below the
    members' current allocation) and upward (the org ceiling vs the sum of
    all team ceilings), under `org_settings`-then-`team` locks. NULL new
    ceiling = unmetered, skips both checks. Flushes, does not commit."""
    org_settings = (
        await session.execute(
            select(OrgSettings)
            .where(OrgSettings.org_id == DEFAULT_ORG_ID)
            .with_for_update()
        )
    ).scalar_one_or_none()
    # No org_settings row = no org ceiling (absence-of-row defaults) - and
    # with no org ceiling there is no cross-team invariant to serialize.
    team = await _lock_team(session, team_id)

    if budget_ceiling_usd is not None:
        allocated = await _allocated_member_budget(session, team_id)
        if budget_ceiling_usd < allocated:
            raise BudgetCeilingBelowAllocationError(
                requested_ceiling=budget_ceiling_usd,
                allocated_total=allocated,
                allocated_noun="members",
            )
        org_ceiling = org_settings.budget_ceiling_usd if org_settings is not None else None
        if org_ceiling is not None:
            # See `_allocated_member_budget`'s comment on the `cast` here -
            # same coalesce-vs-stub gap, not a real nullability gap.
            sibling_sum = cast(
                Decimal,
                (
                    await session.execute(
                        select(func.coalesce(func.sum(Team.budget_ceiling_usd), 0)).where(
                            Team.org_id == DEFAULT_ORG_ID, Team.id != team_id
                        )
                    )
                ).scalar_one(),
            )
            _check_headroom(
                ceiling=org_ceiling, allocated=sibling_sum, requested=budget_ceiling_usd
            )

    team.budget_ceiling_usd = budget_ceiling_usd
    await session.flush()
    return team


async def set_org_budget_ceiling(
    session: AsyncSession, *, budget_ceiling_usd: Decimal | None
) -> OrgSettings:
    """Edit the org-wide ceiling (A3, one level up): rejected if reduced
    below the current SUM of team ceilings. Upserts the `org_settings` row
    first so there is always a row to lock. Flushes, does not commit."""
    await session.execute(
        postgresql.insert(OrgSettings)
        .values(org_id=DEFAULT_ORG_ID)
        .on_conflict_do_nothing(index_elements=[OrgSettings.org_id])
    )
    org_settings = (
        await session.execute(
            select(OrgSettings)
            .where(OrgSettings.org_id == DEFAULT_ORG_ID)
            .with_for_update()
        )
    ).scalar_one()

    if budget_ceiling_usd is not None:
        # See `_allocated_member_budget`'s comment on the `cast` here -
        # same coalesce-vs-stub gap, not a real nullability gap.
        team_ceiling_sum = cast(
            Decimal,
            (
                await session.execute(
                    select(func.coalesce(func.sum(Team.budget_ceiling_usd), 0)).where(
                        Team.org_id == DEFAULT_ORG_ID
                    )
                )
            ).scalar_one(),
        )
        if budget_ceiling_usd < team_ceiling_sum:
            raise BudgetCeilingBelowAllocationError(
                requested_ceiling=budget_ceiling_usd,
                allocated_total=team_ceiling_sum,
                allocated_noun="teams",
            )

    org_settings.budget_ceiling_usd = budget_ceiling_usd
    await session.flush()
    return org_settings
