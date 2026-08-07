"""Integration tests for the Custom Model Registry admin CRUD API (CMR-7)
and a full end-to-end gateway request against a custom model. See
`gatekey/custom-model-registry-technical-design.md` section 9.1's mandatory
test scenarios and section 8.1's security-reviewer mandatory flag list.

See `conftest.py` for the Postgres/Docker/migration/lifespan/validator-mock
plumbing these tests build on, and
`test_self_hosted_providers_api.py`/`test_gateway_ollama_openrouter.py` for
the direct structural precedent (admin-registration + real-provider-HTTP-
mock e2e pattern) this file mirrors. Only the outbound HTTP call to the
"provider" itself is ever intercepted (`httpx.MockTransport`, substituted
for `app.state.provider_http_client` via a FastAPI dependency override) -
everything else (admin router, RBAC, `resolve_route()`'s cache fallback,
credential decrypt, cost computation, usage logging, audit logging) runs
for real against a real Postgres.
"""

from __future__ import annotations

import json
from decimal import Decimal

import asyncpg
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from gatekey.api.deps import get_provider_http_client
from gatekey.db.models.team_membership import TeamRole
from gatekey.db.models.user import UserOrgRole
from gatekey.db.session import create_engine as db_create_engine
from gatekey.db.session import create_session_factory
from gatekey.providers.model_registry import MODEL_REGISTRY, ModelCapability, ModelRoute
from gatekey.providers.pricing import PRICING_TABLE, PricingEntry
from gatekey.services.service_accounts import create_service_account
from gatekey.services.users import create_user

from .conftest import to_asyncpg_dsn
from .phase2_helpers import (  # noqa: F401 - fixtures resolved by name
    _clean_phase2_tables,
    add_membership,
    make_team,
    make_user,
    session_cookie_headers,
    sf,
)

pytestmark = pytest.mark.asyncio

_URL = "/v1/admin/custom-models"


@pytest_asyncio.fixture(autouse=True)
async def _truncate_custom_models(migrated_database_url: str):
    """Ensure each test starts with an empty `custom_models` AND
    `self_hosted_providers` table (the bidirectional-collision tests below
    write to both) - same per-file truncation precedent
    `test_self_hosted_providers_api.py`'s own fixture establishes."""
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute("TRUNCATE TABLE custom_models CASCADE")
        await conn.execute("TRUNCATE TABLE self_hosted_providers CASCADE")
        await conn.execute("TRUNCATE TABLE model_policies")
    finally:
        await conn.close()
    yield


def _register_payload(**overrides) -> dict:
    payload = {
        "name": "custom-gpt-preview",
        "provider": "openai",
        "native_model_id": "gpt-5.5-preview-native",
        "capability": "chat",
        "input_price_per_million_usd": "1.500000",
        "output_price_per_million_usd": "6.000000",
        "pricing_source": "https://openai.com/pricing",
    }
    payload.update(overrides)
    return payload


async def _fetch_row(database_url: str, custom_model_id: str) -> asyncpg.Record | None:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchrow(
            "SELECT id, name, provider, native_model_id, capability, verified "
            "FROM custom_models WHERE id = $1",
            custom_model_id,
        )
    finally:
        await conn.close()


def _mock_client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _canned_openai_shaped_response(model: str, content: str) -> dict:
    return {
        "id": "chatcmpl-cmr7",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 6, "completion_tokens": 4, "total_tokens": 10},
    }


async def _make_service_account_secret(database_url: str) -> str:
    class _StubSettings:
        DATABASE_URL = database_url

    engine = db_create_engine(_StubSettings())  # type: ignore[arg-type]
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            user = await create_user(session, name="cmr7-test-user")
            _row, secret = await create_service_account(session, "cmr7-test-sa", user.id)
            return secret
    finally:
        await engine.dispose()


async def _fetch_val(database_url: str, query: str, *args):
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchval(query, *args)
    finally:
        await conn.close()


async def _configure_openai_key(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-test-cmr7-openai-key"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Admin CRUD - happy path
# ---------------------------------------------------------------------------


async def test_register_creates_unverified_row_no_secret_field(
    client: httpx.AsyncClient, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    response = await client.post(_URL, json=_register_payload(), headers=auth_headers)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["name"] == "custom-gpt-preview"
    assert body["provider"] == "openai"
    assert body["native_model_id"] == "gpt-5.5-preview-native"
    assert body["capability"] == "chat"
    assert body["verified"] is False
    assert body["shadowed_by_registry"] is False
    # No secret-bearing field exists on this response at all (technical
    # design doc section 3.3) - a real, structural difference from
    # `SelfHostedProviderResponse`.
    assert "bearer_token" not in body
    assert "ciphertext" not in body

    row = await _fetch_row(migrated_database_url, body["id"])
    assert row is not None
    assert row["verified"] is False


async def test_get_single_custom_model(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    register_response = await client.post(_URL, json=_register_payload(), headers=auth_headers)
    custom_model_id = register_response.json()["id"]

    response = await client.get(f"{_URL}/{custom_model_id}", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == custom_model_id


async def test_get_single_unknown_404(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.get(
        f"{_URL}/00000000-0000-0000-0000-000000000099", headers=auth_headers
    )
    assert response.status_code == 404


async def test_list_includes_registered_models(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    await client.post(_URL, json=_register_payload(), headers=auth_headers)
    response = await client.get(_URL, headers=auth_headers)
    assert response.status_code == 200
    names = [row["name"] for row in response.json()]
    assert "custom-gpt-preview" in names


async def test_edit_updates_pricing_and_does_not_reset_verified(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    register_response = await client.post(_URL, json=_register_payload(), headers=auth_headers)
    custom_model_id = register_response.json()["id"]

    edit_response = await client.put(
        f"{_URL}/{custom_model_id}",
        json={"input_price_per_million_usd": "2.000000"},
        headers=auth_headers,
    )
    assert edit_response.status_code == 200, edit_response.text
    body = edit_response.json()
    assert Decimal(body["input_price_per_million_usd"]) == Decimal("2.000000")
    # Pricing-only edits never reset `verified` (technical design doc
    # section 2.1) - still False here since this row was never verified.
    assert body["verified"] is False


async def test_remove_deletes_row(
    client: httpx.AsyncClient, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    register_response = await client.post(_URL, json=_register_payload(), headers=auth_headers)
    custom_model_id = register_response.json()["id"]

    delete_response = await client.delete(f"{_URL}/{custom_model_id}", headers=auth_headers)
    assert delete_response.status_code == 204

    row = await _fetch_row(migrated_database_url, custom_model_id)
    assert row is None

    list_response = await client.get(_URL, headers=auth_headers)
    assert custom_model_id not in [row["id"] for row in list_response.json()]


async def test_remove_unknown_404(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.delete(
        f"{_URL}/00000000-0000-0000-0000-000000000099", headers=auth_headers
    )
    assert response.status_code == 404


async def test_edit_unknown_404(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> None:
    response = await client.put(
        f"{_URL}/00000000-0000-0000-0000-000000000099",
        json={"name": "does-not-matter"},
        headers=auth_headers,
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Write-time validation guards
# ---------------------------------------------------------------------------


async def test_register_rejects_model_registry_collision(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Technical design doc section 2.1 guard #1 / section 9.1's first P0
    scenario: a name colliding with a real static `MODEL_REGISTRY` key is
    rejected BEFORE any DB write."""
    response = await client.post(
        _URL, json=_register_payload(name="gpt-4o"), headers=auth_headers
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "custom_model_name_registry_collision"


async def test_register_duplicate_name_conflict(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    first = await client.post(_URL, json=_register_payload(), headers=auth_headers)
    assert first.status_code == 201, first.text

    second = await client.post(
        _URL,
        json=_register_payload(native_model_id="a-different-native-id"),
        headers=auth_headers,
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "custom_model_name_conflict"


async def test_register_embeddings_on_anthropic_rejected(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Technical design doc section 2.5a/2.1 guard #6 - security-reviewer
    mandatory flag list item 5: `capability=embeddings` is only valid for
    `openai`/`vertex_ai`, enforced at WRITE time, not left to 422 on every
    real request."""
    response = await client.post(
        _URL,
        json=_register_payload(
            name="anthropic-embeddings-attempt",
            provider="anthropic",
            capability="embeddings",
            output_price_per_million_usd=None,
        ),
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "custom_model_embeddings_provider_unsupported"


async def test_register_ollama_provider_rejected(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        _URL, json=_register_payload(name="ollama-attempt", provider="ollama"), headers=auth_headers
    )
    # Pydantic's `Literal` on the request schema rejects `provider="ollama"`
    # before the service layer's own guard ever runs.
    assert response.status_code == 422, response.text


async def test_register_capability_pricing_mismatch_rejected(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        _URL,
        json=_register_payload(name="mismatch-model", capability="embeddings"),
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "custom_model_capability_pricing_mismatch"


# ---------------------------------------------------------------------------
# Security-reviewer mandatory flag list item 2: bidirectional collision guard
# ---------------------------------------------------------------------------


async def test_bidirectional_collision_custom_model_rejects_self_hosted_name(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Direction (a): a self-hosted model id already registered blocks a
    custom model from claiming the same name."""
    self_hosted_response = await client.post(
        "/v1/admin/self-hosted-providers",
        json={
            "name": "collision-endpoint",
            "base_url": "http://collision-stub.internal:8000",
            "bearer_token": "token",
            "cost_basis_per_gpu_hour": "1.0000",
            "models": ["shared-collision-name"],
        },
        headers=auth_headers,
    )
    assert self_hosted_response.status_code == 201, self_hosted_response.text

    custom_response = await client.post(
        _URL, json=_register_payload(name="shared-collision-name"), headers=auth_headers
    )
    assert custom_response.status_code == 422, custom_response.text
    assert custom_response.json()["error"]["code"] == "custom_model_name_self_hosted_collision"


async def test_bidirectional_collision_self_hosted_rejects_custom_model_name(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Direction (b): a custom model already registered blocks a
    self-hosted registration from claiming the same name (technical design
    doc section 5 row 15, `_validate_model_ids()`'s new third guard) - must
    be tested independently, not assumed symmetric."""
    custom_response = await client.post(
        _URL, json=_register_payload(name="shared-collision-name-2"), headers=auth_headers
    )
    assert custom_response.status_code == 201, custom_response.text

    self_hosted_response = await client.post(
        "/v1/admin/self-hosted-providers",
        json={
            "name": "collision-endpoint-2",
            "base_url": "http://collision-stub-2.internal:8000",
            "bearer_token": "token",
            "cost_basis_per_gpu_hour": "1.0000",
            "models": ["shared-collision-name-2"],
        },
        headers=auth_headers,
    )
    assert self_hosted_response.status_code == 422, self_hosted_response.text


# ---------------------------------------------------------------------------
# RBAC
# ---------------------------------------------------------------------------


async def test_write_endpoints_reject_unauthenticated(client: httpx.AsyncClient) -> None:
    response = await client.post(_URL, json=_register_payload())
    assert response.status_code in (401, 403)


async def test_member_and_team_lead_get_403(client: httpx.AsyncClient, sf) -> None:
    """Plain member (org_role NULL): 401 on the break-glass-OR-org_admin-OR-
    auditor read dependency, 403 on the session-role-only write dependency.
    A team_lead membership does NOT grant org-wide access either - leading
    a team is not an org-wide privilege (identical rationale to
    `test_phase2_governance_api.py::test_member_session_rejected_on_admin_
    surfaces`)."""
    member_id = await make_user(sf, "cmr7-plain-member")
    team_id = await make_team(sf, "cmr7-team")
    await add_membership(sf, team_id, member_id, role=TeamRole.TEAM_LEAD, budget=None)
    cookie = await session_cookie_headers(sf, member_id)

    assert (await client.get(_URL, headers=cookie)).status_code in (401, 403)
    assert (
        await client.post(_URL, json=_register_payload(), headers=cookie)
    ).status_code in (401, 403)


async def test_auditor_reads_but_cannot_mutate(client: httpx.AsyncClient, sf) -> None:
    auditor_id = await make_user(sf, "cmr7-auditor", org_role=UserOrgRole.AUDITOR)
    cookie = await session_cookie_headers(sf, auditor_id)

    # Reads allowed.
    list_response = await client.get(_URL, headers=cookie)
    assert list_response.status_code == 200, list_response.text

    # Writes rejected.
    register_response = await client.post(_URL, json=_register_payload(), headers=cookie)
    assert register_response.status_code == 403, register_response.text

    # Seed one row via the break-glass admin token to exercise edit/remove/
    # verify rejection too.
    seeded = await client.post(
        _URL, json=_register_payload(), headers={"Authorization": "Bearer integration-test-admin-token"}
    )
    assert seeded.status_code == 201, seeded.text
    custom_model_id = seeded.json()["id"]

    assert (
        await client.put(f"{_URL}/{custom_model_id}", json={"pricing_source": "nope"}, headers=cookie)
    ).status_code == 403
    assert (await client.delete(f"{_URL}/{custom_model_id}", headers=cookie)).status_code == 403
    assert (await client.post(f"{_URL}/{custom_model_id}/verify", headers=cookie)).status_code == 403


async def test_org_admin_session_cookie_has_full_access(client: httpx.AsyncClient, sf) -> None:
    admin_id = await make_user(sf, "cmr7-org-admin", org_role=UserOrgRole.ORG_ADMIN)
    cookie = await session_cookie_headers(sf, admin_id)

    register_response = await client.post(_URL, json=_register_payload(), headers=cookie)
    assert register_response.status_code == 201, register_response.text
    custom_model_id = register_response.json()["id"]

    assert (await client.get(_URL, headers=cookie)).status_code == 200
    edit_response = await client.put(
        f"{_URL}/{custom_model_id}", json={"pricing_source": "updated"}, headers=cookie
    )
    assert edit_response.status_code == 200, edit_response.text
    assert (await client.delete(f"{_URL}/{custom_model_id}", headers=cookie)).status_code == 204


# ---------------------------------------------------------------------------
# Shadowing (security-reviewer mandatory flag list item 1)
# ---------------------------------------------------------------------------


async def test_shadowed_by_registry_reflects_live_registry(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Technical design doc section 2.4/9.1's explicit acceptance test:
    register a name NOT currently in `MODEL_REGISTRY`, confirm
    `shadowed_by_registry: false`; then simulate a LATER Gatekey release
    that adds a colliding static key (test-only registry override via
    `monkeypatch.setitem` - `MODEL_REGISTRY` is a plain module-level dict,
    the same object referenced by both `resolve_model()` and
    `schemas.custom_models.is_shadowed_by_registry()`) and confirm the very
    next `GET` reports `shadowed_by_registry: true` with zero redeploy/
    restart - a live, response-build-time computation, never persisted/
    cached.
    """
    register_response = await client.post(
        _URL, json=_register_payload(name="shadow-test-model"), headers=auth_headers
    )
    assert register_response.status_code == 201, register_response.text
    custom_model_id = register_response.json()["id"]

    before = await client.get(f"{_URL}/{custom_model_id}", headers=auth_headers)
    assert before.json()["shadowed_by_registry"] is False

    monkeypatch.setitem(
        MODEL_REGISTRY,
        "shadow-test-model",
        ModelRoute(
            provider="openai", capability=ModelCapability.CHAT, native_model_id="a-static-native-id"
        ),
    )

    after = await client.get(f"{_URL}/{custom_model_id}", headers=auth_headers)
    assert after.status_code == 200, after.text
    assert after.json()["shadowed_by_registry"] is True

    list_after = await client.get(_URL, headers=auth_headers)
    shadowed_row = next(row for row in list_after.json() if row["id"] == custom_model_id)
    assert shadowed_row["shadowed_by_registry"] is True


async def test_shadowed_gateway_request_routes_to_static_entry(
    app: FastAPI,
    auth_headers: dict[str, str],
    migrated_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Technical design doc section 9.1's second half of the shadowing
    acceptance test / security-reviewer flag list item 1(c): a live gateway
    request for a shadowed name actually routes to the STATIC entry, not
    the custom one - `resolve_route()`'s static-always-wins-first ordering,
    unconditional."""
    secret = await _make_service_account_secret(migrated_database_url)

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json=_canned_openai_shaped_response("static-wins", "hello from the static route")
        )

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_client:
                await _configure_openai_key(admin_client, auth_headers)

                register_response = await admin_client.post(
                    _URL,
                    json=_register_payload(
                        name="shadow-e2e-model", native_model_id="custom-native-id-should-not-be-used"
                    ),
                    headers=auth_headers,
                )
                assert register_response.status_code == 201, register_response.text
                custom_model_id = register_response.json()["id"]
                verify_response = await admin_client.post(
                    f"{_URL}/{custom_model_id}/verify", headers=auth_headers
                )
                assert verify_response.status_code == 200, verify_response.text
                assert verify_response.json()["verified"] is True

                # Simulate a LATER Gatekey release colliding with the
                # already-registered, already-verified custom model - a
                # real static-registry addition always ships with a
                # matching PRICING_TABLE entry too (the import-time
                # `_validate_completeness()` invariant), so the test-only
                # override patches both, exactly mirroring that real-world
                # pairing.
                monkeypatch.setitem(
                    MODEL_REGISTRY,
                    "shadow-e2e-model",
                    ModelRoute(
                        provider="openai",
                        capability=ModelCapability.CHAT,
                        native_model_id="static-native-id-wins",
                    ),
                )
                monkeypatch.setitem(
                    PRICING_TABLE,
                    "shadow-e2e-model",
                    PricingEntry(
                        input_price_per_million_usd=Decimal("1.00"),
                        output_price_per_million_usd=Decimal("2.00"),
                        as_of="2026-08-06",
                        source="test-only static registry override",
                    ),
                )

                chat_response = await admin_client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "shadow-e2e-model",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"Authorization": f"Bearer {secret}"},
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert chat_response.status_code == 200, chat_response.text
    # The STATIC route's native_model_id was sent to the provider, never
    # the custom model's own `custom-native-id-should-not-be-used`.
    assert captured["body"]["model"] == "static-native-id-wins"


# ---------------------------------------------------------------------------
# Routing / capability enforcement / removal
# ---------------------------------------------------------------------------


async def test_e2e_unverified_custom_model_is_unroutable(
    app: FastAPI, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    secret = await _make_service_account_secret(migrated_database_url)

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_client:
            register_response = await admin_client.post(
                _URL, json=_register_payload(name="never-verified-custom-model"), headers=auth_headers
            )
            assert register_response.status_code == 201, register_response.text
            # Deliberately never calling POST .../verify.

            chat_response = await admin_client.post(
                "/v1/chat/completions",
                json={
                    "model": "never-verified-custom-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                headers={"Authorization": f"Bearer {secret}"},
            )
    assert chat_response.status_code == 404
    assert chat_response.json()["error"]["code"] == "model_not_found"


async def test_e2e_custom_model_rejected_on_completions_endpoint(
    app: FastAPI,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    """Technical design doc section 5 row 9 / section 7: `completions.py`
    never receives `custom_model_cache` - structural, not runtime,
    enforcement of the non-goal."""
    secret = await _make_service_account_secret(migrated_database_url)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_canned_openai_shaped_response("n/a", "should never be reached")
        )

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_client:
                await _configure_openai_key(admin_client, auth_headers)
                register_response = await admin_client.post(
                    _URL, json=_register_payload(name="completions-rejection-model"), headers=auth_headers
                )
                custom_model_id = register_response.json()["id"]
                verify_response = await admin_client.post(
                    f"{_URL}/{custom_model_id}/verify", headers=auth_headers
                )
                assert verify_response.json()["verified"] is True

                completions_response = await admin_client.post(
                    "/v1/completions",
                    json={"model": "completions-rejection-model", "prompt": "hi"},
                    headers={"Authorization": f"Bearer {secret}"},
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)
    assert completions_response.status_code == 404
    assert completions_response.json()["error"]["code"] == "model_not_found"


async def test_e2e_removed_custom_model_is_immediately_unroutable(
    app: FastAPI,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    secret = await _make_service_account_secret(migrated_database_url)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_canned_openai_shaped_response("remove-me-native", "hello before removal")
        )

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_client:
                await _configure_openai_key(admin_client, auth_headers)
                register_response = await admin_client.post(
                    _URL,
                    json=_register_payload(
                        name="remove-me-model", native_model_id="remove-me-native"
                    ),
                    headers=auth_headers,
                )
                custom_model_id = register_response.json()["id"]
                verify_response = await admin_client.post(
                    f"{_URL}/{custom_model_id}/verify", headers=auth_headers
                )
                assert verify_response.status_code == 200, verify_response.text
                assert verify_response.json()["verified"] is True

                chat_before = await admin_client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "remove-me-model",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"Authorization": f"Bearer {secret}"},
                )
                assert chat_before.status_code == 200, chat_before.text

                delete_response = await admin_client.delete(
                    f"{_URL}/{custom_model_id}", headers=auth_headers
                )
                assert delete_response.status_code == 204

                chat_after = await admin_client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "remove-me-model",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"Authorization": f"Bearer {secret}"},
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert chat_after.status_code == 404
    assert chat_after.json()["error"]["code"] == "model_not_found"


async def test_e2e_chat_completion_custom_model_cost_matches_formula(
    app: FastAPI,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    """Technical design doc section 9.1's P0 scenario: real per-token
    pricing, never an estimate - `usage_logs.cost_usd` equals
    `compute_custom_model_cost()`'s exact arithmetic."""
    secret = await _make_service_account_secret(migrated_database_url)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_canned_openai_shaped_response("cost-native-id", "hello from cost test")
        )

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_client:
                await _configure_openai_key(admin_client, auth_headers)
                register_response = await admin_client.post(
                    _URL,
                    json=_register_payload(
                        name="cost-formula-model",
                        native_model_id="cost-native-id",
                        input_price_per_million_usd="10.000000",
                        output_price_per_million_usd="20.000000",
                    ),
                    headers=auth_headers,
                )
                assert register_response.status_code == 201, register_response.text
                custom_model_id = register_response.json()["id"]
                verify_response = await admin_client.post(
                    f"{_URL}/{custom_model_id}/verify", headers=auth_headers
                )
                assert verify_response.json()["verified"] is True

                chat_response = await admin_client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "cost-formula-model",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"Authorization": f"Bearer {secret}"},
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert chat_response.status_code == 200, chat_response.text
    usage_row = await _fetch_val(
        migrated_database_url,
        "SELECT cost_usd FROM usage_logs WHERE model = $1 ORDER BY created_at DESC LIMIT 1",
        "cost-formula-model",
    )
    # `_canned_openai_shaped_response`'s usage: 6 prompt + 4 completion tokens.
    expected_cost = (Decimal(6) * Decimal("10.000000") + Decimal(4) * Decimal("20.000000")) / Decimal(
        1_000_000
    )
    assert Decimal(usage_row) == expected_cost


# ---------------------------------------------------------------------------
# Verification: success / failure / cooldown / audit
# ---------------------------------------------------------------------------


async def test_verify_success_sets_verified_true_and_writes_audit_entry(
    app: FastAPI, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_canned_openai_shaped_response("verify-native-id", "pong")
        )

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_client:
                await _configure_openai_key(admin_client, auth_headers)
                register_response = await admin_client.post(
                    _URL,
                    json=_register_payload(
                        name="verify-success-model", native_model_id="verify-native-id"
                    ),
                    headers=auth_headers,
                )
                custom_model_id = register_response.json()["id"]
                assert register_response.json()["verified"] is False

                verify_response = await admin_client.post(
                    f"{_URL}/{custom_model_id}/verify", headers=auth_headers
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert verify_response.status_code == 200, verify_response.text
    assert verify_response.json()["verified"] is True

    audit_row = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        record = await audit_row.fetchrow(
            "SELECT new_value, target_id FROM audit_entries "
            "WHERE action = 'custom_model.test_call' ORDER BY created_at DESC LIMIT 1"
        )
    finally:
        await audit_row.close()
    assert record is not None
    assert record["target_id"] == custom_model_id
    new_value = json.loads(record["new_value"])
    assert new_value["success"] is True
    assert isinstance(new_value["latency_ms"], int)
    assert new_value["latency_ms"] >= 0

    # Verification never writes a usage_logs row / touches budget (security-
    # reviewer mandatory flag list item 6).
    usage_count = await _fetch_val(
        migrated_database_url,
        "SELECT COUNT(*) FROM usage_logs WHERE model = $1",
        "verify-success-model",
    )
    assert usage_count == 0


async def test_verify_failure_leaves_unverified_and_surfaces_real_error(
    app: FastAPI, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    """A wrong-but-real `native_model_id` -> the real provider error is
    surfaced VERBATIM (never swallowed), `verified` stays False."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"message": "model not found upstream"}})

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_client:
                await _configure_openai_key(admin_client, auth_headers)
                register_response = await admin_client.post(
                    _URL,
                    json=_register_payload(
                        name="verify-failure-model", native_model_id="typo-native-id"
                    ),
                    headers=auth_headers,
                )
                custom_model_id = register_response.json()["id"]

                verify_response = await admin_client.post(
                    f"{_URL}/{custom_model_id}/verify", headers=auth_headers
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert verify_response.status_code == 404, verify_response.text
    body = verify_response.json()
    assert body["error"]["code"] == "provider_upstream_error"
    # The REAL upstream error text, never swallowed/generic.
    assert "openai" in body["error"]["message"].lower()
    assert "404" in body["error"]["message"]

    row = await _fetch_row(migrated_database_url, custom_model_id)
    assert row["verified"] is False

    audit_row = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        record = await audit_row.fetchrow(
            "SELECT new_value FROM audit_entries "
            "WHERE action = 'custom_model.test_call' AND target_id = $1 "
            "ORDER BY created_at DESC LIMIT 1",
            custom_model_id,
        )
    finally:
        await audit_row.close()
    assert record is not None
    new_value = json.loads(record["new_value"])
    assert new_value["success"] is False


async def test_verify_no_provider_key_configured_returns_404(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Technical design doc section 2.3 / product spec section 5: verifying
    with no `provider_keys` row configured fails with the SAME
    `ProviderNotConfiguredError` shape a real gateway request would produce
    - never a new credential path."""
    register_response = await client.post(
        _URL, json=_register_payload(name="no-key-configured-model"), headers=auth_headers
    )
    custom_model_id = register_response.json()["id"]

    verify_response = await client.post(f"{_URL}/{custom_model_id}/verify", headers=auth_headers)
    assert verify_response.status_code == 404, verify_response.text
    assert verify_response.json()["error"]["code"] == "provider_not_configured"


async def test_verify_cooldown_rejects_immediate_repeat(
    app: FastAPI, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_canned_openai_shaped_response("cooldown-native-id", "pong")
        )

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_client:
                await _configure_openai_key(admin_client, auth_headers)
                register_response = await admin_client.post(
                    _URL,
                    json=_register_payload(
                        name="cooldown-model", native_model_id="cooldown-native-id"
                    ),
                    headers=auth_headers,
                )
                custom_model_id = register_response.json()["id"]

                first = await admin_client.post(
                    f"{_URL}/{custom_model_id}/verify", headers=auth_headers
                )
                assert first.status_code == 200, first.text

                second = await admin_client.post(
                    f"{_URL}/{custom_model_id}/verify", headers=auth_headers
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert second.status_code == 429, second.text
    assert second.json()["error"]["code"] == "custom_model_verify_cooldown"
    assert "Retry-After" in second.headers


# ---------------------------------------------------------------------------
# Editing native_model_id/provider/capability resets `verified`
# ---------------------------------------------------------------------------


async def test_edit_native_model_id_resets_verified(
    app: FastAPI, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_canned_openai_shaped_response("edit-native-id", "pong"))

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_client:
                await _configure_openai_key(admin_client, auth_headers)
                register_response = await admin_client.post(
                    _URL,
                    json=_register_payload(name="edit-native-model", native_model_id="edit-native-id"),
                    headers=auth_headers,
                )
                custom_model_id = register_response.json()["id"]
                verify_response = await admin_client.post(
                    f"{_URL}/{custom_model_id}/verify", headers=auth_headers
                )
                assert verify_response.json()["verified"] is True

                edit_response = await admin_client.put(
                    f"{_URL}/{custom_model_id}",
                    json={"native_model_id": "a-different-native-id"},
                    headers=auth_headers,
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert edit_response.status_code == 200, edit_response.text
    assert edit_response.json()["verified"] is False
    assert edit_response.json()["native_model_id"] == "a-different-native-id"


async def test_edit_provider_resets_verified(
    app: FastAPI, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_canned_openai_shaped_response("provider-edit-id", "pong"))

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_client:
                await _configure_openai_key(admin_client, auth_headers)
                register_response = await admin_client.post(
                    _URL,
                    json=_register_payload(name="provider-edit-model", native_model_id="provider-edit-id"),
                    headers=auth_headers,
                )
                custom_model_id = register_response.json()["id"]
                verify_response = await admin_client.post(
                    f"{_URL}/{custom_model_id}/verify", headers=auth_headers
                )
                assert verify_response.json()["verified"] is True

                edit_response = await admin_client.put(
                    f"{_URL}/{custom_model_id}",
                    json={"provider": "openrouter"},
                    headers=auth_headers,
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert edit_response.status_code == 200, edit_response.text
    assert edit_response.json()["verified"] is False
    assert edit_response.json()["provider"] == "openrouter"


async def test_edit_capability_resets_verified(
    app: FastAPI, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_canned_openai_shaped_response("capability-edit-id", "pong"))

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_client:
                await _configure_openai_key(admin_client, auth_headers)
                register_response = await admin_client.post(
                    _URL,
                    json=_register_payload(
                        name="capability-edit-model", native_model_id="capability-edit-id"
                    ),
                    headers=auth_headers,
                )
                custom_model_id = register_response.json()["id"]
                verify_response = await admin_client.post(
                    f"{_URL}/{custom_model_id}/verify", headers=auth_headers
                )
                assert verify_response.json()["verified"] is True

                edit_response = await admin_client.put(
                    f"{_URL}/{custom_model_id}",
                    json={"capability": "embeddings", "output_price_per_million_usd": None},
                    headers=auth_headers,
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert edit_response.status_code == 200, edit_response.text
    assert edit_response.json()["verified"] is False
    assert edit_response.json()["capability"] == "embeddings"


# ---------------------------------------------------------------------------
# Chat-capability model requested at /v1/embeddings (and vice versa)
# ---------------------------------------------------------------------------


async def test_e2e_chat_capability_model_rejected_at_embeddings_endpoint(
    app: FastAPI,
    auth_headers: dict[str, str],
    migrated_database_url: str,
) -> None:
    secret = await _make_service_account_secret(migrated_database_url)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json=_canned_openai_shaped_response("chat-only-native-id", "should not be reached")
        )

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as admin_client:
                await _configure_openai_key(admin_client, auth_headers)
                register_response = await admin_client.post(
                    _URL,
                    json=_register_payload(
                        name="chat-only-model", native_model_id="chat-only-native-id"
                    ),
                    headers=auth_headers,
                )
                custom_model_id = register_response.json()["id"]
                verify_response = await admin_client.post(
                    f"{_URL}/{custom_model_id}/verify", headers=auth_headers
                )
                assert verify_response.json()["verified"] is True

                embeddings_response = await admin_client.post(
                    "/v1/embeddings",
                    json={"model": "chat-only-model", "input": "hi"},
                    headers={"Authorization": f"Bearer {secret}"},
                )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert embeddings_response.status_code == 400, embeddings_response.text
    assert embeddings_response.json()["error"]["code"] == "unsupported_request"
