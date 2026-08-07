"""In-process cache and DB-backed service for org->team->service-account
scheduled access windows (Phase 3, BD-16). See
`docs/design/phase-3-security-compliance-design.md` section 5 (5.1-5.3) and
the product spec's §9 (AC9.1-AC9.11).

Precedence and enforcement (security review finding, Phase 3 - supersedes
§5.1's original "narrow at write time, take the innermost enabled layer at
read time" ADR): the ENFORCEMENT decision (`resolve_access_schedule_decision`
below, wired into `api.v1.gateway.common.check_access_schedule`) now checks
EVERY enabled layer (org, team, service-account) cumulatively on every read
- mirroring `services.model_policy.resolve_model_access`'s org-then-team
pattern - instead of resolving only the single most-specific enabled layer
and trusting write-time narrowing to make that "provably equivalent" to a
full check. That equivalence silently breaks the moment a PARENT layer is
tightened AFTER a child layer was already validated as narrower-at-the-time:
e.g. an org window that was widened, a team narrowed under it (valid then),
then the org window is tightened again - the team's now-too-wide row is
never re-checked against the new org window, so the team's callers keep
sailing through the org's tightened schedule indefinitely with zero error
and zero audit signal. Checking every enabled layer at read time closes that
staleness gap. `resolve_effective_schedule` below (the single-innermost-
layer resolver) is KEPT, but is no longer a security decision function - it
now only feeds informational/non-enforcement consumers (AC9.10's admin
display, and `services.scheduler`'s off-hours rotation-timing anchor), where
a "most specific applicable window" is a reasonable simplification because
neither consumer is making an allow/deny call. Write-time narrowing
validation (`validate_schedule_narrows_parent`) stays in place as cheap
defense-in-depth - it just isn't relied on ALONE for the enforcement
decision's correctness anymore, same posture change applied to
`services.residency.resolve_residency`.

Timezone/holiday handling (§5.2): `compliance_settings.
access_schedule_timezone` (stdlib `zoneinfo`, no new dependency) converts
the current instant to org-local weekday/time-of-day/date. Deliberately
bundled into this SAME cache (not a separate one) so the gateway hot path
never pays an extra DB round trip for timezone/holiday config on top of the
schedule rows themselves.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.access_schedule import AccessSchedule, AccessScheduleScopeType
from gatekey.db.models.holiday_date import HolidayDate
from gatekey.errors import GatekeyError

ALL_WEEKDAYS = frozenset(range(1, 8))
_WEEKDAY_ABBREV = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}


# ---------------------------------------------------------------------------
# Snapshot type + cache (§5.1/9.11)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccessScheduleSnapshot:
    enabled: bool
    allowed_days: frozenset[int]
    allowed_hours_start: time | None
    allowed_hours_end: time | None


class AccessScheduleCache:
    """Process-local cache of the org rule, every team's rule, every
    service-account's rule, plus the org's access-schedule timezone and
    flat holiday-date list.

    Same lock-free, GIL-atomic "replace the whole snapshot, never mutate in
    place" contract as `services.model_policy.ModelPolicyCache`/
    `services.residency.ResidencyRuleCache` - see those classes' docstrings
    for the full rationale. Instantiated once per process and stored on
    `app.state` - never construct a second instance and thread it through
    separately.
    """

    def __init__(
        self,
        org: AccessScheduleSnapshot | None = None,
        team: dict[uuid.UUID, AccessScheduleSnapshot] | None = None,
        service_account: dict[uuid.UUID, AccessScheduleSnapshot] | None = None,
        timezone_name: str = "UTC",
        holiday_dates: frozenset[date] | None = None,
    ) -> None:
        self._org = org
        self._team: dict[uuid.UUID, AccessScheduleSnapshot] = dict(team or {})
        self._service_account: dict[uuid.UUID, AccessScheduleSnapshot] = dict(service_account or {})
        self._timezone_name = timezone_name
        self._holiday_dates: frozenset[date] = frozenset(holiday_dates or ())

    def get_org(self) -> AccessScheduleSnapshot | None:
        return self._org

    def get_team(self, team_id: uuid.UUID) -> AccessScheduleSnapshot | None:
        return self._team.get(team_id)

    def get_service_account(self, service_account_id: uuid.UUID) -> AccessScheduleSnapshot | None:
        return self._service_account.get(service_account_id)

    def get_timezone_name(self) -> str:
        return self._timezone_name

    def get_holiday_dates(self) -> frozenset[date]:
        return self._holiday_dates

    def set_all(
        self,
        *,
        org: AccessScheduleSnapshot | None,
        team: dict[uuid.UUID, AccessScheduleSnapshot],
        service_account: dict[uuid.UUID, AccessScheduleSnapshot],
        timezone_name: str,
        holiday_dates: frozenset[date],
    ) -> None:
        """Full replace - the startup-warm write."""
        self._org = org
        self._team = dict(team)
        self._service_account = dict(service_account)
        self._timezone_name = timezone_name
        self._holiday_dates = frozenset(holiday_dates)

    def set_org(self, snapshot: AccessScheduleSnapshot | None) -> None:
        self._org = snapshot

    def set_team(self, team_id: uuid.UUID, snapshot: AccessScheduleSnapshot | None) -> None:
        replacement = dict(self._team)
        if snapshot is None:
            replacement.pop(team_id, None)
        else:
            replacement[team_id] = snapshot
        self._team = replacement

    def set_service_account(
        self, service_account_id: uuid.UUID, snapshot: AccessScheduleSnapshot | None
    ) -> None:
        replacement = dict(self._service_account)
        if snapshot is None:
            replacement.pop(service_account_id, None)
        else:
            replacement[service_account_id] = snapshot
        self._service_account = replacement

    def set_timezone_name(self, timezone_name: str) -> None:
        self._timezone_name = timezone_name

    def set_holiday_dates(self, holiday_dates: frozenset[date]) -> None:
        self._holiday_dates = frozenset(holiday_dates)


# ---------------------------------------------------------------------------
# Precedence resolution + timezone/holiday evaluation (§5.1/5.2) - pure,
# synchronous, zero I/O.
# ---------------------------------------------------------------------------


def resolve_effective_schedule(
    *,
    cache: AccessScheduleCache,
    team_id: uuid.UUID | None,
    service_account_id: uuid.UUID,
) -> AccessScheduleSnapshot | None:
    """The innermost ENABLED layer - service-account, then team, then org.

    NOT the enforcement check anymore (see module docstring) - this is a
    single-window resolver kept only for consumers that display or reason
    about "the one most-specific applicable schedule" rather than making an
    allow/deny decision: AC9.10's admin display (`list_effective_schedules`/
    `describe_effective_schedule`) and `services.scheduler`'s off-hours
    rotation-timing anchor. The actual read-time gateway decision is
    `resolve_access_schedule_decision` below, which checks every enabled
    layer cumulatively, not just this one.

    A disabled (or absent) row at a more specific level defers to the
    next-less-specific enabled level, same "absence/off = no further
    restriction beyond the parent" semantics already established for team
    model policy - never "reopen access". `None` = "Always" (no restriction
    at any level). Only meaningful when the caller is a service-account
    credential (personal keys never reach this - see
    `api.v1.gateway.common.check_access_schedule`'s docstring)."""
    sa = cache.get_service_account(service_account_id)
    if sa is not None and sa.enabled:
        return sa
    if team_id is not None:
        team = cache.get_team(team_id)
        if team is not None and team.enabled:
            return team
    org = cache.get_org()
    if org is not None and org.enabled:
        return org
    return None


@dataclass(frozen=True)
class AccessScheduleDecision:
    """Outcome of the cumulative org/team/service-account schedule check
    (security review finding, Phase 3 - see module docstring). `blocking_
    layer` is `None` only when `allowed=True`; otherwise it names the FIRST
    enabled layer (checked org, then team, then service-account) whose
    window the current instant falls outside of."""

    allowed: bool
    blocking_layer: str | None  # "org" | "team" | "service_account" | None


def resolve_access_schedule_decision(
    *,
    cache: AccessScheduleCache,
    team_id: uuid.UUID | None,
    service_account_id: uuid.UUID,
    now: datetime,
    timezone_name: str,
    holiday_dates: frozenset[date],
) -> AccessScheduleDecision:
    """The real enforcement check (security review finding, Phase 3):
    requires `now` to fall within EVERY enabled layer's window - org AND
    team (if configured) AND service-account (if configured) - not just the
    single innermost enabled layer `resolve_effective_schedule` used to
    resolve to alone. See module docstring for the exact staleness bug this
    replaces (a tightened parent layer silently never re-checked against an
    already-validated-narrower child). Pure, synchronous, zero I/O - up to
    three in-process cache lookups plus `is_within_schedule` calls, same
    cost class as the innermost-only check it replaces (AC9.11)."""
    for layer_name, snapshot in (
        ("org", cache.get_org()),
        ("team", cache.get_team(team_id) if team_id is not None else None),
        ("service_account", cache.get_service_account(service_account_id)),
    ):
        if snapshot is None or not snapshot.enabled:
            continue
        if not is_within_schedule(
            snapshot, now=now, timezone_name=timezone_name, holiday_dates=holiday_dates
        ):
            return AccessScheduleDecision(allowed=False, blocking_layer=layer_name)
    return AccessScheduleDecision(allowed=True, blocking_layer=None)


def _within_hours(current: time, start: time | None, end: time | None) -> bool:
    if start is None or end is None:
        return True  # this layer restricts days only, not hours
    if start <= end:
        return start <= current <= end
    # ponytail: overnight wrap (e.g. 22:00-06:00) - simple modular check,
    # no interval-arithmetic library. Write-time narrowing validation
    # (validate_schedule_narrows_parent) does NOT reason about wrapped
    # intervals - upgrade path if a real org needs a narrowed overnight
    # window; not built speculatively now.
    return current >= start or current <= end


def is_within_schedule(
    effective: AccessScheduleSnapshot | None,
    *,
    now: datetime,
    timezone_name: str,
    holiday_dates: frozenset[date],
) -> bool:
    """§5.2: converts `now` (must be timezone-aware) to the org's local
    weekday/time-of-day/date via stdlib `zoneinfo` - deliberately NOT a
    UTC-date comparison for the holiday lookup (a UTC-date vs org-local-date
    mismatch near midnight would apply the wrong day's holiday status).
    `effective=None` ('Always') always passes without even loading the
    timezone. Pure, synchronous, zero I/O."""
    if effective is None or not effective.enabled:
        return True
    local_now = now.astimezone(ZoneInfo(timezone_name))
    if local_now.date() in holiday_dates:
        return False
    if local_now.isoweekday() not in effective.allowed_days:
        return False
    return _within_hours(local_now.time(), effective.allowed_hours_start, effective.allowed_hours_end)


# ---------------------------------------------------------------------------
# AC9.10 - human-readable resolved schedule ("Mon-Fri 9-6" / "Always")
# ---------------------------------------------------------------------------


def _describe_days(days: frozenset[int]) -> str:
    if not days:
        return "No days allowed"
    ordered = sorted(days)
    if ordered == list(range(1, 8)):
        return "Every day"
    if len(ordered) > 1 and ordered == list(range(ordered[0], ordered[-1] + 1)):
        return f"{_WEEKDAY_ABBREV[ordered[0]]}-{_WEEKDAY_ABBREV[ordered[-1]]}"
    return ", ".join(_WEEKDAY_ABBREV[d] for d in ordered)


def describe_effective_schedule(effective: AccessScheduleSnapshot | None) -> str:
    """AC9.10: a real server-side resolved description ("Mon-Fri 9:00-18:00"
    or "Always"), not merely has-an-override:yes/no."""
    if effective is None or not effective.enabled:
        return "Always"
    days_desc = _describe_days(effective.allowed_days)
    if effective.allowed_hours_start is not None and effective.allowed_hours_end is not None:
        hours_desc = (
            f"{effective.allowed_hours_start.strftime('%H:%M')}-"
            f"{effective.allowed_hours_end.strftime('%H:%M')}"
        )
        return f"{days_desc} {hours_desc}"
    return days_desc


# ---------------------------------------------------------------------------
# Write-time validation (§5.1's write-time narrowing + basic shape checks)
# ---------------------------------------------------------------------------


class InvalidAccessScheduleWindowError(GatekeyError):
    """`allowed_days`/`allowed_hours_start`/`allowed_hours_end` fail a basic
    shape check (out-of-range weekday, or only one of the hour bounds set).
    422, `code="invalid_access_schedule_window"`."""

    status_code = 422
    code = "invalid_access_schedule_window"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class AccessScheduleWidensParentError(GatekeyError):
    """AC9.2 defense-in-depth: a child schedule may only ever narrow its
    resolved parent's `allowed_days`/`allowed_hours` - server-side enforced,
    not merely UI-hidden. Identical shape to `services.residency.
    ResidencyRuleWidensOrgRuleError`/`services.model_policy.
    TeamModelRestrictsOrgDeniedModelError`. 422,
    `code="access_schedule_widens_parent"`."""

    status_code = 422
    code = "access_schedule_widens_parent"

    def __init__(self, reason: str) -> None:
        super().__init__(f"This schedule must narrow its parent's window: {reason}")


def _validate_window(
    allowed_days: list[int], allowed_hours_start: time | None, allowed_hours_end: time | None
) -> frozenset[int]:
    days = frozenset(allowed_days)
    if days - ALL_WEEKDAYS:
        raise InvalidAccessScheduleWindowError(
            "allowed_days entries must be ISO weekday ints 1(Mon)-7(Sun)."
        )
    if (allowed_hours_start is None) != (allowed_hours_end is None):
        raise InvalidAccessScheduleWindowError(
            "allowed_hours_start and allowed_hours_end must be set together, or both omitted."
        )
    return days


def _hours_subset(
    child_start: time | None,
    child_end: time | None,
    parent_start: time | None,
    parent_end: time | None,
) -> bool:
    if parent_start is None or parent_end is None:
        return True  # parent unrestricted on hours - any (validated) child window narrows it
    if child_start is None or child_end is None:
        return False  # child would be hour-unrestricted while the parent isn't -> widens
    # ponytail: assumes both windows are same-day (start <= end); overnight-
    # wrap narrowing validation is a known, documented gap - see
    # `_within_hours`'s note above.
    return child_start >= parent_start and child_end <= parent_end


def validate_schedule_narrows_parent(
    *,
    allowed_days: list[int] | frozenset[int],
    allowed_hours_start: time | None,
    allowed_hours_end: time | None,
    parent: AccessScheduleSnapshot | None,
) -> None:
    """AC9.2: a day-set subset check plus an hour-range subset check against
    the resolved PARENT schedule (the next-less-specific ENABLED layer).
    `parent=None` (no enabled ancestor -> "Always") admits any (already
    shape-validated) child value - there is nothing to narrow against."""
    if parent is None:
        return
    offending_days = frozenset(allowed_days) - parent.allowed_days
    if offending_days:
        raise AccessScheduleWidensParentError(
            "day(s) not allowed by the parent schedule: "
            + ", ".join(str(d) for d in sorted(offending_days))
        )
    if not _hours_subset(
        allowed_hours_start, allowed_hours_end, parent.allowed_hours_start, parent.allowed_hours_end
    ):
        raise AccessScheduleWidensParentError(
            "the allowed-hours window must fall within the parent schedule's window."
        )


# ---------------------------------------------------------------------------
# DB-backed CRUD (admin/team-lead API + cache warmup)
# ---------------------------------------------------------------------------


def _snapshot_from_row(row: AccessSchedule) -> AccessScheduleSnapshot:
    return AccessScheduleSnapshot(
        enabled=row.enabled,
        allowed_days=frozenset(row.allowed_days),
        allowed_hours_start=row.allowed_hours_start,
        allowed_hours_end=row.allowed_hours_end,
    )


async def load_access_schedule_snapshot(
    session: AsyncSession,
) -> tuple[
    AccessScheduleSnapshot | None,
    dict[uuid.UUID, AccessScheduleSnapshot],
    dict[uuid.UUID, AccessScheduleSnapshot],
]:
    """Query every access-schedule row - used at process startup only (to
    warm `AccessScheduleCache`, see `main.py`'s lifespan). NEVER call this
    from a gateway route handler (same zero-DB hot-path rule as
    `services.model_policy.load_policy_snapshot`)."""
    rows = (await session.execute(select(AccessSchedule))).scalars().all()
    org: AccessScheduleSnapshot | None = None
    team: dict[uuid.UUID, AccessScheduleSnapshot] = {}
    service_account: dict[uuid.UUID, AccessScheduleSnapshot] = {}
    for row in rows:
        snapshot = _snapshot_from_row(row)
        if row.scope_type == AccessScheduleScopeType.ORG:
            org = snapshot
        elif row.scope_type == AccessScheduleScopeType.TEAM:
            assert row.scope_team_id is not None
            team[row.scope_team_id] = snapshot
        else:
            assert row.scope_service_account_id is not None
            service_account[row.scope_service_account_id] = snapshot
    return org, team, service_account


async def load_holiday_dates(session: AsyncSession) -> frozenset[date]:
    """Startup-warm only (same rule as `load_access_schedule_snapshot`)."""
    rows = (
        await session.execute(select(HolidayDate.holiday_date).where(HolidayDate.org_id == DEFAULT_ORG_ID))
    ).scalars().all()
    return frozenset(rows)


async def get_org_access_schedule(session: AsyncSession) -> AccessSchedule | None:
    stmt = select(AccessSchedule).where(
        AccessSchedule.org_id == DEFAULT_ORG_ID,
        AccessSchedule.scope_type == AccessScheduleScopeType.ORG,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_team_access_schedule(session: AsyncSession, team_id: uuid.UUID) -> AccessSchedule | None:
    stmt = select(AccessSchedule).where(AccessSchedule.scope_team_id == team_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_service_account_access_schedule(
    session: AsyncSession, service_account_id: uuid.UUID
) -> AccessSchedule | None:
    stmt = select(AccessSchedule).where(AccessSchedule.scope_service_account_id == service_account_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def _resolve_org_parent(session: AsyncSession) -> AccessScheduleSnapshot | None:
    org_row = await get_org_access_schedule(session)
    if org_row is not None and org_row.enabled:
        return _snapshot_from_row(org_row)
    return None


async def _resolve_service_account_parent(
    session: AsyncSession, *, team_id: uuid.UUID | None
) -> AccessScheduleSnapshot | None:
    if team_id is not None:
        team_row = await get_team_access_schedule(session, team_id)
        if team_row is not None and team_row.enabled:
            return _snapshot_from_row(team_row)
    return await _resolve_org_parent(session)


async def set_org_access_schedule(
    session: AsyncSession,
    *,
    enabled: bool,
    allowed_days: list[int],
    allowed_hours_start: time | None,
    allowed_hours_end: time | None,
    cache: AccessScheduleCache | None = None,
) -> AccessSchedule:
    """Validate then atomically full-replace-upsert the org-wide schedule.
    The org row is the root of the precedence chain - nothing to narrow
    against.

    Hardening pass item 2 (QA audit of every `on_conflict_do_update(...).
    returning(...)` call site for the same defect fixed in `services.
    residency.set_org_residency_rule`/`services.dlp.set_dlp_policy` - see
    that function's docstring for the full mechanism): `execution_options=
    {"populate_existing": True}` on the upsert below is REQUIRED, not
    decorative, and IS live-triggered here. `api/v1/admin/access_schedule.
    py`'s `put_org_access_schedule_endpoint` pre-reads the CURRENT row
    (`get_org_access_schedule(session)`, for its own audit-entry
    `old_value`) into this SAME session's identity map BEFORE calling this
    function. Without `populate_existing`, SQLAlchemy 2.0's ORM-enabled
    `INSERT ... RETURNING` matches the returned row's primary key against
    that already-identity-mapped (stale, pre-update) object and returns it
    unchanged instead of the fresh post-update values on every UPDATE (not
    the first-ever INSERT, which has no pre-existing identity-mapped object
    to collide with). That means `cache.set_org(...)` below would silently
    re-arm `AccessScheduleCache` with the OLD, pre-tightening window -
    `resolve_access_schedule_decision()` (read on every single gateway
    request from a service-account credential) would keep enforcing the
    OLD, more permissive window for the rest of this process's lifetime, a
    real enforcement-correctness bug (e.g. a narrowed allowed-hours window,
    or re-enabling enforcement after it was relaxed, could silently never
    take effect)."""
    days = _validate_window(allowed_days, allowed_hours_start, allowed_hours_end)
    insert_stmt = postgresql.insert(AccessSchedule).values(
        org_id=DEFAULT_ORG_ID,
        scope_type=AccessScheduleScopeType.ORG,
        scope_team_id=None,
        scope_service_account_id=None,
        enabled=enabled,
        allowed_days=sorted(days),
        allowed_hours_start=allowed_hours_start,
        allowed_hours_end=allowed_hours_end,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[AccessSchedule.org_id],
        index_where=text("scope_type = 'org'"),
        set_={
            "enabled": insert_stmt.excluded.enabled,
            "allowed_days": insert_stmt.excluded.allowed_days,
            "allowed_hours_start": insert_stmt.excluded.allowed_hours_start,
            "allowed_hours_end": insert_stmt.excluded.allowed_hours_end,
            "updated_at": text("now()"),
        },
    ).returning(AccessSchedule)
    row = (
        await session.execute(upsert_stmt, execution_options={"populate_existing": True})
    ).scalar_one()
    await session.commit()
    if cache is not None:
        cache.set_org(_snapshot_from_row(row))
    return row


async def set_team_access_schedule(
    session: AsyncSession,
    team_id: uuid.UUID,
    *,
    enabled: bool,
    allowed_days: list[int],
    allowed_hours_start: time | None,
    allowed_hours_end: time | None,
    cache: AccessScheduleCache | None = None,
) -> AccessSchedule:
    """Validate (including AC9.2 narrowing-only defense-in-depth against the
    CURRENT org schedule, re-read directly from the DB) then atomically
    full-replace-upsert one team's schedule.

    Hardening pass item 2: `execution_options={"populate_existing": True}`
    on the upsert below is REQUIRED for the same reason as `set_org_
    access_schedule`'s identical fix (see that function's docstring for the
    full mechanism) - `api/v1/teams.py`'s `put_team_access_schedule_
    endpoint` also pre-reads the current row (`get_team_access_schedule`)
    into this session's identity map before calling this function, and IS
    live-triggered: without this fix, `cache.set_team(...)` below would
    silently re-arm `AccessScheduleCache` with the OLD, pre-tightening
    team window, which `resolve_access_schedule_decision()` would keep
    enforcing on every gateway request from that team's service accounts."""
    days = _validate_window(allowed_days, allowed_hours_start, allowed_hours_end)
    parent = await _resolve_org_parent(session)
    validate_schedule_narrows_parent(
        allowed_days=days,
        allowed_hours_start=allowed_hours_start,
        allowed_hours_end=allowed_hours_end,
        parent=parent,
    )
    insert_stmt = postgresql.insert(AccessSchedule).values(
        org_id=DEFAULT_ORG_ID,
        scope_type=AccessScheduleScopeType.TEAM,
        scope_team_id=team_id,
        scope_service_account_id=None,
        enabled=enabled,
        allowed_days=sorted(days),
        allowed_hours_start=allowed_hours_start,
        allowed_hours_end=allowed_hours_end,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[AccessSchedule.scope_team_id],
        index_where=text("scope_team_id IS NOT NULL"),
        set_={
            "enabled": insert_stmt.excluded.enabled,
            "allowed_days": insert_stmt.excluded.allowed_days,
            "allowed_hours_start": insert_stmt.excluded.allowed_hours_start,
            "allowed_hours_end": insert_stmt.excluded.allowed_hours_end,
            "updated_at": text("now()"),
        },
    ).returning(AccessSchedule)
    row = (
        await session.execute(upsert_stmt, execution_options={"populate_existing": True})
    ).scalar_one()
    await session.commit()
    if cache is not None:
        cache.set_team(team_id, _snapshot_from_row(row))
    return row


async def set_service_account_access_schedule(
    session: AsyncSession,
    service_account_id: uuid.UUID,
    *,
    team_id: uuid.UUID | None,
    enabled: bool,
    allowed_days: list[int],
    allowed_hours_start: time | None,
    allowed_hours_end: time | None,
    cache: AccessScheduleCache | None = None,
) -> AccessSchedule:
    """Validate (AC9.2 narrowing-only against the resolved team/org parent,
    re-read directly from the DB) then atomically full-replace-upsert one
    key's schedule. `team_id` is the key's OWN team attribution (the
    caller looks this up from the `ServiceAccountKey` row) - used only to
    resolve the correct parent, never persisted on this row.

    Hardening pass item 2: `execution_options={"populate_existing": True}`
    on the upsert below is REQUIRED for the same reason as `set_org_
    access_schedule`'s identical fix (see that function's docstring for the
    full mechanism) - `api/v1/keys.py`'s `put_key_access_schedule_endpoint`
    also pre-reads the current row (`get_service_account_access_schedule`)
    into this session's identity map before calling this function, and IS
    live-triggered: without this fix, `cache.set_service_account(...)`
    below would silently re-arm `AccessScheduleCache` with the OLD,
    pre-tightening key-level window, which `resolve_access_schedule_
    decision()` would keep enforcing on every gateway request from this
    service-account key."""
    days = _validate_window(allowed_days, allowed_hours_start, allowed_hours_end)
    parent = await _resolve_service_account_parent(session, team_id=team_id)
    validate_schedule_narrows_parent(
        allowed_days=days,
        allowed_hours_start=allowed_hours_start,
        allowed_hours_end=allowed_hours_end,
        parent=parent,
    )
    insert_stmt = postgresql.insert(AccessSchedule).values(
        org_id=DEFAULT_ORG_ID,
        scope_type=AccessScheduleScopeType.SERVICE_ACCOUNT,
        scope_team_id=None,
        scope_service_account_id=service_account_id,
        enabled=enabled,
        allowed_days=sorted(days),
        allowed_hours_start=allowed_hours_start,
        allowed_hours_end=allowed_hours_end,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[AccessSchedule.scope_service_account_id],
        index_where=text("scope_service_account_id IS NOT NULL"),
        set_={
            "enabled": insert_stmt.excluded.enabled,
            "allowed_days": insert_stmt.excluded.allowed_days,
            "allowed_hours_start": insert_stmt.excluded.allowed_hours_start,
            "allowed_hours_end": insert_stmt.excluded.allowed_hours_end,
            "updated_at": text("now()"),
        },
    ).returning(AccessSchedule)
    row = (
        await session.execute(upsert_stmt, execution_options={"populate_existing": True})
    ).scalar_one()
    await session.commit()
    if cache is not None:
        cache.set_service_account(service_account_id, _snapshot_from_row(row))
    return row


async def delete_org_access_schedule(
    session: AsyncSession, *, cache: AccessScheduleCache | None = None
) -> bool:
    stmt = delete(AccessSchedule).where(
        AccessSchedule.org_id == DEFAULT_ORG_ID, AccessSchedule.scope_type == AccessScheduleScopeType.ORG
    )
    result = await session.execute(stmt)
    await session.commit()
    deleted = (result.rowcount or 0) > 0
    if deleted and cache is not None:
        cache.set_org(None)
    return deleted


async def delete_team_access_schedule(
    session: AsyncSession, team_id: uuid.UUID, *, cache: AccessScheduleCache | None = None
) -> bool:
    stmt = delete(AccessSchedule).where(AccessSchedule.scope_team_id == team_id)
    result = await session.execute(stmt)
    await session.commit()
    deleted = (result.rowcount or 0) > 0
    if deleted and cache is not None:
        cache.set_team(team_id, None)
    return deleted


async def delete_service_account_access_schedule(
    session: AsyncSession, service_account_id: uuid.UUID, *, cache: AccessScheduleCache | None = None
) -> bool:
    stmt = delete(AccessSchedule).where(AccessSchedule.scope_service_account_id == service_account_id)
    result = await session.execute(stmt)
    await session.commit()
    deleted = (result.rowcount or 0) > 0
    if deleted and cache is not None:
        cache.set_service_account(service_account_id, None)
    return deleted


# ---------------------------------------------------------------------------
# AC9.10 - effective-schedule listing for every service-account key
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeyEffectiveSchedule:
    service_account_id: uuid.UUID
    name: str
    team_id: uuid.UUID | None
    effective: str


async def list_effective_schedules(
    session: AsyncSession, *, cache: AccessScheduleCache
) -> list[KeyEffectiveSchedule]:
    """`GET /v1/admin/keys/schedules` (AC9.10) - the fully-resolved
    effective schedule per service-account key, not merely
    has-an-override:yes/no. Lazy import of `services.service_accounts` to
    avoid a top-level circular import (mirrors `services.rotation`'s own
    lazy-import convention for cross-module lookups)."""
    from gatekey.services.service_accounts import list_service_accounts

    keys = await list_service_accounts(session)
    return [
        KeyEffectiveSchedule(
            service_account_id=key.id,
            name=key.name,
            team_id=key.team_id,
            effective=describe_effective_schedule(
                resolve_effective_schedule(cache=cache, team_id=key.team_id, service_account_id=key.id)
            ),
        )
        for key in keys
    ]


# ---------------------------------------------------------------------------
# Holiday dates (§1.12, flat org-wide list - no calendar-ref indirection)
# ---------------------------------------------------------------------------


class DuplicateHolidayDateError(GatekeyError):
    status_code = 409
    code = "holiday_date_already_exists"

    def __init__(self, holiday_date: date) -> None:
        super().__init__(f"A holiday date already exists for {holiday_date.isoformat()}.")


async def list_holiday_dates(session: AsyncSession) -> list[HolidayDate]:
    stmt = (
        select(HolidayDate)
        .where(HolidayDate.org_id == DEFAULT_ORG_ID)
        .order_by(HolidayDate.holiday_date)
    )
    return list((await session.execute(stmt)).scalars().all())


async def add_holiday_date(
    session: AsyncSession,
    *,
    holiday_date: date,
    label: str | None,
    cache: AccessScheduleCache | None = None,
) -> HolidayDate:
    row = HolidayDate(org_id=DEFAULT_ORG_ID, holiday_date=holiday_date, label=label)
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise DuplicateHolidayDateError(holiday_date) from None
    await session.commit()
    if cache is not None:
        cache.set_holiday_dates(cache.get_holiday_dates() | {holiday_date})
    return row


async def delete_holiday_date(
    session: AsyncSession, holiday_date_id: uuid.UUID, *, cache: AccessScheduleCache | None = None
) -> HolidayDate | None:
    row = (
        await session.execute(
            select(HolidayDate).where(
                HolidayDate.org_id == DEFAULT_ORG_ID, HolidayDate.id == holiday_date_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    await session.execute(delete(HolidayDate).where(HolidayDate.id == holiday_date_id))
    await session.commit()
    if cache is not None:
        cache.set_holiday_dates(cache.get_holiday_dates() - {row.holiday_date})
    return row
