"""Unit tests for `services/team_periods.py`'s pure arithmetic (Phase 2,
BD-10 - design doc ADR-6/ADR-10).

`ensure_current_period`'s locked DB path is exercised by the later
concurrency/integration QA task; these tests cover the pure, no-DB pieces:
calendar boundary computation, the looped catch-up advance, and the
rollover/reset arithmetic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from gatekey.db.models.team import TeamPeriodEnd, TeamPeriodType
from gatekey.services.team_periods import (
    advance_period_start,
    apply_period_end,
    compute_period_end,
)


def _dt(year: int, month: int, day: int, hour: int = 0) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


# --- compute_period_end -------------------------------------------------------


def test_monthly_period_end_is_first_of_next_calendar_month() -> None:
    assert compute_period_end(TeamPeriodType.MONTHLY, _dt(2026, 1, 15)) == _dt(2026, 2, 1)


def test_monthly_period_end_crosses_year_boundary() -> None:
    assert compute_period_end(TeamPeriodType.MONTHLY, _dt(2026, 12, 31)) == _dt(2027, 1, 1)


def test_monthly_start_exactly_on_boundary_ends_next_boundary() -> None:
    assert compute_period_end(TeamPeriodType.MONTHLY, _dt(2026, 2, 1)) == _dt(2026, 3, 1)


def test_quarterly_period_end_is_next_quarter_start() -> None:
    # Q1 (Jan-Mar) -> Apr 1, regardless of where in the quarter it started.
    assert compute_period_end(TeamPeriodType.QUARTERLY, _dt(2026, 2, 10)) == _dt(2026, 4, 1)
    # Q4 -> next year's Jan 1.
    assert compute_period_end(TeamPeriodType.QUARTERLY, _dt(2026, 11, 5)) == _dt(2027, 1, 1)


def test_compute_period_end_accepts_plain_string_period_type() -> None:
    assert compute_period_end("monthly", _dt(2026, 6, 20)) == _dt(2026, 7, 1)
    assert compute_period_end("quarterly", _dt(2026, 6, 20)) == _dt(2026, 7, 1)


# --- advance_period_start -----------------------------------------------------


def test_advance_returns_start_unchanged_before_boundary() -> None:
    start = _dt(2026, 8, 1)
    assert advance_period_start(TeamPeriodType.MONTHLY, start, _dt(2026, 8, 30)) == start


def test_advance_single_crossing_moves_to_current_period_start() -> None:
    assert advance_period_start(
        TeamPeriodType.MONTHLY, _dt(2026, 7, 10), _dt(2026, 8, 4)
    ) == _dt(2026, 8, 1)


def test_advance_long_dormant_team_catches_up_in_one_pass() -> None:
    """ADR-10: looped to the correct current boundary, not single-stepped."""
    assert advance_period_start(
        TeamPeriodType.MONTHLY, _dt(2025, 3, 20), _dt(2026, 8, 4)
    ) == _dt(2026, 8, 1)
    assert advance_period_start(
        TeamPeriodType.QUARTERLY, _dt(2025, 3, 20), _dt(2026, 8, 4)
    ) == _dt(2026, 7, 1)


def test_advance_at_exact_boundary_instant_starts_new_period() -> None:
    # `end <= now` - the boundary instant itself belongs to the new period.
    assert advance_period_start(
        TeamPeriodType.MONTHLY, _dt(2026, 7, 1), _dt(2026, 8, 1)
    ) == _dt(2026, 8, 1)


# --- apply_period_end (ADR-6) -------------------------------------------------


def test_reset_leaves_budget_unchanged() -> None:
    assert apply_period_end(
        budget_usd=Decimal("100"),
        current_spend_usd=Decimal("30"),
        on_period_end=TeamPeriodEnd.RESET,
    ) == Decimal("100")


def test_rollover_compounds_unspent_amount_onto_budget() -> None:
    assert apply_period_end(
        budget_usd=Decimal("100"),
        current_spend_usd=Decimal("30"),
        on_period_end=TeamPeriodEnd.ROLLOVER,
    ) == Decimal("170")


def test_rollover_overspent_member_never_reduces_budget() -> None:
    """leftover = max(0, budget - spend) - overspend rolls nothing negative."""
    assert apply_period_end(
        budget_usd=Decimal("100"),
        current_spend_usd=Decimal("130"),
        on_period_end=TeamPeriodEnd.ROLLOVER,
    ) == Decimal("100")


def test_rollover_fully_unspent_budget_doubles() -> None:
    assert apply_period_end(
        budget_usd=Decimal("50"),
        current_spend_usd=Decimal("0"),
        on_period_end=TeamPeriodEnd.ROLLOVER,
    ) == Decimal("100")


def test_null_budget_skips_arithmetic_for_both_modes() -> None:
    for mode in (TeamPeriodEnd.RESET, TeamPeriodEnd.ROLLOVER):
        assert (
            apply_period_end(
                budget_usd=None, current_spend_usd=Decimal("999"), on_period_end=mode
            )
            is None
        )
