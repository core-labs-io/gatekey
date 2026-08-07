"""Integration tests for CMR-4's gateway wiring: `resolve_route()`'s custom-
model-cache fallback threaded into `api/v1/gateway/chat.py`/`embeddings.py`,
and the new `custom_model_id`-discriminated cost-computation branch in both.

See `gatekey/custom-model-registry-technical-design.md` section 9.1's
mandatory integration scenarios:

  - "Register + verify a custom model, gateway chat request -> real provider
    call, correct `usage_logs.cost_usd` matching `compute_custom_model_
    cost()` exactly."
  - "Register + verify a custom `capability=embeddings` model on `openai` or
    `vertex_ai`, gateway `/v1/embeddings` request -> succeeds, correct
    cost."
  - "Custom model requested at `/v1/completions` -> 404, never routes."
  - "Chat-capability custom model requested at `/v1/embeddings` (and vice
    versa) -> 422 `UnsupportedRequestError`." (the real, pre-existing
    `errors.UnsupportedRequestError` this shared capability check raises is
    actually HTTP 400 in this codebase, not 422 - confirmed against
    `errors.py` directly; this suite asserts the real status code, not the
    design doc's paraphrase of it.)

The admin CRUD/verify HTTP endpoints (`POST /v1/admin/custom-models`, its
`/verify` sibling, and `main.py`'s cache-warming lifespan wiring) are later
tasks (CMR-5/CMR-6) not yet landed - these tests therefore stand in for
"register + verify" by directly overriding the `get_custom_model_route_
cache` FastAPI dependency with a pre-populated `CustomModelRouteCache`
(exactly the object `load_custom_model_route_snapshot()` + a real admin
`/verify` call would produce once CMR-5/CMR-6 land), the same
`app.dependency_overrides` mechanism `test_self_hosted_pipeline_extra.py`
already uses to stand in for `get_provider_http_client`. This proves the
GATEWAY half (this task's actual scope) exhaustively; the admin-API half is
out of scope here and re-verified end-to-end once CMR-5/CMR-6 land.

Service-account key setup bypasses `POST /v1/admin/service-accounts`
(which requires a team) and instead creates a flat, non-team-attributed key
directly via `services.service_accounts.create_service_account` - the same
helper shape `test_self_hosted_providers_api.py::_make_service_account_
secret` already established - so cost assertions can read the simple flat
`users.current_spend_usd` counter directly, rather than a team-membership
counter.
"""

from __future__ import annotations

import json
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
        "id": "chatcmpl-custom-model-e2e",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 6, "completion_tokens": 4, "total_tokens": 10},
    }


def _canned_embeddings_response(model: str) -> dict:
    return {
        "object": "list",
        "data": [{"object": "embedding", "embedding": [0.1, 0.2, 0.3], "index": 0}],
        "model": model,
        "usage": {"prompt_tokens": 5, "total_tokens": 5},
    }


async def _make_service_account_secret(database_url: str, *, name: str) -> tuple[str, str]:
    """Mirrors `test_self_hosted_providers_api.py`'s identical helper's
    docstring for why this bypasses `POST /v1/admin/service-accounts`.
    Returns `(secret, user_id)` - a flat, non-team-attributed key, so tests
    can independently verify `users.current_spend_usd` was actually
    charged."""

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
        json={"api_key": "sk-custom-model-e2e-test-key"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text


async def _fetch_usage_log(database_url: str, request_model: str) -> asyncpg.Record | None:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchrow(
            "SELECT provider, model, cost_usd, status, success, prompt_tokens, "
            "completion_tokens, self_hosted_provider_id FROM usage_logs "
            "WHERE model = $1 ORDER BY created_at DESC LIMIT 1",
            request_model,
        )
    finally:
        await conn.close()


async def _fetch_user_spend(database_url: str, user_id: str) -> Decimal:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchval("SELECT current_spend_usd FROM users WHERE id = $1", user_id)
    finally:
        await conn.close()


def _chat_entry(**overrides) -> CustomModelCacheEntry:
    kwargs = dict(
        id=uuid.uuid4(),
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id="gpt-4o-custom-model-e2e",
        input_price_per_million_usd=Decimal("2.00"),
        output_price_per_million_usd=Decimal("8.00"),
    )
    kwargs.update(overrides)
    return CustomModelCacheEntry(**kwargs)


def _embeddings_entry(**overrides) -> CustomModelCacheEntry:
    kwargs = dict(
        id=uuid.uuid4(),
        provider="openai",
        capability=ModelCapability.EMBEDDINGS,
        native_model_id="text-embedding-custom-model-e2e",
        input_price_per_million_usd=Decimal("0.05"),
        output_price_per_million_usd=None,
    )
    kwargs.update(overrides)
    return CustomModelCacheEntry(**kwargs)


# ---------------------------------------------------------------------------
# Chat: real per-token cost, via precomputed_cost_usd - not the static or
# self-hosted formula.
# ---------------------------------------------------------------------------


async def test_custom_model_chat_completion_charges_real_per_token_cost(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    """The design doc's own P0 scenario: a custom-model chat request
    dispatches through the UNMODIFIED `openai` provider client (`route.
    provider` carries the real BYOK value, not a sentinel - technical
    design doc section 2.2), and `usage_logs.cost_usd` equals `compute_
    custom_model_cost()`'s exact arithmetic against the row's own
    admin-entered rates - never `PricingEntryMissingError`, never the
    self-hosted GPU-hour proxy."""
    secret, user_id = await _make_service_account_secret(
        migrated_database_url, name="custom-model-chat-e2e-sa"
    )
    await _set_openai_key(client, auth_headers)

    entry = _chat_entry()
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-gpt": entry})

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_canned_chat_response("my-custom-gpt", "hello from custom model"))

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                chat_response = await c.post(
                    "/v1/chat/completions",
                    json={
                        "model": "my-custom-gpt",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"Authorization": f"Bearer {secret}"},
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)
        app.dependency_overrides.pop(get_custom_model_route_cache, None)

    assert chat_response.status_code == 200, chat_response.text
    body = chat_response.json()
    assert body["choices"][0]["message"]["content"] == "hello from custom model"

    # Dispatch: the REAL openai provider client fired, against the row's
    # native_model_id - no new dispatch branch, zero special-casing.
    assert captured["body"]["model"] == "gpt-4o-custom-model-e2e"

    usage_row = await _fetch_usage_log(migrated_database_url, "my-custom-gpt")
    assert usage_row is not None
    assert usage_row["provider"] == "openai"  # the REAL BYOK provider, never a sentinel
    assert usage_row["status"] == "ok"
    assert usage_row["success"] is True
    assert usage_row["prompt_tokens"] == 6
    assert usage_row["completion_tokens"] == 4
    assert usage_row["self_hosted_provider_id"] is None

    expected_cost = compute_custom_model_cost(entry, prompt_tokens=6, completion_tokens=4)
    assert expected_cost == Decimal("2.00") * 6 / Decimal(1_000_000) + Decimal("8.00") * 4 / Decimal(
        1_000_000
    )
    assert Decimal(usage_row["cost_usd"]) == expected_cost

    # Idempotent, single-write cost accounting: the user's current_spend_usd
    # moved by EXACTLY the logged cost, no double-charge.
    user_spend = await _fetch_user_spend(migrated_database_url, user_id)
    assert Decimal(user_spend) == expected_cost


async def test_custom_model_chat_completion_streaming_charges_real_per_token_cost(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    """The streaming branch's cost computation is a SEPARATE code path from
    the non-streaming one (`_sse_event_stream`'s own `precomputed_cost_usd`
    block) - proven independently rather than assumed symmetric."""
    secret, user_id = await _make_service_account_secret(
        migrated_database_url, name="custom-model-stream-e2e-sa"
    )
    await _set_openai_key(client, auth_headers)

    entry = _chat_entry(native_model_id="gpt-4o-custom-model-stream-e2e")
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-gpt-stream": entry})

    def handler(request: httpx.Request) -> httpx.Response:
        lines = [
            'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1700000000,'
            '"model":"my-custom-gpt-stream","choices":[{"index":0,"delta":{"role":"assistant",'
            '"content":"hi"},"finish_reason":null}]}\n\n',
            'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1700000000,'
            '"model":"my-custom-gpt-stream","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
            'data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1700000000,'
            '"model":"my-custom-gpt-stream","choices":[],'
            '"usage":{"prompt_tokens":6,"completion_tokens":4,"total_tokens":10}}\n\n',
            "data: [DONE]\n\n",
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(lines).encode("utf-8"),
        )

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                async with c.stream(
                    "POST",
                    "/v1/chat/completions",
                    json={
                        "model": "my-custom-gpt-stream",
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                        "stream_options": {"include_usage": True},
                    },
                    headers={"Authorization": f"Bearer {secret}"},
                ) as chat_response:
                    assert chat_response.status_code == 200
                    async for _ in chat_response.aiter_bytes():
                        pass
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)
        app.dependency_overrides.pop(get_custom_model_route_cache, None)

    usage_row = await _fetch_usage_log(migrated_database_url, "my-custom-gpt-stream")
    assert usage_row is not None
    assert usage_row["provider"] == "openai"
    assert usage_row["prompt_tokens"] == 6
    assert usage_row["completion_tokens"] == 4

    expected_cost = compute_custom_model_cost(entry, prompt_tokens=6, completion_tokens=4)
    assert Decimal(usage_row["cost_usd"]) == expected_cost

    user_spend = await _fetch_user_spend(migrated_database_url, user_id)
    assert Decimal(user_spend) == expected_cost


# ---------------------------------------------------------------------------
# Embeddings: new wiring (self-hosted never had an embeddings precedent).
# ---------------------------------------------------------------------------


async def test_custom_model_embeddings_charges_real_per_token_cost_no_output_term(
    app: FastAPI,
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    secret, user_id = await _make_service_account_secret(
        migrated_database_url, name="custom-model-embeddings-e2e-sa"
    )
    await _set_openai_key(client, auth_headers)

    entry = _embeddings_entry()
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-embedder": entry})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_canned_embeddings_response("my-custom-embedder"))

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                embeddings_response = await c.post(
                    "/v1/embeddings",
                    json={"model": "my-custom-embedder", "input": "hello"},
                    headers={"Authorization": f"Bearer {secret}"},
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)
        app.dependency_overrides.pop(get_custom_model_route_cache, None)

    assert embeddings_response.status_code == 200, embeddings_response.text

    usage_row = await _fetch_usage_log(migrated_database_url, "my-custom-embedder")
    assert usage_row is not None
    assert usage_row["provider"] == "openai"
    assert usage_row["prompt_tokens"] == 5
    assert usage_row["completion_tokens"] is None

    expected_cost = compute_custom_model_cost(entry, prompt_tokens=5, completion_tokens=None)
    assert expected_cost == Decimal("0.05") * 5 / Decimal(1_000_000)
    assert Decimal(usage_row["cost_usd"]) == expected_cost

    user_spend = await _fetch_user_spend(migrated_database_url, user_id)
    assert Decimal(user_spend) == expected_cost


# ---------------------------------------------------------------------------
# Capability mismatch: existing check fires unchanged (technical design doc
# section 2.2 - resolve_route()'s custom-model branch sets the row's REAL
# capability, never hardcoded).
# ---------------------------------------------------------------------------


async def test_chat_capability_custom_model_rejected_on_embeddings_endpoint(
    app: FastAPI,
    migrated_database_url: str,
) -> None:
    secret, _user_id = await _make_service_account_secret(
        migrated_database_url, name="chat-only-on-embeddings-sa"
    )
    cache = CustomModelRouteCache()
    cache.set_all({"my-chat-only-custom-model": _chat_entry()})

    app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                response = await c.post(
                    "/v1/embeddings",
                    json={"model": "my-chat-only-custom-model", "input": "hi"},
                    headers={"Authorization": f"Bearer {secret}"},
                )
    finally:
        app.dependency_overrides.pop(get_custom_model_route_cache, None)

    # The real, pre-existing `errors.UnsupportedRequestError` status code is
    # 400 in this codebase (confirmed against `errors.py` directly) - not
    # the 422 the design doc's error-handling table paraphrases it as.
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "unsupported_request"


async def test_embeddings_capability_custom_model_rejected_on_chat_endpoint(
    app: FastAPI,
    migrated_database_url: str,
) -> None:
    secret, _user_id = await _make_service_account_secret(
        migrated_database_url, name="embeddings-only-on-chat-sa"
    )
    cache = CustomModelRouteCache()
    cache.set_all({"my-embeddings-only-custom-model": _embeddings_entry()})

    app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                response = await c.post(
                    "/v1/chat/completions",
                    json={
                        "model": "my-embeddings-only-custom-model",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"Authorization": f"Bearer {secret}"},
                )
    finally:
        app.dependency_overrides.pop(get_custom_model_route_cache, None)

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "unsupported_request"


# ---------------------------------------------------------------------------
# /v1/completions never routes a custom model - structural enforcement
# (completions.py has no `custom_model_cache` dependency at all, so
# overriding the dependency PROVIDER here has zero effect on it - this test
# proves that structural claim directly, not just "believed true because
# the code was never touched").
# ---------------------------------------------------------------------------


async def test_custom_model_never_routes_at_completions_endpoint(
    app: FastAPI,
    migrated_database_url: str,
) -> None:
    secret, _user_id = await _make_service_account_secret(
        migrated_database_url, name="custom-model-completions-sa"
    )
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-gpt-completions": _chat_entry(provider="openai")})

    app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                response = await c.post(
                    "/v1/completions",
                    json={"model": "my-custom-gpt-completions", "prompt": "hi"},
                    headers={"Authorization": f"Bearer {secret}"},
                )
    finally:
        app.dependency_overrides.pop(get_custom_model_route_cache, None)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "model_not_found"
