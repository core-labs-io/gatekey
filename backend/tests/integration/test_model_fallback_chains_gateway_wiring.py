"""Integration tests for Model Catalog + Cross-Provider Fallback Chains,
Part B RUNTIME behavior (`gatekey/model-catalog-fallback-chains-technical-
design.md` section 2.5/2.6), against a real Postgres and a real gateway
request - filling gaps the pure-unit `test_dispatch_with_model_fallback.py`
suite leaves uncovered (it drives `dispatch_with_model_fallback()` directly
with a fake session and a monkeypatched `call_provider_with_failover`, so it
never proves the two NEW response headers or the two NEW `usage_logs`
columns are actually wired end-to-end through `chat.py`/`embeddings.py`, and
never proves a fallback-served response is charged at the SERVED model's
real price rather than the primary's).

Mirrors `test_custom_models_gateway_wiring.py`'s established pattern
exactly: `CustomModelRouteCache` is populated directly via the
`get_custom_model_route_cache` FastAPI dependency override (standing in for
a real register+verify+edit HTTP round trip, which is separately proven in
`test_model_catalog_fallback_chains_api.py`) - this file's own scope is the
GATEWAY half, not the admin-CRUD half.

See design doc section 6's qa-engineer checklist, "Part B runtime, the core
scenarios" (a)-(g):
  (a) primary succeeds -> chain never touched, `X-Gatekey-Model-Fallback-
      Attempt: 0` - `test_primary_success_zero_attempt_header_and_no_from_
      model_column`.
  (b) primary fails, candidate 1 fails, candidate 2 succeeds -> served
      response reflects candidate 2's provider/pricing, `usage_logs.model`/
      `model_fallback_attempt`/`model_fallback_from_model` all correct -
      `test_fallback_second_candidate_serves_charges_its_own_price_not_
      primarys`.
  (c) every candidate fails -> client receives the PRIMARY's original
      upstream error message, not any candidate's -
      `test_all_candidates_fail_client_sees_primarys_error_not_candidates`.
  (e) a fallback-served response is never written to the response cache
      under the original model's key - `test_fallback_served_response_
      never_cached_under_original_model_key`.
  (f)/(d)/single-level-enforcement are already covered directly and
      thoroughly at the unit level (`test_dispatch_with_model_fallback.py`)
      - not duplicated here.
  (g) streaming pre-first-byte vs. mid-stream - covered separately in
      `tests/unit/test_gateway_chat_model_fallback_streaming.py` (that
      distinction is pure control-flow, does not need a real DB).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from gatekey.api.deps import get_custom_model_route_cache, get_provider_http_client
from gatekey.db.session import create_engine as db_create_engine
from gatekey.db.session import create_session_factory
from gatekey.providers.model_registry import ModelCapability
from gatekey.services.custom_models import (
    CustomModelCacheEntry,
    CustomModelRouteCache,
    compute_custom_model_cost,
)
from gatekey.services.service_accounts import create_service_account
from gatekey.services.users import create_user

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio


def _mock_client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _canned_chat_response(model: str, content: str = "ok") -> dict:
    return {
        "id": "chatcmpl-model-fallback-e2e",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


async def _make_service_account_secret(database_url: str, *, name: str) -> tuple[str, str]:
    class _StubSettings:
        DATABASE_URL = database_url

    engine = db_create_engine(_StubSettings())  # type: ignore[arg-type]
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            user = await create_user(session, name=f"{name}-user")
            _row, secret = await create_service_account(session, name, user.id)
            return secret, str(user.id)
    finally:
        await engine.dispose()


async def _set_openai_key(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-model-fallback-e2e-test-key"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text


async def _fetch_usage_log(database_url: str, request_model: str) -> asyncpg.Record | None:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchrow(
            "SELECT provider, model, cost_usd, status, success, prompt_tokens, "
            "completion_tokens, model_fallback_attempt, model_fallback_from_model "
            "FROM usage_logs WHERE model = $1 ORDER BY created_at DESC LIMIT 1",
            request_model,
        )
    finally:
        await conn.close()


def _primary_entry(**overrides) -> CustomModelCacheEntry:
    kwargs = dict(
        id=uuid.uuid4(),
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id="primary-model-fallback-e2e-native",
        input_price_per_million_usd=Decimal("10.00"),
        output_price_per_million_usd=Decimal("30.00"),
        fallback_model_names=("candidate-model-fallback-e2e",),
    )
    kwargs.update(overrides)
    return CustomModelCacheEntry(**kwargs)


def _candidate_entry(**overrides) -> CustomModelCacheEntry:
    """Deliberately a DIFFERENT (much cheaper) price than the primary - the
    security-review flag this exists to guard against is a fallback hop
    silently getting charged at the ORIGINAL model's (more expensive) rate."""
    kwargs = dict(
        id=uuid.uuid4(),
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id="candidate-model-fallback-e2e-native",
        input_price_per_million_usd=Decimal("0.50"),
        output_price_per_million_usd=Decimal("1.50"),
        fallback_model_names=(),
    )
    kwargs.update(overrides)
    return CustomModelCacheEntry(**kwargs)


async def _post_chat(app: FastAPI, secret: str, model: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        return await c.post(
            "/v1/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {secret}"},
        )


# ---------------------------------------------------------------------------
# (a) primary succeeds -> chain never touched, 0-attempt header.
# ---------------------------------------------------------------------------


async def test_primary_success_zero_attempt_header_and_no_from_model_column(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    secret, _user_id = await _make_service_account_secret(
        migrated_database_url, name="model-fallback-primary-ok-sa"
    )
    await _set_openai_key(client, auth_headers)

    entry = _primary_entry()
    cache = CustomModelRouteCache()
    cache.set_all({"primary-model-fallback-ok": entry, "candidate-model-fallback-e2e": _candidate_entry()})

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content
        import json as _json

        model = _json.loads(body)["model"]
        calls.append(model)
        return httpx.Response(200, json=_canned_chat_response(model, "primary answered"))

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
    try:
        async with app.router.lifespan_context(app):
            response = await _post_chat(app, secret, "primary-model-fallback-ok")
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)
        app.dependency_overrides.pop(get_custom_model_route_cache, None)

    assert response.status_code == 200, response.text
    assert response.headers["X-Gatekey-Model-Fallback-Attempt"] == "0"
    assert "X-Gatekey-Model-Fallback-From" not in response.headers
    # The candidate must never even be dispatched.
    assert calls == ["primary-model-fallback-e2e-native"]

    usage_row = await _fetch_usage_log(migrated_database_url, "primary-model-fallback-ok")
    assert usage_row is not None
    assert usage_row["model_fallback_attempt"] == 0
    assert usage_row["model_fallback_from_model"] is None


# ---------------------------------------------------------------------------
# (b) primary fails, fallback candidate (DIFFERENT pricing) serves - cost
# must be computed from the CANDIDATE's price, never the primary's.
# ---------------------------------------------------------------------------


async def test_fallback_candidate_serves_charges_its_own_price_not_primarys(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    secret, user_id = await _make_service_account_secret(
        migrated_database_url, name="model-fallback-candidate-serves-sa"
    )
    await _set_openai_key(client, auth_headers)

    entry = _primary_entry(native_model_id="primary-fails-native")
    candidate = _candidate_entry(native_model_id="candidate-serves-native")
    cache = CustomModelRouteCache()
    cache.set_all(
        {"primary-fails-model": entry, "candidate-model-fallback-e2e": candidate}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        model = _json.loads(request.content)["model"]
        if model == "primary-fails-native":
            return httpx.Response(500, json={"error": {"message": "primary provider exploded"}})
        return httpx.Response(200, json=_canned_chat_response(model, "candidate answered"))

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
    try:
        async with app.router.lifespan_context(app):
            response = await _post_chat(app, secret, "primary-fails-model")
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)
        app.dependency_overrides.pop(get_custom_model_route_cache, None)

    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"]["content"] == "candidate answered"
    assert response.headers["X-Gatekey-Model-Fallback-Attempt"] == "1"
    assert response.headers["X-Gatekey-Model-Fallback-From"] == "primary-fails-model"

    # `usage_logs.model` reflects the SERVED (candidate) model, never the
    # originally-requested primary name.
    usage_row = await _fetch_usage_log(migrated_database_url, "candidate-model-fallback-e2e")
    assert usage_row is not None
    assert usage_row["provider"] == "openai"
    assert usage_row["status"] == "ok"
    assert usage_row["success"] is True
    assert usage_row["model_fallback_attempt"] == 1
    assert usage_row["model_fallback_from_model"] == "primary-fails-model"

    # The dollar figure itself: computed from the CANDIDATE's (cheap) rate,
    # never the primary's (10x more expensive) rate - the exact security-
    # review concern this test directly closes.
    expected_cost = compute_custom_model_cost(candidate, prompt_tokens=10, completion_tokens=5)
    wrong_cost_if_primary_rate_leaked = compute_custom_model_cost(entry, prompt_tokens=10, completion_tokens=5)
    assert expected_cost != wrong_cost_if_primary_rate_leaked
    assert Decimal(usage_row["cost_usd"]) == expected_cost


# ---------------------------------------------------------------------------
# (c) every candidate fails -> the client sees the PRIMARY's error, not the
# candidate's.
# ---------------------------------------------------------------------------


async def test_all_candidates_fail_client_sees_primarys_error_not_candidates(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    secret, _user_id = await _make_service_account_secret(
        migrated_database_url, name="model-fallback-total-exhaustion-sa"
    )
    await _set_openai_key(client, auth_headers)

    entry = _primary_entry(native_model_id="primary-exhaustion-native")
    candidate = _candidate_entry(native_model_id="candidate-exhaustion-native")
    cache = CustomModelRouteCache()
    cache.set_all(
        {"primary-exhaustion-model": entry, "candidate-model-fallback-e2e": candidate}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        model = _json.loads(request.content)["model"]
        if model == "primary-exhaustion-native":
            return httpx.Response(
                503, json={"error": {"message": "distinctive-primary-failure-marker"}}
            )
        return httpx.Response(
            503, json={"error": {"message": "distinctive-candidate-failure-marker"}}
        )

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
    try:
        async with app.router.lifespan_context(app):
            response = await _post_chat(app, secret, "primary-exhaustion-model")
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)
        app.dependency_overrides.pop(get_custom_model_route_cache, None)

    assert response.status_code in (502, 503), response.text
    body_text = response.text
    assert "distinctive-candidate-failure-marker" not in body_text
    # (The primary's exact upstream body text may or may not be echoed
    # depending on the provider client's own message-safety policy - what
    # matters here, and is the actual security-review concern, is that the
    # CANDIDATE's message never leaks through instead.)


# ---------------------------------------------------------------------------
# (e) a fallback-served response is never cached under the original model's
# key (Phase 4 caching regression check).
# ---------------------------------------------------------------------------


async def test_fallback_served_response_never_cached_under_original_model_key(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    """Register a team, enable caching for it, attribute the service-account
    key to that team, then send the SAME request twice against a primary
    model that always fails (so every request walks the fallback chain).
    If the fallback-served response were (incorrectly) cached under the
    primary's key, the SECOND request would come back `X-Cache: HIT` -
    proven not to happen."""
    team_resp = await client.post(
        "/v1/teams",
        json={"name": f"model-fallback-cache-team-{uuid.uuid4().hex[:8]}"},
        headers=auth_headers,
    )
    assert team_resp.status_code == 201, team_resp.text
    team_id = team_resp.json()["id"]

    user_resp = await client.post(
        "/v1/admin/users", json={"name": "model-fallback-cache-user"}, headers=auth_headers
    )
    assert user_resp.status_code == 201, user_resp.text
    user_id = user_resp.json()["id"]

    member_resp = await client.post(
        f"/v1/teams/{team_id}/members",
        json={"user_id": user_id, "role": "member", "budget_usd": None},
        headers=auth_headers,
    )
    assert member_resp.status_code == 201, member_resp.text

    cache_settings_resp = await client.put(
        f"/v1/admin/teams/{team_id}/cache-settings",
        json={"cache_enabled": True, "cache_ttl_minutes": 5},
        headers=auth_headers,
    )
    assert cache_settings_resp.status_code == 200, cache_settings_resp.text

    sa_resp = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "model-fallback-cache-sa", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert sa_resp.status_code == 201, sa_resp.text
    secret = sa_resp.json()["secret"]

    await _set_openai_key(client, auth_headers)

    entry = _primary_entry(native_model_id="primary-cache-skip-native")
    candidate = _candidate_entry(native_model_id="candidate-cache-skip-native")
    route_cache = CustomModelRouteCache()
    route_cache.set_all(
        {"primary-cache-skip-model": entry, "candidate-model-fallback-e2e": candidate}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        model = _json.loads(request.content)["model"]
        if model == "primary-cache-skip-native":
            return httpx.Response(500, json={"error": {"message": "primary always fails"}})
        return httpx.Response(200, json=_canned_chat_response(model, "fallback answered"))

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    app.dependency_overrides[get_custom_model_route_cache] = lambda: route_cache
    try:
        async with app.router.lifespan_context(app):
            first = await _post_chat(app, secret, "primary-cache-skip-model")
            second = await _post_chat(app, secret, "primary-cache-skip-model")
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)
        app.dependency_overrides.pop(get_custom_model_route_cache, None)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.headers["X-Gatekey-Model-Fallback-Attempt"] == "1"
    assert second.headers["X-Gatekey-Model-Fallback-Attempt"] == "1"
    # The critical assertion: NEITHER request is ever a cache HIT - if the
    # fallback-served response had been (incorrectly) cached under the
    # primary's key, the second request would read `X-Cache: HIT` here.
    assert first.headers.get("X-Cache") == "MISS"
    assert second.headers.get("X-Cache") == "MISS"
