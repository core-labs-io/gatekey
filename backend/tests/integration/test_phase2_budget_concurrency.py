"""BD-21: budget concurrency + period-boundary integration tests against a
real Postgres (design doc sections 3.2/3.3/3.5, AC2.2/AC2.3, ADR-5/6/7/10).

Semantics note for the spend-time tests (AC2.3): the ratified budget design
(phase-1.4 design doc section 5, carried into Phase 2 unchanged) is
check-before-call + charge-actual-cost-after - a request's cost is unknowable
before the provider responds, so there is no pre-reservation. "Excess
rejected deterministically" therefore means: (a) charges are atomic
single-statement increments (no lost updates, counters exact under
concurrency), (b) membership and team aggregate move in lockstep (ADR-7),
and (c) the moment committed spend reaches the budget, EVERY subsequent
check rejects, deterministically. Overshoot is bounded by the number of
requests already in flight past their pre-check - the documented, accepted
"N completes, N+1 is blocked" window, asserted as such here.

Assignment-time (section 3.3 / ADR-5) has no such window: the SELECT ... FOR
UPDATE lock serializes writers, so the over-ceiling outcome is exactly
deterministic and asserted exactly.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from gatekey.api.v1.gateway.common import check_budget_available
from gatekey.db.models.team import Team, TeamPeriodEnd
from gatekey.errors import BudgetCeilingExceededError, BudgetExhaustedError
from gatekey.services import budget as budget_service
from gatekey.services.join_requests import submit_join_request
from gatekey.services.team_budget import approve_join_request, create_team_membership
from gatekey.services.team_periods import ensure_current_period

from .phase2_helpers import (  # noqa: F401 - fixtures resolved by name
    _clean_phase2_tables,
    add_membership,
    fetch_row,
    make_team,
    make_user,
    set_team_spend,
    sf,
)

pytestmark = pytest.mark.asyncio

# 1M prompt + 1M completion tokens at $0.15/$0.60 per million = $0.75/charge.
MODEL = "openrouter/openai/gpt-4o-mini"
COST_PER_CHARGE = Decimal("0.75")
TOKENS = 1_000_000


async def _charge_op(sf, team_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """One gateway-shaped spend operation: budget check, then (on pass) the
    atomic charge - exactly the sequence the three gateway routes run."""
    async with sf() as session:
        try:
            await check_budget_available(session, user_id, team_id=team_id)
        except BudgetExhaustedError:
            return "rejected"
        await budget_service.record_team_membership_usage_charge(
            session,
            team_id=team_id,
            user_id=user_id,
            model=MODEL,
            prompt_tokens=TOKENS,
            completion_tokens=TOKENS,
        )
        return "charged"


async def _counters(database_url: str, team_id: uuid.UUID, user_id: uuid.UUID):
    row = await fetch_row(
        database_url,
        "SELECT m.current_spend_usd AS member_spend, t.current_spend_usd AS team_spend "
        "FROM team_memberships m JOIN teams t ON t.id = m.team_id "
        "WHERE m.team_id = $1 AND m.user_id = $2",
        team_id,
        user_id,
    )
    assert row is not None
    return Decimal(row["member_spend"]), Decimal(row["team_spend"])


# --- AC2.3: spend-time concurrency -------------------------------------------


async def test_concurrent_charges_lose_no_updates_and_stay_in_lockstep(
    sf, migrated_database_url: str
) -> None:
    """20 fully concurrent charges on one membership: final counters must be
    exactly 20 * cost on BOTH the membership and the team aggregate (ADR-7)
    - any read-modify-write race would lose at least one update."""
    user_id = await make_user(sf, "cc-user")
    team_id = await make_team(sf, "cc-team")
    await add_membership(sf, team_id, user_id, budget=None)  # unmetered

    n = 20
    results = await asyncio.gather(*(_charge_op(sf, team_id, user_id) for _ in range(n)))
    assert results.count("charged") == n

    member_spend, team_spend = await _counters(migrated_database_url, team_id, user_id)
    assert member_spend == COST_PER_CHARGE * n
    assert team_spend == COST_PER_CHARGE * n


async def test_concurrent_charges_excess_rejected_and_counters_consistent(
    sf, migrated_database_url: str
) -> None:
    """Membership with headroom for 3 charges, 8 fired concurrently: at
    least the 3 in-headroom ops succeed, every failure is the 402-shaped
    BudgetExhaustedError, counters equal successes * cost exactly (membership
    and team in lockstep), and - the deterministic part - once spend >=
    budget, a full second concurrent wave is rejected to the last request."""
    user_id = await make_user(sf, "ex-user")
    team_id = await make_team(sf, "ex-team")
    budget = COST_PER_CHARGE * 3
    await add_membership(sf, team_id, user_id, budget=budget)

    n = 8
    results = await asyncio.gather(*(_charge_op(sf, team_id, user_id) for _ in range(n)))
    charged = results.count("charged")
    rejected = results.count("rejected")
    assert charged + rejected == n
    # The 3 in-headroom charges always land; overshoot is bounded by the
    # in-flight window (at most all n pass the pre-check simultaneously).
    assert 3 <= charged <= n

    member_spend, team_spend = await _counters(migrated_database_url, team_id, user_id)
    assert member_spend == COST_PER_CHARGE * charged  # no lost/phantom updates
    assert team_spend == member_spend  # ADR-7 lockstep

    # Deterministic rejection: spend >= budget is now committed, so every
    # further op - even a fully concurrent wave - must reject.
    second_wave = await asyncio.gather(
        *(_charge_op(sf, team_id, user_id) for _ in range(n))
    )
    assert second_wave == ["rejected"] * n
    member_after, team_after = await _counters(migrated_database_url, team_id, user_id)
    assert member_after == member_spend
    assert team_after == team_spend


# --- section 3.3 / AC2.2: assignment-time concurrency (ADR-5) ----------------


async def test_concurrent_membership_creates_never_exceed_ceiling(
    sf, migrated_database_url: str
) -> None:
    """Ceiling $250, 8 concurrent $100 member-adds: the FOR UPDATE lock
    serializes them, so EXACTLY 2 succeed and the allocated total never
    exceeds the ceiling - the design's own named failure mode (both writers
    seeing the same pre-write headroom) would yield 3+ successes."""
    team_id = await make_team(sf, "acc-team", ceiling=Decimal(250))
    user_ids = [await make_user(sf, f"acc-user-{i}") for i in range(8)]

    async def add_op(user_id: uuid.UUID) -> str:
        async with sf() as session:
            try:
                await create_team_membership(
                    session, team_id=team_id, user_id=user_id, budget_usd=Decimal(100)
                )
                await session.commit()
                return "ok"
            except BudgetCeilingExceededError:
                await session.rollback()
                return "rejected"

    results = await asyncio.gather(*(add_op(uid) for uid in user_ids))
    assert results.count("ok") == 2, results
    assert results.count("rejected") == 6

    allocated = await fetch_row(
        migrated_database_url,
        "SELECT COALESCE(SUM(budget_usd), 0) AS total, COUNT(*) AS n "
        "FROM team_memberships WHERE team_id = $1",
        team_id,
    )
    assert int(allocated["n"]) == 2
    assert Decimal(allocated["total"]) == Decimal(200)


async def test_concurrent_join_request_approvals_never_exceed_ceiling(
    sf, migrated_database_url: str
) -> None:
    """5 pending join requests, ceiling $100, all approved concurrently at
    $40: exactly 2 approvals land; the rest 422 with NO membership row and
    their requests still pending (AC6.7 atomicity under the failure path)."""
    team_id = await make_team(sf, "jr-team", ceiling=Decimal(100))
    requests: list[tuple[uuid.UUID, uuid.UUID]] = []  # (request_id, user_id)
    for i in range(5):
        user_id = await make_user(sf, f"jr-user-{i}")
        async with sf() as session:
            row = await submit_join_request(
                session,
                requester_user_id=user_id,
                requester_name=f"JR User {i}",
                team_id=team_id,
            )
            await session.commit()
            requests.append((row.id, user_id))

    async def approve_op(request_id: uuid.UUID, user_id: uuid.UUID) -> str:
        async with sf() as session:
            try:
                await approve_join_request(
                    session,
                    request_id=request_id,
                    team_id=team_id,
                    requester_user_id=user_id,
                    budget_usd=Decimal(40),
                    approved_by_user_id=None,
                )
                await session.commit()
                return "approved"
            except BudgetCeilingExceededError:
                await session.rollback()
                return "rejected"

    results = await asyncio.gather(*(approve_op(rid, uid) for rid, uid in requests))
    assert results.count("approved") == 2, results
    assert results.count("rejected") == 3

    summary = await fetch_row(
        migrated_database_url,
        "SELECT COALESCE(SUM(budget_usd), 0) AS total, COUNT(*) AS members "
        "FROM team_memberships WHERE team_id = $1",
        team_id,
    )
    assert int(summary["members"]) == 2
    assert Decimal(summary["total"]) == Decimal(80)  # never exceeds 100

    statuses = await fetch_row(
        migrated_database_url,
        "SELECT COUNT(*) FILTER (WHERE status = 'pending') AS pending, "
        "COUNT(*) FILTER (WHERE status = 'approved') AS approved "
        "FROM join_requests WHERE team_id = $1",
        team_id,
    )
    # AC6.7: a failed approval leaves the request pending - no intermediate
    # approved-but-unbudgeted state.
    assert int(statuses["pending"]) == 3
    assert int(statuses["approved"]) == 2


# --- section 3.5: lazy period boundary (ADR-6/10) ----------------------------


def _past_period_start() -> datetime:
    """A period start guaranteed to be at least one full calendar month in
    the past, so `ensure_current_period` sees a crossed boundary."""
    now = datetime.now(timezone.utc)
    year, month = (now.year, now.month - 2) if now.month > 2 else (now.year - 1, now.month + 10)
    return datetime(year, month, 15, tzinfo=timezone.utc)


def _current_month_start() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


async def test_period_reset_zeroes_spend_keeps_budget(
    sf, migrated_database_url: str
) -> None:
    team_id = await make_team(
        sf,
        "reset-team",
        ceiling=Decimal(100),
        on_period_end=TeamPeriodEnd.RESET,
        started_at=_past_period_start(),
    )
    metered = await make_user(sf, "reset-metered")
    unmetered = await make_user(sf, "reset-unmetered")
    await add_membership(sf, team_id, metered, budget=Decimal(10), spend=Decimal(4))
    await add_membership(sf, team_id, unmetered, budget=None, spend=Decimal(2))
    await set_team_spend(sf, team_id, Decimal(6))

    async with sf() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
        assert await ensure_current_period(session, team) is True

    team_row = await fetch_row(
        migrated_database_url,
        "SELECT current_spend_usd, current_period_started_at FROM teams WHERE id = $1",
        team_id,
    )
    # ADR-7: aggregate decremented by the summed prior spend (back to 0).
    assert Decimal(team_row["current_spend_usd"]) == Decimal(0)
    # Advanced to the start of the CURRENT calendar month (caught up in one
    # pass, ADR-10) - derived independently of the implementation's helper.
    assert team_row["current_period_started_at"] == _current_month_start()

    rows = await fetch_row(
        migrated_database_url,
        "SELECT COUNT(*) FILTER (WHERE current_spend_usd <> 0) AS nonzero_spend, "
        "COUNT(*) FILTER (WHERE budget_usd = 10) AS budget_kept, "
        "COUNT(*) FILTER (WHERE budget_usd IS NULL) AS still_unmetered "
        "FROM team_memberships WHERE team_id = $1",
        team_id,
    )
    assert int(rows["nonzero_spend"]) == 0  # spend always resets to 0
    assert int(rows["budget_kept"]) == 1  # reset: budget unchanged
    assert int(rows["still_unmetered"]) == 1


async def test_period_rollover_compounds_unspent_budget(
    sf, migrated_database_url: str
) -> None:
    team_id = await make_team(
        sf,
        "rollover-team",
        on_period_end=TeamPeriodEnd.ROLLOVER,
        started_at=_past_period_start(),
    )
    underspender = await make_user(sf, "roll-under")
    overspender = await make_user(sf, "roll-over")
    unmetered = await make_user(sf, "roll-unmetered")
    await add_membership(sf, team_id, underspender, budget=Decimal(10), spend=Decimal(4))
    await add_membership(sf, team_id, overspender, budget=Decimal(10), spend=Decimal(12))
    await add_membership(sf, team_id, unmetered, budget=None, spend=Decimal(2))
    await set_team_spend(sf, team_id, Decimal(18))

    async with sf() as session:
        team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one()
        assert await ensure_current_period(session, team) is True

    async def member(user_id):
        return await fetch_row(
            migrated_database_url,
            "SELECT budget_usd, current_spend_usd FROM team_memberships "
            "WHERE team_id = $1 AND user_id = $2",
            team_id,
            user_id,
        )

    under = await member(underspender)
    over = await member(overspender)
    unm = await member(unmetered)
    # ADR-6: budget += max(0, budget - spend); spend always resets to 0.
    assert Decimal(under["budget_usd"]) == Decimal(16)  # 10 + (10 - 4)
    assert Decimal(over["budget_usd"]) == Decimal(10)  # overspent -> no credit
    assert unm["budget_usd"] is None  # unmetered skips the arithmetic
    for row in (under, over, unm):
        assert Decimal(row["current_spend_usd"]) == Decimal(0)

    team_spend = await fetch_row(
        migrated_database_url, "SELECT current_spend_usd FROM teams WHERE id = $1", team_id
    )
    assert Decimal(team_spend["current_spend_usd"]) == Decimal(0)  # 18 - (4+12+2)


async def test_charge_after_boundary_uses_post_reset_counters(
    sf, migrated_database_url: str
) -> None:
    """The gateway hot path itself applies the crossing: a member exhausted
    LAST period charges cleanly this period because `check_budget_available`
    runs `ensure_current_period` first (design doc 3.5)."""
    team_id = await make_team(
        sf,
        "touch-team",
        on_period_end=TeamPeriodEnd.RESET,
        started_at=_past_period_start(),
    )
    user_id = await make_user(sf, "touch-user")
    budget = COST_PER_CHARGE * 2
    # Exhausted under last period's counters.
    await add_membership(sf, team_id, user_id, budget=budget, spend=budget)
    await set_team_spend(sf, team_id, budget)

    assert await _charge_op(sf, team_id, user_id) == "charged"
    member_spend, team_spend = await _counters(migrated_database_url, team_id, user_id)
    assert member_spend == COST_PER_CHARGE  # post-reset counter, one charge
    assert team_spend == COST_PER_CHARGE
