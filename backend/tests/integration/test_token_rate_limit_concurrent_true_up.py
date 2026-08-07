"""Hardening pass item 4 (QA follow-up): does the post-response `tokens_per_
min` true-up (`services.rate_limit.record_token_usage()`) genuinely avoid
double-counting/under-counting when multiple requests' responses land and
true-up the SAME rolling-window counter concurrently?

`test_phase4_distributed_rate_limit.py` already proves the PRE-emptive
`requests_per_min` axis (`RateLimiter._check_counter`/`check_and_consume_
rate_limit`'s `store.try_consume()`) is safe under real concurrent access
from independent Redis connections/processes. It does NOT exercise the
`tokens_per_min` axis's DIFFERENT mechanic at all: `record_token_usage()`
uses `store.incr_by()` (unconditional atomic add - `RedisSharedStateStore.
incr_by` via the `_INCR_BY_SCRIPT` Lua script), not `try_consume()` (gate-
and-increment). This file is the `incr_by` analogue of that file's `try_
consume` proof - same "N genuinely-independent Redis connections, fired via
`asyncio.gather`" methodology, applied to the "true-up after response"
accounting path item 4 introduced.

Requires a real Redis (`GATEKEY_TEST_REDIS_URL`) - skips cleanly (not
silently passes) when unavailable, same convention every other Redis-gated
Phase 4 test in this suite already uses.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from gatekey.db.models.rate_limit_rule import RateLimitOnLimit, RateLimitScopeType
from gatekey.services.rate_limit import (
    RateLimitCache,
    RateLimitRuleSnapshot,
    _pool_rate_limit_key,
    check_and_consume_rate_limit,
    record_token_usage,
)
from gatekey.services.shared_state import RedisSharedStateStore


def _skip_if_no_redis() -> str:
    url = os.environ.get("GATEKEY_TEST_REDIS_URL")
    if not url:
        pytest.skip("Redis not configured (GATEKEY_TEST_REDIS_URL)")
    return url


def _token_rule(*, tokens_per_min: int) -> RateLimitRuleSnapshot:
    return RateLimitRuleSnapshot(
        id=uuid.uuid4(),
        scope_type=RateLimitScopeType.ORG_DEFAULT_PER_USER,
        requests_per_min=None,
        tokens_per_min=tokens_per_min,
        on_limit=RateLimitOnLimit.REJECT,
        max_queue_wait_seconds=1,
    )


@pytest.mark.asyncio
async def test_concurrent_post_response_true_ups_neither_double_count_nor_lose_updates() -> None:
    """50 concurrent `record_token_usage()` calls (standing in for 50
    requests' responses landing back-to-back), each truing-up 10 tokens,
    fired across 5 genuinely independent `RedisSharedStateStore` connections
    (never sharing an in-process object) - the counter must land at EXACTLY
    500, not less (a lost update from a naive read-modify-write race) and
    not more (a double-count)."""
    redis_url = _skip_if_no_redis()
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    rule = _token_rule(tokens_per_min=1_000_000)  # high enough that nothing gates mid-run

    cache = RateLimitCache()
    cache.set_org_rule(org_id, rule)

    instances = [RedisSharedStateStore(redis_url) for _ in range(5)]
    try:
        # Clean slate for this fresh org_id/user_id's Redis key (a fresh
        # UUID pair already guarantees this, but delete defensively).
        key = _pool_rate_limit_key(rule, None, user_id, counter_type="tokens")
        await instances[0].delete(key)

        async def _one_true_up(instance_index: int) -> None:
            store = instances[instance_index % len(instances)]
            await record_token_usage(
                store, cache, org_id=org_id, team_id=None, user_id=user_id, total_tokens=10
            )

        await asyncio.gather(*[_one_true_up(i) for i in range(50)])

        final_count = await instances[0].get_int(key)
        assert final_count == 500, (
            f"expected exactly 500 (50 concurrent true-ups x 10 tokens each, no lost updates "
            f"and no double-counting), got {final_count}"
        )
    finally:
        for instance in instances:
            try:
                await instance.aclose()
            except RuntimeError:
                pass


@pytest.mark.asyncio
async def test_concurrent_true_ups_produce_an_exact_gate_trip_point() -> None:
    """End-to-end version of the above: after the exact-count concurrent
    true-up completes, `check_and_consume_rate_limit()`'s gate must trip at
    precisely the configured limit - not one request early (an over-count
    artifact) or one request late (an under-count / lost-update artifact)."""
    redis_url = _skip_if_no_redis()
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()
    rule = _token_rule(tokens_per_min=500)

    cache = RateLimitCache()
    cache.set_org_rule(org_id, rule)

    instances = [RedisSharedStateStore(redis_url) for _ in range(5)]
    try:
        key = _pool_rate_limit_key(rule, None, user_id, counter_type="tokens")
        await instances[0].delete(key)

        # Gate must be open before any usage lands.
        pre_decision = await check_and_consume_rate_limit(
            instances[0], org_rule=rule, team_rule=None, user_rule=None, team_id=None, user_id=user_id
        )
        assert pre_decision.allowed is True

        async def _one_true_up(instance_index: int) -> None:
            store = instances[instance_index % len(instances)]
            await record_token_usage(
                store, cache, org_id=org_id, team_id=None, user_id=user_id, total_tokens=10
            )

        # 50 concurrent true-ups x 10 tokens = exactly 500 = the configured
        # limit itself - the gate's own semantics (`tokens_used < token_
        # limit`) mean landing EXACTLY on the limit must now be closed.
        await asyncio.gather(*[_one_true_up(i) for i in range(50)])

        post_decision = await check_and_consume_rate_limit(
            instances[0], org_rule=rule, team_rule=None, user_rule=None, team_id=None, user_id=user_id
        )
        assert post_decision.allowed is False, (
            "gate stayed open after usage reached the configured limit exactly - "
            "a lost update would under-count and wrongly leave this open"
        )
        assert post_decision.remaining == 0
    finally:
        for instance in instances:
            try:
                await instance.aclose()
            except RuntimeError:
                pass
