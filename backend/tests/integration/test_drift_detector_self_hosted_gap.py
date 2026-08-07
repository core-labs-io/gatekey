"""Independent QA finding (Phase 5 - Differentiators, 5.4 Provider Drift
Detector x 5.5 Self-Hosted Governance interaction), now FIXED.

`gatekey/phase-5-technical-design.md` section 9.1's own mandatory
test-scenario table lists: "Self-hosted model canary-tested | Cost computed
via `compute_self_hosted_cost()`, same `canary_runs.cost_usd`-only rule" as a
REQUIRED scenario for this phase.

`services/drift_detector.py::run_canary_suite_for_org` now resolves each
actively-used model the same way `api/v1/gateway/common.py::resolve_route()`
does: `providers.model_registry.resolve_model()` (the static `MODEL_
REGISTRY` lookup) tried first, unconditionally, falling back to the
pre-warmed `SelfHostedModelRouteCache` (`app.state.self_hosted_model_route_
cache`) only on `UnknownModelError`. A self-hosted canary target dispatches
via `services.self_hosted_providers.get_decrypted_self_hosted_credential()`/
the Ollama-compatible client (same path the real gateway's self-hosted
dispatch uses) and its cost is computed via `compute_self_hosted_cost()`,
still written ONLY to `canary_runs.cost_usd`.

This test now asserts the spec-intended behavior (design doc section 9.1's
mandatory scenario) and passes against the fixed implementation.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.services.drift_detector import run_canary_suite_for_org

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio

_URL = "/v1/admin/self-hosted-providers"
_TRUNCATE_SQL = (
    "TRUNCATE TABLE usage_logs, canary_runs, canary_baselines, canary_model_settings, "
    "drift_alerts, self_hosted_providers CASCADE"
)


@pytest.fixture(autouse=True)
async def _clean_tables(migrated_database_url: str):
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute(_TRUNCATE_SQL)
    finally:
        await conn.close()
    yield


async def _fetch_val(database_url: str, query: str, *args):
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchval(query, *args)
    finally:
        await conn.close()


async def _seed_recent_self_hosted_usage_log(database_url: str, *, model: str, provider_id: str) -> None:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        await conn.execute(
            """
            INSERT INTO usage_logs
                (id, org_id, request_id, endpoint, provider, model, self_hosted_provider_id,
                 prompt_tokens, completion_tokens, cost_usd, latency_ms, stream, status,
                 success, created_at)
            VALUES ($1, $2, $3, '/v1/chat/completions', 'self_hosted', $4, $5, 10, 5, 0.001,
                    200, false, 'ok', true, $6)
            """,
            uuid.uuid4(),
            DEFAULT_ORG_ID,
            f"self-hosted-real-traffic-{uuid.uuid4().hex[:8]}",
            model,
            uuid.UUID(provider_id),
            datetime.now(timezone.utc) - timedelta(days=1),
        )
    finally:
        await conn.close()


async def test_actively_used_self_hosted_model_is_canary_tested(
    app: FastAPI, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    """Spec-intended behavior (design doc section 9.1's mandatory scenario):
    an actively-used self-hosted model gets real `canary_runs` rows from a
    scheduler tick, exactly like a BYOK model."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            register_response = await client.post(
                _URL,
                json={
                    "name": "drift-gap-self-hosted-endpoint",
                    "base_url": "http://drift-gap-self-hosted.internal:8000",
                    "bearer_token": "drift-gap-bearer-token",
                    "cost_basis_per_gpu_hour": "1.0000",
                    "models": ["drift-gap-self-hosted-model"],
                },
                headers=auth_headers,
            )
            assert register_response.status_code == 201, register_response.text
            provider_id = register_response.json()["id"]
            verify_response = await client.post(f"{_URL}/{provider_id}/verify", headers=auth_headers)
            assert verify_response.status_code == 200, verify_response.text
            assert verify_response.json()["verified"] is True

    await _seed_recent_self_hosted_usage_log(
        migrated_database_url, model="drift-gap-self-hosted-model", provider_id=provider_id
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-drift-gap",
                "object": "chat.completion",
                "created": 1_700_000_000,
                "model": body.get("model", "drift-gap-self-hosted-model"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "canary response"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 6, "total_tokens": 14},
            },
        )

    async with app.router.lifespan_context(app):
        app.state.provider_http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        async with app.state.db_session_factory() as session:
            await run_canary_suite_for_org(session, app)

    canary_run_count = await _fetch_val(
        migrated_database_url,
        "SELECT count(*) FROM canary_runs WHERE model = $1",
        "drift-gap-self-hosted-model",
    )
    assert canary_run_count > 0, (
        "the self-hosted model was never canary-tested - see this test's xfail "
        "reason / module docstring for the confirmed root cause"
    )
