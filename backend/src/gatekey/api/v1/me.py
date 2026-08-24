"""Self-view usage endpoint (Phase 2, section 5.8).

`GET /v1/me/usage?range=` - session auth; the caller's own usage across
every credential they hold (personal keys and user-attributed
service-account keys alike - `usage_logs.user_id` is the common
attribution). Reuses `services.usage_logs.get_usage_summary` with a
`user_id` filter and the admin dashboard's response models, so the
breakdown shape stays consistent with the existing usage summaries by
construction. Rolling-window ranges only (24h/7d/30d) - `custom` is an
admin-dashboard affordance, not part of this contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.v1.admin.usage import (
    _RANGE_DELTAS,
    SpendByDayResponse,
    SpendByModelResponse,
    SpendByUserResponse,
    UsageSummaryResponse,
)
from gatekey.db.session import get_db_session
from gatekey.services.sessions import SessionContext, get_current_session
from gatekey.services.usage_logs import get_phase4_dashboard_metrics, get_usage_summary

router = APIRouter(prefix="/v1/me", tags=["me"])


@router.get("/usage", response_model=UsageSummaryResponse)
async def get_my_usage_endpoint(
    range: Literal["24h", "7d", "30d"] = Query(default="7d"),
    ctx: SessionContext = Depends(get_current_session),
    session: AsyncSession = Depends(get_db_session),
) -> UsageSummaryResponse:
    now = datetime.now(timezone.utc)
    user_id = ctx.require_user_id()
    since = now - _RANGE_DELTAS[range]
    summary = await get_usage_summary(session, since=since, until=now, user_id=user_id)
    # Post-ship fix: this call, and the 8 phase4=* fields below, were
    # missing entirely - UsageSummaryResponse has had no default for its
    # Phase 4 fields since they were added, so every call to this endpoint
    # was raising a Pydantic ValidationError (an unhandled 500) before this
    # fix. See services/usage_logs.py's get_phase4_dashboard_metrics
    # docstring for why cache_hit_rate/cache_hits/cache_misses/
    # cost_saved_caching_usd stay team/org-scoped rather than per-user even
    # though this call passes user_id (cache_lookup_events has no user_id
    # column) - a real, flagged limitation, not an oversight.
    phase4 = await get_phase4_dashboard_metrics(session, since=since, until=now, user_id=user_id)
    return UsageSummaryResponse(
        total_spend_usd=summary.total_spend_usd,
        request_count=summary.request_count,
        avg_latency_ms=summary.avg_latency_ms,
        error_rate=summary.error_rate,
        spend_by_day=[
            SpendByDayResponse(date=d.date, spend_usd=d.spend_usd) for d in summary.spend_by_day
        ],
        spend_by_model=[
            SpendByModelResponse(model=m.model, spend_usd=m.spend_usd)
            for m in summary.spend_by_model
        ],
        # With the user_id filter this contains at most the caller
        # themselves - kept for shape consistency with the admin summary.
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
