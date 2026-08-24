"""Lazy, touch-based team budget-period rollover/reset (Phase 2, BD-10).

See `docs/design/phase-2-multi-tenant-governance-design.md` section 3.5
(ADR-6 for the rollover/reset arithmetic, ADR-10 for why this is lazy/
touch-based rather than a scheduler daemon). `ensure_current_period` is
called at the start of every code path that reads or writes team/membership
spend state - on the gateway hot path the common case is a single in-hand
datetime comparison with zero extra I/O; only a genuine boundary crossing
engages the locked path.

Lock-ordering note (deadlock avoidance): the crossing path locks the `teams`
row first (`SELECT ... FOR UPDATE`, composing with `services/team_budget.py`'s
ADR-5 discipline), then the team's `team_memberships` rows. Every other
writer that touches both tables takes locks in the same parent-then-child
order - see `services.budget.record_team_membership_usage_charge`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.db.models.team import Team, TeamPeriodEnd, TeamPeriodType
from gatekey.db.models.team_membership import TeamMembership

logger = logging.getLogger("gatekey")


@dataclass(frozen=True)
class TeamPeriodInfo:
    """The minimal team fields `ensure_current_period`'s cheap-comparison
    path needs - carried inside `services.budget.TeamMembershipBudgetState`
    so the gateway hot path gets them from the budget-state query's join
    (design doc section 8: no additional round trip). Field names match the
    `Team` ORM columns so either object can be passed to
    `ensure_current_period`."""

    id: uuid.UUID
    period_type: TeamPeriodType
    current_period_started_at: datetime


def compute_period_end(
    period_type: TeamPeriodType | str, started_at: datetime
) -> datetime:
    """First instant of the calendar month/quarter after the one containing
    `started_at` - i.e. the exclusive end of the currently active period.

    Calendar-based: a team created mid-month still rolls at the calendar
    boundary (monthly -> 1st of next month; quarterly -> next Jan/Apr/Jul/
    Oct 1st), matching "monthly/quarterly, calendar-based" in the task spec.
    Preserves `started_at`'s tzinfo (timestamptz rows are UTC-aware).
    """
    period = TeamPeriodType(period_type)
    if period is TeamPeriodType.MONTHLY:
        months_per_period = 1
        period_start_month = started_at.month
    else:
        months_per_period = 3
        period_start_month = ((started_at.month - 1) // 3) * 3 + 1
    end_month = period_start_month + months_per_period
    end_year = started_at.year
    if end_month > 12:
        end_month -= 12
        end_year += 1
    return datetime(end_year, end_month, 1, tzinfo=started_at.tzinfo)


def advance_period_start(
    period_type: TeamPeriodType | str, started_at: datetime, now: datetime
) -> datetime:
    """Advance `started_at` to the start of the period containing `now` -
    looped to the correct current boundary in one pass (ADR-10: a
    long-dormant team catches up in one call, not one period per touch).
    Returns `started_at` unchanged if no boundary has been crossed yet."""
    start = started_at
    end = compute_period_end(period_type, start)
    while end <= now:
        start = end
        end = compute_period_end(period_type, start)
    return start


def apply_period_end(
    *,
    budget_usd: Decimal | None,
    current_spend_usd: Decimal,
    on_period_end: TeamPeriodEnd,
) -> Decimal | None:
    """ADR-6's per-membership boundary arithmetic: returns the membership's
    new `budget_usd` (`current_spend_usd` always resets to 0 regardless of
    `on_period_end` - the caller applies that).

    - `reset`: budget unchanged (the configured nominal per-period figure).
    - `rollover`: budget += max(0, budget - spend) - unspent allowance
      compounds into the next period's effective ceiling (the documented,
      chosen consequence of opting into rollover; deliberately NOT
      re-checked against the team ceiling - a system-driven credit is not
      an assignment).
    - NULL budget (unmetered): skips the arithmetic entirely.
    """
    if budget_usd is None:
        return None
    if on_period_end is TeamPeriodEnd.ROLLOVER:
        return budget_usd + max(Decimal(0), budget_usd - current_spend_usd)
    return budget_usd


async def ensure_current_period(
    session: AsyncSession, team: Team | TeamPeriodInfo
) -> bool:
    """Apply any pending period-boundary crossing for `team`. Returns True
    if a crossing was applied (callers holding pre-crossing spend state must
    re-read it), False otherwise.

    Common case (not yet past the boundary): a single datetime comparison
    against fields the caller already has in hand - zero I/O, zero writes
    (design doc section 3.5's hot-path cost requirement).

    On a crossing: locks the `teams` row `FOR UPDATE` (serializing against
    concurrent crossings, ADR-5 assignment writes, and in-flight charges),
    re-checks under the lock (another request may have already applied it
    while we waited), advances `current_period_started_at` to the correct
    current boundary, applies ADR-6's reset/rollover arithmetic to every
    membership (locked child rows, parent-then-child order - see module
    docstring), and adjusts the denormalized `teams.current_spend_usd` by
    the summed spend deltas (ADR-7's invariant). The rollover/reset rule is
    applied once per crossing-touch, not once per elapsed period - only the
    just-ended (touched) period ever accrued spend. Commits before
    returning; no non-DB awaits happen while the lock is held.
    """
    now = datetime.now(timezone.utc)
    if now < compute_period_end(team.period_type, team.current_period_started_at):
        return False

    locked = (
        await session.execute(select(Team).where(Team.id == team.id).with_for_update())
    ).scalar_one_or_none()
    if locked is None:
        # Team deleted between the caller's read and this lock - nothing to
        # do; the caller's own membership lookup will fail on re-read.
        await session.rollback()
        return False
    if now < compute_period_end(locked.period_type, locked.current_period_started_at):
        # Another request applied this crossing while we waited on the lock.
        await session.commit()
        return False

    new_start = advance_period_start(
        locked.period_type, locked.current_period_started_at, now
    )
    # Deliberately NOT filtered to `removed_at IS NULL` (added by `0049`) -
    # a removed member's spend during this period still really happened
    # and must count toward zeroing/adjusting the TEAM aggregate below
    # (ADR-7), the same as an active member's. Only the per-membership
    # mutation loop right after this excludes removed rows - their
    # `budget_usd`/`current_spend_usd` stay frozen as a historical record
    # (untouched by rollover/reset math) rather than silently changing
    # budget on a membership nobody can currently use.
    memberships = (
        (
            await session.execute(
                select(TeamMembership)
                .where(TeamMembership.team_id == locked.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    total_prior_spend = Decimal(0)
    for membership in memberships:
        total_prior_spend += membership.current_spend_usd
        if membership.removed_at is not None:
            continue
        membership.budget_usd = apply_period_end(
            budget_usd=membership.budget_usd,
            current_spend_usd=membership.current_spend_usd,
            on_period_end=locked.on_period_end,
        )
        membership.current_spend_usd = Decimal(0)
    locked.current_period_started_at = new_start
    # ADR-7: adjust the denormalized aggregate by the summed deltas rather
    # than zeroing it - preserves the invariant even if the cached total had
    # drifted from SUM(membership spend).
    locked.current_spend_usd = locked.current_spend_usd - total_prior_spend
    await session.commit()
    logger.info(
        "team_period_boundary_applied",
        extra={
            "team_id": str(locked.id),
            "new_period_started_at": new_start.isoformat(),
            "memberships_reset": len(memberships),
        },
    )
    return True
