"""Integration tests for the self-hosted-provider admin CRUD API and a full
end-to-end `/v1/chat/completions` request against a self-hosted model
(Phase 5 - Differentiators, 5.5 Unified Governance for BYOK + Self-Hosted
OSS Models). See `gatekey/phase-5-product-spec.md` AC5.5.1/AC5.5.3/AC5.5.5-
AC5.5.9 and `gatekey/phase-5-technical-design.md` section 9.1's mandatory
test scenarios.

See `conftest.py` for the Postgres/Docker/migration/lifespan/validator-mock
plumbing these tests build on. `OllamaValidator.validate` is monkeypatched
to always report `VALID` by the autouse `_default_valid_validators` fixture
(`conftest.py`) - re-verification tests below rely on this exactly like
`test_gateway_ollama_openrouter.py`'s Ollama key setup does.
"""

from __future__ import annotations

import json
from decimal import Decimal

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from gatekey.api.deps import get_provider_http_client
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.session import create_engine as db_create_engine
from gatekey.db.session import create_session_factory
from gatekey.services.encryption import EnvKeyProvider, build_aad, decrypt_secret
from gatekey.services.service_accounts import create_service_account
from gatekey.services.users import create_user

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio

_URL = "/v1/admin/self-hosted-providers"


@pytest.fixture(autouse=True)
async def _truncate_self_hosted_providers(migrated_database_url: str):
    """Ensure each test starts with an empty `self_hosted_providers` table
    AND an unconfigured `model_policies` row - one of these tests
    (`test_get_model_policy_accepts_self_hosted_model_id_after_registration`)
    writes an org model-policy row that would otherwise leak into a LATER
    test in this same file (e.g. the e2e chat-completion test denying a
    different self-hosted model id under a stale allowlist) - same
    per-file truncation precedent `test_model_policy_api.py`'s own
    `_truncate_model_policies` fixture establishes. `CASCADE` on
    `self_hosted_providers` because `usage_logs.self_hosted_provider_id`
    FKs to it (same rationale/precedent as `conftest.py`'s `_truncate_
    provider_keys`)."""
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute("TRUNCATE TABLE self_hosted_providers CASCADE")
        await conn.execute("TRUNCATE TABLE model_policies")
    finally:
        await conn.close()
    yield


def _register_payload(**overrides) -> dict:
    payload = {
        "name": "vllm-internal-llama3",
        "base_url": "http://vllm-internal.example.internal:8000",
        "bearer_token": "s3cr3t-bearer-token",
        "cost_basis_per_gpu_hour": "2.5000",
        "models": ["vllm-internal-llama3"],
    }
    payload.update(overrides)
    return payload


async def _fetch_row(database_url: str, provider_id: str) -> asyncpg.Record | None:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchrow(
            "SELECT ciphertext, nonce, auth_tag, base_url, verified, models, "
            "cost_basis_per_gpu_hour FROM self_hosted_providers WHERE id = $1",
            provider_id,
        )
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Admin CRUD
# ---------------------------------------------------------------------------


async def test_register_persists_encrypted_bearer_token_never_plaintext(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
    master_key_bytes: bytes,
) -> None:
    response = await client.post(_URL, json=_register_payload(), headers=auth_headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "vllm-internal-llama3"
    assert body["verified"] is False
    assert body["models"] == ["vllm-internal-llama3"]
    assert "bearer_token" not in body
    assert "ciphertext" not in body
    assert "s3cr3t-bearer-token" not in response.text

    row = await _fetch_row(migrated_database_url, body["id"])
    assert row is not None
    ciphertext = bytes(row["ciphertext"])
    assert isinstance(ciphertext, bytes) and len(ciphertext) > 0
    assert b"s3cr3t-bearer-token" not in ciphertext
    assert row["verified"] is False

    plaintext = decrypt_secret(
        ciphertext,
        nonce=bytes(row["nonce"]),
        auth_tag=bytes(row["auth_tag"]),
        aad=build_aad(str(DEFAULT_ORG_ID), f"self_hosted:{body['id']}"),
        key_provider=EnvKeyProvider(master_key_bytes),
    )
    assert json.loads(plaintext) == "s3cr3t-bearer-token"


async def test_register_rejects_model_registry_collision(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """AC5.5.5/design doc section 7.3: a self-hosted model id colliding with
    a real static `MODEL_REGISTRY` key is rejected at registration time."""
    response = await client.post(
        _URL, json=_register_payload(name="collider", models=["gpt-4o"]), headers=auth_headers
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "self_hosted_model_registry_collision"


async def test_register_duplicate_name_conflict(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    first = await client.post(_URL, json=_register_payload(), headers=auth_headers)
    assert first.status_code == 201, first.text

    second = await client.post(
        _URL,
        json=_register_payload(models=["another-model-id"]),
        headers=auth_headers,
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "self_hosted_provider_name_conflict"


async def test_register_rejects_model_already_claimed_by_another_provider(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    first = await client.post(_URL, json=_register_payload(), headers=auth_headers)
    assert first.status_code == 201, first.text

    second = await client.post(
        _URL,
        json=_register_payload(name="a-second-endpoint"),  # same `models` list
        headers=auth_headers,
    )
    assert second.status_code == 422, second.text
    assert second.json()["error"]["code"] == "self_hosted_model_already_claimed"


async def test_list_includes_registered_providers(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    await client.post(_URL, json=_register_payload(), headers=auth_headers)
    response = await client.get(_URL, headers=auth_headers)
    assert response.status_code == 200
    names = [row["name"] for row in response.json()]
    assert "vllm-internal-llama3" in names


async def test_edit_updates_fields_and_resets_verified_on_base_url_change(
    client: httpx.AsyncClient, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    register_response = await client.post(_URL, json=_register_payload(), headers=auth_headers)
    provider_id = register_response.json()["id"]

    verify_response = await client.post(f"{_URL}/{provider_id}/verify", headers=auth_headers)
    assert verify_response.status_code == 200, verify_response.text
    assert verify_response.json()["verified"] is True

    edit_response = await client.put(
        f"{_URL}/{provider_id}",
        json={"base_url": "http://a-different-endpoint.internal:8000"},
        headers=auth_headers,
    )
    assert edit_response.status_code == 200, edit_response.text
    body = edit_response.json()
    assert body["base_url"] == "http://a-different-endpoint.internal:8000"
    # AC5.5.3: an endpoint/credential change invalidates a prior
    # verification - must be re-verified before it is routable again.
    assert body["verified"] is False

    row = await _fetch_row(migrated_database_url, provider_id)
    assert row["verified"] is False


async def test_edit_cost_basis_only_does_not_reset_verified(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    register_response = await client.post(_URL, json=_register_payload(), headers=auth_headers)
    provider_id = register_response.json()["id"]
    await client.post(f"{_URL}/{provider_id}/verify", headers=auth_headers)

    edit_response = await client.put(
        f"{_URL}/{provider_id}",
        json={"cost_basis_per_gpu_hour": "9.9999"},
        headers=auth_headers,
    )
    assert edit_response.status_code == 200, edit_response.text
    body = edit_response.json()
    assert Decimal(body["cost_basis_per_gpu_hour"]) == Decimal("9.9999")
    assert body["verified"] is True


async def test_edit_unknown_provider_404(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.put(
        f"{_URL}/00000000-0000-0000-0000-000000000099",
        json={"name": "does-not-matter"},
        headers=auth_headers,
    )
    assert response.status_code == 404


async def test_remove_deletes_row(
    client: httpx.AsyncClient, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    register_response = await client.post(_URL, json=_register_payload(), headers=auth_headers)
    provider_id = register_response.json()["id"]

    delete_response = await client.delete(f"{_URL}/{provider_id}", headers=auth_headers)
    assert delete_response.status_code == 204

    row = await _fetch_row(migrated_database_url, provider_id)
    assert row is None

    list_response = await client.get(_URL, headers=auth_headers)
    assert provider_id not in [row["id"] for row in list_response.json()]


async def test_reverify_sets_verified_true_when_probe_succeeds(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    register_response = await client.post(_URL, json=_register_payload(), headers=auth_headers)
    provider_id = register_response.json()["id"]
    assert register_response.json()["verified"] is False

    verify_response = await client.post(f"{_URL}/{provider_id}/verify", headers=auth_headers)
    assert verify_response.status_code == 200, verify_response.text
    assert verify_response.json()["verified"] is True


async def test_reverify_unknown_provider_404(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        f"{_URL}/00000000-0000-0000-0000-000000000099/verify", headers=auth_headers
    )
    assert response.status_code == 404


async def test_write_endpoints_reject_unauthenticated(client: httpx.AsyncClient) -> None:
    response = await client.post(_URL, json=_register_payload())
    assert response.status_code in (401, 403)


async def test_get_model_policy_accepts_self_hosted_model_id_after_registration(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """AC5.5.6/design doc section 2.3(d): a verified self-hosted model id is
    addable to the org model-access policy exactly like any BYOK model - no
    special-casing, and the `unknown_model_in_policy` 422 that a NEVER-
    registered self-hosted-looking id would still get proves this isn't
    just permissive-by-accident."""
    register_response = await client.post(_URL, json=_register_payload(), headers=auth_headers)
    provider_id = register_response.json()["id"]
    await client.post(f"{_URL}/{provider_id}/verify", headers=auth_headers)

    put_response = await client.put(
        "/v1/admin/model-policy",
        json={"mode": "allowlist", "models": ["vllm-internal-llama3"]},
        headers=auth_headers,
    )
    assert put_response.status_code == 200, put_response.text
    assert put_response.json()["models"] == ["vllm-internal-llama3"]

    rejected_response = await client.put(
        "/v1/admin/model-policy",
        json={"mode": "allowlist", "models": ["never-registered-self-hosted-model"]},
        headers=auth_headers,
    )
    assert rejected_response.status_code == 422
    assert rejected_response.json()["error"]["code"] == "unknown_model_in_policy"


# ---------------------------------------------------------------------------
# Full end-to-end chat completion against a self-hosted model
# ---------------------------------------------------------------------------


async def _make_service_account_secret(database_url: str) -> tuple[str, str]:
    """See `test_gateway_ollama_openrouter.py`'s identical helper's
    docstring for why this bypasses `POST /v1/admin/service-accounts`.
    Returns `(secret, user_id)` - the caller-attributed `User.id`, so tests
    can independently verify `current_spend_usd` was actually charged."""

    class _StubSettings:
        DATABASE_URL = database_url

    engine = db_create_engine(_StubSettings())  # type: ignore[arg-type]
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            user = await create_user(session, name="self-hosted-e2e-test-user")
            _row, secret = await create_service_account(session, "self-hosted-e2e-test-sa", user.id)
            return secret, str(user.id)
    finally:
        await engine.dispose()


def _mock_client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _canned_openai_shaped_response(model: str, content: str) -> dict:
    return {
        "id": "chatcmpl-self-hosted-e2e",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 6, "completion_tokens": 4, "total_tokens": 10},
    }


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
        return await conn.fetchval(
            "SELECT current_spend_usd FROM users WHERE id = $1", user_id
        )
    finally:
        await conn.close()


async def test_e2e_chat_completion_self_hosted_model_full_pipeline(
    app: FastAPI,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    """Design doc section 9.1's P0 scenario: "Self-hosted chat request flows
    through DLP/residency/budget identically to a BYOK request." Exercises
    the REAL dispatch/cost/logging path end to end (admin registration ->
    verification -> `resolve_route()`'s cache fallback -> credential
    decrypt -> `ollama_provider.create_chat_completion` -> `compute_self_
    hosted_cost()` -> `usage_logs` with `provider="self_hosted"` and
    `self_hosted_provider_id` set) - only the actual outbound HTTP call to
    the "self-hosted server" is intercepted via `httpx.MockTransport`, same
    mocking strategy as `test_gateway_ollama_openrouter.py`.
    """
    secret, user_id = await _make_service_account_secret(migrated_database_url)

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=_canned_openai_shaped_response(
                "e2e-self-hosted-llama3", "hello from the stubbed self-hosted endpoint"
            ),
        )

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as admin_client:
                register_response = await admin_client.post(
                    "/v1/admin/self-hosted-providers",
                    json={
                        "name": "e2e-self-hosted-endpoint",
                        "base_url": "http://self-hosted-stub.internal:8000",
                        "bearer_token": "e2e-bearer-token",
                        "cost_basis_per_gpu_hour": "3.6000",  # $1/hour == $1 per 3600s
                        "models": ["e2e-self-hosted-llama3"],
                    },
                    headers=auth_headers,
                )
                assert register_response.status_code == 201, register_response.text
                provider_id = register_response.json()["id"]

                verify_response = await admin_client.post(
                    f"/v1/admin/self-hosted-providers/{provider_id}/verify",
                    headers=auth_headers,
                )
                assert verify_response.status_code == 200, verify_response.text
                assert verify_response.json()["verified"] is True

                chat_response = await admin_client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "e2e-self-hosted-llama3",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"Authorization": f"Bearer {secret}"},
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert chat_response.status_code == 200, chat_response.text
    body = chat_response.json()
    assert body["choices"][0]["message"]["content"] == "hello from the stubbed self-hosted endpoint"

    # Routing: dispatched to the admin-registered base_url/bearer_token, and
    # the model field sent to the "self-hosted server" is the SAME string
    # the admin typed at registration (no separate native-id mapping).
    assert captured["url"] == "http://self-hosted-stub.internal:8000/v1/chat/completions"
    assert captured["authorization"] == "Bearer e2e-bearer-token"
    assert captured["body"]["model"] == "e2e-self-hosted-llama3"

    # Cost/logging: `provider="self_hosted"`, `self_hosted_provider_id` set,
    # a real (nonzero-capable) cost landed in `usage_logs.cost_usd` via
    # `compute_self_hosted_cost()`, never `PricingEntryMissingError` (this
    # model is NOT a `PRICING_TABLE` key).
    usage_row = await _fetch_usage_log(migrated_database_url, "e2e-self-hosted-llama3")
    assert usage_row is not None
    assert usage_row["provider"] == "self_hosted"
    assert usage_row["status"] == "ok"
    assert usage_row["success"] is True
    assert usage_row["prompt_tokens"] == 6
    assert usage_row["completion_tokens"] == 4
    assert str(usage_row["self_hosted_provider_id"]) == provider_id
    # Cost is computed from wall-clock latency, not token counts (AC5.5.7) -
    # cannot assert an exact figure (real elapsed time), but it must be a
    # real, present, non-negative Decimal, not NULL and not a token-based
    # guess.
    assert usage_row["cost_usd"] is not None
    assert Decimal(usage_row["cost_usd"]) >= Decimal("0")

    # Budget: the atomic `record_usage_charge` write actually ran for this
    # self-hosted request (AC5.5.8 - "budgets ... apply identically") - the
    # user's `current_spend_usd` moved by exactly the SAME amount logged in
    # `usage_logs.cost_usd` above (single shared charge amount, no
    # double-charge/undercharge between the two writes).
    user_spend = await _fetch_user_spend(migrated_database_url, user_id)
    assert Decimal(user_spend) == Decimal(usage_row["cost_usd"])


# ---------------------------------------------------------------------------
# Hardening pass item 6: `GET /v1/admin/self-hosted-providers/{id}/usage` -
# the per-endpoint requests/estimated-cost/avg-latency breakdown named in the
# Phase 5 technical design's API-contract table but never built.
# ---------------------------------------------------------------------------


async def test_usage_endpoint_unknown_provider_404(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get(
        f"{_URL}/00000000-0000-0000-0000-000000000099/usage", headers=auth_headers
    )
    assert response.status_code == 404


async def test_usage_endpoint_requires_admin_or_auditor(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{_URL}/00000000-0000-0000-0000-000000000099/usage")
    assert response.status_code in (401, 403)


async def test_usage_endpoint_zero_when_no_requests_yet(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    register_response = await client.post(_URL, json=_register_payload(), headers=auth_headers)
    provider_id = register_response.json()["id"]

    response = await client.get(f"{_URL}/{provider_id}/usage", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["self_hosted_provider_id"] == provider_id
    assert body["total_requests"] == 0
    assert Decimal(body["total_estimated_cost_usd"]) == Decimal("0")
    assert body["avg_latency_ms"] == 0


async def test_usage_endpoint_reflects_a_real_request_through_the_full_pipeline(
    app: FastAPI,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    """Registers a real endpoint, drives one real (mocked-HTTP) chat
    completion through it (same mechanic as `test_e2e_chat_completion_self_
    hosted_model_full_pipeline` above), then confirms `GET .../usage`
    reflects exactly that one request - proving the endpoint is a real
    `GROUP BY self_hosted_provider_id`-style aggregate over `usage_logs`,
    not a stub. Also proves a DIFFERENT, never-called self-hosted provider
    stays at zero (the aggregate is correctly scoped to one endpoint, never
    org-wide)."""
    secret, _user_id = await _make_service_account_secret(migrated_database_url)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_canned_openai_shaped_response(
                "usage-endpoint-llama3", "hello from the usage-endpoint test"
            ),
        )

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as admin_client:
                register_response = await admin_client.post(
                    _URL,
                    json={
                        "name": "usage-endpoint-provider",
                        "base_url": "http://usage-endpoint-stub.internal:8000",
                        "bearer_token": "usage-endpoint-bearer-token",
                        "cost_basis_per_gpu_hour": "3.6000",
                        "models": ["usage-endpoint-llama3"],
                    },
                    headers=auth_headers,
                )
                assert register_response.status_code == 201, register_response.text
                provider_id = register_response.json()["id"]
                verify_response = await admin_client.post(
                    f"{_URL}/{provider_id}/verify", headers=auth_headers
                )
                assert verify_response.status_code == 200, verify_response.text

                # A second, never-called provider - must stay at zero below.
                other_register_response = await admin_client.post(
                    _URL,
                    json=_register_payload(name="usage-endpoint-unused-provider"),
                    headers=auth_headers,
                )
                assert other_register_response.status_code == 201, other_register_response.text
                other_provider_id = other_register_response.json()["id"]

                chat_response = await admin_client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "usage-endpoint-llama3",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"Authorization": f"Bearer {secret}"},
                )
                assert chat_response.status_code == 200, chat_response.text

                usage_response = await admin_client.get(
                    f"{_URL}/{provider_id}/usage", headers=auth_headers
                )
                other_usage_response = await admin_client.get(
                    f"{_URL}/{other_provider_id}/usage", headers=auth_headers
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    usage_row = await _fetch_usage_log(migrated_database_url, "usage-endpoint-llama3")
    assert usage_row is not None

    assert usage_response.status_code == 200, usage_response.text
    body = usage_response.json()
    assert body["self_hosted_provider_id"] == provider_id
    assert body["total_requests"] == 1
    assert Decimal(body["total_estimated_cost_usd"]) == Decimal(usage_row["cost_usd"])
    assert Decimal(body["total_estimated_cost_usd"]) >= Decimal("0")
    # Real wall-clock latency was recorded (never a fabricated/zero figure
    # for an actual served request).
    assert body["avg_latency_ms"] > 0

    assert other_usage_response.status_code == 200, other_usage_response.text
    other_body = other_usage_response.json()
    assert other_body["total_requests"] == 0
    assert Decimal(other_body["total_estimated_cost_usd"]) == Decimal("0")


async def test_e2e_self_hosted_model_rejected_on_completions_endpoint(
    app: FastAPI,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    """AC5.5.4/design doc section 7.3: a self-hosted model id is
    unroutable on `/v1/completions` - `resolve_route()` there never receives
    the cache argument (structural enforcement, not a runtime check), so
    this 404s exactly like any other unknown model."""
    secret, _user_id = await _make_service_account_secret(migrated_database_url)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            register_response = await client.post(
                "/v1/admin/self-hosted-providers",
                json={
                    "name": "completions-rejection-endpoint",
                    "base_url": "http://self-hosted-stub-2.internal:8000",
                    "bearer_token": "token",
                    "cost_basis_per_gpu_hour": "1.0000",
                    "models": ["completions-rejection-model"],
                },
                headers=auth_headers,
            )
            assert register_response.status_code == 201, register_response.text
            provider_id = register_response.json()["id"]
            verify_response = await client.post(
                f"/v1/admin/self-hosted-providers/{provider_id}/verify", headers=auth_headers
            )
            assert verify_response.json()["verified"] is True

            completions_response = await client.post(
                "/v1/completions",
                json={"model": "completions-rejection-model", "prompt": "hi"},
                headers={"Authorization": f"Bearer {secret}"},
            )
    assert completions_response.status_code == 404
    assert completions_response.json()["error"]["code"] == "model_not_found"


async def test_e2e_unverified_self_hosted_model_is_unroutable(
    app: FastAPI,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    """design doc section 7.3: registered but NOT (yet) verified ->
    `resolve_route()`'s cache-backed fallback treats it as unknown -
    `ModelNotFoundError`, same 404 shape as any unknown model."""
    secret, _user_id = await _make_service_account_secret(migrated_database_url)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            register_response = await client.post(
                "/v1/admin/self-hosted-providers",
                json={
                    "name": "never-verified-endpoint",
                    "base_url": "http://self-hosted-stub-3.internal:8000",
                    "bearer_token": "token",
                    "cost_basis_per_gpu_hour": "1.0000",
                    "models": ["never-verified-model"],
                },
                headers=auth_headers,
            )
            assert register_response.status_code == 201, register_response.text
            # Deliberately never calling POST .../verify.

            chat_response = await client.post(
                "/v1/chat/completions",
                json={
                    "model": "never-verified-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"Authorization": f"Bearer {secret}"},
            )
    assert chat_response.status_code == 404
    assert chat_response.json()["error"]["code"] == "model_not_found"
