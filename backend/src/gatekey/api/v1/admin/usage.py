"""Admin usage-dashboard endpoint (Phase 1.5 / 1.6; extended Phase 4 -
Reliability & Cost Efficiency, AC4.5.1-AC4.5.7).

`GET /v1/admin/usage/summary` - totals by user, by model, over a selectable
time range, per `gatekey/phase-1-core-gateway.md` 1.5. Response shape matches
`gatekey/phase-1-admin-console-ui-requirements.md` section 11's documented
mock shape exactly, so the frontend dashboard can wire directly to this
endpoint with no field renaming. Phase 4 adds `provider` as a third filter
dimension (alongside the existing `team_id`) and three new metric fields
(`cache_hit_rate`, `failover_events_count`, `cost_saved_*_usd`) - additive
only, no existing field renamed/removed (AC4.5.1/AC4.5.2).

`GET /v1/admin/usage/export` - CSV/JSON export of the same aggregate over
the same filterable dimensions (AC4.5.3), plus a `report=cost_efficiency`
one-click shortcut (AC4.5.6) - see that endpoint's docstring for the exact
contract.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import require_admin_or_auditor
from gatekey.db.session import get_db_session
from gatekey.errors import UnsupportedRequestError
from gatekey.services.usage_logs import get_phase4_dashboard_metrics, get_usage_summary

# Phase 2 (section 5.8): auth moved from router-level `require_admin` to the
# endpoint's own `require_admin_or_auditor` - identical acceptance for the
# break-glass token and org_admin sessions, plus read access for auditor
# sessions (this router has exactly one, read-only endpoint).
router = APIRouter(prefix="/v1/admin/usage", tags=["admin", "usage"])

TimeRange = Literal["24h", "7d", "30d", "90d", "custom"]

_RANGE_DELTAS = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
}


def _resolve_window(
    range_: TimeRange, start: datetime | None, end: datetime | None
) -> tuple[datetime, datetime]:
    """Shared range-resolution for both `/summary` and `/export` - AC4.5.2's
    `24h/7d/30d/90d/custom` time-range selector."""
    now = datetime.now(timezone.utc)
    if range_ == "custom":
        if start is None or end is None:
            raise UnsupportedRequestError(
                "range=custom requires both 'start' and 'end' query parameters."
            )
        return start, end
    return now - _RANGE_DELTAS[range_], now


class SpendByDayResponse(BaseModel):
    date: str
    spend_usd: Decimal


class SpendByModelResponse(BaseModel):
    model: str
    spend_usd: Decimal


class SpendByUserResponse(BaseModel):
    user: str
    requests: int
    spend_usd: Decimal
    budget_usd: Decimal | None


class UsageSummaryResponse(BaseModel):
    total_spend_usd: Decimal
    request_count: int
    avg_latency_ms: float
    error_rate: float
    spend_by_day: list[SpendByDayResponse]
    spend_by_model: list[SpendByModelResponse]
    spend_by_user: list[SpendByUserResponse]
    # Phase 4 (AC4.5.1) - additive fields, existing fields unchanged.
    cache_hit_rate: float
    cache_hits: int
    cache_misses: int
    failover_events_count: int
    degraded_requests_count: int
    cost_saved_caching_usd: Decimal
    cost_saved_degradation_usd: Decimal
    cost_saved_total_usd: Decimal


async def _build_summary_response(
    session: AsyncSession, *, since: datetime, until: datetime, team_id: uuid.UUID | None, provider: str | None
) -> UsageSummaryResponse:
    summary = await get_usage_summary(session, since=since, until=until, team_id=team_id, provider=provider)
    phase4 = await get_phase4_dashboard_metrics(
        session, since=since, until=until, team_id=team_id, provider=provider
    )
    return UsageSummaryResponse(
        total_spend_usd=summary.total_spend_usd,
        request_count=summary.request_count,
        avg_latency_ms=summary.avg_latency_ms,
        error_rate=summary.error_rate,
        spend_by_day=[SpendByDayResponse(date=d.date, spend_usd=d.spend_usd) for d in summary.spend_by_day],
        spend_by_model=[
            SpendByModelResponse(model=m.model, spend_usd=m.spend_usd) for m in summary.spend_by_model
        ],
        spend_by_user=[
            SpendByUserResponse(
                user=u.user, requests=u.requests, spend_usd=u.spend_usd, budget_usd=u.budget_usd
            )
            for u in summary.spend_by_user
        ],
        cache_hit_rate=phase4.cache_hit_rate,
        cache_hits=phase4.cache_hits,
        cache_misses=phase4.cache_misses,
        failover_events_count=phase4.failover_events_count,
        degraded_requests_count=phase4.degraded_requests_count,
        cost_saved_caching_usd=phase4.cost_saved_caching_usd,
        cost_saved_degradation_usd=phase4.cost_saved_degradation_usd,
        cost_saved_total_usd=phase4.cost_saved_total_usd,
    )


@router.get(
    "/summary",
    response_model=UsageSummaryResponse,
    dependencies=[Depends(require_admin_or_auditor)],
)
async def get_usage_summary_endpoint(
    range: TimeRange = Query(default="7d"),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    team_id: uuid.UUID | None = Query(default=None),
    provider: str | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> UsageSummaryResponse:
    """`range` selects a rolling window ending now (`24h`/`7d`/`30d`/`90d`,
    AC4.5.2 added `90d`), or `range=custom` with explicit `start`/`end`
    (ISO 8601) for an arbitrary window. Phase 2 (section 5.8): optional
    `team_id` narrows every aggregate to one team's usage-log rows. Phase 4
    (AC4.5.2): optional `provider` narrows every aggregate (existing and
    new) the same way. Response shape is additive-only vs. the Phase 1/2
    contract (AC4.5.1) - no existing field renamed/removed."""
    since, until = _resolve_window(range, start, end)
    return await _build_summary_response(session, since=since, until=until, team_id=team_id, provider=provider)


# ============================================================================
# Phase 4 (AC4.5.3/AC4.5.6): CSV/JSON export.
# ============================================================================

_CSV_HEADER = (
    "time_range_start",
    "time_range_end",
    "team_id",
    "provider",
    "total_spend_usd",
    "request_count",
    "avg_latency_ms",
    "error_rate",
    "cache_hit_rate",
    "cache_hits",
    "cache_misses",
    "failover_events_count",
    "degraded_requests_count",
    "cost_saved_caching_usd",
    "cost_saved_degradation_usd",
    "cost_saved_total_usd",
)


def _export_row(
    summary: UsageSummaryResponse,
    *,
    since: datetime,
    until: datetime,
    team_id: uuid.UUID | None,
    provider: str | None,
) -> dict[str, object]:
    return {
        "time_range_start": since.isoformat(),
        "time_range_end": until.isoformat(),
        "team_id": str(team_id) if team_id is not None else "all",
        "provider": provider or "all",
        "total_spend_usd": str(summary.total_spend_usd),
        "request_count": summary.request_count,
        "avg_latency_ms": summary.avg_latency_ms,
        "error_rate": summary.error_rate,
        "cache_hit_rate": summary.cache_hit_rate,
        "cache_hits": summary.cache_hits,
        "cache_misses": summary.cache_misses,
        "failover_events_count": summary.failover_events_count,
        "degraded_requests_count": summary.degraded_requests_count,
        "cost_saved_caching_usd": str(summary.cost_saved_caching_usd),
        "cost_saved_degradation_usd": str(summary.cost_saved_degradation_usd),
        "cost_saved_total_usd": str(summary.cost_saved_total_usd),
    }


@router.get(
    "/export",
    dependencies=[Depends(require_admin_or_auditor)],
)
async def export_usage_summary_endpoint(
    format: Literal["csv", "json"] = Query(default="csv"),
    range: TimeRange = Query(default="7d"),
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    team_id: uuid.UUID | None = Query(default=None),
    provider: str | None = Query(default=None),
    report: Literal["cost_efficiency"] | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """AC4.5.3: CSV/JSON export of the same filterable dashboard aggregate
    `/summary` returns (all metrics: requests, cost, cache hits, failovers,
    downgrades, cost saved), one row = the totals for the entire filtered
    window (not a per-request-log export - this is a dashboard/ROI-report
    export, not a raw audit-log dump; `GET /v1/admin/audit-entries`/
    `GET /v1/admin/usage/summary`'s `spend_by_day` cover the per-day/
    per-event granularity separately).

    `?report=cost_efficiency` (AC4.5.6): the one-click "Cost Efficiency
    Report" shortcut - forces `team_id=None` (org-wide) and `range=30d`
    regardless of any `team_id`/`range`/`start`/`end` also passed, so a
    single URL always produces the same pre-filtered, finance-ready export.
    `provider` is still honored under `report=cost_efficiency` (an org may
    want a per-provider cost-efficiency report) - only the team/range
    dimensions are pinned.

    CSV includes a descriptive header row (AC4.5.3); JSON is a single-
    object body (not a list) since this is one aggregated row.
    """
    if report == "cost_efficiency":
        team_id = None
        range = "30d"
        start = None
        end = None

    since, until = _resolve_window(range, start, end)
    summary = await _build_summary_response(session, since=since, until=until, team_id=team_id, provider=provider)
    row = _export_row(summary, since=since, until=until, team_id=team_id, provider=provider)

    filename_stub = "cost-efficiency-report" if report == "cost_efficiency" else "usage-export"

    if format == "json":
        body = json.dumps(row)
        return StreamingResponse(
            iter([body]),
            media_type="application/json",
            headers={"Content-Disposition": f"attachment; filename={filename_stub}.json"},
        )

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(_CSV_HEADER)
    writer.writerow([row[col] for col in _CSV_HEADER])
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename_stub}.csv"},
    )
