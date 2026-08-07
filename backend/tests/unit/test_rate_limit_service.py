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
from gatekey.services.rate_limit import (
    RateLimitCache,
    check_and_consume_rate_limit,
    get_rule_current_status,
    record_token_usage,
    snapshot_from_rule,
)
from gatekey.services.shared_state import InProcessSharedStateStore


def _rule(
    *,
    requests_per_min: int | None,
    scope_type: RateLimitScopeType,
    on_limit=RateLimitOnLimit.REJECT,
    tokens_per_min: int | None = None,
) -> RateLimitRule:
    return RateLimitRule(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        scope_type=scope_type,
        scope_team_id=None,
        scope_user_id=None,
        requests_per_min=requests_per_min,
        tokens_per_min=tokens_per_min,
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
# Hardening pass item 4: `tokens_per_min` enforcement (AC4.2.1/AC4.2.4) -
# previously validated/stored but never checked on the live path. AC2.4's
# "never estimate/pre-charge" contract: `check_and_consume_rate_limit()`
# ONLY ever READS the tokens counter (never increments it); `record_token_
# usage()` is the ONLY place it is ever incremented, and only after a real
# provider response.
# ============================================================================


@pytest.mark.asyncio
async def test_tokens_per_min_never_trips_on_its_own_first_request_no_matter_how_large() -> None:
    """The literal AC2.4 guarantee: a tokens/min gate can only ever block
    based on ALREADY-consumed tokens from PRIOR requests - it never
    estimates or pre-charges the current request's own (not-yet-known)
    usage, so even a request whose eventual usage will vastly exceed the
    limit is never blocked by that fact alone."""
    store = InProcessSharedStateStore()
    rule = _rule(
        requests_per_min=None, tokens_per_min=5, scope_type=RateLimitScopeType.ORG_DEFAULT_PER_USER
    )
    decision = await check_and_consume_rate_limit(
        store, org_rule=rule, team_rule=None, user_rule=None, team_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    assert decision.allowed is True
    assert decision.limit == 5
    assert decision.remaining == 5  # nothing consumed yet - a pure, unmodified read


@pytest.mark.asyncio
async def test_tokens_per_min_check_never_increments_the_counter() -> None:
    """`check_and_consume_rate_limit()` must be a pure READ on the tokens
    axis - calling it repeatedly must never itself move the counter (unlike
    the requests axis, where `try_consume` increments on every passing
    call)."""
    store = InProcessSharedStateStore()
    rule = _rule(
        requests_per_min=None, tokens_per_min=100, scope_type=RateLimitScopeType.ORG_DEFAULT_PER_USER
    )
    team_id = uuid.uuid4()
    user_id = uuid.uuid4()
    for _ in range(5):
        decision = await check_and_consume_rate_limit(
            store, org_rule=rule, team_rule=None, user_rule=None, team_id=team_id, user_id=user_id
        )
        assert decision.allowed is True
        assert decision.remaining == 100  # unchanged across every call - never incremented here


@pytest.mark.asyncio
async def test_record_token_usage_then_gate_trips_once_prior_usage_reaches_the_limit() -> None:
    """The real, intended lifecycle: `record_token_usage()` (called from
    `api.v1.gateway.common.record_usage_charge()` after a real provider
    response) accumulates tokens from prior requests; a LATER `check_and_
    consume_rate_limit()` call then sees that already-consumed total and
    blocks once it is at/over the limit."""
    store = InProcessSharedStateStore()
    cache = RateLimitCache()
    org_id = uuid.uuid4()
    team_id = uuid.uuid4()
    user_id = uuid.uuid4()
    rule = _rule(
        requests_per_min=None, tokens_per_min=10, scope_type=RateLimitScopeType.ORG_DEFAULT_PER_USER
    )
    cache.set_org_rule(org_id, snapshot_from_rule(rule))

    # Before any usage: full headroom.
    first = await check_and_consume_rate_limit(
        store, org_rule=rule, team_rule=None, user_rule=None, team_id=team_id, user_id=user_id
    )
    assert first.allowed is True
    assert first.remaining == 10

    # A real provider response reported 10 tokens - accumulate it.
    await record_token_usage(store, cache, org_id=org_id, team_id=None, user_id=user_id, total_tokens=10)

    # Now already at the limit - the NEXT request's gate check trips.
    second = await check_and_consume_rate_limit(
        store, org_rule=rule, team_rule=None, user_rule=None, team_id=team_id, user_id=user_id
    )
    assert second.allowed is False
    assert second.limit == 10
    assert second.remaining == 0


@pytest.mark.asyncio
async def test_record_token_usage_is_a_no_op_when_rule_has_no_tokens_per_min_configured() -> None:
    """A rule with `tokens_per_min=None` (only `requests_per_min` set)
    contributes nothing to accumulate against - `record_token_usage` must
    not fabricate a counter for an axis that was never configured."""
    store = InProcessSharedStateStore()
    cache = RateLimitCache()
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    rule = _rule(requests_per_min=10, scope_type=RateLimitScopeType.ORG_DEFAULT_PER_USER)
    cache.set_org_rule(org_id, snapshot_from_rule(rule))

    await record_token_usage(store, cache, org_id=org_id, team_id=None, user_id=user_id, total_tokens=99999)

    # The requests-axis counter (the only one this rule configures) must be
    # completely unaffected - still full headroom.
    decision = await check_and_consume_rate_limit(
        store, org_rule=rule, team_rule=None, user_rule=None, team_id=uuid.uuid4(), user_id=user_id
    )
    assert decision.allowed is True
    assert decision.remaining == 9  # only THIS check's own try_consume() moved it


@pytest.mark.asyncio
async def test_requests_and_tokens_axes_are_independent_either_can_trip() -> None:
    """A rule configuring BOTH axes: exceeding either one trips the
    request, exactly like the existing pool-vs-personal additive semantics
    (AC4.2.2/AC4.2.9), just applied within a single rule's two axes."""
    store = InProcessSharedStateStore()
    cache = RateLimitCache()
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    rule = _rule(
        requests_per_min=100, tokens_per_min=10, scope_type=RateLimitScopeType.ORG_DEFAULT_PER_USER
    )
    cache.set_org_rule(org_id, snapshot_from_rule(rule))

    # Plenty of request headroom, but tokens already exhausted by a prior
    # request's real usage.
    await record_token_usage(store, cache, org_id=org_id, team_id=None, user_id=user_id, total_tokens=10)
    decision = await check_and_consume_rate_limit(
        store, org_rule=rule, team_rule=None, user_rule=None, team_id=uuid.uuid4(), user_id=user_id
    )
    assert decision.allowed is False
    assert decision.limit == 10  # the TOKEN limit tripped, not the (still-healthy) request limit


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
