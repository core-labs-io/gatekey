"""Persisted per-request usage log + usage-summary aggregation (Phase 1.5).

`record_usage_log()` is called from every gateway route handler's success
and failure paths (see `api/v1/gateway/common.py` /
`api/v1/gateway/{chat,completions,embeddings}.py`) - it is a best-effort,
never-raises write: a logging failure must never turn a successful (or
already-failed) gateway response into a 500, so any exception here is
caught and logged, not propagated. This mirrors the existing "charge-write
failure after bytes already on the wire" acceptance in Phase 1.4's budget
design - persistence is best-effort here for the same class of reason.

`get_usage_summary()` powers the admin usage dashboard (`api/v1/admin/usage.py`)
- aggregation shape matches `gatekey/phase-1-admin-console-ui-requirements.md`
section 11's documented mock shape exactly, so the frontend's dashboard
screen can wire directly to this response with no field renaming.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.cache_lookup_event import CacheLookupEvent
from gatekey.db.models.degradation_event import DegradationEvent
from gatekey.db.models.usage_log import UsageLog
from gatekey.db.models.user import User

logger = logging.getLogger("gatekey")


async def record_usage_log(
    session: AsyncSession,
    *,
    request_id: str,
    endpoint: str,
    provider: str | None,
    model: str | None,
    user_id: uuid.UUID | None,
    service_account_key_id: uuid.UUID | None,
    team_id: uuid.UUID | None = None,
    personal_api_key_id: uuid.UUID | None = None,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cost_usd: Decimal | None,
    raw_provider_cost_usd: Decimal | None = None,
    latency_ms: int | None,
    stream: bool,
    status: str,
    success: bool,
    cache_hit: bool = False,
    failover_attempt: int = 0,
    failover_key_id: uuid.UUID | None = None,
    original_model: str | None = None,
    degraded_from_model: str | None = None,
    degraded_to_model: str | None = None,
    self_hosted_provider_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """Best-effort insert of one `UsageLog` row. Never raises - see module
    docstring. Uses its own commit so a failure here can never roll back
    anything the caller already committed (e.g. `record_usage_charge`'s
    write, which always happens first at every call site).

    Phase 2 (BD-11): `team_id`/`personal_api_key_id` attribute the row to
    the resolved team context and the exact credential kind (exactly one of
    `service_account_key_id`/`personal_api_key_id` is set per row).
    `raw_provider_cost_usd` is the pre-normalization provider cost;
    `fx_rate_applied` is always 1 this phase (ADR-9's identity
    normalization - `raw_provider_cost_usd == cost_usd` by construction).

    Phase 4 (Reliability & Cost Efficiency): `cache_hit`/`failover_attempt`/
    `failover_key_id`/`original_model`/`degraded_from_model`/
    `degraded_to_model` are the new columns migration `0031`/`0029` added -
    every parameter defaults to its byte-for-byte pre-Phase-4 value (no
    cache hit, no failover, not degraded) so every pre-existing call site
    that doesn't pass them is unaffected. `original_model`/
    `degraded_from_model` are populated with the SAME value on a degraded
    request (two columns, two independent dashboard-query docstrings - see
    `db/models/usage_log.py` - kept in lockstep here rather than choosing
    one).

    Phase 5 (5.5): `self_hosted_provider_id` (migration `0040`) - `None` for
    every non-self-hosted request (the overwhelming majority, byte-for-byte
    pre-Phase-5 behavior); `api.v1.gateway.chat`'s call sites pass
    `effective_route.self_hosted_provider_id` when `effective_route.provider
    == "self_hosted"`. `provider` (the existing plain-string column) already
    carries the literal value `"self_hosted"` for these rows.

    Returns the created row's `id` (the FK target for `degradation_events.
    request_id`) on success, `None` on failure (mirrors the "never raise"
    contract - a caller that needs the id, e.g. `api.v1.gateway.common.
    log_degradation_event()`, treats `None` as "could not link, log the
    degradation event without a usage_log reference").
    """
    try:
        row = UsageLog(
            org_id=DEFAULT_ORG_ID,
            user_id=user_id,
            service_account_key_id=service_account_key_id,
            team_id=team_id,
            personal_api_key_id=personal_api_key_id,
            request_id=request_id,
            endpoint=endpoint,
            provider=provider,
            model=model,
            original_model=original_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            raw_provider_cost_usd=raw_provider_cost_usd,
            fx_rate_applied=Decimal(1),
            latency_ms=latency_ms,
            stream=stream,
            status=status,
            success=success,
            cache_hit=cache_hit,
            failover_attempt=failover_attempt,
            failover_key_id=failover_key_id,
            degraded_from_model=degraded_from_model,
            degraded_to_model=degraded_to_model,
            self_hosted_provider_id=self_hosted_provider_id,
        )
        session.add(row)
        await session.flush()
        row_id = row.id
        await session.commit()
        return row_id
    except Exception:
        logger.error("usage_log_persist_failed", exc_info=True, extra={"request_id": request_id})
        try:
            await session.rollback()
        except Exception:
            pass
        return None


@dataclass(frozen=True)
class SpendByDay:
    date: str
    spend_usd: Decimal


@dataclass(frozen=True)
class SpendByModel:
    model: str
    spend_usd: Decimal


@dataclass(frozen=True)
class SpendByUser:
    user: str
    requests: int
    spend_usd: Decimal
    budget_usd: Decimal | None


@dataclass(frozen=True)
class UsageSummary:
    total_spend_usd: Decimal
    request_count: int
    avg_latency_ms: float
    error_rate: float
    spend_by_day: list[SpendByDay] = field(default_factory=list)
    spend_by_model: list[SpendByModel] = field(default_factory=list)
    spend_by_user: list[SpendByUser] = field(default_factory=list)


async def get_usage_summary(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime,
    user_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    provider: str | None = None,
) -> UsageSummary:
    """Aggregate `usage_logs` (+ current `users` budget state) for the
    default org over `[since, until)`.

    Every aggregate below is computed with a single `GROUP BY` query against
    the indexed `(org_id, created_at)` columns - no N+1 per-row Python
    aggregation.

    Phase 2 (section 5.8): optional `user_id` (the `/v1/me/usage` self-view
    - the caller's own rows across every credential they hold) and
    `team_id` (the admin summary's team filter) narrow every aggregate
    identically - one implementation, not per-endpoint forks.

    Phase 4 (AC4.5.2): optional `provider` narrows every aggregate the same
    way - "All providers" (the existing, unfiltered behavior) when omitted.
    """
    base_filter = [
        UsageLog.org_id == DEFAULT_ORG_ID,
        UsageLog.created_at >= since,
        UsageLog.created_at < until,
    ]
    if user_id is not None:
        base_filter.append(UsageLog.user_id == user_id)
    if team_id is not None:
        base_filter.append(UsageLog.team_id == team_id)
    if provider is not None:
        base_filter.append(UsageLog.provider == provider)

    totals_stmt = select(
        func.coalesce(func.sum(UsageLog.cost_usd), 0),
        func.count(UsageLog.id),
        func.coalesce(func.avg(UsageLog.latency_ms), 0),
        func.coalesce(func.sum(case((UsageLog.success.is_(False), 1), else_=0)), 0),
    ).where(*base_filter)
    totals_row = (await session.execute(totals_stmt)).one()
    total_spend_usd = Decimal(totals_row[0] or 0)
    request_count = int(totals_row[1] or 0)
    avg_latency_ms = float(totals_row[2] or 0)
    error_count = int(totals_row[3] or 0)
    error_rate = (error_count / request_count) if request_count else 0.0

    by_day_stmt = (
        select(
            func.to_char(UsageLog.created_at, "YYYY-MM-DD").label("day"),
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
        )
        .where(*base_filter)
        .group_by("day")
        .order_by("day")
    )
    spend_by_day = [
        SpendByDay(date=row.day, spend_usd=Decimal(row[1] or 0))
        for row in (await session.execute(by_day_stmt)).all()
    ]

    by_model_stmt = (
        select(UsageLog.model, func.coalesce(func.sum(UsageLog.cost_usd), 0))
        .where(*base_filter, UsageLog.model.is_not(None))
        .group_by(UsageLog.model)
        .order_by(func.coalesce(func.sum(UsageLog.cost_usd), 0).desc())
    )
    spend_by_model = [
        SpendByModel(model=row[0], spend_usd=Decimal(row[1] or 0))
        for row in (await session.execute(by_model_stmt)).all()
    ]

    by_user_stmt = (
        select(
            User.id,
            User.name,
            User.budget_usd,
            func.count(UsageLog.id),
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
        )
        .select_from(User)
        .join(UsageLog, UsageLog.user_id == User.id)
        .where(*base_filter)
        .group_by(User.id, User.name, User.budget_usd)
        .order_by(func.coalesce(func.sum(UsageLog.cost_usd), 0).desc())
    )
    spend_by_user = [
        SpendByUser(
            user=row[1],
            requests=int(row[3] or 0),
            spend_usd=Decimal(row[4] or 0),
            budget_usd=row[2],
        )
        for row in (await session.execute(by_user_stmt)).all()
    ]

    return UsageSummary(
        total_spend_usd=total_spend_usd,
        request_count=request_count,
        avg_latency_ms=avg_latency_ms,
        error_rate=error_rate,
        spend_by_day=spend_by_day,
        spend_by_model=spend_by_model,
        spend_by_user=spend_by_user,
    )


# ============================================================================
# Phase 4 dashboard extension (AC4.5.1-AC4.5.7) - cache hit rate, failover
# event count, cost saved via caching + graceful degradation.
# ============================================================================


@dataclass(frozen=True)
class Phase4DashboardMetrics:
    cache_hits: int
    cache_misses: int
    cache_hit_rate: float  # 0.0-1.0; 0.0 if hits+misses == 0
    failover_events_count: int
    degraded_requests_count: int
    cost_saved_caching_usd: Decimal
    cost_saved_degradation_usd: Decimal
    cost_saved_total_usd: Decimal


async def get_phase4_dashboard_metrics(
    session: AsyncSession,
    *,
    since: datetime,
    until: datetime,
    team_id: uuid.UUID | None = None,
    provider: str | None = None,
) -> Phase4DashboardMetrics:
    """AC4.5.1/AC4.5.5: the three new dashboard metric cards, computed over
    `[since, until)`, filterable identically to `get_usage_summary` (team/
    provider).

    - **Cache hit rate**: from `cache_lookup_events` (Postgres audit log of
      every cache lookup, hit or miss - see that model's docstring), NOT
      from the Redis cache data itself (which holds only live, unexpired
      entries and cannot answer a historical hit-rate-over-time question).
    - **Failover events count**: AC4.5.7 - one row per REQUEST that used a
      backup key (`usage_logs.failover_attempt > 0`), not one per retry
      attempt - `call_provider_with_failover` retries exactly once per
      request (see that function's docstring) and `record_usage_log()` is
      called once per request, so this is naturally already "per request,
      not per retry" without extra de-duplication here. Computed from
      `usage_logs` (not the `failover_events` audit table) specifically
      because `usage_logs` has `team_id`/`provider` columns to filter by -
      `failover_events` has neither (org-wide only, see that table's
      model docstring); `GET /v1/admin/failover-events` is the org-wide,
      unfiltered-by-team detail view for that table instead.
    - **Cost saved via caching** (AC4.5.5): `cache_hits * average_request_
      cost`, where `average_request_cost` is the average `cost_usd` of
      non-cache-hit `usage_logs` rows for the same team/provider filter
      over the same window (the phase spec's exact formula - deliberately
      the SAME window as the query, not a separate trailing-30-days lookup,
      since this function is already called with whatever window the
      caller/dashboard selected).
    - **Cost saved via graceful degradation** (AC4.5.5): `sum(original_cost
      - degraded_cost)` from `degradation_events`, filtered by `team_id` and
      the date range. `degradation_events` has no `provider` column (see
      that model's docstring) - a `provider` filter therefore does not
      narrow this half of the total; flagged, not silently guessed.
    """
    cache_filter = [
        CacheLookupEvent.org_id == DEFAULT_ORG_ID,
        CacheLookupEvent.occurred_at >= since,
        CacheLookupEvent.occurred_at < until,
    ]
    if team_id is not None:
        cache_filter.append(CacheLookupEvent.team_id == team_id)
    if provider is not None:
        cache_filter.append(CacheLookupEvent.provider == provider)

    cache_stmt = select(
        func.coalesce(func.sum(case((CacheLookupEvent.hit.is_(True), 1), else_=0)), 0),
        func.coalesce(func.sum(case((CacheLookupEvent.hit.is_(False), 1), else_=0)), 0),
    ).where(*cache_filter)
    cache_row = (await session.execute(cache_stmt)).one()
    cache_hits = int(cache_row[0] or 0)
    cache_misses = int(cache_row[1] or 0)
    cache_hit_rate = (cache_hits / (cache_hits + cache_misses)) if (cache_hits + cache_misses) else 0.0

    usage_filter = [
        UsageLog.org_id == DEFAULT_ORG_ID,
        UsageLog.created_at >= since,
        UsageLog.created_at < until,
    ]
    if team_id is not None:
        usage_filter.append(UsageLog.team_id == team_id)
    if provider is not None:
        usage_filter.append(UsageLog.provider == provider)

    failover_stmt = select(func.count(UsageLog.id)).where(
        *usage_filter, UsageLog.failover_attempt > 0
    )
    failover_events_count = int((await session.execute(failover_stmt)).scalar_one() or 0)

    degraded_count_stmt = select(func.count(UsageLog.id)).where(
        *usage_filter, UsageLog.degraded_to_model.is_not(None)
    )
    degraded_requests_count = int((await session.execute(degraded_count_stmt)).scalar_one() or 0)

    avg_cost_stmt = select(func.avg(UsageLog.cost_usd)).where(
        *usage_filter, UsageLog.cache_hit.is_(False), UsageLog.cost_usd.is_not(None)
    )
    avg_non_cache_hit_cost = (await session.execute(avg_cost_stmt)).scalar_one()
    cost_saved_caching_usd = (
        Decimal(avg_non_cache_hit_cost) * cache_hits if avg_non_cache_hit_cost is not None else Decimal(0)
    )

    degradation_filter = [
        DegradationEvent.created_at >= since,
        DegradationEvent.created_at < until,
    ]
    if team_id is not None:
        degradation_filter.append(DegradationEvent.team_id == team_id)
    degradation_stmt = select(
        func.coalesce(func.sum(DegradationEvent.original_cost - DegradationEvent.degraded_cost), 0)
    ).where(*degradation_filter)
    cost_saved_degradation_usd = Decimal(
        (await session.execute(degradation_stmt)).scalar_one() or 0
    )

    return Phase4DashboardMetrics(
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        cache_hit_rate=cache_hit_rate,
        failover_events_count=failover_events_count,
        degraded_requests_count=degraded_requests_count,
        cost_saved_caching_usd=cost_saved_caching_usd,
        cost_saved_degradation_usd=cost_saved_degradation_usd,
        cost_saved_total_usd=cost_saved_caching_usd + cost_saved_degradation_usd,
    )

