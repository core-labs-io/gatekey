"""Integration tests (US-G8): a full `POST /v1/chat/completions` request
routed through an Ollama/OpenRouter gateway-facing model key, against a
real Postgres and a stubbed/mocked provider HTTP server (never a live
Ollama instance or live OpenRouter API in CI) - confirms routing, cost
(`$0` for Ollama / real pass-through pricing for OpenRouter), and usage
logging behave identically to the existing 3-provider integration tests.

Deliberately bypasses `POST /v1/admin/service-accounts` (a pre-existing,
unrelated bug in that route/fixture - see
`tests/integration/test_service_accounts_api.py`'s `_truncate_service_
account_keys` fixture - makes it return 422 instead of 201 in this test
session) by creating the user + service-account key directly via the
service layer against the same migrated database, using a throwaway
engine/session independent of the app under test. Everything downstream
of that (provider-key admin API, model resolution, model-policy check,
budget check, credential decrypt, the actual dispatch into
`providers/ollama.py` / `providers/openrouter.py`, cost computation, and
usage-log persistence) runs for real, unmocked, against real Postgres -
only the outbound HTTP call to the "provider" itself is intercepted via
`httpx.MockTransport`, substituted for `app.state.provider_http_client`
via a FastAPI dependency override.
"""

from __future__ import annotations

import json
from decimal import Decimal

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from gatekey.api.deps import get_provider_http_client
from gatekey.db.session import create_engine as db_create_engine
from gatekey.db.session import create_session_factory
from gatekey.services.service_accounts import create_service_account
from gatekey.services.users import create_user

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio


async def _make_service_account_secret(database_url: str) -> str:
    """Create a user + service-account key directly via the service layer,
    against a throwaway engine bound to the already-migrated test database.

    See module docstring for why this bypasses `POST /v1/admin/
    service-accounts` rather than using it.
    """

    class _StubSettings:
        DATABASE_URL = database_url

    engine = db_create_engine(_StubSettings())  # type: ignore[arg-type]
    session_factory: async_sessionmaker = create_session_factory(engine)
    try:
        async with session_factory() as session:
            user = await create_user(session, name="us-g8-test-user")
            _row, secret = await create_service_account(session, "us-g8-test-sa", user.id)
            return secret
    finally:
        await engine.dispose()


def _mock_client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _fetch_usage_log(database_url: str, request_model: str) -> asyncpg.Record | None:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchrow(
            "SELECT provider, model, cost_usd, status, success, prompt_tokens, completion_tokens "
            "FROM usage_logs WHERE model = $1 ORDER BY created_at DESC LIMIT 1",
            request_model,
        )
    finally:
        await conn.close()


def _canned_openai_shaped_response(model: str, content: str) -> dict:
    return {
        "id": "chatcmpl-us-g8",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    }


async def test_gateway_chat_completion_routed_through_ollama_stubbed_server(
    app: FastAPI,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    secret = await _make_service_account_secret(migrated_database_url)

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json=_canned_openai_shaped_response("llama3.1", "hello from stubbed ollama")
        )

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                put_response = await client.put(
                    "/v1/admin/providers/ollama/key",
                    json={"base_url": "http://ollama-stub.internal:11434"},
                    headers=auth_headers,
                )
                assert put_response.status_code == 200

                response = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "ollama/llama3.1",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"Authorization": f"Bearer {secret}"},
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "hello from stubbed ollama"

    # Routing: the stubbed server actually received the request at the
    # admin-configured base_url, not a hardcoded/fixed URL.
    assert captured["url"] == "http://ollama-stub.internal:11434/v1/chat/completions"
    assert captured["body"]["model"] == "llama3.1"

    # Cost: Ollama is $0.00, not merely "unmetered"/None - a confirmed,
    # present $0 charge (see providers/pricing.py).
    usage_row = await _fetch_usage_log(migrated_database_url, "ollama/llama3.1")
    assert usage_row is not None
    assert usage_row["provider"] == "ollama"
    assert usage_row["status"] == "ok"
    assert usage_row["success"] is True
    assert usage_row["prompt_tokens"] == 4
    assert usage_row["completion_tokens"] == 3
    assert Decimal(usage_row["cost_usd"]) == Decimal("0")


async def test_gateway_chat_completion_routed_through_openrouter_stubbed_server(
    app: FastAPI,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    secret = await _make_service_account_secret(migrated_database_url)

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        return httpx.Response(
            200,
            json=_canned_openai_shaped_response("openai/gpt-4o-mini", "hello from stubbed openrouter"),
        )

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                put_response = await client.put(
                    "/v1/admin/providers/openrouter/key",
                    json={"api_key": "sk-or-us-g8-test"},
                    headers=auth_headers,
                )
                assert put_response.status_code == 200

                response = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "openrouter/openai/gpt-4o-mini",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"Authorization": f"Bearer {secret}"},
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "hello from stubbed openrouter"

    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["authorization"] == "Bearer sk-or-us-g8-test"

    usage_row = await _fetch_usage_log(migrated_database_url, "openrouter/openai/gpt-4o-mini")
    assert usage_row is not None
    assert usage_row["provider"] == "openrouter"
    assert usage_row["status"] == "ok"
    assert usage_row["success"] is True
    # Real, non-zero pass-through pricing (input $0.15 / output $0.60 per
    # million tokens): 4 prompt + 3 completion tokens, computed exactly.
    expected_cost = (Decimal(4) * Decimal("0.15") + Decimal(3) * Decimal("0.60")) / Decimal("1000000")
    assert Decimal(usage_row["cost_usd"]) == expected_cost
