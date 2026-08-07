"""Unit tests for `services/join_requests.py`'s pure pieces (Phase 2,
BD-15): the A7 Mon-Fri business-day computation and the escalation-reason
logic. The DB-backed submit/approve/reject paths are integration-tested by
the later QA task."""

from __future__ import annotations

from datetime import datetime, timezone

from gatekey.services.join_requests import (
    business_days_between,
    compute_escalation_reason,
)


def _dt(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=timezone.utc)


# 2026-08-03 is a Monday.
_MON = _dt(2026, 8, 3)
_FRI = _dt(2026, 8, 7)
_SAT = _dt(2026, 8, 8)
_NEXT_MON = _dt(2026, 8, 10)


def test_same_day_is_zero_business_days() -> None:
    assert business_days_between(_MON, _MON) == 0


def test_end_before_start_is_zero() -> None:
    assert business_days_between(_FRI, _MON) == 0


def test_monday_to_friday_is_four_business_days() -> None:
    assert business_days_between(_MON, _FRI) == 4


def test_weekend_days_do_not_count() -> None:
    # Friday -> next Monday spans Sat+Sun, only Monday counts.
    assert business_days_between(_FRI, _NEXT_MON) == 1
    assert business_days_between(_FRI, _SAT) == 0


def test_monday_to_next_monday_is_five_business_days() -> None:
    assert business_days_between(_MON, _NEXT_MON) == 5


# --- compute_escalation_reason ------------------------------------------------


def test_no_team_lead_escalates_immediately() -> None:
    assert (
        compute_escalation_reason(has_team_lead=False, requested_at=_MON, now=_MON)
        == "no_team_lead"
    )


def test_lead_present_and_fresh_request_does_not_escalate() -> None:
    assert (
        compute_escalation_reason(has_team_lead=True, requested_at=_MON, now=_FRI)
        is None
    )


def test_lead_present_but_five_business_days_pending_escalates() -> None:
    assert (
        compute_escalation_reason(has_team_lead=True, requested_at=_MON, now=_NEXT_MON)
        == "pending_over_5_business_days"
    )
