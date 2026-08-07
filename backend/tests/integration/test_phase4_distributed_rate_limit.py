"""AC4.2.6 / phase-4 NFR: "Rate limiter accuracy under distributed
deployment ... Acceptance test: simulate 3 parallel Gateway instances, fire
150 requests at once, verify exactly 100 succeed (or queued) and 50 are
rejected" against a 100 requests/minute limit.

Before this file existed, this exact scenario was never exercised anywhere
in the test suite - every existing rate-limit test
(`tests/unit/test_rate_limit_service.py`) drives `check_and_consume_rate_
limit()` sequentially against a SINGLE `InProcessSharedStateStore` instance,
which proves the counting logic is correct but says nothing about the
"distributed, multi-instance-safe" claim itself (an in-process store isn't
shared across instances at all, by definition - the exact naive approach
design doc section 1.5/AC4.2.6 explicitly rejects). This test instead
builds THREE SEPARATE `RedisSharedStateStore` instances (each with its own
Redis connection/pool, exactly like three independent Gateway processes
would each have) pointed at the SAME real Redis, and fires 150 concurrent
requests distributed round-robin across the three - the only way to
actually prove the Lua-script-based atomic `INCR` in `services.shared_
state.RedisSharedStateStore` is safe under real concurrent access from
independent clients, not just correct when called sequentially from one.

Requires a real Redis (`GATEKEY_TEST_REDIS_URL`) - skips cleanly (not
silently passes) when unavailable, same convention every other
Redis-gated Phase 4 test in this suite already uses.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from gatekey.db.models.rate_limit_rule import RateLimitOnLimit, RateLimitRule, RateLimitScopeType
from gatekey.services.rate_limit import check_and_consume_rate_limit
from gatekey.services.shared_state import RedisSharedStateStore


def _skip_if_no_redis() -> str:
    url = os.environ.get("GATEKEY_TEST_REDIS_URL")
    if not url:
        pytest.skip("Redis not configured (GATEKEY_TEST_REDIS_URL)")
    return url


def _team_rule(*, requests_per_min: int, team_id: uuid.UUID) -> RateLimitRule:
    return RateLimitRule(
        id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        scope_type=RateLimitScopeType.TEAM,
        scope_team_id=team_id,
        scope_user_id=None,
        requests_per_min=requests_per_min,
        tokens_per_min=None,
        on_limit=RateLimitOnLimit.REJECT,
        max_queue_wait_seconds=5,
    )


@pytest.mark.asyncio
async def test_three_gateway_instances_150_requests_at_100rpm_limit_exactly_100_succeed() -> None:
    """The literal AC4.2.6 acceptance test, run for real against Redis."""
    redis_url = _skip_if_no_redis()

    # Three independent stores/connections - simulating three independent
    # Gateway processes, each with its own Redis client, all sharing state
    # only via the real Redis server (never via any in-process object).
    instances = [RedisSharedStateStore(redis_url) for _ in range(3)]
    try:
        team_id = uuid.uuid4()
        user_id = uuid.uuid4()  # same user_id: this is a single shared TEAM pool under test.
        rule = _team_rule(requests_per_min=100, team_id=team_id)

        # Clean slate - a fresh team_id/user_id per test run already
        # guarantees a fresh Redis key, but delete defensively in case of a
        # UUID collision across parallel test workers.
        from gatekey.services.rate_limit import _pool_rate_limit_key

        # Fix 2 (QA/security review finding) changed `_pool_rate_limit_key`'s
        # signature to `(pool_rule, team_id, user_id)` - it now branches on
        # the rule's own `scope_type` to decide the TEAM-shared vs.
        # ORG_DEFAULT_PER_USER-per-user key shape (see that function's
        # module note in `services/rate_limit.py`). This test's `rule` is
        # already the TEAM-scoped rule under test - reuse it here instead of
        # the old 2-arg call.
        await instances[0].delete(_pool_rate_limit_key(rule, team_id, user_id))

        async def _one_request(instance_index: int) -> bool:
            store = instances[instance_index % len(instances)]
            decision = await check_and_consume_rate_limit(
                store,
                org_rule=None,
                team_rule=rule,
                user_rule=None,
                team_id=team_id,
                user_id=user_id,
            )
            return decision.allowed

        # Fire all 150 "requests" concurrently (asyncio.gather - genuine
        # concurrent dispatch, not sequential), round-robin distributed
        # across the three simulated instances.
        results = await asyncio.gather(*[_one_request(i) for i in range(150)])

        allowed_count = sum(1 for r in results if r)
        rejected_count = sum(1 for r in results if not r)

        assert allowed_count == 100, (
            f"AC4.2.6 violated: expected exactly 100 allowed across 3 concurrent Redis-backed "
            f"instances at a 100rpm limit, got {allowed_count} (rejected={rejected_count})"
        )
        assert rejected_count == 50
    finally:
        for instance in instances:
            try:
                await instance.aclose()
            except RuntimeError:
                # Windows/ProactorEventLoop teardown quirk - see
                # test_cache_lookup_overhead_nfr.py's identical note.
                pass


@pytest.mark.asyncio
async def test_distributed_counter_survives_a_simulated_instance_restart() -> None:
    """A naive in-process counter would reset to zero if "instance 1"
    restarted mid-window - the whole reason AC4.2.6 rejects that approach.
    Simulates exactly that: instance 1 consumes some of the budget, is then
    discarded and replaced by a brand-new `RedisSharedStateStore` (a fresh
    TCP connection/pool, standing in for a fresh process), and the
    replacement must see the SAME already-consumed count - never a reset."""
    redis_url = _skip_if_no_redis()
    team_id = uuid.uuid4()
    user_id = uuid.uuid4()
    rule = _team_rule(requests_per_min=10, team_id=team_id)

    instance_1 = RedisSharedStateStore(redis_url)
    try:
        for _ in range(7):
            decision = await check_and_consume_rate_limit(
                instance_1, org_rule=None, team_rule=rule, user_rule=None, team_id=team_id, user_id=user_id
            )
            assert decision.allowed is True
    finally:
        try:
            await instance_1.aclose()
        except RuntimeError:
            pass

    # "Instance 1" is gone. A brand new store/connection - the counter must
    # already show 7 consumed, not reset to 0.
    instance_2 = RedisSharedStateStore(redis_url)
    try:
        # 3 more should succeed (10 - 7 = 3 remaining)...
        for _ in range(3):
            decision = await check_and_consume_rate_limit(
                instance_2, org_rule=None, team_rule=rule, user_rule=None, team_id=team_id, user_id=user_id
            )
            assert decision.allowed is True
        # ...and the 11th (across both "instances" combined) must be rejected.
        decision = await check_and_consume_rate_limit(
            instance_2, org_rule=None, team_rule=rule, user_rule=None, team_id=team_id, user_id=user_id
        )
        assert decision.allowed is False
    finally:
        try:
            await instance_2.aclose()
        except RuntimeError:
            pass
