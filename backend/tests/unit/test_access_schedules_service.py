"""Unit tests for `services/access_schedules.py` (Phase 3, BD-16).

All pure/synchronous/zero-I/O - no database, mirrors `test_residency_
service.py`'s posture toward `ResidencyRuleCache`/`resolve_residency`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timezone

import pytest

from gatekey.errors import GatekeyError
from gatekey.services.access_schedules import (
    AccessScheduleCache,
    AccessScheduleDecision,
    AccessScheduleSnapshot,
    AccessScheduleWidensParentError,
    describe_effective_schedule,
    is_within_schedule,
    resolve_access_schedule_decision,
    resolve_effective_schedule,
    validate_schedule_narrows_parent,
)

WEEKDAYS_MON_FRI = frozenset({1, 2, 3, 4, 5})
NINE_TO_SIX = AccessScheduleSnapshot(
    enabled=True,
    allowed_days=WEEKDAYS_MON_FRI,
    allowed_hours_start=time(9, 0),
    allowed_hours_end=time(18, 0),
)


# --- resolve_effective_schedule (precedence, §5.1) ----------------------------


def test_resolve_effective_schedule_none_anywhere_is_always() -> None:
    cache = AccessScheduleCache()
    sa_id = uuid.uuid4()
    assert resolve_effective_schedule(cache=cache, team_id=None, service_account_id=sa_id) is None


def test_resolve_effective_schedule_org_layer_applies_when_no_narrower_layer() -> None:
    cache = AccessScheduleCache(org=NINE_TO_SIX)
    sa_id = uuid.uuid4()
    assert resolve_effective_schedule(cache=cache, team_id=None, service_account_id=sa_id) == NINE_TO_SIX


def test_resolve_effective_schedule_team_layer_wins_over_org() -> None:
    team_id = uuid.uuid4()
    sa_id = uuid.uuid4()
    team_schedule = AccessScheduleSnapshot(
        enabled=True, allowed_days=frozenset({1}), allowed_hours_start=None, allowed_hours_end=None
    )
    cache = AccessScheduleCache(org=NINE_TO_SIX, team={team_id: team_schedule})
    result = resolve_effective_schedule(cache=cache, team_id=team_id, service_account_id=sa_id)
    assert result == team_schedule


def test_resolve_effective_schedule_service_account_layer_wins_over_team_and_org() -> None:
    team_id = uuid.uuid4()
    sa_id = uuid.uuid4()
    sa_schedule = AccessScheduleSnapshot(
        enabled=True, allowed_days=frozenset({2}), allowed_hours_start=None, allowed_hours_end=None
    )
    cache = AccessScheduleCache(
        org=NINE_TO_SIX, team={team_id: NINE_TO_SIX}, service_account={sa_id: sa_schedule}
    )
    result = resolve_effective_schedule(cache=cache, team_id=team_id, service_account_id=sa_id)
    assert result == sa_schedule


def test_resolve_effective_schedule_disabled_layer_defers_to_next_layer() -> None:
    """A disabled (or absent) row at a more specific level defers to the
    next-less-specific ENABLED level - "absence/off = no further
    restriction", never "reopen access" beyond the parent."""
    team_id = uuid.uuid4()
    sa_id = uuid.uuid4()
    disabled_sa = AccessScheduleSnapshot(
        enabled=False, allowed_days=frozenset({1}), allowed_hours_start=None, allowed_hours_end=None
    )
    cache = AccessScheduleCache(org=NINE_TO_SIX, service_account={sa_id: disabled_sa})
    result = resolve_effective_schedule(cache=cache, team_id=team_id, service_account_id=sa_id)
    assert result == NINE_TO_SIX


# --- AccessScheduleCache -------------------------------------------------------


def test_cache_set_service_account_keeps_other_entries_and_can_remove() -> None:
    cache = AccessScheduleCache()
    sa_a, sa_b = uuid.uuid4(), uuid.uuid4()
    cache.set_service_account(sa_a, NINE_TO_SIX)
    cache.set_service_account(sa_b, NINE_TO_SIX)
    assert cache.get_service_account(sa_a) == NINE_TO_SIX
    cache.set_service_account(sa_a, None)
    assert cache.get_service_account(sa_a) is None
    assert cache.get_service_account(sa_b) == NINE_TO_SIX


def test_cache_set_all_replaces_whole_snapshot() -> None:
    cache = AccessScheduleCache(org=NINE_TO_SIX)
    cache.set_all(
        org=None, team={}, service_account={}, timezone_name="America/New_York", holiday_dates=frozenset()
    )
    assert cache.get_org() is None
    assert cache.get_timezone_name() == "America/New_York"


# --- is_within_schedule (§5.2, timezone/holiday/hours evaluation) -------------


def test_is_within_schedule_none_effective_is_always_allowed() -> None:
    now = datetime(2026, 8, 4, 3, 0, tzinfo=timezone.utc)  # a Tuesday, 3am UTC
    assert is_within_schedule(None, now=now, timezone_name="UTC", holiday_dates=frozenset()) is True


def test_is_within_schedule_disabled_effective_is_always_allowed() -> None:
    disabled = AccessScheduleSnapshot(
        enabled=False, allowed_days=frozenset(), allowed_hours_start=None, allowed_hours_end=None
    )
    now = datetime(2026, 8, 4, 3, 0, tzinfo=timezone.utc)
    assert is_within_schedule(disabled, now=now, timezone_name="UTC", holiday_dates=frozenset()) is True


def test_is_within_schedule_rejects_disallowed_weekday() -> None:
    # 2026-08-08 is a Saturday - not in Mon-Fri.
    now = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
    assert is_within_schedule(NINE_TO_SIX, now=now, timezone_name="UTC", holiday_dates=frozenset()) is False


def test_is_within_schedule_rejects_outside_hour_window() -> None:
    # 2026-08-04 is a Tuesday, 20:00 UTC - outside 9-18.
    now = datetime(2026, 8, 4, 20, 0, tzinfo=timezone.utc)
    assert is_within_schedule(NINE_TO_SIX, now=now, timezone_name="UTC", holiday_dates=frozenset()) is False


def test_is_within_schedule_allows_within_day_and_hour_window() -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)  # Tuesday, noon
    assert is_within_schedule(NINE_TO_SIX, now=now, timezone_name="UTC", holiday_dates=frozenset()) is True


def test_is_within_schedule_blocks_a_configured_holiday_even_on_an_allowed_day() -> None:
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)  # Tuesday, noon - otherwise allowed
    holidays = frozenset({now.date()})
    assert is_within_schedule(NINE_TO_SIX, now=now, timezone_name="UTC", holiday_dates=holidays) is False


def test_is_within_schedule_converts_to_org_local_timezone_before_evaluating() -> None:
    """23:30 UTC on a Tuesday is already Wednesday 08:30 in a UTC+9 zone -
    timezone conversion must happen before the weekday/hour check, not a
    naive UTC-only comparison."""
    tuesday_only = AccessScheduleSnapshot(
        enabled=True, allowed_days=frozenset({2}), allowed_hours_start=None, allowed_hours_end=None
    )
    now = datetime(2026, 8, 4, 23, 30, tzinfo=timezone.utc)  # Tuesday UTC
    assert is_within_schedule(tuesday_only, now=now, timezone_name="UTC", holiday_dates=frozenset()) is True
    assert (
        is_within_schedule(tuesday_only, now=now, timezone_name="Asia/Tokyo", holiday_dates=frozenset())
        is False
    )


def test_is_within_schedule_handles_overnight_hour_wrap() -> None:
    overnight = AccessScheduleSnapshot(
        enabled=True,
        allowed_days=frozenset(range(1, 8)),
        allowed_hours_start=time(22, 0),
        allowed_hours_end=time(6, 0),
    )
    late_night = datetime(2026, 8, 4, 23, 0, tzinfo=timezone.utc)
    early_morning = datetime(2026, 8, 4, 3, 0, tzinfo=timezone.utc)
    midday = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    assert is_within_schedule(overnight, now=late_night, timezone_name="UTC", holiday_dates=frozenset())
    assert is_within_schedule(overnight, now=early_morning, timezone_name="UTC", holiday_dates=frozenset())
    assert not is_within_schedule(overnight, now=midday, timezone_name="UTC", holiday_dates=frozenset())


# --- resolve_access_schedule_decision (cumulative read-time check, security --
# review fix - see module docstring) -------------------------------------------


MON_TO_SUN_9_TO_6 = AccessScheduleSnapshot(
    enabled=True,
    allowed_days=frozenset(range(1, 8)),
    allowed_hours_start=time(9, 0),
    allowed_hours_end=time(18, 0),
)
TUESDAY_ONLY = AccessScheduleSnapshot(
    enabled=True, allowed_days=frozenset({2}), allowed_hours_start=None, allowed_hours_end=None
)
_TUESDAY_NOON_UTC = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)  # a Tuesday
_SATURDAY_NOON_UTC = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)  # a Saturday


def test_resolve_access_schedule_decision_no_layers_enabled_is_always_allowed() -> None:
    cache = AccessScheduleCache()
    decision = resolve_access_schedule_decision(
        cache=cache,
        team_id=None,
        service_account_id=uuid.uuid4(),
        now=_SATURDAY_NOON_UTC,
        timezone_name="UTC",
        holiday_dates=frozenset(),
    )
    assert decision == AccessScheduleDecision(allowed=True, blocking_layer=None)


def test_resolve_access_schedule_decision_checks_org_layer_even_when_team_layer_passes() -> None:
    """Reproduces the access-schedule analog of the residency staleness bug:
    a team schedule that is satisfied must NOT, on its own, grant access if
    a separately-configured, enabled org schedule is violated - both layers
    are checked cumulatively, not just the innermost enabled one."""
    team_id = uuid.uuid4()
    cache = AccessScheduleCache(org=TUESDAY_ONLY, team={team_id: MON_TO_SUN_9_TO_6})
    # The team's own window (every day, 9-18) is satisfied by Saturday noon,
    # but the org's window (Tuesday only) is not - must still block.
    decision = resolve_access_schedule_decision(
        cache=cache,
        team_id=team_id,
        service_account_id=uuid.uuid4(),
        now=_SATURDAY_NOON_UTC,
        timezone_name="UTC",
        holiday_dates=frozenset(),
    )
    assert decision.allowed is False
    assert decision.blocking_layer == "org"


def test_resolve_access_schedule_decision_allows_when_every_enabled_layer_is_satisfied() -> None:
    team_id = uuid.uuid4()
    cache = AccessScheduleCache(org=MON_TO_SUN_9_TO_6, team={team_id: TUESDAY_ONLY})
    decision = resolve_access_schedule_decision(
        cache=cache,
        team_id=team_id,
        service_account_id=uuid.uuid4(),
        now=_TUESDAY_NOON_UTC,
        timezone_name="UTC",
        holiday_dates=frozenset(),
    )
    assert decision == AccessScheduleDecision(allowed=True, blocking_layer=None)


def test_resolve_access_schedule_decision_disabled_layer_never_participates() -> None:
    team_id = uuid.uuid4()
    disabled_org = AccessScheduleSnapshot(
        enabled=False, allowed_days=frozenset({2}), allowed_hours_start=None, allowed_hours_end=None
    )
    cache = AccessScheduleCache(org=disabled_org, team={team_id: MON_TO_SUN_9_TO_6})
    decision = resolve_access_schedule_decision(
        cache=cache,
        team_id=team_id,
        service_account_id=uuid.uuid4(),
        now=_SATURDAY_NOON_UTC,
        timezone_name="UTC",
        holiday_dates=frozenset(),
    )
    assert decision.allowed is True


# --- validate_schedule_narrows_parent (§5.1 write-time narrowing) -------------


def test_narrowing_validation_passes_when_parent_is_none() -> None:
    validate_schedule_narrows_parent(
        allowed_days=[1, 2, 3], allowed_hours_start=None, allowed_hours_end=None, parent=None
    )  # no exception


def test_narrowing_validation_rejects_a_day_outside_the_parent() -> None:
    with pytest.raises(AccessScheduleWidensParentError):
        validate_schedule_narrows_parent(
            allowed_days=[1, 6],  # Saturday not in parent's Mon-Fri
            allowed_hours_start=None,
            allowed_hours_end=None,
            parent=NINE_TO_SIX,
        )


def test_narrowing_validation_accepts_a_strict_subset() -> None:
    validate_schedule_narrows_parent(
        allowed_days=[1, 2],
        allowed_hours_start=time(10, 0),
        allowed_hours_end=time(16, 0),
        parent=NINE_TO_SIX,
    )  # no exception


def test_narrowing_validation_rejects_hours_wider_than_parent() -> None:
    with pytest.raises(AccessScheduleWidensParentError):
        validate_schedule_narrows_parent(
            allowed_days=[1], allowed_hours_start=time(8, 0), allowed_hours_end=time(19, 0), parent=NINE_TO_SIX
        )


def test_narrowing_validation_rejects_unrestricted_hours_when_parent_restricts() -> None:
    with pytest.raises(AccessScheduleWidensParentError):
        validate_schedule_narrows_parent(
            allowed_days=[1], allowed_hours_start=None, allowed_hours_end=None, parent=NINE_TO_SIX
        )


def test_widens_parent_error_is_a_gatekey_error_with_422() -> None:
    assert issubclass(AccessScheduleWidensParentError, GatekeyError)
    assert AccessScheduleWidensParentError("x").status_code == 422
    assert AccessScheduleWidensParentError("x").code == "access_schedule_widens_parent"


# --- describe_effective_schedule (AC9.10) --------------------------------------


def test_describe_effective_schedule_none_is_always() -> None:
    assert describe_effective_schedule(None) == "Always"


def test_describe_effective_schedule_disabled_is_always() -> None:
    disabled = AccessScheduleSnapshot(
        enabled=False, allowed_days=frozenset({1}), allowed_hours_start=None, allowed_hours_end=None
    )
    assert describe_effective_schedule(disabled) == "Always"


def test_describe_effective_schedule_every_day_no_hours() -> None:
    every_day = AccessScheduleSnapshot(
        enabled=True, allowed_days=frozenset(range(1, 8)), allowed_hours_start=None, allowed_hours_end=None
    )
    assert describe_effective_schedule(every_day) == "Every day"


def test_describe_effective_schedule_contiguous_range_with_hours() -> None:
    assert describe_effective_schedule(NINE_TO_SIX) == "Mon-Fri 09:00-18:00"


def test_describe_effective_schedule_noncontiguous_days() -> None:
    scattered = AccessScheduleSnapshot(
        enabled=True, allowed_days=frozenset({1, 3, 5}), allowed_hours_start=None, allowed_hours_end=None
    )
    assert describe_effective_schedule(scattered) == "Mon, Wed, Fri"
