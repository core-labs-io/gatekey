"""Hardening pass item 6 (QA follow-up): does `GET /v1/admin/self-hosted-
providers/{id}/usage` genuinely scope its aggregate to ONLY that one
provider's traffic?

`test_self_hosted_providers_api.py::test_usage_endpoint_reflects_a_real_
request_through_the_full_pipeline` already proves a real request is
reflected and a NEVER-called second provider stays at zero - but it never
exercises the harder case: TWO self-hosted providers that BOTH have real,
DIFFERENT amounts of traffic (a `WHERE self_hosted_provider_id = :wrong_id`
bug, or an unfiltered/summed aggregate, could still slip through a
zero-vs-nonzero check but would fail an exact-count-per-provider check), and
whether a BYOK (openai) request bleeds into either self-hosted total at all
(`services.usage_logs.get_self_hosted_provider_usage`'s `UsageLog.self_
hosted_provider_id == self_hosted_provider_id` filter should exclude every
BYOK row outright, since those rows have `self_hosted_provider_id IS NULL`).

Drives real (mocked-HTTP) dispatch for both self-hosted endpoints AND a real
BYOK provider through the actual gateway pipeline, then asserts each
provider's `/usage` reflects EXACTLY its own request count/cost, never the
other self-hosted provider's or the BYOK provider's.
"""

from __future__ import annotations

from decimal import Decimal

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from gatekey.api.deps import get_provider_http_client

from .conftest import to_asyncpg_dsn
from .test_self_hosted_providers_api import (
    _URL,
    _canned_openai_shaped_response,
    _make_service_account_secret,
    _mock_client_for,
)

pytestmark = pytest.mark.asyncio


async def _fetch_usage_logs_for_model(database_url: str, model: str) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetch(
            "SELECT provider, model, cost_usd, self_hosted_provider_id FROM usage_logs "
            "WHERE model = $1 ORDER BY created_at",
            model,
        )
    finally:
        await conn.close()


async def test_usage_endpoint_never_cross_contaminates_two_self_hosted_providers_or_byok(
    app: FastAPI,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    secret, _user_id = await _make_service_account_secret(migrated_database_url)

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host == "selfhosted-a.internal":
            return httpx.Response(
                200, json=_canned_openai_shaped_response("isolation-model-a", "served by provider A")
            )
        if host == "selfhosted-b.internal":
            return httpx.Response(
                200, json=_canned_openai_shaped_response("isolation-model-b", "served by provider B")
            )
        if "openai" in request.headers.get("authorization", ""):
            pass
        return httpx.Response(
            200, json=_canned_openai_shaped_response("gpt-4o", "served by BYOK openai")
        )

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                # Two DIFFERENT self-hosted providers, distinct cost bases so
                # their totals are trivially distinguishable if ever summed
                # together by mistake.
                provider_a_resp = await client.post(
                    _URL,
                    json={
                        "name": "isolation-provider-a",
                        "base_url": "http://selfhosted-a.internal:8000",
                        "bearer_token": "token-a",
                        "cost_basis_per_gpu_hour": "2.0000",
                        "models": ["isolation-model-a"],
                    },
                    headers=auth_headers,
                )
                assert provider_a_resp.status_code == 201, provider_a_resp.text
                provider_a_id = provider_a_resp.json()["id"]
                assert (
                    await client.post(f"{_URL}/{provider_a_id}/verify", headers=auth_headers)
                ).status_code == 200

                provider_b_resp = await client.post(
                    _URL,
                    json={
                        "name": "isolation-provider-b",
                        "base_url": "http://selfhosted-b.internal:8000",
                        "bearer_token": "token-b",
                        "cost_basis_per_gpu_hour": "9.0000",
                        "models": ["isolation-model-b"],
                    },
                    headers=auth_headers,
                )
                assert provider_b_resp.status_code == 201, provider_b_resp.text
                provider_b_id = provider_b_resp.json()["id"]
                assert (
                    await client.post(f"{_URL}/{provider_b_id}/verify", headers=auth_headers)
                ).status_code == 200

                # A real BYOK provider too - its traffic must never appear in
                # either self-hosted provider's `/usage` total.
                byok_key_resp = await client.put(
                    "/v1/admin/providers/openai/key",
                    json={"api_key": "sk-isolation-test-openai"},
                    headers=auth_headers,
                )
                assert byok_key_resp.status_code == 200, byok_key_resp.text

                # Provider A: 3 real requests. Provider B: 2 real requests
                # (deliberately different counts - a summed/unfiltered
                # aggregate would show 5 on both, an exact-count check
                # catches that where a zero-vs-nonzero check would not).
                for _ in range(3):
                    resp = await client.post(
                        "/v1/chat/completions",
                        json={
                            "model": "isolation-model-a",
                            "messages": [{"role": "user", "content": "hi from A"}],
                        },
                        headers={"Authorization": f"Bearer {secret}"},
                    )
                    assert resp.status_code == 200, resp.text

                for _ in range(2):
                    resp = await client.post(
                        "/v1/chat/completions",
                        json={
                            "model": "isolation-model-b",
                            "messages": [{"role": "user", "content": "hi from B"}],
                        },
                        headers={"Authorization": f"Bearer {secret}"},
                    )
                    assert resp.status_code == 200, resp.text

                # One BYOK request through the same gateway pipeline.
                byok_chat_resp = await client.post(
                    "/v1/chat/completions",
                    json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi BYOK"}]},
                    headers={"Authorization": f"Bearer {secret}"},
                )
                assert byok_chat_resp.status_code == 200, byok_chat_resp.text

                usage_a_resp = await client.get(f"{_URL}/{provider_a_id}/usage", headers=auth_headers)
                usage_b_resp = await client.get(f"{_URL}/{provider_b_id}/usage", headers=auth_headers)
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    # Sanity: confirm the real DB rows landed the way the test intends
    # before trusting the aggregate endpoint's own arithmetic.
    a_logs = await _fetch_usage_logs_for_model(migrated_database_url, "isolation-model-a")
    b_logs = await _fetch_usage_logs_for_model(migrated_database_url, "isolation-model-b")
    assert len(a_logs) == 3, a_logs
    assert len(b_logs) == 2, b_logs
    assert all(str(r["self_hosted_provider_id"]) == provider_a_id for r in a_logs)
    assert all(str(r["self_hosted_provider_id"]) == provider_b_id for r in b_logs)

    assert usage_a_resp.status_code == 200, usage_a_resp.text
    body_a = usage_a_resp.json()
    assert body_a["self_hosted_provider_id"] == provider_a_id
    assert body_a["total_requests"] == 3, body_a
    assert body_a["total_estimated_cost_usd"] is not None
    assert Decimal(body_a["total_estimated_cost_usd"]) == sum(
        (Decimal(str(r["cost_usd"])) for r in a_logs), Decimal("0")
    )

    assert usage_b_resp.status_code == 200, usage_b_resp.text
    body_b = usage_b_resp.json()
    assert body_b["self_hosted_provider_id"] == provider_b_id
    assert body_b["total_requests"] == 2, body_b
    assert Decimal(body_b["total_estimated_cost_usd"]) == sum(
        (Decimal(str(r["cost_usd"])) for r in b_logs), Decimal("0")
    )

    # Provider B's cost basis (9.0/hr) is far higher than A's (2.0/hr) - if
    # the two totals were ever accidentally summed/cross-attributed the
    # numbers below would not hold independently.
    assert body_a["total_estimated_cost_usd"] != body_b["total_estimated_cost_usd"]
    assert body_a["total_requests"] != body_b["total_requests"]

    # Neither self-hosted provider's count (3 or 2) includes the BYOK
    # request (would make either total 4, or one of them 6 if fully
    # cross-contaminated) - the exact counts above already prove this, this
    # assertion just makes the intent explicit.
    assert body_a["total_requests"] + body_b["total_requests"] == 5
