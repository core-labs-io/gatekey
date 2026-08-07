"""Hardening pass item 1: a real, end-to-end proof that Phase 4's Fix 3
cache-invalidation-on-policy-write wiring (`services.residency.
set_org_residency_rule`/`set_team_residency_rule`, `services.dlp.
set_dlp_policy`/`set_team_dlp_override`, all calling `services.
response_cache.CacheInvalidator.clear_team`/`clear_all`) actually works
against a REAL Redis - not just "the code reads correctly," which is all
that was ever verified before this test existed.

Requires a real Redis (`GATEKEY_TEST_REDIS_URL`) - skips cleanly (not
silently passes) when unavailable, same convention every other Redis-gated
Phase 4 test in this suite already uses (see `test_phase4_reliability_
cost.py`'s `app_with_redis`/`app_with_redis_client` fixtures, replicated
here so this file has no cross-module fixture dependency).

Both tests below follow the identical three-step shape:
  1. Populate a response-cache entry for a team under a PERMISSIVE policy
     (a real request, real MISS, confirmed real HIT on an identical repeat -
     proving the cache genuinely works before tightening anything).
  2. Tighten the relevant policy via the REAL admin `PUT` endpoint (never
     poking `CacheInvalidator` directly).
  3. Repeat the IDENTICAL request a third time and assert it is NOT served
     from the now-stale cache entry - either a genuine cache miss occurs
     (the request goes through full residency/DLP re-evaluation and is
     rejected under the new, tighter policy), which is exactly what would
     NOT happen if the write-time invalidation call were ever silently
     dropped or made a no-op.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from gatekey.api.deps import get_provider_http_client
from gatekey.config import Settings
from gatekey.main import create_app

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio

_SSN = "234-56-7890"  # Presidio's UsSsnRecognizer invalidates the textbook "123-45-6789" placeholder.


def _skip_if_no_redis() -> str:
    url = os.environ.get("GATEKEY_TEST_REDIS_URL")
    if not url:
        pytest.skip("Redis not configured (GATEKEY_TEST_REDIS_URL)")
    return url


@pytest_asyncio.fixture(autouse=True)
async def _truncate_residency_and_dlp_tables(migrated_database_url: str) -> AsyncIterator[None]:
    """`residency_rules`/`dlp_policies`/`team_dlp_action_overrides` are never
    truncated between test files/functions by default (this whole session
    shares one Postgres instance) - both tests below WRITE an org-wide rule/
    policy via a real admin `PUT`, which would otherwise leak into whichever
    test runs next (including the other test in this same file) and produce
    a confusing false failure/false pass unrelated to cache invalidation
    itself."""
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute("TRUNCATE TABLE residency_rules, dlp_policies, team_dlp_action_overrides")
    finally:
        await conn.close()
    yield


@pytest_asyncio.fixture
async def app_with_redis(migrated_database_url: str, admin_token: str, master_key_bytes: bytes) -> FastAPI | None:
    """Create app with Redis enabled, or None if Redis URL not set - same
    shape as `test_phase4_reliability_cost.py`'s fixture of the same name."""
    redis_url = os.environ.get("GATEKEY_TEST_REDIS_URL")
    if not redis_url:
        return None
    settings = Settings(
        _env_file=None,
        DATABASE_URL=migrated_database_url,
        GATEKEY_ADMIN_TOKEN=admin_token,
        GATEKEY_MASTER_KEY=base64.b64encode(master_key_bytes).decode(),
        GATEKEY_REDIS_URL=redis_url,
    )
    return create_app(settings=settings)


@pytest_asyncio.fixture
async def app_with_redis_client(app_with_redis: FastAPI | None) -> Any:
    """`httpx.AsyncClient` bound to `app_with_redis`, with its real lifespan
    driven (so `app.state.shared_state_store` is genuinely the Redis-backed
    implementation) - `None` when Redis isn't configured, matching
    `app_with_redis`'s own contract (see that fixture's QA-fix note in
    `test_phase4_reliability_cost.py` for why a bare early `return` here
    would be a fixture ERROR, not a clean skip)."""
    if app_with_redis is None:
        yield None
        return
    async with app_with_redis.router.lifespan_context(app_with_redis):
        transport = httpx.ASGITransport(app=app_with_redis)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


def _canned_response(content: str = "hello from the cache-invalidation e2e test") -> dict:
    return {
        "id": "chatcmpl-cache-invalidation-e2e",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "gpt-4o",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    }


async def _make_team_with_service_account(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> str:
    """Fresh user + team + membership + service-account key + team cache
    opt-in (`cache_enabled=true`) - a from-scratch equivalent of conftest.py's
    `default_user_id`/`default_team_id` fixtures (those are tied to the
    plain, non-Redis `client` fixture - this file drives everything through
    `app_with_redis_client` instead, so it builds its own)."""
    user_resp = await client.post(
        "/v1/admin/users", json={"name": "cache-invalidation-e2e-user"}, headers=auth_headers
    )
    assert user_resp.status_code == 201, user_resp.text
    user_id = user_resp.json()["id"]

    team_resp = await client.post(
        "/v1/teams",
        json={"name": f"cache-invalidation-e2e-team-{uuid.uuid4().hex[:8]}"},
        headers=auth_headers,
    )
    assert team_resp.status_code == 201, team_resp.text
    team_id = team_resp.json()["id"]

    member_resp = await client.post(
        f"/v1/teams/{team_id}/members",
        json={"user_id": user_id, "role": "member", "budget_usd": None},
        headers=auth_headers,
    )
    assert member_resp.status_code == 201, member_resp.text

    cache_resp = await client.put(
        f"/v1/admin/teams/{team_id}/cache-settings",
        json={"cache_enabled": True, "cache_ttl_minutes": 5},
        headers=auth_headers,
    )
    assert cache_resp.status_code == 200, cache_resp.text

    sa_resp = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "cache-invalidation-e2e-key", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert sa_resp.status_code == 201, sa_resp.text
    return team_id, sa_resp.json()["secret"]


async def test_residency_rule_tightening_invalidates_cached_response(
    app_with_redis: FastAPI | None, app_with_redis_client: Any, auth_headers: dict[str, str]
) -> None:
    _skip_if_no_redis()
    if app_with_redis is None:
        pytest.skip("Redis app not available")
    client = app_with_redis_client

    team_id, secret = await _make_team_with_service_account(client, auth_headers)

    key_resp = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-cache-invalidation-residency-test"},
        headers=auth_headers,
    )
    assert key_resp.status_code == 200, key_resp.text

    # Step 0 (permissive): openai resolves to region "us" (`services.
    # residency._PROVIDER_STATIC_REGION`) - an explicit rule that allows
    # "us" (alongside "eu") is unambiguously permissive for this request.
    permissive_resp = await client.put(
        "/v1/admin/residency-rules",
        json={"allowed_regions": ["us", "eu"], "violation_behavior": "hard_block"},
        headers=auth_headers,
    )
    assert permissive_resp.status_code == 200, permissive_resp.text

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_canned_response())

    app_with_redis.dependency_overrides[get_provider_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "residency cache-invalidation e2e test"}],
    }
    try:
        # Step 1: populate the cache under the permissive rule.
        first = await client.post(
            "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {secret}"}
        )
        assert first.status_code == 200, first.text
        assert first.headers["X-Cache"] == "MISS"

        # Sanity check: an identical repeat, still under the permissive
        # rule, really does hit the cache - proves the setup is valid
        # before we go on to prove invalidation.
        second = await client.post(
            "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {secret}"}
        )
        assert second.status_code == 200, second.text
        assert second.headers["X-Cache"] == "HIT"

        # Step 2: tighten the org-wide residency rule via the REAL admin PUT
        # endpoint - "us" (openai's region) is no longer allowed. This is
        # what must fire `CacheInvalidator.clear_all()` (`services.
        # residency.set_org_residency_rule`'s Fix 3 wiring).
        tightened_resp = await client.put(
            "/v1/admin/residency-rules",
            json={"allowed_regions": ["eu"], "violation_behavior": "hard_block"},
            headers=auth_headers,
        )
        assert tightened_resp.status_code == 200, tightened_resp.text

        # Step 3: the identical request again. If invalidation had NOT
        # fired, this would still be a stale cache HIT, returning the
        # cached 200 response with zero residency re-evaluation - the bug
        # Fix 3 closes. With invalidation working, this is a genuine cache
        # miss that goes through full `check_residency()` re-evaluation
        # against the NEW rule, which now hard-blocks openai's "us" region.
        third = await client.post(
            "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {secret}"}
        )
    finally:
        del app_with_redis.dependency_overrides[get_provider_http_client]

    assert third.status_code == 403, (
        f"expected the tightened residency rule to reject this request after cache "
        f"invalidation, got {third.status_code}: {third.text}"
    )
    assert third.json()["error"]["code"] == "residency_violation"
    # Never a stale HIT - either header is acceptable proof the cache did
    # not silently serve the old entry (some pipelines omit the header
    # entirely on an error path; both prove the point).
    assert third.headers.get("X-Cache") != "HIT"

    # Regression pin (a REAL bug this test uncovered, fixed alongside it -
    # see `services.residency.set_org_residency_rule`'s "Hardening pass
    # item 1" docstring note): the in-process `ResidencyRuleCache` itself
    # (a SEPARATE thing from the Redis response cache this test's main
    # assertions above already cover) must also reflect the tightened rule,
    # not a stale pre-update snapshot poisoned by SQLAlchemy's identity map.
    live_org_rule = app_with_redis.state.residency_rule_cache.get_org_rule()
    assert live_org_rule is not None
    assert live_org_rule.allowed_regions == frozenset({"eu"})


async def test_dlp_policy_tightening_invalidates_cached_response(
    app_with_redis: FastAPI | None, app_with_redis_client: Any, auth_headers: dict[str, str]
) -> None:
    _skip_if_no_redis()
    if app_with_redis is None:
        pytest.skip("Redis app not available")
    client = app_with_redis_client

    team_id, secret = await _make_team_with_service_account(client, auth_headers)

    key_resp = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-cache-invalidation-dlp-test"},
        headers=auth_headers,
    )
    assert key_resp.status_code == 200, key_resp.text

    # Step 0 (permissive): no DLP row configured yet at all - `services.dlp.
    # load_dlp_policy`'s documented "absence of a row = every detector off"
    # default - a request containing an SSN-shaped string is not scanned at
    # all and is freely cacheable.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_canned_response())

    app_with_redis.dependency_overrides[get_provider_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": f"my SSN is {_SSN} today, please help"}],
    }
    try:
        # Step 1: populate the cache under the permissive (no-DLP) policy.
        first = await client.post(
            "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {secret}"}
        )
        assert first.status_code == 200, first.text
        assert first.headers["X-Cache"] == "MISS"

        # Sanity check: an identical repeat really does hit the cache.
        second = await client.post(
            "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {secret}"}
        )
        assert second.status_code == 200, second.text
        assert second.headers["X-Cache"] == "HIT"

        # Step 2: tighten the org DLP policy via the REAL admin PUT endpoint
        # - enable the SSN detector with a hard `block` action. This is what
        # must fire `CacheInvalidator.clear_all()` (`services.dlp.set_dlp_
        # policy`'s Fix 3 wiring).
        tightened_resp = await client.put(
            "/v1/admin/dlp-policy",
            json={"ssn_detector_enabled": True, "default_action": "block"},
            headers=auth_headers,
        )
        assert tightened_resp.status_code == 200, tightened_resp.text

        # Step 3: the identical request again. A stale HIT would return the
        # cached 200 with the raw SSN content never re-scanned - the bug
        # Fix 3 closes. With invalidation working, this is a genuine cache
        # miss that goes through a full, synchronous DLP scan under the new
        # policy, which now blocks on the SSN finding.
        third = await client.post(
            "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {secret}"}
        )
    finally:
        del app_with_redis.dependency_overrides[get_provider_http_client]

    assert third.status_code == 403, (
        f"expected the tightened DLP policy to block this request after cache "
        f"invalidation, got {third.status_code}: {third.text}"
    )
    assert third.json()["error"]["code"] == "dlp_blocked"
    assert third.headers.get("X-Cache") != "HIT"
