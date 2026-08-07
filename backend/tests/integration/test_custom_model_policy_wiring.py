"""Integration tests for CMR-5: widening `services.model_policy.set_policy`/
`set_team_model_policy`'s "known model id" validation to also accept a
verified custom model's name via `CustomModelRouteCache.known_model_ids()`
(technical design doc section 3.2/5 rows 13-14).

The admin CRUD/verify HTTP endpoints for `custom_models`
(`POST /v1/admin/custom-models` etc.) are a later task (CMR-6, not yet
landed) - these tests therefore stand in for "register + verify" the same
way `test_custom_models_gateway_wiring.py` already does: overriding the
`get_custom_model_route_cache` FastAPI dependency with a pre-populated
`CustomModelRouteCache` (exactly what `load_custom_model_route_snapshot()` +
a real admin `/verify` call would produce once CMR-6 lands).

A name never placed in the cache stands in for "registered but not yet
verified" (or never registered at all) - `CustomModelRouteCache.
known_model_ids()` only ever contains `verified=true` rows by construction
(confirmed directly: `services.custom_models.load_custom_model_route_
snapshot()`'s query has a hardcoded `WHERE ... verified.is_(True)` clause,
and `CustomModelRouteCache`'s own docstring states every entry it ever holds
is therefore already verified) - so this is a faithful proxy for the real
invariant, not a shortcut around it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from gatekey.api.deps import get_custom_model_route_cache
from gatekey.providers.model_registry import ModelCapability
from gatekey.services.custom_models import CustomModelCacheEntry, CustomModelRouteCache

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _truncate_policy_tables(migrated_database_url: str):
    """Ensure each test starts with no `model_policies`/`team_model_policies`
    rows - same per-file truncation precedent `test_model_policy_api.py`'s
    `_truncate_model_policies` establishes."""
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute("TRUNCATE TABLE model_policies")
        await conn.execute("TRUNCATE TABLE team_model_policies")
    finally:
        await conn.close()
    yield


def _verified_chat_entry(**overrides) -> CustomModelCacheEntry:
    kwargs = dict(
        id=uuid.uuid4(),
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id="gpt-4o-cmr5-policy-e2e",
        input_price_per_million_usd=Decimal("2.00"),
        output_price_per_million_usd=Decimal("8.00"),
    )
    kwargs.update(overrides)
    return CustomModelCacheEntry(**kwargs)


# ---------------------------------------------------------------------------
# Org-level policy: `PUT /v1/admin/model-policy`
# ---------------------------------------------------------------------------


async def test_org_model_policy_accepts_verified_custom_model_name(
    app: FastAPI, auth_headers: dict[str, str]
) -> None:
    """Technical design doc section 3.2/9.1: a verified custom model's name
    is addable to the org model-access policy exactly like any static or
    self-hosted model - no special-casing (previously this would have been
    rejected as `unknown_model_in_policy`)."""
    cache = CustomModelRouteCache()
    cache.set_all({"my-verified-custom-model": _verified_chat_entry()})

    app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.put(
                    "/v1/admin/model-policy",
                    json={"mode": "allowlist", "models": ["my-verified-custom-model"]},
                    headers=auth_headers,
                )
    finally:
        app.dependency_overrides.pop(get_custom_model_route_cache, None)

    assert response.status_code == 200, response.text
    assert response.json()["models"] == ["my-verified-custom-model"]


async def test_org_model_policy_rejects_unverified_custom_model_name(
    app: FastAPI, auth_headers: dict[str, str]
) -> None:
    """A custom model that was never placed in `CustomModelRouteCache`
    (registered-but-unverified, or never registered) is STILL rejected as
    unknown - proving the widening isn't accidentally permissive."""
    cache = CustomModelRouteCache()  # empty - nothing verified/routable

    app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.put(
                    "/v1/admin/model-policy",
                    json={"mode": "allowlist", "models": ["my-unverified-custom-model"]},
                    headers=auth_headers,
                )
    finally:
        app.dependency_overrides.pop(get_custom_model_route_cache, None)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "unknown_model_in_policy"
    assert "my-unverified-custom-model" in response.json()["error"]["message"]


async def test_org_model_policy_rejects_custom_model_removed_from_cache(
    app: FastAPI, auth_headers: dict[str, str]
) -> None:
    """A name that WAS verified/routable and then disappeared from the cache
    (removed, or edited in a way that reset `verified`) is rejected the same
    way - the cache's CURRENT membership, not any point-in-time history, is
    the sole source of truth this validation consults."""
    cache = CustomModelRouteCache()
    cache.set_all({"was-verified-then-removed": _verified_chat_entry()})
    cache.set_all({})  # simulates removal / a verified -> False edit

    app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                response = await client.put(
                    "/v1/admin/model-policy",
                    json={"mode": "allowlist", "models": ["was-verified-then-removed"]},
                    headers=auth_headers,
                )
    finally:
        app.dependency_overrides.pop(get_custom_model_route_cache, None)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "unknown_model_in_policy"


async def test_org_model_policy_put_with_no_custom_model_cache_wired_is_unaffected(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Regression gate (technical design doc section 9.3): the real
    `get_custom_model_route_cache` dependency (an EMPTY, freshly-warmed
    cache, since this test never registers anything) is threaded through
    with no override at all here - byte-for-byte pre-feature behavior for
    an org that never configures a custom model."""
    response = await client.put(
        "/v1/admin/model-policy",
        json={"mode": "allowlist", "models": ["gpt-4o"]},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["models"] == ["gpt-4o"]

    rejected = await client.put(
        "/v1/admin/model-policy",
        json={"mode": "allowlist", "models": ["totally-unknown-model"]},
        headers=auth_headers,
    )
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "unknown_model_in_policy"


# ---------------------------------------------------------------------------
# Team-level narrowing overlay: `PUT /v1/teams/{team_id}/model-restrictions`
# ---------------------------------------------------------------------------


async def test_team_model_restrictions_accepts_verified_custom_model_name(
    app: FastAPI, auth_headers: dict[str, str]
) -> None:
    """`set_team_model_policy`'s identical widening (technical design doc
    section 5 rows 13-14) - a verified custom model's name narrows a team's
    overlay exactly like any static/self-hosted model. Uses the break-glass
    admin token's `org_admin` bypass (`require_team_role`'s
    `org_admin_bypass`) rather than a real team-lead session - same
    precedent `test_phase2_governance_api.py::test_break_glass_token_
    drives_phase2_admin_surfaces` already establishes for team-scoped
    routes."""
    cache = CustomModelRouteCache()
    cache.set_all({"my-verified-team-custom-model": _verified_chat_entry()})

    app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                team_response = await client.post(
                    "/v1/teams", json={"name": "cmr5-policy-team"}, headers=auth_headers
                )
                assert team_response.status_code == 201, team_response.text
                team_id = team_response.json()["id"]

                response = await client.put(
                    f"/v1/teams/{team_id}/model-restrictions",
                    json={"models": ["my-verified-team-custom-model"]},
                    headers=auth_headers,
                )
    finally:
        app.dependency_overrides.pop(get_custom_model_route_cache, None)

    assert response.status_code == 200, response.text
    assert response.json()["team_restriction"] == ["my-verified-team-custom-model"]


async def test_team_model_restrictions_rejects_unverified_custom_model_name(
    app: FastAPI, auth_headers: dict[str, str]
) -> None:
    """Mirrors the org-level rejection test above, for the team overlay -
    a never-verified/never-registered custom model name is rejected, this
    time via `TeamModelRestrictsOrgDeniedModelError` (`set_team_model_
    policy`'s own "unknown or org-denied" combined check), not
    `UnknownModelInPolicyError`."""
    cache = CustomModelRouteCache()  # empty - nothing verified/routable

    app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                team_response = await client.post(
                    "/v1/teams", json={"name": "cmr5-policy-team-2"}, headers=auth_headers
                )
                assert team_response.status_code == 201, team_response.text
                team_id = team_response.json()["id"]

                response = await client.put(
                    f"/v1/teams/{team_id}/model-restrictions",
                    json={"models": ["my-unverified-team-custom-model"]},
                    headers=auth_headers,
                )
    finally:
        app.dependency_overrides.pop(get_custom_model_route_cache, None)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "team_model_restricts_org_denied_model"
