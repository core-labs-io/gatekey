"""Integration tests for Phase 4: Reliability & Cost Efficiency features.

These tests verify:
- Rate limiting with Redis-backed sliding window counters
- Exact-match response caching with TTL
- Graceful degradation with budget proximity detection
- Multi-key failover and backup groups
- Shared state store (Redis-backed)

Redis is optional - tests that require Redis skip if GATEKEY_REDIS_URL is unset.
"""

from __future__ import annotations

import base64
import os
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from fastapi import FastAPI

from gatekey.config import Settings
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.main import create_app

from .conftest import to_asyncpg_dsn


def _skip_if_no_redis() -> None:
    """Skip test if Redis is not configured."""
    if not os.environ.get("GATEKEY_TEST_REDIS_URL"):
        pytest.skip("Redis not configured (GATEKEY_TEST_REDIS_URL)")


@pytest_asyncio.fixture(autouse=True)
async def _truncate_phase4_admin_tables(migrated_database_url: str) -> AsyncIterator[None]:
    """Give every test in this module a clean slate for the Phase 4 admin
    tables it exercises.

    `conftest.py`'s `_truncate_provider_keys` only handles `provider_keys`
    itself (the pre-Phase-4 table). These tables are new this phase and
    several tests here depend on a genuinely clean state: the
    one-row-per-org partial unique index on `rate_limit_rules`
    (`uq_rate_limit_rules_org_default`) would otherwise reject every POST
    after the first one across this whole session-scoped database, and
    `test_list_failover_events_empty` requires an empty starting list.
    `CASCADE` because `provider_keys.backup_group_id` FKs to
    `backup_groups` - listing `provider_keys` in the same statement keeps
    the truncate scoped to exactly these tables (mirrors
    `_truncate_provider_keys`'s own CASCADE rationale).
    """
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute(
            "TRUNCATE TABLE rate_limit_rules, caching_settings, degradation_policies, "
            "backup_groups, provider_keys, failover_events, degradation_events, "
            "cache_lookup_events, rate_limit_rejection_events CASCADE"
        )
    finally:
        await conn.close()
    yield


# ============================================================================
# Rate Limiting Tests
# ============================================================================


@pytest_asyncio.fixture
async def app_with_redis(migrated_database_url: str, admin_token: str, master_key_bytes: bytes) -> FastAPI | None:
    """Create app with Redis enabled, or None if Redis URL not set."""
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
    """Create httpx client for app with Redis, or None.

    QA fix: the `app_with_redis is None` (no Redis configured) branch used
    to be a bare `return` - since this is an async-generator fixture (it
    `yield`s below), a `return` before ever reaching that `yield` makes the
    underlying async generator raise immediately with no yielded value,
    which pytest-asyncio surfaces as a fixture ERROR (not a clean skip) for
    every test that depends on this fixture whenever Redis isn't configured
    - this fixture had no consumers before this QA pass added one
    (`TestSharedStateStore.test_redis_state_store`), so the bug was latent
    and never observed. `yield None` instead, matching `app_with_redis`'s
    own `FastAPI | None` "None means not configured" contract, so a
    depending test can `pytest.skip()` on a clean `None` value exactly as
    `test_redis_state_store` already does for `app_with_redis` itself.
    """
    if app_with_redis is None:
        yield None
        return
    import httpx
    async with app_with_redis.router.lifespan_context(app_with_redis):
        transport = httpx.ASGITransport(app=app_with_redis)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client


class TestRateLimiting:
    """Tests for rate limiting configuration and enforcement."""

    @pytest.mark.asyncio
    async def test_create_rate_limit_rule(self, client, auth_headers: dict[str, str]):
        """Create an org-wide rate limit rule."""
        response = await client.post(
            "/v1/admin/rate-limit-rules",
            json={
                "requests_per_minute": 100,
                "tokens_per_minute": 10000,
                "on_limit": "reject",
                "max_queue_wait_seconds": 30,
            },
            headers=auth_headers,
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["requests_per_minute"] == 100
        assert data["tokens_per_minute"] == 10000
        assert data["on_limit"] == "reject"
        assert data["max_queue_wait_seconds"] == 30
        assert data["scope_type"] == "org_default_per_user"
        assert data["scope_team_id"] is None

    @pytest.mark.asyncio
    async def test_list_rate_limit_rules(self, client, auth_headers: dict[str, str]):
        """List all rate limit rules."""
        # Create a rule first
        await client.post(
            "/v1/admin/rate-limit-rules",
            json={
                "requests_per_minute": 50,
                "tokens_per_minute": 5000,
                "on_limit": "reject",
                "max_queue_wait_seconds": 15,
            },
            headers=auth_headers,
        )

        response = await client.get("/v1/admin/rate-limit-rules", headers=auth_headers)
        assert response.status_code == 200, response.text
        data = response.json()
        assert "rules" in data
        assert len(data["rules"]) >= 1

    @pytest.mark.asyncio
    async def test_update_rate_limit_rule(self, client, auth_headers: dict[str, str]):
        """Update an existing rate limit rule."""
        # Create a rule first
        create_resp = await client.post(
            "/v1/admin/rate-limit-rules",
            json={
                "requests_per_minute": 200,
                "tokens_per_minute": 20000,
                "on_limit": "reject",
                "max_queue_wait_seconds": 60,
            },
            headers=auth_headers,
        )
        rule_id = create_resp.json()["id"]

        # Update it
        response = await client.put(
            f"/v1/admin/rate-limit-rules/{rule_id}",
            json={
                "requests_per_minute": 150,
                "tokens_per_minute": 15000,
                "on_limit": "queue_and_retry",
                "max_queue_wait_seconds": 45,
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["requests_per_minute"] == 150
        assert data["tokens_per_minute"] == 15000
        assert data["on_limit"] == "queue_and_retry"
        assert data["max_queue_wait_seconds"] == 45

    @pytest.mark.asyncio
    async def test_delete_rate_limit_rule(self, client, auth_headers: dict[str, str]):
        """Delete a rate limit rule."""
        # Create a rule first
        create_resp = await client.post(
            "/v1/admin/rate-limit-rules",
            json={
                "requests_per_minute": 75,
                "tokens_per_minute": 7500,
                "on_limit": "reject",
                "max_queue_wait_seconds": 20,
            },
            headers=auth_headers,
        )
        rule_id = create_resp.json()["id"]

        # Delete it
        response = await client.delete(f"/v1/admin/rate-limit-rules/{rule_id}", headers=auth_headers)
        assert response.status_code == 204, response.text

        # Verify it's gone
        response = await client.get("/v1/admin/rate-limit-rules", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert all(r["id"] != rule_id for r in data["rules"])

    @pytest.mark.asyncio
    async def test_rate_limit_validation_errors(self, client, auth_headers: dict[str, str]):
        """Test validation errors for rate limit rules."""
        # Missing both limits
        response = await client.post(
            "/v1/admin/rate-limit-rules",
            json={
                "on_limit": "reject",
                "max_queue_wait_seconds": 30,
            },
            headers=auth_headers,
        )
        assert response.status_code == 400, response.text
        data = response.json()
        assert data["error"]["code"] == "missing_limit"

        # Invalid queue wait
        response = await client.post(
            "/v1/admin/rate-limit-rules",
            json={
                "requests_per_minute": 100,
                "on_limit": "reject",
                "max_queue_wait_seconds": 5,  # too low
            },
            headers=auth_headers,
        )
        assert response.status_code == 400, response.text
        data = response.json()
        assert data["error"]["code"] == "invalid_queue_wait"


class TestRateLimitRuleStatus:
    """Gap 3 (audit finding, AC4.2.8): read-only current-utilization/
    queue-depth status for one rate limit rule, sourced from the same
    counter the live gateway pipeline actually gates requests against."""

    @pytest.mark.asyncio
    async def test_status_unknown_rule_returns_404(self, client, auth_headers: dict[str, str]):
        response = await client.get(
            f"/v1/admin/rate-limit-rules/{uuid.uuid4()}/status", headers=auth_headers
        )
        assert response.status_code == 404, response.text

    @pytest.mark.asyncio
    async def test_status_requires_admin(self, client):
        response = await client.get(f"/v1/admin/rate-limit-rules/{uuid.uuid4()}/status")
        assert response.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_org_default_rule_status_unavailable_without_user_id(
        self, client, auth_headers: dict[str, str]
    ):
        create_resp = await client.post(
            "/v1/admin/rate-limit-rules",
            json={"requests_per_minute": 100, "on_limit": "reject", "max_queue_wait_seconds": 30},
            headers=auth_headers,
        )
        assert create_resp.status_code == 201, create_resp.text
        rule_id = create_resp.json()["id"]

        response = await client.get(
            f"/v1/admin/rate-limit-rules/{rule_id}/status", headers=auth_headers
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["available"] is False
        assert data["reason"] is not None
        assert data["requests_used_last_60s"] is None
        # Never a fabricated queue-depth number - see service module docstring.
        assert data["queue_depth"] is None
        assert data["queue_depth_tracked"] is False

    @pytest.mark.asyncio
    async def test_org_default_rule_status_reflects_real_usage_given_user_id(
        self, client, auth_headers: dict[str, str]
    ):
        create_resp = await client.post(
            "/v1/admin/rate-limit-rules",
            json={"requests_per_minute": 5, "on_limit": "reject", "max_queue_wait_seconds": 30},
            headers=auth_headers,
        )
        rule_id = create_resp.json()["id"]
        user_id = uuid.uuid4()

        response = await client.get(
            f"/v1/admin/rate-limit-rules/{rule_id}/status",
            params={"user_id": str(user_id)},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["available"] is True
        assert data["requests_limit"] == 5
        # Nothing has been consumed against this brand-new counter yet.
        assert data["requests_used_last_60s"] == 0
        assert data["requests_remaining"] == 5


# ============================================================================
# Caching Settings Tests
# ============================================================================


class TestCachingSettings:
    """Tests for caching settings configuration."""

    @pytest.mark.asyncio
    async def test_get_caching_settings_default(self, client, auth_headers: dict[str, str]):
        """Get caching settings when not configured - should return defaults."""
        response = await client.get("/v1/admin/caching-settings", headers=auth_headers)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["org_id"] == str(DEFAULT_ORG_ID)
        assert data["enabled"] is True  # default per design doc
        assert data["ttl_seconds"] == 3600  # default 1 hour

    @pytest.mark.asyncio
    async def test_update_caching_settings(self, client, auth_headers: dict[str, str]):
        """Update caching settings."""
        response = await client.put(
            "/v1/admin/caching-settings",
            json={
                "enabled": True,
                "ttl_seconds": 1800,  # 30 minutes
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["enabled"] is True
        assert data["ttl_seconds"] == 1800

    @pytest.mark.asyncio
    async def test_caching_ttl_validation(self, client, auth_headers: dict[str, str]):
        """Test TTL validation."""
        # TTL too low
        response = await client.put(
            "/v1/admin/caching-settings",
            json={
                "enabled": True,
                "ttl_seconds": 30,  # below 60
            },
            headers=auth_headers,
        )
        assert response.status_code == 400, response.text
        data = response.json()
        assert data["error"]["code"] == "invalid_ttl"

        # TTL too high
        response = await client.put(
            "/v1/admin/caching-settings",
            json={
                "enabled": True,
                "ttl_seconds": 100000,  # above 86400
            },
            headers=auth_headers,
        )
        assert response.status_code == 400, response.text
        data = response.json()
        assert data["error"]["code"] == "invalid_ttl"

    @pytest.mark.asyncio
    async def test_clear_caching_settings(self, client, auth_headers: dict[str, str]):
        """Clear caching settings (reset to defaults)."""
        # First update to non-default
        await client.put(
            "/v1/admin/caching-settings",
            json={
                "enabled": False,
                "ttl_seconds": 7200,
            },
            headers=auth_headers,
        )

        # Clear
        response = await client.post("/v1/admin/caching-settings/clear", headers=auth_headers)
        assert response.status_code == 200, response.text
        data = response.json()
        assert "message" in data

        # Verify defaults restored
        response = await client.get("/v1/admin/caching-settings", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert data["enabled"] is True
        assert data["ttl_seconds"] == 3600


# ============================================================================
# Degradation Policy Tests
# ============================================================================


class TestDegradationPolicy:
    """Tests for graceful degradation policy configuration."""

    @pytest.mark.asyncio
    async def test_get_degradation_policy_default(self, client, auth_headers: dict[str, str]):
        """Get degradation policy when not configured - should return defaults."""
        response = await client.get("/v1/admin/degradation-policy", headers=auth_headers)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["scope_type"] == "org"
        assert data["scope_team_id"] is None
        assert data["enabled"] is False
        assert Decimal(data["threshold_pct_of_budget"]) == Decimal("10.0")
        assert data["downgrade_target_model"] == ""

    @pytest.mark.asyncio
    async def test_update_degradation_policy(self, client, auth_headers: dict[str, str]):
        """Update degradation policy."""
        response = await client.put(
            "/v1/admin/degradation-policy",
            json={
                "enabled": True,
                "threshold_pct_of_budget": 25.0,
                "downgrade_target_model": "gpt-4o-mini",
            },
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["enabled"] is True
        assert Decimal(data["threshold_pct_of_budget"]) == Decimal("25.0")
        assert data["downgrade_target_model"] == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_degradation_threshold_validation(self, client, auth_headers: dict[str, str]):
        """Test threshold percentage validation."""
        # Below 1%
        response = await client.put(
            "/v1/admin/degradation-policy",
            json={
                "enabled": True,
                "threshold_pct_of_budget": 0.5,
                "downgrade_target_model": "gpt-4o-mini",
            },
            headers=auth_headers,
        )
        assert response.status_code == 400, response.text
        data = response.json()
        assert data["error"]["code"] == "invalid_threshold"

        # Above 99%
        response = await client.put(
            "/v1/admin/degradation-policy",
            json={
                "enabled": True,
                "threshold_pct_of_budget": 100.0,
                "downgrade_target_model": "gpt-4o-mini",
            },
            headers=auth_headers,
        )
        assert response.status_code == 400, response.text
        data = response.json()
        assert data["error"]["code"] == "invalid_threshold"


# ============================================================================
# Backup Group Tests
# ============================================================================


class TestBackupGroups:
    """Tests for backup group configuration."""

    @pytest.mark.asyncio
    async def test_create_backup_group(self, client, auth_headers: dict[str, str]):
        """Create a backup group with multiple keys."""
        response = await client.post(
            "/v1/admin/backup-groups",
            json={
                "name": "primary-backup-group",
                "keys": ["key1", "key2", "key3"],
            },
            headers=auth_headers,
        )
        assert response.status_code == 201, response.text
        data = response.json()
        assert data["name"] == "primary-backup-group"
        assert data["keys"] == ["key1", "key2", "key3"]

    @pytest.mark.asyncio
    async def test_list_backup_groups(self, client, auth_headers: dict[str, str]):
        """List all backup groups."""
        response = await client.get("/v1/admin/backup-groups", headers=auth_headers)
        assert response.status_code == 200, response.text
        data = response.json()
        assert isinstance(data, list)

    @pytest.mark.asyncio
    async def test_delete_backup_group(self, client, auth_headers: dict[str, str]):
        """Delete a backup group."""
        # Create a backup group first
        create_resp = await client.post(
            "/v1/admin/backup-groups",
            json={
                "name": "temp-backup-group",
                "keys": ["key1", "key2"],
            },
            headers=auth_headers,
        )
        group_id = create_resp.json()["id"]

        # Delete it
        response = await client.delete(f"/v1/admin/backup-groups/{group_id}", headers=auth_headers)
        assert response.status_code == 204, response.text

        # Verify it's gone
        response = await client.get("/v1/admin/backup-groups", headers=auth_headers)
        assert response.status_code == 200
        data = response.json()
        assert all(g["id"] != group_id for g in data)


# ============================================================================
# Failover Event Tests
# ============================================================================


class TestFailoverEvents:
    """Tests for failover event tracking."""

    @pytest.mark.asyncio
    async def test_list_failover_events_empty(self, client, auth_headers: dict[str, str]):
        """List failover events when none exist."""
        response = await client.get("/v1/admin/failover-events", headers=auth_headers)
        assert response.status_code == 200, response.text
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 0

    @pytest.mark.asyncio
    async def test_list_failover_events_with_filter(self, client, auth_headers: dict[str, str]):
        """List failover events with date filters."""
        from_date = "2024-01-01"
        to_date = "2024-12-31"
        response = await client.get(
            f"/v1/admin/failover-events?from={from_date}&to={to_date}&limit=10",
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert isinstance(data, list)


# ============================================================================
# Shared State Store Tests
# ============================================================================


class TestSharedStateStore:
    """Tests for shared state store (Redis-backed)."""

    @pytest.mark.asyncio
    async def test_redis_state_store(
        self, app_with_redis: FastAPI | None, app_with_redis_client, auth_headers: dict[str, str]
    ):
        """Test Redis-backed shared state store functionality.

        QA fixes (two independent bugs found in this one test, both only
        ever exercised now - `GATEKEY_TEST_REDIS_URL` was unset in every
        prior environment this suite ran in, so this test silently skipped
        every time and neither bug was ever caught):

        1. Asserted `app_with_redis.state.redis is not None` -
           `main.py`'s lifespan never sets any attribute named `redis` on
           `app.state` (it sets `app.state.shared_state_store` - see
           `main.py`'s Phase 4 lifespan section). `FastAPI`'s `State.
           __getattr__` raises `AttributeError` for any unset attribute
           name (it does not return `None`), so this assertion could never
           have passed even with Redis configured. Fixed to check the real
           attribute name.
        2. Depended on the plain `app_with_redis`/`client` fixtures - `client`
           (from `conftest.py`) is bound to the UNRELATED, non-Redis `app`
           fixture, and `app_with_redis` on its own never has its lifespan
           entered (nothing calls `app_with_redis.router.lifespan_context`),
           so `app.state.shared_state_store` was never even assigned in the
           first place. Fixed to depend on `app_with_redis_client` (this
           same file's fixture that actually drives
           `app_with_redis.router.lifespan_context`), which is what runs
           `main.py`'s lifespan startup and populates `app.state.
           shared_state_store` for real.

        Strengthened beyond the original `is not None` presence check to
        actually exercise the real Redis backend (a set/get round trip
        through `SharedStateStore`'s own interface) and confirm it is
        genuinely the Redis-backed implementation, not the in-process
        default.
        """
        _skip_if_no_redis()
        if app_with_redis is None:
            pytest.skip("Redis app not available")
        # Depending on this fixture (even though its own `client` return
        # value is unused below) is what actually drives
        # `app_with_redis.router.lifespan_context(...)` - see docstring bug 2.
        assert app_with_redis_client is not None

        from gatekey.services.shared_state import RedisSharedStateStore

        store = app_with_redis.state.shared_state_store
        assert store is not None
        assert isinstance(store, RedisSharedStateStore)

        key = f"qa-shared-state-smoke-test:{uuid.uuid4()}"
        await store.set_json(key, {"marker": "ok"}, ttl_seconds=30)
        assert await store.get_json(key) == {"marker": "ok"}
        await store.delete(key)
        assert await store.get_json(key) is None