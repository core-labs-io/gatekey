"""Integration tests for Phase 5 (Differentiators, 5.4 Provider Drift
Detector) against a real Postgres instance and a mocked provider HTTP
transport (never a live provider API) - AC5.4.1/AC5.4.9/AC5.4.11, and the
design doc's own NFR acceptance test (section 10): "Canary cost is tracked
wholly separately from user-attributable usage."

Everything downstream of the mocked outbound HTTP call runs for real:
provider-key admin API, credential decrypt, `services.drift_detector`'s
actual dispatch/cost-computation/persistence logic, against real Postgres -
mirrors `tests/integration/test_gateway_ollama_openrouter.py`'s established
mocking pattern (`httpx.MockTransport` substituted for `app.state.
provider_http_client`), but drives `services.drift_detector.
run_canary_suite_for_org` directly (the scheduler-tick call path) rather
than a gateway request.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.canary_run import CanaryRun
from gatekey.services.drift_detector import establish_baseline_if_ready, flag_drift, run_canary_suite_for_org

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio

# The 5 fixed, code/migration-seeded canary prompts (`alembic/versions/
# 0039_create_drift_detector_tables.py`) - stable literal UUIDs.
_FACTUAL_PROMPT_ID = uuid.UUID("00000000-0000-0000-0000-000000000101")

_TRUNCATE_SQL = (
    "TRUNCATE TABLE usage_logs, canary_runs, canary_baselines, canary_model_settings, "
    "drift_alerts, users, audit_entries CASCADE"
)


@pytest_asyncio.fixture
async def sf(migrated_database_url: str):
    engine = create_async_engine(migrated_database_url, pool_size=5, max_overflow=10)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _clean_drift_tables(migrated_database_url: str):
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute(_TRUNCATE_SQL)
    finally:
        await conn.close()
    yield


async def _execute(database_url: str, query: str, *args) -> None:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        await conn.execute(query, *args)
    finally:
        await conn.close()


async def _fetch_val(database_url: str, query: str, *args):
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchval(query, *args)
    finally:
        await conn.close()


async def _fetch_row(database_url: str, query: str, *args):
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchrow(query, *args)
    finally:
        await conn.close()


async def _fetch_all(database_url: str, query: str, *args):
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetch(query, *args)
    finally:
        await conn.close()


async def _seed_recent_real_usage_log(database_url: str, *, model: str) -> None:
    """AC5.4.8's "actively used" definition - >=1 real (non-canary)
    `usage_logs` row in the trailing 7 days. Inserted directly (not via a
    live gateway request) so this test controls exactly what "actively
    used" means without depending on the rest of the gateway pipeline."""
    await _execute(
        database_url,
        """
        INSERT INTO usage_logs
            (id, org_id, request_id, endpoint, provider, model, prompt_tokens,
             completion_tokens, cost_usd, latency_ms, stream, status, success, created_at)
        VALUES ($1, $2, $3, '/v1/chat/completions', 'openai', $4, 10, 5, 0.001, 200,
                false, 'ok', true, $5)
        """,
        uuid.uuid4(),
        DEFAULT_ORG_ID,
        f"real-traffic-{uuid.uuid4().hex[:8]}",
        model,
        datetime.now(timezone.utc) - timedelta(days=1),
    )


def _canned_openai_response(model: str, content: str) -> dict:
    return {
        "id": "chatcmpl-drift-canary",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 8, "completion_tokens": 6, "total_tokens": 14},
    }


async def _register_openai_key(client, auth_headers: dict[str, str]) -> None:
    response = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-test-canary-key-1234567890"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text


# --- Admin surface smoke tests ----------------------------------------------


async def test_canary_prompts_lists_the_five_seeded_prompts(
    client, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/v1/admin/drift-detector/canary-prompts", headers=auth_headers)
    assert response.status_code == 200, response.text
    prompts = response.json()
    assert len(prompts) == 5
    assert all(p["enabled"] for p in prompts)
    assert all(0 < p["max_tokens"] <= 200 for p in prompts)


async def test_status_alerts_and_history_start_empty(
    client, auth_headers: dict[str, str]
) -> None:
    for path in ("status", "alerts", "canary-history"):
        response = await client.get(f"/v1/admin/drift-detector/{path}", headers=auth_headers)
        assert response.status_code == 200, response.text
        assert response.json() == []


async def test_per_model_enable_disable_persists_and_is_audited(
    client, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    response = await client.put(
        "/v1/admin/drift-detector/models/gpt-4o", json={"enabled": False}, headers=auth_headers
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"model": "gpt-4o", "enabled": False}

    status_response = await client.get("/v1/admin/drift-detector/status", headers=auth_headers)
    assert status_response.status_code == 200, status_response.text
    [entry] = status_response.json()
    assert entry["model"] == "gpt-4o"
    assert entry["canary_enabled"] is False

    audit_count = await _fetch_val(
        migrated_database_url,
        "SELECT count(*) FROM audit_entries WHERE action = $1 AND target_id = $2",
        "drift_detector.canary_model_setting.update",
        "gpt-4o",
    )
    assert audit_count == 1


# --- Cost separation (hard NFR, AC5.4.9) ------------------------------------


async def test_canary_cost_never_touches_user_attributable_usage_or_budget(
    app: FastAPI, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    await _seed_recent_real_usage_log(migrated_database_url, model="gpt-4o")

    captured_bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_bodies.append(body)
        return httpx.Response(200, json=_canned_openai_response("gpt-4o", "canary response text"))

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _register_openai_key(client, auth_headers)

            user_response = await client.post(
                "/v1/admin/users", json={"name": "drift-nfr-user"}, headers=auth_headers
            )
            assert user_response.status_code == 201, user_response.text
            assert Decimal(str(user_response.json()["current_spend_usd"])) == Decimal("0")

            usage_logs_before = await _fetch_val(
                migrated_database_url, "SELECT count(*) FROM usage_logs WHERE model = $1", "gpt-4o"
            )

            # Substitute the mocked transport for the REAL provider HTTP
            # client the scheduler tick would otherwise use - set on
            # `app.state` directly (the same attribute `run_canary_suite_
            # for_org` reads), not via `dependency_overrides` (that only
            # affects request-time `Depends(...)` resolution, and this call
            # path never goes through a FastAPI request at all).
            app.state.provider_http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

            async with app.state.db_session_factory() as session:
                summary = await run_canary_suite_for_org(session, app)

            assert summary.models_tested == 1
            assert summary.runs_recorded == 5  # the 5 seeded canary_prompts
            # A REAL provider call was made per prompt (proves this is not
            # a simulated/fake call).
            assert len(captured_bodies) == 5
            assert all(body["model"] == "gpt-4o" for body in captured_bodies)

            final_user_response = await client.get("/v1/admin/users", headers=auth_headers)
            assert final_user_response.status_code == 200, final_user_response.text
            [user_row] = [
                u for u in final_user_response.json() if u["name"] == "drift-nfr-user"
            ]

    # (a) zero NEW usage_logs rows reference canary traffic.
    usage_logs_after = await _fetch_val(
        migrated_database_url, "SELECT count(*) FROM usage_logs WHERE model = $1", "gpt-4o"
    )
    assert usage_logs_after == usage_logs_before

    # (b) no user's current_spend_usd changed.
    assert Decimal(str(user_row["current_spend_usd"])) == Decimal("0")

    # (c) canary_runs.cost_usd sums to a nonzero, bounded figure - the ONLY
    # place canary spend is ever visible.
    canary_rows = await _fetch_all(
        migrated_database_url,
        "SELECT cost_usd, is_canary, latency_ms, refusal_detected, output_text "
        "FROM canary_runs WHERE model = $1",
        "gpt-4o",
    )
    assert len(canary_rows) == 5
    assert all(row["is_canary"] for row in canary_rows)
    total_canary_cost = sum((Decimal(str(row["cost_usd"])) for row in canary_rows), Decimal("0"))
    assert total_canary_cost > Decimal("0")
    assert total_canary_cost < Decimal("1")  # AC5.4.10's cost-floor claim
    assert all(row["output_text"] == "canary response text" for row in canary_rows)
    assert all(row["refusal_detected"] is False for row in canary_rows)


async def test_canary_suite_skips_models_with_no_configured_credential(
    app: FastAPI, migrated_database_url: str
) -> None:
    """AC error-handling table 7.2: no key configured for an actively-used
    model's provider - skip that model, never crash the tick."""
    await _seed_recent_real_usage_log(migrated_database_url, model="gpt-4o")

    async with app.router.lifespan_context(app):
        async with app.state.db_session_factory() as session:
            summary = await run_canary_suite_for_org(session, app)

    assert summary.models_tested == 1
    assert summary.runs_recorded == 0

    canary_count = await _fetch_val(
        migrated_database_url, "SELECT count(*) FROM canary_runs WHERE model = $1", "gpt-4o"
    )
    assert canary_count == 0


async def test_disabled_model_is_never_canary_tested(
    app: FastAPI, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    await _seed_recent_real_usage_log(migrated_database_url, model="gpt-4o")

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            await _register_openai_key(client, auth_headers)
            disable_response = await client.put(
                "/v1/admin/drift-detector/models/gpt-4o", json={"enabled": False}, headers=auth_headers
            )
            assert disable_response.status_code == 200, disable_response.text

            async with app.state.db_session_factory() as session:
                summary = await run_canary_suite_for_org(session, app)

    assert summary.models_tested == 0
    assert summary.runs_recorded == 0


# --- Export to audit log (AC5.2.10/AC5.4.11) --------------------------------


async def test_export_drift_alert_writes_audit_entry_and_updates_status(
    client, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    alert_id = uuid.uuid4()
    await _execute(
        migrated_database_url,
        """
        INSERT INTO drift_alerts (id, model, metric, baseline_value, observed_value, delta_pct, status)
        VALUES ($1, 'gpt-4o', 'latency', 100.0, 200.0, 100.00, 'open')
        """,
        alert_id,
    )

    response = await client.post(
        f"/v1/admin/drift-detector/alerts/{alert_id}/export", headers=auth_headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "exported_to_audit"
    assert "latency" in body["message"]
    assert "100.00%" in body["message"] or "100.0" in body["message"]

    stored_status = await _fetch_val(
        migrated_database_url, "SELECT status FROM drift_alerts WHERE id = $1", alert_id
    )
    assert stored_status == "exported_to_audit"

    audit_row = await _fetch_val(
        migrated_database_url,
        "SELECT count(*) FROM audit_entries WHERE action = 'drift.alert_exported' AND target_id = $1",
        str(alert_id),
    )
    assert audit_row == 1


async def test_export_unknown_alert_returns_404(client, auth_headers: dict[str, str]) -> None:
    response = await client.post(
        f"/v1/admin/drift-detector/alerts/{uuid.uuid4()}/export", headers=auth_headers
    )
    assert response.status_code == 404, response.text


# --- flag_drift / establish_baseline_if_ready - real DB write path ---------


async def test_flag_drift_writes_a_real_drift_alert_row_when_latency_crosses_threshold(
    sf: async_sessionmaker, migrated_database_url: str
) -> None:
    """Exercises the actual DB INSERT path for both `establish_baseline_
    if_ready` and `flag_drift` (not just the pure threshold-math unit
    tests) - proves the NUMERIC(10,4)/(6,2) columns accept real computed
    Decimal values without a scale/precision error, and that a genuine
    latency deviation produces a real, queryable `drift_alerts` row."""
    model = "gpt-4o"

    async with sf() as session:
        # 7 canary_runs at latency_ms=300, no established baseline yet.
        established = False
        for _ in range(7):
            await session.execute(
                CanaryRun.__table__.insert().values(
                    model=model,
                    prompt_id=_FACTUAL_PROMPT_ID,
                    output_text="Paris is the capital of France.",
                    latency_ms=100,
                    refusal_detected=False,
                    cost_usd=Decimal("0.0001"),
                )
            )
        await session.commit()
        established = await establish_baseline_if_ready(
            session, model=model, prompt_id=_FACTUAL_PROMPT_ID
        )
        await session.commit()
    assert established is True

    baseline_latency = await _fetch_val(
        migrated_database_url,
        "SELECT baseline_latency_ms FROM canary_baselines WHERE model = $1 AND prompt_id = $2",
        model,
        _FACTUAL_PROMPT_ID,
    )
    assert Decimal(str(baseline_latency)) == Decimal("100.00")

    # Now record 7 MORE runs with a dramatically higher latency (>50%
    # deviation) so the rolling window vs. baseline crosses the threshold.
    async with sf() as session:
        for _ in range(7):
            await session.execute(
                CanaryRun.__table__.insert().values(
                    model=model,
                    prompt_id=_FACTUAL_PROMPT_ID,
                    output_text="Paris is the capital of France.",
                    latency_ms=500,
                    refusal_detected=False,
                    cost_usd=Decimal("0.0001"),
                )
            )
        await session.commit()
        new_alerts = await flag_drift(session, model=model)
        await session.commit()

    assert len(new_alerts) == 1
    assert new_alerts[0].metric == "latency"

    stored_alert = await _fetch_row(
        migrated_database_url,
        "SELECT model, metric, baseline_value, observed_value, delta_pct, status "
        "FROM drift_alerts WHERE model = $1",
        model,
    )
    assert stored_alert is not None
    assert stored_alert["metric"] == "latency"
    assert stored_alert["status"] == "open"
    assert Decimal(str(stored_alert["observed_value"])) > Decimal(str(stored_alert["baseline_value"]))
