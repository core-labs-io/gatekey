"""Independent QA follow-up for Phase 5 (Differentiators, 5.5 Unified
Governance for BYOK + Self-Hosted OSS Models) - closes two gaps left by
`test_self_hosted_providers_api.py`'s own coverage:

1. The design doc's own P0 scenario (section 9.1: "Self-hosted chat request
   flows through DLP/residency/budget identically to a BYOK request") is
   only exercised for the BUDGET half by the existing e2e test
   (`test_e2e_chat_completion_self_hosted_model_full_pipeline`) - it never
   sends content that would trigger a DLP redact/block action, so it cannot
   actually prove DLP runs for a self-hosted-routed request, only that it
   doesn't crash. This module adds that missing proof (mirrors
   `test_dlp_gateway.py`'s established redact/block assertion pattern,
   applied to a self-hosted model instead of a BYOK one).

2. AC5.5.4 ("Chat-completions only... self-hosted models are not routable
   for `/v1/completions` or `/v1/embeddings`") is only tested for
   `/v1/completions` in the existing suite - `/v1/embeddings` has NO test at
   all. `resolve_route()` is called identically (no `self_hosted_cache`
   argument) at both call sites per its own docstring, so this is expected
   to behave the same as the completions case - this test proves it, rather
   than leaving it "structurally true but untested."
"""

from __future__ import annotations

import json

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from gatekey.api.deps import get_provider_http_client

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio

_URL = "/v1/admin/self-hosted-providers"
_SSN = "234-56-7890"  # Presidio's UsSsnRecognizer invalidates the textbook "123-45-6789" placeholder.


@pytest.fixture(autouse=True)
async def _clean_tables(migrated_database_url: str):
    async def _truncate() -> None:
        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            await conn.execute("TRUNCATE TABLE self_hosted_providers CASCADE")
            await conn.execute("TRUNCATE TABLE model_policies")
            await conn.execute(
                "TRUNCATE TABLE dlp_policies, dlp_custom_patterns, "
                "team_dlp_action_overrides, dlp_scan_results CASCADE"
            )
        finally:
            await conn.close()

    await _truncate()
    yield
    await _truncate()


def _mock_client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _canned_response(model: str, content: str = "ok") -> dict:
    return {
        "id": "chatcmpl-self-hosted-extra",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 6, "completion_tokens": 4, "total_tokens": 10},
    }


async def _register_and_verify_self_hosted_provider(
    client: httpx.AsyncClient, auth_headers: dict[str, str], *, name: str, model: str
) -> str:
    register_response = await client.post(
        _URL,
        json={
            "name": name,
            "base_url": f"http://{name}.internal:8000",
            "bearer_token": "extra-test-bearer-token",
            "cost_basis_per_gpu_hour": "1.0000",
            "models": [model],
        },
        headers=auth_headers,
    )
    assert register_response.status_code == 201, register_response.text
    provider_id = register_response.json()["id"]
    verify_response = await client.post(f"{_URL}/{provider_id}/verify", headers=auth_headers)
    assert verify_response.status_code == 200, verify_response.text
    assert verify_response.json()["verified"] is True
    return provider_id


async def _make_service_account_secret(client, auth_headers, *, user_id: str, team_id: str) -> str:
    response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "self-hosted-extra-key", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["secret"]


# ---------------------------------------------------------------------------
# (1) DLP genuinely runs for a self-hosted-routed request (design doc P0).
# ---------------------------------------------------------------------------


async def test_self_hosted_chat_completion_dlp_redacts_before_reaching_the_endpoint(
    app: FastAPI,
    client,
    auth_headers,
    default_user_id,
    default_team_id,
    migrated_database_url: str,
) -> None:
    """Not just "doesn't crash" - the PROVIDER-BOUND request body actually
    has the SSN redacted, proving `run_dlp_scan()` genuinely executes on the
    self-hosted dispatch path, same as any BYOK request (mirrors
    `test_dlp_gateway.py::test_dlp_redact_mode_strips_pii_before_reaching_provider`'s
    exact assertion shape)."""
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_canned_response("self-hosted-dlp-model"))

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                dlp_resp = await c.put(
                    "/v1/admin/dlp-policy",
                    json={"ssn_detector_enabled": True, "default_action": "redact"},
                    headers=auth_headers,
                )
                assert dlp_resp.status_code == 200, dlp_resp.text

                await _register_and_verify_self_hosted_provider(
                    c, auth_headers, name="self-hosted-dlp-endpoint", model="self-hosted-dlp-model"
                )

                chat_response = await c.post(
                    "/v1/chat/completions",
                    json={
                        "model": "self-hosted-dlp-model",
                        "messages": [
                            {"role": "user", "content": f"My SSN is {_SSN}, please help."}
                        ],
                    },
                    headers={"Authorization": f"Bearer {secret}"},
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert chat_response.status_code == 200, chat_response.text
    sent_content = captured["body"]["messages"][0]["content"]
    assert _SSN not in sent_content, (
        f"real SSN reached the self-hosted endpoint unredacted: {sent_content!r} - "
        "DLP did not run on the self-hosted dispatch path"
    )


async def test_self_hosted_chat_completion_dlp_block_prevents_provider_call(
    app: FastAPI,
    client,
    auth_headers,
    default_user_id,
    default_team_id,
    migrated_database_url: str,
) -> None:
    """Same proof, block mode: the mock self-hosted endpoint must never be
    invoked at all once the DLP policy is set to block."""
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_canned_response("self-hosted-block-model"))

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
                dlp_resp = await c.put(
                    "/v1/admin/dlp-policy",
                    json={"ssn_detector_enabled": True, "default_action": "block"},
                    headers=auth_headers,
                )
                assert dlp_resp.status_code == 200, dlp_resp.text

                await _register_and_verify_self_hosted_provider(
                    c, auth_headers, name="self-hosted-block-endpoint", model="self-hosted-block-model"
                )

                chat_response = await c.post(
                    "/v1/chat/completions",
                    json={
                        "model": "self-hosted-block-model",
                        "messages": [{"role": "user", "content": f"My SSN is {_SSN}."}],
                    },
                    headers={"Authorization": f"Bearer {secret}"},
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert chat_response.status_code == 403, chat_response.text
    assert chat_response.json()["error"]["code"] == "dlp_blocked"
    assert call_count == 0, "the self-hosted endpoint must never be called once DLP blocks the request"


# ---------------------------------------------------------------------------
# (2) AC5.5.4 - /v1/embeddings also rejects a self-hosted model id.
# ---------------------------------------------------------------------------


async def test_self_hosted_model_rejected_on_embeddings_endpoint(
    app: FastAPI,
    auth_headers: dict[str, str],
    default_user_id,
    default_team_id,
) -> None:
    """AC5.5.4's other half - `/v1/completions` is already covered by
    `test_self_hosted_providers_api.py::test_e2e_self_hosted_model_rejected_on_completions_endpoint`;
    `/v1/embeddings` had NO test at all before this. `resolve_route()` there
    is called with no `self_hosted_cache` argument (same as completions.py),
    so a real, registered-and-VERIFIED self-hosted model id must still 404
    exactly like any unknown model id."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            secret_resp = await client.post(
                "/v1/admin/service-accounts",
                json={
                    "name": "self-hosted-embeddings-rejection-key",
                    "user_id": default_user_id,
                    "team_id": default_team_id,
                },
                headers=auth_headers,
            )
            assert secret_resp.status_code == 201, secret_resp.text
            secret = secret_resp.json()["secret"]

            await _register_and_verify_self_hosted_provider(
                client, auth_headers, name="embeddings-rejection-endpoint", model="embeddings-rejection-model"
            )

            embeddings_response = await client.post(
                "/v1/embeddings",
                json={"model": "embeddings-rejection-model", "input": "hi"},
                headers={"Authorization": f"Bearer {secret}"},
            )
    assert embeddings_response.status_code == 404, embeddings_response.text
    assert embeddings_response.json()["error"]["code"] == "model_not_found"
