"""Graceful degradation service for cost-efficient model substitution.

Phase 4 (Reliability & Cost Efficiency, design doc section 1.7).

Graceful degradation allows Gatekey to automatically substitute a more
expensive model with a cheaper fallback when a user or team is approaching
their budget limit. This prevents hard blocks while continuing to serve
requests.

The degradation policy is configured per org/team with:
- enabled: whether degradation is active
- threshold_pct_of_budget: when to trigger (e.g., 90% means trigger when
  10% of budget remains)
- downgrade_target_model: the model to use when degraded

When degradation triggers:
1. Check if current spend is within threshold of budget ceiling
2. If yes, substitute the requested model with the configured fallback
3. Add response headers: X-Gatekey-Degraded, X-Gatekey-Degraded-From, X-Gatekey-Degraded-To
4. Log a degradation event for cost savings calculation

The policy is cumulative: both org-level AND team-level policies are
evaluated, but they don't merge their threshold/model settings. Whichever
layer is enabled supplies its own values wholesale.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.db.models.degradation_event import DegradationEvent
from gatekey.db.models.degradation_policy import DegradationPolicy, DegradationScopeType
# `services.team_budget` owns assignment-time *ceiling* enforcement
# (create/update membership budgets) - it does not read current spend state
# and has no `get_team_membership_budget_state`/`get_budget_state` functions.
# The actual budget-state readers (current spend vs. ceiling) live in
# `services.budget` - the same functions `api.v1.gateway.common.
# check_budget_available()` already reads through for its own exhaustion
# check (schema/code-drift fix: this import previously pointed at the wrong
# module and made this module fail to import at all).
from gatekey.services.budget import get_team_membership_budget_state, get_budget_state

logger = logging.getLogger("gatekey")


# ============================================================================
# Degradation Policy Cache
# ============================================================================


@dataclass(frozen=True)
class DegradationPolicySnapshot:
    """Immutable snapshot of a degradation policy configuration."""

    enabled: bool
    threshold_pct_of_budget: Decimal  # e.g., Decimal('10.0') = trigger when 10% remains
    downgrade_target_model: str


class DegradationPolicyCache:
    """Process-local cache of degradation policies.

    Same lock-free, GIL-atomic "replace the whole snapshot, never mutate in
    place" contract as other caches in this codebase.
    """

    def __init__(
        self,
        org_policy: DegradationPolicySnapshot | None = None,
        team_policies: dict[uuid.UUID, DegradationPolicySnapshot] | None = None,
    ) -> None:
        self._org_policy: DegradationPolicySnapshot | None = org_policy
        self._team_policies: dict[uuid.UUID, DegradationPolicySnapshot] = dict(team_policies or {})

    def get_org_policy(self) -> DegradationPolicySnapshot | None:
        return self._org_policy

    def get_team_policy(self, team_id: uuid.UUID) -> DegradationPolicySnapshot | None:
        return self._team_policies.get(team_id)

    def set_all(
        self,
        org_policy: DegradationPolicySnapshot | None,
        team_policies: dict[uuid.UUID, DegradationPolicySnapshot],
    ) -> None:
        """Full replace - the startup-warm write."""
        self._org_policy = org_policy
        self._team_policies = dict(team_policies)

    def set_org_policy(self, policy: DegradationPolicySnapshot | None) -> None:
        self._org_policy = policy

    def set_team_policy(self, team_id: uuid.UUID, policy: DegradationPolicySnapshot | None) -> None:
        replacement = dict(self._team_policies)
        if policy is None:
            replacement.pop(team_id, None)
        else:
            replacement[team_id] = policy
        self._team_policies = replacement


async def load_degradation_policy_snapshot(
    session: AsyncSession,
) -> tuple[DegradationPolicySnapshot | None, dict[uuid.UUID, DegradationPolicySnapshot]]:
    """Query every degradation policy row - used at process startup only.

    Returns (org_policy, team_policies).
    """
    rows = (await session.execute(select(DegradationPolicy))).scalars().all()

    org_policy: DegradationPolicySnapshot | None = None
    team_policies: dict[uuid.UUID, DegradationPolicySnapshot] = {}

    for row in rows:
        policy_snapshot = DegradationPolicySnapshot(
            enabled=row.enabled,
            threshold_pct_of_budget=row.threshold_pct_of_budget,
            downgrade_target_model=row.downgrade_target_model,
        )
        if row.scope_type == DegradationScopeType.ORG:
            org_policy = policy_snapshot
        elif row.scope_type == DegradationScopeType.TEAM and row.scope_team_id is not None:
            team_policies[row.scope_team_id] = policy_snapshot

    return org_policy, team_policies


def resolve_effective_degradation_policy(
    cache: DegradationPolicyCache, *, team_id: uuid.UUID | None
) -> DegradationPolicySnapshot | None:
    """Cache-backed, zero-I/O replacement for `load_effective_degradation_
    policy()`'s live per-request DB read (Fix 6, NFR gap - AC4.3.4).

    Thin wrapper around `resolve_degradation_policy()` (already pure) fed
    from `DegradationPolicyCache` (now warmed at startup by `main.py`'s
    lifespan, same pattern `ModelPolicyCache`/`ResidencyRuleCache`/
    `RateLimitCache` already use) instead of two live DB point lookups -
    `load_effective_degradation_policy()` below is unchanged and remains
    the read-through source of truth the startup warm and the admin write
    endpoints' cache-refresh both ultimately derive from.
    """
    team_policy = cache.get_team_policy(team_id) if team_id is not None else None
    return resolve_degradation_policy(org_policy=cache.get_org_policy(), team_policy=team_policy)


async def load_effective_degradation_policy(
    session: AsyncSession, *, org_id: uuid.UUID, team_id: uuid.UUID | None
) -> DegradationPolicySnapshot | None:
    """Live, per-request resolution of the effective degradation policy for
    one gateway request.

    Fix 6: no longer called from the gateway hot path (see
    `resolve_effective_degradation_policy()` above, now used instead by
    `api.v1.gateway.common.check_and_apply_degradation()`) - kept as the
    read-through implementation `DegradationPolicyCache`'s startup warm and
    the admin write endpoints' cache-refresh both derive from, and for any
    caller that genuinely needs a live-DB read (none currently). Two point
    lookups (indexed on the same partial-unique indexes the admin write API
    upserts through).
    """
    org_row = (
        await session.execute(
            select(DegradationPolicy).where(
                DegradationPolicy.org_id == org_id,
                DegradationPolicy.scope_type == DegradationScopeType.ORG,
            )
        )
    ).scalar_one_or_none()
    team_row = None
    if team_id is not None:
        team_row = (
            await session.execute(
                select(DegradationPolicy).where(DegradationPolicy.scope_team_id == team_id)
            )
        ).scalar_one_or_none()

    def _snapshot(row: DegradationPolicy | None) -> DegradationPolicySnapshot | None:
        if row is None:
            return None
        return DegradationPolicySnapshot(
            enabled=row.enabled,
            threshold_pct_of_budget=row.threshold_pct_of_budget,
            downgrade_target_model=row.downgrade_target_model,
        )

    return resolve_degradation_policy(
        org_policy=_snapshot(org_row), team_policy=_snapshot(team_row)
    )


# ============================================================================
# Budget Proximity Check
# ============================================================================


def _check_budget_proximity(
    budget_state: tuple[Decimal | None, Decimal],
    threshold_pct: Decimal,
) -> tuple[bool, Decimal]:
    """Check if the user/team is within the degradation threshold of their budget.

    Args:
        budget_state: (budget_usd, current_spend_usd) - the user/team's budget state
        threshold_pct: the threshold percentage (e.g., Decimal('10.0') means
                       trigger when remaining budget < 10% of ceiling)

    Returns:
        (triggered, remaining_pct) where:
        - triggered: whether degradation should occur
        - remaining_pct: percentage of budget remaining (0-100)
    """
    budget_usd, current_spend_usd = budget_state

    if budget_usd is None:
        # Unmetered - never degrade
        return False, Decimal("100")

    if budget_usd <= Decimal("0"):
        # Zero or negative budget - this shouldn't happen but handle it
        return False, Decimal("0")

    remaining = budget_usd - current_spend_usd
    remaining_pct = (remaining / budget_usd) * Decimal("100")

    # Trigger degradation if remaining is less than threshold
    triggered = remaining_pct < threshold_pct

    return triggered, remaining_pct


# ============================================================================
# Degradation Policy Resolution
# ============================================================================


def resolve_degradation_policy(
    *,
    org_policy: DegradationPolicySnapshot | None,
    team_policy: DegradationPolicySnapshot | None,
) -> DegradationPolicySnapshot | None:
    """Resolve the effective degradation policy for a request.

    The policy is cumulative on the `enabled` flag only - a team's policy
    only ever applies if BOTH the team's own `enabled` AND the org's `enabled`
    are true.

    `threshold_pct_of_budget`/`downgrade_target_model` themselves are NOT
    merged - whichever layer is effectively enabled supplies its own values
    wholesale.

    Returns the effective policy, or None if degradation is not enabled.
    """
    org_enabled = org_policy is not None and org_policy.enabled
    team_enabled = team_policy is not None and team_policy.enabled

    if not org_enabled and not team_enabled:
        return None

    # If team policy is enabled and org is also enabled, use team policy
    # (team is more specific, but only applies if org is enabled too)
    if team_enabled and org_enabled:
        return team_policy

    # If only org is enabled, use org policy
    if org_enabled:
        return org_policy

    # Should be unreachable but just in case
    return None


# ============================================================================
# Degradation Decision
# ============================================================================


@dataclass(frozen=True)
class DegradationDecision:
    """Result of a degradation check."""

    triggered: bool
    original_model: str
    degraded_model: str | None  # The substituted model, or None if not triggered
    remaining_pct: Decimal  # Percentage of budget remaining
    original_cost_estimate: Decimal | None  # Estimated cost of original model
    degraded_cost_estimate: Decimal | None  # Estimated cost of degraded model


# ============================================================================
# Main Degradation Check
# ============================================================================


async def check_degradation(
    session: AsyncSession,
    user_id: uuid.UUID,
    team_id: uuid.UUID | None,
    original_model: str,
    *,
    org_policy: DegradationPolicySnapshot | None,
    team_policy: DegradationPolicySnapshot | None,
    cost_per_token: Decimal = Decimal("0.000001"),  # Default $1/M tokens
) -> DegradationDecision:
    """Check if graceful degradation should be triggered for this request.

    This is called after policy resolution (to know the original model) but
    before provider routing (to substitute the model if needed).

    Args:
        session: database session
        user_id: the authenticated user
        team_id: the team context (None for legacy flat user path)
        original_model: the model requested by the user
        org_policy: the org-level degradation policy
        team_policy: the team-level degradation policy
        cost_per_token: estimated cost per token for cost estimation

    Returns:
        DegradationDecision with whether to trigger, and what model to use
    """
    # Resolve effective policy
    effective_policy = resolve_degradation_policy(
        org_policy=org_policy,
        team_policy=team_policy,
    )

    if effective_policy is None or not effective_policy.enabled:
        return DegradationDecision(
            triggered=False,
            original_model=original_model,
            degraded_model=None,
            remaining_pct=Decimal("100"),
            original_cost_estimate=None,
            degraded_cost_estimate=None,
        )

    # Get budget state
    budget_state: tuple[Decimal | None, Decimal] | None

    if team_id is not None:
        membership_state = await get_team_membership_budget_state(session, team_id=team_id, user_id=user_id)
        if membership_state is None:
            # No team membership - use flat user budget
            user_state = await get_budget_state(session, user_id=user_id)
            budget_state = (
                (user_state.budget_usd, user_state.current_spend_usd)
                if user_state
                else (None, Decimal("0"))
            )
        else:
            budget_state = (membership_state.budget_usd, membership_state.current_spend_usd)
    else:
        user_state = await get_budget_state(session, user_id=user_id)
        budget_state = (
            (user_state.budget_usd, user_state.current_spend_usd)
            if user_state
            else (None, Decimal("0"))
        )

    # Check budget proximity
    triggered, remaining_pct = _check_budget_proximity(budget_state, effective_policy.threshold_pct_of_budget)

    if not triggered:
        return DegradationDecision(
            triggered=False,
            original_model=original_model,
            degraded_model=None,
            remaining_pct=remaining_pct,
            original_cost_estimate=None,
            degraded_cost_estimate=None,
        )

    # Degradation triggered - substitute model
    degraded_model = effective_policy.downgrade_target_model

    # Estimate costs (simplified - in production would use actual pricing)
    original_cost_estimate = cost_per_token * Decimal("1000")  # $1 per 1M tokens estimate
    degraded_cost_estimate = cost_per_token * Decimal("500")  # Assume cheaper model is half price

    return DegradationDecision(
        triggered=True,
        original_model=original_model,
        degraded_model=degraded_model,
        remaining_pct=remaining_pct,
        original_cost_estimate=original_cost_estimate,
        degraded_cost_estimate=degraded_cost_estimate,
    )


# ============================================================================
# Degradation Event Logging
# ============================================================================


class DegradationEventLogger:
    """Logger for degradation events for cost savings calculation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def log_degradation(
        self,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        request_id: uuid.UUID | None,
        original_model: str,
        degraded_model: str,
        original_cost: Decimal,
        degraded_cost: Decimal,
    ) -> uuid.UUID:
        """Log a degradation event for cost savings tracking.

        `request_id` (`degradation_events.request_id`, FK to `usage_logs.
        id`) is genuinely nullable at the schema level (`ON DELETE SET
        NULL` - see `db/models/degradation_event.py`) - `None` here is not
        an error case, just "the originating usage-log row's id wasn't
        available to link" (e.g. `services.usage_logs.record_usage_log()`
        itself failed and returned `None` - a best-effort write, not a hard
        dependency; `api.v1.gateway.common.log_degradation_event()` is the
        one caller of this that can pass `None` this way).

        Returns the ID of the created degradation event.
        """
        event = DegradationEvent(
            team_id=team_id,
            user_id=user_id,
            request_id=request_id,
            original_model=original_model,
            degraded_model=degraded_model,
            original_cost=original_cost,
            degraded_cost=degraded_cost,
        )
        self._session.add(event)
        await self._session.flush()
        event_id = event.id
        await self._session.commit()
        return event_id

    async def get_cost_savings(
        self,
        team_id: uuid.UUID,
        start_date: datetime,
        end_date: datetime,
    ) -> Decimal:
        """Calculate cost savings from degradation for a period.

        Returns the total savings (original_cost - degraded_cost).
        """
        stmt = (
            select(
                DegradationEvent.original_cost - DegradationEvent.degraded_cost
            )
            .where(
                DegradationEvent.team_id == team_id,
                DegradationEvent.created_at >= start_date,
                DegradationEvent.created_at <= end_date,
            )
        )

        result = await self._session.execute(stmt)
        savings = result.scalars().all()

        total = sum(savings, Decimal("0"))
        return total


# ============================================================================
# Degradation Headers
# ============================================================================


def get_degradation_headers(
    original_model: str,
    degraded_model: str,
) -> dict[str, str]:
    """Build response headers for a degraded request.

    Returns headers dict with:
    - X-Gatekey-Degraded: "true"
    - X-Gatekey-Degraded-From: original_model
    - X-Gatekey-Degraded-To: degraded_model
    """
    return {
        "X-Gatekey-Degraded": "true",
        "X-Gatekey-Degraded-From": original_model,
        "X-Gatekey-Degraded-To": degraded_model,
    }


def is_degraded_response(headers: dict[str, str]) -> bool:
    """Check if a response was served with degradation."""
    return headers.get("X-Gatekey-Degraded", "").lower() == "true"
