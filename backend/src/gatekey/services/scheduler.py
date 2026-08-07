"""In-process asyncio scheduler loop (Phase 3, BD-11, design doc sections
4.2/10 fork #1).

The first true wall-clock-driven periodic-job mechanism in this codebase -
unlike Phase 2's ADR-10 lazy/touch-based `services.team_periods` pattern,
service-account key rotation (AC7.5: zero admin action required) and the
audit purge job (AC1.6/AC1.7) have a real "fire at a specific wall-clock
moment with nobody making a request" requirement that a lazy/touch-based
check cannot satisfy for a dormant, low-traffic org.

`run_scheduler_loop` is started as a single background `asyncio.Task` in
`main.py`'s lifespan (one per worker process) and cancelled cleanly on
shutdown, alongside the existing `provider_http_client`/`vertex_token_cache`
singletons.

**Multi-worker safety (atomic claim mechanism)**: if the backend runs as
multiple worker processes/replicas, each runs this loop independently and
would, without care, double-fire the same due rotation. Closed via an
atomic claim-and-advance `UPDATE ... WHERE id = :id AND next_rotation_at =
:expected RETURNING *` (see `run_due_rotations` below) - the SAME
`UPDATE ... RETURNING` optimistic-concurrency pattern already used
elsewhere in this codebase (e.g. `services.service_accounts.
revoke_service_account`), applied to a new problem shape: the row's
`next_rotation_at` is advanced to the NEXT cycle in the same statement that
claims it for processing, so only one worker's `UPDATE` matches the `WHERE`
clause and returns a row; every other worker's identical `UPDATE` affects
zero rows. No distributed lock, no new dependency.

Each tick does a small, bounded amount of DB-bound work (`_ROTATION_BATCH_
SIZE`/`_PURGE_BATCH_SIZE` per iteration) and never blocks the event loop
beyond ordinary awaited I/O - `asyncio.sleep` between ticks, never a busy
loop.

Scope note (design doc section 4.2's exact job list): this loop runs
rotation-firing + the audit purge job + the log/prompt-retention purge job.
It deliberately does NOT run an access-schedule emergency-override expiry
sweep - overrides are evaluated live at request time (`expires_at`/
`revoked_at` columns compared against `now()` on the read path, design doc
section 5.3), so no cleanup job is needed or specified for them.

The log-prompt-retention purge job (`run_log_prompt_purge_if_due`, design
doc section 7.3/AC6.2) is a DIFFERENT, independently-scheduled function
against different tables (`usage_logs`, `dlp_scan_results`) and a different
config column (`compliance_settings.log_prompt_retention_days`) - never
sharing a code path with the audit purge, so a future change to one window
structurally cannot affect the other's data (AC6.2's explicit requirement).
Unlike `audit_retention_days`, `log_prompt_retention_days` is a DB-level
`NOT NULL` column (`db/models/compliance_settings.py`, design doc section
1.2) with no "never purge" state - this window always has a finite value
(default 30) and this job always fires, it is never NULL-skipped the way
the audit purge is.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import delete, select, update

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.audit_entry import AuditEntry
from gatekey.db.models.dlp_scan_result import DlpScanResult
from gatekey.db.models.rotation_policy import RotationPolicy, RotationScopeType
from gatekey.db.models.service_account_key import ServiceAccountKey
from gatekey.db.models.shadow_ai_ingest_event import ShadowAiIngestEvent
from gatekey.db.models.usage_log import UsageLog
from gatekey.services import provider_key_health
from gatekey.services.access_schedules import resolve_effective_schedule
from gatekey.services.compliance_settings import get_effective_compliance_settings
from gatekey.services.drift_detector import run_canary_suite_for_org
from gatekey.services.rotation import (
    AccessScheduleWindow,
    compute_next_rotation,
    deliver_service_account_rotation_notification,
)
from gatekey.services.service_accounts import rotate_service_account_key
from gatekey.services.shadow_ai import get_shadow_ai_ingest_config

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("gatekey")

DEFAULT_POLL_INTERVAL_SECONDS = 60
# Bounds per-tick DB work (design doc section 2's "must not block the event
# loop" requirement) - a tick with more due rows than this simply picks the
# rest up on the next tick, `poll_interval_seconds` later.
_ROTATION_BATCH_SIZE = 20
_PURGE_BATCH_SIZE = 5000

# AC4.1.6 / technical design section 6.2: provider-key health checks run
# every 5 minutes, not every scheduler tick (`DEFAULT_POLL_INTERVAL_SECONDS`
# is 60s). Unlike the audit/log-prompt purge jobs (whose underlying DELETE
# is itself naturally a no-op once nothing is past the cutoff, so they can
# safely re-run every tick with no separate "last run" bookkeeping), a
# health check makes a real outbound call per key every time it runs - it
# must NOT fire on every 60s tick. Tracked via a plain in-memory
# `app.state` marker (module docstring's "in-memory app.state marker"
# option) rather than a new DB column/table (schema is frozen this task) -
# same "acceptable to lose on restart, cheap to re-derive" posture as
# `services.provider_key_health`'s own health state.
PROVIDER_KEY_HEALTH_CHECK_INTERVAL_SECONDS = 5 * 60

# Phase 5 (Differentiators, 5.4 Provider Drift Detector, AC5.4.8): the daily
# canary suite - exact same in-memory `app.state` last-run-marker shape as
# `PROVIDER_KEY_HEALTH_CHECK_INTERVAL_SECONDS` above, just a 24h interval
# instead of 5 minutes (design doc section 2.2/5 wiring checklist row 2).
DRIFT_CANARY_CHECK_INTERVAL_SECONDS = 24 * 60 * 60

# Phase 5 (Differentiators, 5.1 Shadow AI Discovery, AC5.1.10): fallback
# retention window for `run_shadow_ai_purge_if_due` below when no
# `shadow_ai_ingest_config` row exists yet for the org (mirrors that
# column's own DB `server_default`, `alembic/versions/0042_create_shadow_ai_
# tables.py`) - a defensive fallback only; in practice no rows can exist to
# purge until an Org Admin has generated an ingestion token (AC5.1.4
# fail-closed), by which point a config row always exists.
_SHADOW_AI_DEFAULT_RETENTION_DAYS = 90


async def run_scheduler_loop(app: "FastAPI", *, poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS) -> None:
    """The single background loop task - started once per process in
    `main.py`'s lifespan. Runs until cancelled. Any exception from a single
    tick's work is caught and logged so one bad tick (e.g. a transient DB
    blip) never kills the loop for the rest of the process's life."""
    while True:
        await asyncio.sleep(poll_interval_seconds)
        try:
            async with app.state.db_session_factory() as session:
                await run_due_rotations(session, app)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("scheduler_rotation_tick_failed", exc_info=True)
        try:
            async with app.state.db_session_factory() as session:
                await run_audit_purge_if_due(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("scheduler_audit_purge_tick_failed", exc_info=True)
        try:
            async with app.state.db_session_factory() as session:
                await run_log_prompt_purge_if_due(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("scheduler_log_prompt_purge_tick_failed", exc_info=True)
        try:
            async with app.state.db_session_factory() as session:
                await run_provider_key_health_check_if_due(session, app)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("scheduler_provider_key_health_check_tick_failed", exc_info=True)
        try:
            async with app.state.db_session_factory() as session:
                await run_drift_canary_if_due(session, app)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("scheduler_drift_canary_tick_failed", exc_info=True)
        try:
            async with app.state.db_session_factory() as session:
                await run_shadow_ai_purge_if_due(session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("scheduler_shadow_ai_purge_tick_failed", exc_info=True)


async def _resolve_access_schedule_window(
    session: "AsyncSession", app: "FastAPI", *, service_account_id: uuid.UUID | None
) -> AccessScheduleWindow | None:
    """Now that `services.access_schedules` exists (BD-16), resolve the
    claimed key's REAL effective schedule (org->team->key precedence) for
    `compute_next_rotation`'s AC7.3 off-hours anchor - see that function's
    docstring for why `access_schedule=None` used to be the only option
    here. One extra indexed lookup per due candidate (bounded by
    `_ROTATION_BATCH_SIZE`, same cost class as the rotation itself) - not a
    hot gateway path, so this isn't a candidate for the zero-I/O discipline
    `AccessScheduleCache` gives the request-time check. `app.state.
    access_schedule_cache` may not exist on a bare/mocked `FastAPI`-like
    object in a unit test - falls back to `None` (byte-for-byte the prior
    behavior) rather than raising."""
    cache = getattr(app.state, "access_schedule_cache", None)
    if cache is None or service_account_id is None:
        return None
    key = (
        await session.execute(
            select(ServiceAccountKey.team_id).where(ServiceAccountKey.id == service_account_id)
        )
    ).one_or_none()
    team_id = key.team_id if key is not None else None
    effective = resolve_effective_schedule(
        cache=cache, team_id=team_id, service_account_id=service_account_id
    )
    if effective is None:
        return None
    return AccessScheduleWindow(
        enabled=effective.enabled,
        allowed_days=tuple(sorted(effective.allowed_days)),
        allowed_hours_start=effective.allowed_hours_start,
        allowed_hours_end=effective.allowed_hours_end,
    )


async def run_due_rotations(session: "AsyncSession", app: "FastAPI") -> int:
    """AC7.1/AC7.5: fires every due, ENABLED, `service_account`-scoped
    rotation policy - never `provider_key` scope (that scope is always
    `manual_guided`, admin-initiated only; see `services.rotation` module
    docstring). Returns the number of rotations actually fired (0 if none
    due, or if every due row was already claimed by another worker this
    tick).

    One SELECT (bounded, `_ROTATION_BATCH_SIZE`) to find candidates, then
    one atomic claim-and-advance `UPDATE ... RETURNING` PER candidate (see
    module docstring) - not a single bulk UPDATE, because each row's next
    `next_rotation_at` must be computed individually
    (`compute_next_rotation`, which needs that row's own `interval_days`/
    `rotate_at_local_time`).
    """
    now = datetime.now(timezone.utc)
    compliance = await get_effective_compliance_settings(session)

    candidates = (
        (
            await session.execute(
                select(RotationPolicy)
                .where(
                    RotationPolicy.org_id == DEFAULT_ORG_ID,
                    RotationPolicy.scope_type == RotationScopeType.SERVICE_ACCOUNT,
                    RotationPolicy.enabled.is_(True),
                    RotationPolicy.next_rotation_at.is_not(None),
                    RotationPolicy.next_rotation_at <= now,
                )
                .order_by(RotationPolicy.next_rotation_at)
                .limit(_ROTATION_BATCH_SIZE)
            )
        )
        .scalars()
        .all()
    )

    fired = 0
    for policy in candidates:
        interval_days = policy.interval_days or 90
        access_schedule = await _resolve_access_schedule_window(
            session, app, service_account_id=policy.scope_service_account_id
        )
        next_next = compute_next_rotation(
            now=now,
            interval_days=interval_days,
            rotate_at_local_time=policy.rotate_at_local_time,
            timezone_name=compliance.access_schedule_timezone,
            access_schedule=access_schedule,
        )
        claim_stmt = (
            update(RotationPolicy)
            .where(
                RotationPolicy.id == policy.id,
                RotationPolicy.next_rotation_at == policy.next_rotation_at,
            )
            .values(next_rotation_at=next_next, last_rotated_at=now)
            .returning(RotationPolicy.id, RotationPolicy.scope_service_account_id, RotationPolicy.overlap_buffer_minutes)
        )
        claimed = (await session.execute(claim_stmt)).one_or_none()
        if claimed is None:
            # Another worker already claimed this row this tick - skip.
            continue
        await session.commit()

        key_id = claimed.scope_service_account_id
        if key_id is None:
            continue  # CHECK constraint guarantees this never happens; defensive only.
        rotated = await rotate_service_account_key(
            session, key_id=key_id, overlap_buffer_minutes=claimed.overlap_buffer_minutes
        )
        if rotated is None:
            # Key was revoked/deleted between the claim and the rotation -
            # the claimed policy row already advanced; nothing further to do.
            await session.rollback()
            continue
        row, _secret = rotated
        overlap_expires_at = row.previous_secret_valid_until
        await session.commit()
        fired += 1

        # No HTTP response exists in this loop's context to defer delivery
        # after (unlike the manual "Rotate now" admin route, which uses
        # `BackgroundTasks`) - awaited directly; failures are caught and
        # logged inside this function, never propagate.
        await deliver_service_account_rotation_notification(
            app,
            service_account_id=row.id,
            key_name=row.name,
            rotated_at=now,
            overlap_expires_at=overlap_expires_at,
        )

    return fired


async def _purge_rows_older_than(session: "AsyncSession", model, cutoff: datetime) -> int:
    """Shared batched-delete loop (`_PURGE_BATCH_SIZE` per iteration) for
    every org-scoped, `created_at`-indexed table this scheduler purges from.
    Both `run_audit_purge_if_due` and `run_log_prompt_purge_if_due` share
    this one code path for the DELETE mechanics themselves (looping/
    batching/commit shape) while each still reads its own independent
    config column and decides independently whether/when to call this at
    all - the "never share a purge routine" requirement (AC6.2) is about the
    config-driven decision of *what* to purge and *when*, not about
    duplicating the batching loop's plumbing three times."""
    total_deleted = 0
    while True:
        stmt = delete(model).where(
            model.id.in_(
                select(model.id)
                .where(model.org_id == DEFAULT_ORG_ID, model.created_at < cutoff)
                .limit(_PURGE_BATCH_SIZE)
            )
        )
        result = await session.execute(stmt)
        await session.commit()
        deleted_this_round = result.rowcount or 0
        total_deleted += deleted_this_round
        if deleted_this_round < _PURGE_BATCH_SIZE:
            break
    return total_deleted


async def run_audit_purge_if_due(session: "AsyncSession") -> int:
    """Design doc section 7.3 / AC1.6-AC1.7 - the ONE sanctioned exception
    to `audit_entries`'s "never DELETE" discipline (see `db/models/
    audit_entry.py`'s module docstring for the full exception statement).

    `compliance_settings.audit_retention_days = NULL` (the default) means
    this function returns immediately without issuing any DELETE - the
    purge job never fires for an org that hasn't explicitly configured a
    finite window. Batched (`_PURGE_BATCH_SIZE` per iteration, looped) to
    avoid one long-running transaction against a potentially large table.
    Returns the total number of rows deleted this call.

    Phase 5 (5.2, AC5.2.7): also a no-op whenever `chain_enabled = true` for
    the org, regardless of `audit_retention_days` - deleting a row
    structurally breaks a hash chain (see `db/models/audit_entry.py`'s
    module docstring and `services/compliance_settings.py`'s mutual-
    exclusivity enforcement, which is the primary defense; this guard is
    the scheduler-side backstop for a row that somehow ended up in both
    states, e.g. mid-migration/rollback).
    """
    compliance = await get_effective_compliance_settings(session)
    if compliance.chain_enabled:
        return 0
    if compliance.audit_retention_days is None:
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(days=compliance.audit_retention_days)
    total_deleted = await _purge_rows_older_than(session, AuditEntry, cutoff)
    if total_deleted:
        logger.info("audit_purge_completed", extra={"deleted": total_deleted})
    return total_deleted


async def run_log_prompt_purge_if_due(session: "AsyncSession") -> int:
    """Design doc section 7.3 / AC6.2 - the log/prompt retention purge job,
    independently scheduled from (and sharing no code path with) the audit
    purge above.

    Reads `compliance_settings.log_prompt_retention_days` and, against that
    cutoff, hard-deletes (AC6.4 - no soft/tombstone) rows from two tables:

    - `usage_logs`: no raw prompt/response text is ever stored here (see
      `db/models/usage_log.py`'s module docstring - metadata/token-counts/
      cost only), so purging this table means deleting old rows entirely,
      not redacting content within them.
    - `dlp_scan_results`: the more privacy-sensitive target - `findings` is
      metadata-only by default, but `raw_flagged_content` CAN hold actual
      flagged substrings when an org has `dlp_policies.
      store_raw_flagged_content = true` (see `db/models/dlp_scan_result.py`).
      Deliberately keyed by `request_id` (text), not a FK to `usage_logs.id`
      (design doc section 1.9), so this purge is independent of whether a
      matching `usage_logs` row still exists.

    No FK anywhere in this codebase references `usage_logs.id` or
    `dlp_scan_results.id` (both are pure historical/leaf tables - checked
    against every model in `db/models/`), so batched deletes against either
    table cannot violate a foreign key.

    Unlike `audit_retention_days`, `log_prompt_retention_days` is a DB-level
    `NOT NULL` column with a default of 30 (`db/models/compliance_settings.
    py`) - there is no "never purge" configuration for this window, so this
    function has no NULL-skip guard and always fires against a finite
    cutoff. Returns the total number of rows deleted (both tables combined)
    this call.
    """
    compliance = await get_effective_compliance_settings(session)
    cutoff = datetime.now(timezone.utc) - timedelta(days=compliance.log_prompt_retention_days)
    deleted_usage_logs = await _purge_rows_older_than(session, UsageLog, cutoff)
    deleted_dlp_scan_results = await _purge_rows_older_than(session, DlpScanResult, cutoff)
    total_deleted = deleted_usage_logs + deleted_dlp_scan_results
    if total_deleted:
        logger.info(
            "log_prompt_purge_completed",
            extra={
                "deleted_usage_logs": deleted_usage_logs,
                "deleted_dlp_scan_results": deleted_dlp_scan_results,
            },
        )
    return total_deleted


async def run_provider_key_health_check_if_due(session: "AsyncSession", app: "FastAPI") -> int:
    """AC4.1.6 / technical design section 6.2: refresh every backup-group
    provider key's health status, but only once every
    `PROVIDER_KEY_HEALTH_CHECK_INTERVAL_SECONDS` (5 minutes) of wall-clock
    time, even though this is invoked on every `DEFAULT_POLL_INTERVAL_
    SECONDS` (60s) scheduler tick - see module-level constant's docstring
    for why this needs "if due" gating unlike the purge jobs.

    Uses `time.monotonic()` (not wall-clock `datetime.now()`) for the
    due-check itself, same rationale as `services.provider_key_health`'s own
    failure-window tracking: immune to system clock adjustments mid-process.
    Returns the number of keys checked this call (0 if not due yet).
    """
    import time

    from gatekey.services.encryption import EnvKeyProvider

    now = time.monotonic()
    last_run = getattr(app.state, "last_provider_key_health_check_at", None)
    if last_run is not None and (now - last_run) < PROVIDER_KEY_HEALTH_CHECK_INTERVAL_SECONDS:
        return 0

    app.state.last_provider_key_health_check_at = now
    # `EnvKeyProvider.from_settings` mirrors `deliver_service_account_
    # rotation_notification`'s (this same scheduler's other job) pattern for
    # building a `KeyProvider` outside of a FastAPI request context, where
    # `api.deps.get_key_provider`'s `Depends(...)` wiring isn't available.
    keys_checked, error_messages = await provider_key_health.refresh_provider_key_health(
        session,
        app.state.shared_state_store,
        key_provider=EnvKeyProvider.from_settings(app.state.settings),
        timeout_seconds=3.0,
    )
    if error_messages:
        logger.warning(
            "provider_key_health_check_completed_with_errors",
            extra={"keys_checked": keys_checked, "errors": error_messages},
        )
    return keys_checked


async def run_drift_canary_if_due(session: "AsyncSession", app: "FastAPI") -> int:
    """AC5.4.8 / design doc section 2.2: run the daily canary suite
    (`services.drift_detector.run_canary_suite_for_org`), but only once
    every `DRIFT_CANARY_CHECK_INTERVAL_SECONDS` (24h) of wall-clock time,
    even though this is invoked on every `DEFAULT_POLL_INTERVAL_SECONDS`
    (60s) scheduler tick - the EXACT same `app.state` in-memory last-run-
    marker pattern `run_provider_key_health_check_if_due` above already
    establishes, just a longer interval (design doc section 5 wiring
    checklist "5.2 (Drift Detector, 5.4)" row 2).

    Uses `time.monotonic()` for the due-check itself, same rationale as
    `run_provider_key_health_check_if_due`: immune to system clock
    adjustments mid-process. Returns the number of models actually tested
    this call (0 if not due yet).
    """
    import time

    now = time.monotonic()
    last_run = getattr(app.state, "last_drift_canary_check_at", None)
    if last_run is not None and (now - last_run) < DRIFT_CANARY_CHECK_INTERVAL_SECONDS:
        return 0

    app.state.last_drift_canary_check_at = now
    summary = await run_canary_suite_for_org(session, app)
    logger.info(
        "drift_canary_tick_completed",
        extra={
            "models_tested": summary.models_tested,
            "runs_recorded": summary.runs_recorded,
            "baselines_established": summary.baselines_established,
            "alerts_flagged": summary.alerts_flagged,
        },
    )
    return summary.models_tested


async def run_shadow_ai_purge_if_due(session: "AsyncSession") -> int:
    """Design doc section 5's wiring checklist "5.5 (Shadow AI, 5.1)" row 5 /
    AC5.1.10 - the Shadow AI retention purge job, independently scheduled
    from (and sharing no code path with) the audit/log-prompt purge jobs
    above, against its own dedicated config column
    (`shadow_ai_ingest_config.shadow_ai_retention_days`) - a distinct,
    privacy-sensitive data category (network destination metadata about
    employees, not AI-gateway traffic), per that column's own module
    docstring.

    Mirrors `run_log_prompt_purge_if_due`'s shape EXACTLY (not
    `run_drift_canary_if_due`'s/`run_provider_key_health_check_if_due`'s
    interval-gated shape): `shadow_ai_retention_days` is `NOT NULL` at the
    DB level (default 90, same "no configurable NULL/never-purge state" as
    `log_prompt_retention_days`) - this job ALWAYS fires against a finite
    cutoff, on every scheduler tick, with no `app.state` last-run marker.
    The underlying batched DELETE (`_purge_rows_older_than`) is itself a
    naturally cheap no-op once nothing is past the cutoff, exactly like the
    audit/log-prompt purges' own rationale for being safe to re-run every
    tick.

    Purges by `created_at` (ingestion time), NOT `occurred_at` (the reported
    connection event's own timestamp, which is caller-supplied at ingest
    time and not a trustworthy retention anchor) - same cutoff-column
    convention every purge job in this module uses. Falls back to
    `_SHADOW_AI_DEFAULT_RETENTION_DAYS` if no `shadow_ai_ingest_config` row
    exists yet (see that constant's own docstring - a defensive-only path).
    Returns the number of rows deleted this call.
    """
    config = await get_shadow_ai_ingest_config(session)
    retention_days = (
        config.shadow_ai_retention_days if config is not None else _SHADOW_AI_DEFAULT_RETENTION_DAYS
    )
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    total_deleted = await _purge_rows_older_than(session, ShadowAiIngestEvent, cutoff)
    if total_deleted:
        logger.info("shadow_ai_purge_completed", extra={"deleted": total_deleted})
    return total_deleted
