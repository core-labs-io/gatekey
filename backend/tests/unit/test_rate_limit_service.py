"""Unit tests for `services.rate_limit`'s live per-request enforcement path
(Phase 4 gateway-pipeline wiring) - `check_and_consume_rate_limit()`'s
additive team-pool-AND-personal-user-limit semantics (AC4.2.2/AC4.2.9),
independent of any FastAPI/DB plumbing (see `test_gateway_phase4_pipeline.py`
for the route-level integration of this same logic).
"""

from __future__ import annotations

import uuid

import pytest

from gatekey.db.models.rate_limit_rule import RateLimitOnLimit, RateLimitRule, RateLimitScopeType
from gatekey.services.rate_limit import check_and_consume_rate_limit, get_rule_current_status
from gatekey.services.shared_state import InProcessSharedStateStore


def _rule(
    *, requests_per_min: int | None, scope_type: RateLimitScopeType, on_limit=RateLimitOnLimit.REJECT
) -> RateLimitRule:
    return RateLimitRule(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        scope_type=scope_type,
        scope_team_id=None,
        scope_user_id=None,
        requests_per_min=requests_per_min,
        tokens_per_min=None,
        on_limit=on_limit,
        max_queue_wait_seconds=5,
    )


@pytest.mark.asyncio
async def test_unconfigured_is_a_pure_no_op() -> None:
    store = InProcessSharedStateStore()
    decision = await check_and_consume_rate_limit(
        store,
        org_rule=None,
        team_rule=None,
        user_rule=None,
        team_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
    )
    assert decision.configured is False
    assert decision.allowed is True


@pytest.mark.asyncio
async def test_team_rule_falls_back_to_org_rule_when_no_team_rule() -> None:
    store = InProcessSharedStateStore()
    org_rule = _rule(requests_per_min=2, scope_type=RateLimitScopeType.ORG_DEFAULT_PER_USER)
    team_id = uuid.uuid4()
    user_id = uuid.uuid4()

    first = await check_and_consume_rate_limit(
        store, org_rule=org_rule, team_rule=None, user_rule=None, team_id=team_id, user_id=user_id
    )
    second = await check_and_consume_rate_limit(
        store, org_rule=org_rule, team_rule=None, user_rule=None, team_id=team_id, user_id=user_id
    )
    third = await check_and_consume_rate_limit(
        store, org_rule=org_rule, team_rule=None, user_rule=None, team_id=team_id, user_id=user_id
    )
    assert first.allowed is True
    assert second.allowed is True
    assert third.allowed is False  # 3rd request trips the requests_per_min=2 org default


@pytest.mark.asyncio
async def test_team_rule_takes_precedence_over_org_rule() -> None:
    store = InProcessSharedStateStore()
    org_rule = _rule(requests_per_min=100, scope_type=RateLimitScopeType.ORG_DEFAULT_PER_USER)
    team_rule = _rule(requests_per_min=1, scope_type=RateLimitScopeType.TEAM)
    team_id = uuid.uuid4()
    user_id = uuid.uuid4()

    first = await check_and_consume_rate_limit(
        store, org_rule=org_rule, team_rule=team_rule, user_rule=None, team_id=team_id, user_id=user_id
    )
    second = await check_and_consume_rate_limit(
        store, org_rule=org_rule, team_rule=team_rule, user_rule=None, team_id=team_id, user_id=user_id
    )
    assert first.allowed is True
    assert first.limit == 1  # the team rule's limit, not the org default's 100
    assert second.allowed is False


@pytest.mark.asyncio
async def test_additive_user_limit_trips_even_when_team_pool_has_headroom() -> None:
    """AC4.2.2/AC4.2.9: exceeding EITHER the team pool OR the personal
    per-user limit trips the request, even when the other side still has
    headroom."""
    store = InProcessSharedStateStore()
    team_rule = _rule(requests_per_min=100, scope_type=RateLimitScopeType.TEAM)
    user_rule = _rule(requests_per_min=1, scope_type=RateLimitScopeType.USER)
    team_id = uuid.uuid4()
    user_id = uuid.uuid4()

    first = await check_and_consume_rate_limit(
        store,
        org_rule=None,
        team_rule=team_rule,
        user_rule=user_rule,
        team_id=team_id,
        user_id=user_id,
    )
    second = await check_and_consume_rate_limit(
        store,
        org_rule=None,
        team_rule=team_rule,
        user_rule=user_rule,
        team_id=team_id,
        user_id=user_id,
    )
    assert first.allowed is True
    assert second.allowed is False
    assert second.limit == 1  # the personal limit was the one that tripped


@pytest.mark.asyncio
async def test_personal_user_limit_is_independent_of_team() -> None:
    """The personal counter is keyed by user_id alone - two different teams'
    shared pools don't leak into (or get affected by) a caller's own
    personal limit."""
    store = InProcessSharedStateStore()
    user_rule = _rule(requests_per_min=1, scope_type=RateLimitScopeType.USER)
    user_id = uuid.uuid4()

    first = await check_and_consume_rate_limit(
        store,
        org_rule=None,
        team_rule=None,
        user_rule=user_rule,
        team_id=uuid.uuid4(),
        user_id=user_id,
    )
    second = await check_and_consume_rate_limit(
        store,
        org_rule=None,
        team_rule=None,
        user_rule=user_rule,
        team_id=uuid.uuid4(),  # a DIFFERENT team_id - still the same user
        user_id=user_id,
    )
    assert first.allowed is True
    assert second.allowed is False


@pytest.mark.asyncio
async def test_rejected_decision_carries_on_limit_and_queue_wait_from_tripped_rule() -> None:
    store = InProcessSharedStateStore()
    rule = _rule(
        requests_per_min=0,
        scope_type=RateLimitScopeType.ORG_DEFAULT_PER_USER,
        on_limit=RateLimitOnLimit.QUEUE_RETRY,
    )
    decision = await check_and_consume_rate_limit(
        store, org_rule=rule, team_rule=None, user_rule=None, team_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    assert decision.allowed is False
    assert decision.on_limit == RateLimitOnLimit.QUEUE_RETRY
    assert decision.max_queue_wait_seconds == 5
    assert decision.rule is rule


# ============================================================================
# `get_rule_current_status` (Phase 4, AC4.2.8 - admin "current utilization"
# read). Must read the EXACT same counter `check_and_consume_rate_limit`
# above increments - these tests exercise both functions against the same
# store/rule to prove that.
# ============================================================================


@pytest.mark.asyncio
async def test_status_reflects_real_consumed_count_for_user_scoped_rule() -> None:
    store = InProcessSharedStateStore()
    user_id = uuid.uuid4()
    rule = _rule(requests_per_min=10, scope_type=RateLimitScopeType.USER)
    rule.scope_user_id = user_id

    await check_and_consume_rate_limit(
        store, org_rule=None, team_rule=None, user_rule=rule, team_id=uuid.uuid4(), user_id=user_id
    )
    await check_and_consume_rate_limit(
        store, org_rule=None, team_rule=None, user_rule=rule, team_id=uuid.uuid4(), user_id=user_id
    )

    status = await get_rule_current_status(store, rule=rule, user_id=None)
    assert status.available is True
    assert status.requests_limit == 10
    assert status.requests_used_last_60s == 2
    assert status.requests_remaining == 8


@pytest.mark.asyncio
async def test_status_reflects_real_consumed_count_for_team_scoped_rule_given_user_id() -> None:
    store = InProcessSharedStateStore()
    team_id = uuid.uuid4()
    user_id = uuid.uuid4()
    rule = _rule(requests_per_min=5, scope_type=RateLimitScopeType.TEAM)
    rule.scope_team_id = team_id

    await check_and_consume_rate_limit(
        store, org_rule=None, team_rule=rule, user_rule=None, team_id=team_id, user_id=user_id
    )

    status = await get_rule_current_status(store, rule=rule, user_id=user_id)
    assert status.available is True
    assert status.requests_used_last_60s == 1
    assert status.requests_remaining == 4


@pytest.mark.asyncio
async def test_status_available_for_team_rule_without_user_id() -> None:
    """Fix 2: the team pool is now a genuinely team-wide SHARED counter
    (keyed by `team_id` alone), so reading it needs no `user_id` at all -
    unlike the org-default-per-user scope (see the next test), which
    remains genuinely per-user and still requires one."""
    store = InProcessSharedStateStore()
    team_id = uuid.uuid4()
    rule = _rule(requests_per_min=5, scope_type=RateLimitScopeType.TEAM)
    rule.scope_team_id = team_id

    # Two different users contribute to the SAME shared counter.
    await check_and_consume_rate_limit(
        store, org_rule=None, team_rule=rule, user_rule=None, team_id=team_id, user_id=uuid.uuid4()
    )
    await check_and_consume_rate_limit(
        store, org_rule=None, team_rule=rule, user_rule=None, team_id=team_id, user_id=uuid.uuid4()
    )

    status = await get_rule_current_status(store, rule=rule, user_id=None)
    assert status.available is True
    assert status.requests_used_last_60s == 2
    assert status.requests_remaining == 3


@pytest.mark.asyncio
async def test_status_unavailable_for_org_default_per_user_rule_without_user_id() -> None:
    """No fabricated aggregate number - the org-default-per-user scope
    genuinely has no single org-wide counter (it's per-user by name and
    design), so without a `user_id` this must report unavailable, not
    guess."""
    store = InProcessSharedStateStore()
    rule = _rule(requests_per_min=5, scope_type=RateLimitScopeType.ORG_DEFAULT_PER_USER)

    status = await get_rule_current_status(store, rule=rule, user_id=None)
    assert status.available is False
    assert status.reason is not None
    assert status.requests_used_last_60s is None


@pytest.mark.asyncio
async def test_status_queue_depth_always_untracked() -> None:
    """The live `queue_and_retry` path polls in-process and never writes to
    a real Redis-backed queue - `queue_depth` must never be a fabricated
    number."""
    store = InProcessSharedStateStore()
    rule = _rule(requests_per_min=5, scope_type=RateLimitScopeType.ORG_DEFAULT_PER_USER)

    status = await get_rule_current_status(store, rule=rule, user_id=uuid.uuid4())
    assert status.queue_depth is None
    assert status.queue_depth_tracked is False


@pytest.mark.asyncio
async def test_status_available_but_no_limit_when_requests_per_min_unset() -> None:
    store = InProcessSharedStateStore()
    rule = _rule(requests_per_min=None, scope_type=RateLimitScopeType.USER)
    rule.scope_user_id = uuid.uuid4()

    status = await get_rule_current_status(store, rule=rule, user_id=None)
    assert status.available is True
    assert status.requests_limit is None
    assert status.requests_used_last_60s is None
