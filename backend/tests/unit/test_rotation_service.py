"""Unit tests for `services/rotation.py`'s pure, DB-free
`compute_next_rotation` (Phase 3, AC7.3 off-hours timing resolution)."""

from __future__ import annotations

from datetime import datetime, time, timezone

from gatekey.services.rotation import (
    AccessScheduleWindow,
    DEFAULT_ORG_OFF_HOURS_LOCAL_TIME,
    compute_next_rotation,
)


def _utc(*args, **kwargs) -> datetime:
    return datetime(*args, tzinfo=timezone.utc, **kwargs)


# --- branch (b): no access schedule -> org off-hours / rotate_at_local_time --


def test_no_schedule_no_override_uses_org_default_off_hours() -> None:
    now = _utc(2026, 8, 4, 10, 0)  # a Tuesday
    result = compute_next_rotation(
        now=now, interval_days=30, rotate_at_local_time=None, timezone_name="UTC"
    )
    assert result == _utc(2026, 9, 3, 2, 0)  # +30 days, anchored at 02:00 UTC
    assert result.time() == DEFAULT_ORG_OFF_HOURS_LOCAL_TIME


def test_policy_rotate_at_local_time_overrides_org_default() -> None:
    now = _utc(2026, 8, 4, 10, 0)
    result = compute_next_rotation(
        now=now,
        interval_days=7,
        rotate_at_local_time=time(3, 30),
        timezone_name="UTC",
    )
    assert result == _utc(2026, 8, 11, 3, 30)


def test_timezone_conversion_anchors_in_org_local_time() -> None:
    # America/New_York is UTC-4 in August (DST) - 02:00 local = 06:00 UTC.
    now = _utc(2026, 8, 4, 10, 0)
    result = compute_next_rotation(
        now=now,
        interval_days=1,
        rotate_at_local_time=None,
        timezone_name="America/New_York",
    )
    assert result == _utc(2026, 8, 5, 6, 0)


# --- branch (a): enabled access-schedule window -> anchor at window close ---


def test_enabled_access_schedule_anchors_at_allowed_hours_end() -> None:
    now = _utc(2026, 8, 4, 10, 0)
    schedule = AccessScheduleWindow(
        enabled=True,
        allowed_days=(1, 2, 3, 4, 5),
        allowed_hours_start=time(9, 0),
        allowed_hours_end=time(17, 0),
    )
    result = compute_next_rotation(
        now=now,
        interval_days=14,
        rotate_at_local_time=time(3, 30),  # ignored - schedule branch wins
        timezone_name="UTC",
        access_schedule=schedule,
    )
    assert result == _utc(2026, 8, 18, 17, 0)


def test_enabled_access_schedule_without_hours_falls_back_to_org_default() -> None:
    now = _utc(2026, 8, 4, 10, 0)
    schedule = AccessScheduleWindow(
        enabled=True, allowed_days=(1, 2, 3, 4, 5), allowed_hours_start=None, allowed_hours_end=None
    )
    result = compute_next_rotation(
        now=now, interval_days=1, rotate_at_local_time=None, timezone_name="UTC", access_schedule=schedule
    )
    assert result.time() == DEFAULT_ORG_OFF_HOURS_LOCAL_TIME


def test_disabled_access_schedule_is_ignored_falls_back_to_branch_b() -> None:
    now = _utc(2026, 8, 4, 10, 0)
    schedule = AccessScheduleWindow(
        enabled=False,
        allowed_days=(1, 2, 3, 4, 5),
        allowed_hours_start=time(9, 0),
        allowed_hours_end=time(17, 0),
    )
    result = compute_next_rotation(
        now=now,
        interval_days=1,
        rotate_at_local_time=time(4, 0),
        timezone_name="UTC",
        access_schedule=schedule,
    )
    assert result == _utc(2026, 8, 5, 4, 0)
