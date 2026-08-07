"""Unit tests for `services.response_cache`'s `ResponseCache` get/set
round-trip (Phase 4 gateway-pipeline wiring), specifically the `expires_at`
serialization fix (`set()` previously stored a raw epoch-seconds float while
`get()` assumed an ISO-8601 string for anything non-`str`, which raised
`TypeError: '>' not supported between instances of 'datetime.datetime' and
'float'` on the very first real read-after-write - never caught by any
existing test because nothing called this code before this task's gateway
wiring work).
"""

from __future__ import annotations

import uuid

import pytest

from gatekey.services.response_cache import ResponseCache
from gatekey.services.shared_state import InProcessSharedStateStore


@pytest.mark.asyncio
async def test_set_then_get_entry_round_trips_without_raising() -> None:
    store = InProcessSharedStateStore()
    cache = ResponseCache(store)
    team_id, user_id = uuid.uuid4(), uuid.uuid4()

    wrote = await cache.set(
        team_id,
        user_id,
        "openai",
        "gpt-4o",
        "a" * 64,
        "us",
        {"id": "chatcmpl-1", "choices": []},
        ttl_seconds=60,
        input_tokens=3,
        output_tokens=7,
    )
    assert wrote is True

    entry = await cache.get_entry(team_id, user_id, "openai", "gpt-4o", "a" * 64, "us")
    assert entry is not None
    assert entry.response_body == {"id": "chatcmpl-1", "choices": []}
    assert entry.input_tokens == 3
    assert entry.output_tokens == 7
    assert 0 < entry.ttl_remaining_seconds <= 60


@pytest.mark.asyncio
async def test_get_entry_miss_returns_none() -> None:
    store = InProcessSharedStateStore()
    cache = ResponseCache(store)
    entry = await cache.get_entry(uuid.uuid4(), uuid.uuid4(), "openai", "gpt-4o", "b" * 64, "us")
    assert entry is None


@pytest.mark.asyncio
async def test_residency_zone_partitions_the_cache_key() -> None:
    """A response cached under one residency zone must never be served to a
    lookup for a different zone - AC3.3/design doc section 2.3's residency
    boundary, enforced structurally by the cache key shape, not by a
    separate runtime check."""
    store = InProcessSharedStateStore()
    cache = ResponseCache(store)
    team_id, user_id = uuid.uuid4(), uuid.uuid4()

    await cache.set(
        team_id, user_id, "openai", "gpt-4o", "c" * 64, "us", {"body": "us-response"}, ttl_seconds=60
    )

    same_zone = await cache.get_entry(team_id, user_id, "openai", "gpt-4o", "c" * 64, "us")
    other_zone = await cache.get_entry(team_id, user_id, "openai", "gpt-4o", "c" * 64, "eu")
    assert same_zone is not None
    assert other_zone is None
