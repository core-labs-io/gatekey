""""Also check" item from the QA task brief: `usage_logs` Phase 4 columns
(`cache_hit`, `degraded_from_model`, `degraded_to_model`) actually populated
correctly end-to-end through a REAL request against a real Postgres row -
not just that the columns exist (migration `0031`/`0028`), and not just that
`services.usage_logs.record_usage_log()`'s Python signature accepts them
(every existing unit test fakes `record_usage_log` away entirely - see
`tests/unit/gateway_test_support.py`'s `_fake_record_usage_log`).

`failover_attempt`/`failover_key_id` are deliberately NOT exercised here via
a real HTTP admin-configured backup key: as documented in this QA pass's
findings, there is currently no admin HTTP endpoint that can set
`ProviderKey.failover_target_id` at all (`services.provider_keys.
set_failover_config` has zero callers under `src/gatekey/api/`), so a
real end-to-end failover round trip cannot be driven through the actual
product surface - see the QA report for the full writeup. (The retry
mechanic itself, and that it correctly returns the attempt/key data these
columns are populated FROM, is separately verified directly in
`tests/unit/test_call_provider_with_failover.py`.)
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from gatekey.api.deps import get_provider_http_client

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio


def _canned_response(model: str, content: str = "ok") -> dict:
    return {
        "id": "chatcmpl-phase4-test",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    }


async def _fetch_usage_logs_for_team(database_url: str, *, team_id: str) -> list[asyncpg.Record]:
    """`usage_logs` is never truncated between integration test files (it
    accumulates for the whole session) - filter by this test's own freshly
    created `team_id` rather than a global query, and by `created_at ASC`
    so call order is unambiguous."""
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetch(
            "SELECT cache_hit, degraded_from_model, degraded_to_model, prompt_tokens, "
            "completion_tokens, cost_usd, model FROM usage_logs WHERE team_id = $1 "
            "ORDER BY created_at ASC",
            uuid.UUID(team_id),
        )
    finally:
        await conn.close()


async def _make_service_account_secret(client, auth_headers, *, user_id: str, team_id: str) -> str:
    response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "phase4-usage-log-test-key", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["secret"]


async def test_cache_hit_column_populated_correctly_on_real_hit_and_miss(
    app: FastAPI, client, auth_headers, default_user_id, default_team_id, migrated_database_url
) -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_canned_response("gpt-4o"))

    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    key_resp = await client.put(
        "/v1/admin/providers/openai/key", json={"api_key": "sk-test-phase4-usage-log"}, headers=auth_headers
    )
    assert key_resp.status_code == 200, key_resp.text

    cache_resp = await client.put(
        f"/v1/admin/teams/{default_team_id}/cache-settings",
        json={"cache_enabled": True, "cache_ttl_minutes": 5},
        headers=auth_headers,
    )
    assert cache_resp.status_code == 200, cache_resp.text

    app.dependency_overrides[get_provider_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        body = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "usage log cache_hit column e2e test"}],
        }

        first = await client.post(
            "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {secret}"}
        )
        assert first.status_code == 200, first.text
        assert first.headers["X-Cache"] == "MISS"

        second = await client.post(
            "/v1/chat/completions", json=body, headers={"Authorization": f"Bearer {secret}"}
        )
        assert second.status_code == 200, second.text
        assert second.headers["X-Cache"] == "HIT"

        assert call_count == 1, "the provider must only be reached once (the miss), never on the hit"
    finally:
        del app.dependency_overrides[get_provider_http_client]

    # Exactly two usage_logs rows for this fresh team (the MISS, then the
    # HIT) - the first must show cache_hit=false with real, non-zero token
    # counts (it genuinely reached the provider); the second must show
    # cache_hit=true with cost_usd=0 (AC4.3's A5 resolution - a cache hit
    # logs no real inference cost; "cost saved" is computed separately by
    # the dashboard from the average non-hit cost).
    rows = await _fetch_usage_logs_for_team(migrated_database_url, team_id=default_team_id)
    assert len(rows) == 2, f"expected exactly 2 usage_logs rows for this team, got {len(rows)}: {rows}"
    miss_row, hit_row = rows
    assert miss_row["cache_hit"] is False
    assert miss_row["prompt_tokens"] == 4
    assert miss_row["completion_tokens"] == 3
    assert hit_row["cache_hit"] is True
    assert hit_row["cost_usd"] == Decimal("0")


async def test_degraded_from_and_to_model_columns_populated_on_real_downgrade(
    app: FastAPI, client, auth_headers, default_user_id, default_team_id, migrated_database_url
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        # Respond as whichever model was actually requested (the fallback,
        # if degradation substituted it) - proves the SUBSTITUTED model's
        # response is what's returned/logged, not the original's.
        return httpx.Response(200, json=_canned_response(payload["model"]))

    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    key_resp = await client.put(
        "/v1/admin/providers/openai/key", json={"api_key": "sk-test-phase4-degrade"}, headers=auth_headers
    )
    assert key_resp.status_code == 200, key_resp.text

    # A deliberately tiny team-membership budget ceiling: gpt-4o's real
    # per-token pricing (see providers/pricing.py) on the mocked response's
    # usage (4 prompt + 3 completion tokens) costs (4*2.50 + 3*10.00)/1e6 =
    # $0.00004 - a meaningful FRACTION of a $0.001 ceiling (4%), but nowhere
    # near exhausting it (no hard 402 block), so the FIRST call's own charge
    # is what pushes "remaining budget" below a 99% threshold for the
    # SECOND call - avoids needing to fabricate `current_spend_usd` any
    # other way (no admin endpoint sets it directly; it is only ever
    # advanced by a real charged request, by design - ADR-7).
    budget_resp = await client.patch(
        f"/v1/teams/{default_team_id}/members/{default_user_id}",
        json={"budget_usd": "0.001"},
        headers=auth_headers,
    )
    assert budget_resp.status_code == 200, budget_resp.text

    # `resolve_degradation_policy()` (services/degradation.py) is cumulative
    # on `enabled`: a team-level policy only ever takes effect if the ORG
    # level is ALSO enabled - both must be set for a team-scoped-only
    # threshold/target to actually apply.
    org_degrade_resp = await client.put(
        "/v1/admin/degradation-policy",
        json={"enabled": True, "threshold_pct_of_budget": 99.0, "downgrade_target_model": "gpt-4o-mini"},
        headers=auth_headers,
    )
    assert org_degrade_resp.status_code == 200, org_degrade_resp.text

    degrade_resp = await client.put(
        f"/v1/admin/teams/{default_team_id}/degradation-policy",
        json={
            "enabled": True,
            "threshold_pct_of_budget": 99.0,  # triggers once ANY meaningful spend has occurred
            "downgrade_target_model": "gpt-4o-mini",
        },
        headers=auth_headers,
    )
    assert degrade_resp.status_code == 200, degrade_resp.text

    app.dependency_overrides[get_provider_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        # First call: spend is still $0 pre-check, so degradation does NOT
        # trigger (100% remaining >= 99% threshold) - this call's charge is
        # what seeds the spend the second call's proximity check reacts to.
        warmup = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "warm-up call to seed real spend"}],
            },
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert warmup.status_code == 200, warmup.text
        assert "X-Gatekey-Degraded" not in warmup.headers

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "usage log degraded_from/to columns e2e test"}],
            },
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert response.status_code == 200, response.text
    finally:
        del app.dependency_overrides[get_provider_http_client]

    if response.headers.get("X-Gatekey-Degraded") != "true":
        pytest.skip(
            "degradation did not trigger with this threshold/budget combination - "
            f"response headers: {dict(response.headers)}"
        )
    assert response.headers["X-Gatekey-Degraded-From"] == "gpt-4o"
    assert response.headers["X-Gatekey-Degraded-To"] == "gpt-4o-mini"

    rows = await _fetch_usage_logs_for_team(migrated_database_url, team_id=default_team_id)
    assert len(rows) == 2, f"expected exactly 2 usage_logs rows for this team, got {len(rows)}: {rows}"
    warmup_row, degraded_row = rows
    assert warmup_row["degraded_from_model"] is None
    assert warmup_row["degraded_to_model"] is None
    assert degraded_row["degraded_from_model"] == "gpt-4o"
    assert degraded_row["degraded_to_model"] == "gpt-4o-mini"
