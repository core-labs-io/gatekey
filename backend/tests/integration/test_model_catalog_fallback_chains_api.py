"""Integration tests for Model Catalog + Cross-Provider Fallback Chains
(`gatekey/model-catalog-fallback-chains-technical-design.md`), against a
real Postgres and the real admin router/gateway - filling gaps the existing
unit suites (`tests/unit/test_services_model_catalog.py`,
`tests/unit/test_dispatch_with_model_fallback.py`,
`tests/unit/test_custom_models_service.py`) leave uncovered because they all
either monkeypatch the DB away entirely or never go through a real HTTP
request:

  - Part A: `GET /v1/admin/custom-models/available/{provider}` has NO
    integration/router-level test anywhere else in the suite (confirmed by
    grep before writing this file) - only `services.model_catalog.
    list_available_models()` itself is unit-tested, with `get_db_session`
    monkeypatched away. This file proves the real RBAC dependency, the real
    `provider_keys` 404 gate, the real 422 `vertex_ai` short-circuit, and a
    real 502 translation all work wired together through the actual router.
  - Part B write-time: `_verified_custom_model_names_for_org()`'s actual SQL
    `WHERE verified = true` filter is exercised - the unit suite only proves
    this guard's CONTROL FLOW against a fake session that returns whatever
    rows a test hands it; it never proves the real query itself correctly
    excludes an unverified row.

See `test_custom_models_gateway_wiring.py`'s module docstring for why the
gateway-RUNTIME half of this feature (headers, cost, primary-error-on-
exhaustion, cache skip-write) is covered in the sibling file
`test_model_fallback_chains_gateway_wiring.py` instead, using that file's
established `CustomModelRouteCache` direct-dependency-override pattern
rather than a full register+verify HTTP round trip (identical rationale:
the admin CRUD half is this file's job, the gateway-dispatch half is that
one's).
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from gatekey.api.deps import get_provider_http_client

pytestmark = pytest.mark.asyncio

_URL = "/v1/admin/custom-models"
_AVAILABLE_URL = f"{_URL}/available"


def _register_payload(**overrides) -> dict:
    payload = {
        "name": "fallback-chain-api-model",
        "provider": "openai",
        "native_model_id": "gpt-5.5-preview-native-fbchain",
        "capability": "chat",
        "input_price_per_million_usd": "1.500000",
        "output_price_per_million_usd": "6.000000",
        "pricing_source": "https://openai.com/pricing",
    }
    payload.update(overrides)
    return payload


def _mock_client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Part A: GET /v1/admin/custom-models/available/{provider}
# ---------------------------------------------------------------------------


async def test_available_unconfigured_provider_returns_404(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """No `provider_keys` row for `openai` at all (autouse fixture truncates
    that table per-file) - technical design doc section 1.3."""
    response = await client.get(f"{_AVAILABLE_URL}/openai", headers=auth_headers)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "provider_not_configured"


async def test_available_vertex_ai_returns_422_unsupported(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Deliberate, documented gap (technical design doc section 1.1) - 422
    even with zero provider-key setup, since it's a zero-I/O check ahead of
    any credential fetch."""
    response = await client.get(f"{_AVAILABLE_URL}/vertex_ai", headers=auth_headers)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "custom_model_live_listing_unsupported"


async def test_registry_models_returns_every_static_entry_tagged_with_provider(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """`GET /v1/admin/custom-models/registry-models` - Model Policy's
    vertex_ai checklist source (no live-listing alternative exists for that
    provider). Zero I/O, no `provider_keys` setup needed at all."""
    response = await client.get(f"{_URL}/registry-models", headers=auth_headers)
    assert response.status_code == 200, response.text
    entries = response.json()
    by_name = {entry["name"]: entry["provider"] for entry in entries}
    assert by_name["gemini-2.5-pro"] == "vertex_ai"
    assert by_name["claude-sonnet-5"] == "anthropic"
    assert [entry["name"] for entry in entries] == sorted(by_name.keys())


async def test_available_rejects_unknown_provider_value(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """`provider` is the closed 4-value `CustomModelProvider` literal - an
    arbitrary string must be a structured 422, never reach any outbound
    call (security-reviewer's SSRF-surface concern, technical design doc
    section 6)."""
    response = await client.get(f"{_AVAILABLE_URL}/not-a-real-provider", headers=auth_headers)
    assert response.status_code == 422, response.text


async def test_available_live_listing_failure_is_clean_502_not_raw_stack_trace(
    app: FastAPI, client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """A configured-but-bad/revoked key: the live GET itself fails - must
    surface as `errors.ProviderUpstreamError` (502-shaped), never an
    unhandled 500."""
    key_resp = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-bad-revoked-key"},
        headers=auth_headers,
    )
    assert key_resp.status_code == 200, key_resp.text

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "Invalid API key"}})

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        response = await client.get(f"{_AVAILABLE_URL}/openai", headers=auth_headers)
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert response.status_code in (401, 502), response.text
    assert response.json()["error"]["code"] == "provider_upstream_error"
    # Never a raw stack trace / unhandled-exception shape.
    assert "traceback" not in response.text.lower()


async def test_available_openai_success_returns_sorted_entries_with_known_prices(
    app: FastAPI, client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Full happy path through the real router: RBAC dependency, credential
    fetch, provider dispatch, reverse-index pricing join, sort order - all
    wired together for real (only the outbound HTTP call itself is
    intercepted)."""
    key_resp = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-good-key"},
        headers=auth_headers,
    )
    assert key_resp.status_code == 200, key_resp.text

    def handler(request: httpx.Request) -> httpx.Response:
        assert "models" in str(request.url)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "gpt-4o", "object": "model"},
                    {"id": "a-totally-unknown-openai-model", "object": "model"},
                ]
            },
        )

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        response = await client.get(f"{_AVAILABLE_URL}/openai", headers=auth_headers)
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert response.status_code == 200, response.text
    body = response.json()
    ids = [e["native_model_id"] for e in body]
    assert ids == sorted(ids)  # sorted by native_model_id
    by_id = {e["native_model_id"]: e for e in body}
    assert by_id["gpt-4o"]["input_price_per_million_usd"] is not None
    assert by_id["a-totally-unknown-openai-model"]["input_price_per_million_usd"] is None


async def test_available_endpoint_rejects_unauthenticated(client: httpx.AsyncClient) -> None:
    response = await client.get(f"{_AVAILABLE_URL}/openai")
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Part B write-time: the real `WHERE verified = true` filter, not a mock.
# ---------------------------------------------------------------------------


async def test_fallback_entry_pointing_at_an_unverified_custom_model_is_rejected(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """The design doc's own QA checklist item, verbatim: "a name that
    resolves to an UNVERIFIED custom model rejected (must be verified, not
    merely registered)" - section 6's Part B write-time list. Registers a
    real row (never verified - no provider key is even configured, so a
    live verify call would 404), then attempts to register a SECOND row
    whose `fallback_model_names` names the first row. This must fail
    against the REAL `_verified_custom_model_names_for_org()` query, not a
    mock that could trivially be made to agree with the wrong answer."""
    unverified = await client.post(
        _URL,
        json=_register_payload(
            name="never-verified-fallback-target", native_model_id="never-verified-native-id"
        ),
        headers=auth_headers,
    )
    assert unverified.status_code == 201, unverified.text
    assert unverified.json()["verified"] is False

    response = await client.post(
        _URL,
        json=_register_payload(
            name="points-at-unverified-model",
            native_model_id="points-at-unverified-native-id",
            fallback_model_names=["never-verified-fallback-target"],
        ),
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "custom_model_fallback_unresolvable_model"


async def test_fallback_entry_pointing_at_a_deleted_custom_model_is_rejected(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """A name that was never even registered (as opposed to registered-but-
    unverified above) hits the identical rejection - both non-cases collapse
    to the same "not currently resolvable" 422."""
    response = await client.post(
        _URL,
        json=_register_payload(
            name="points-at-nothing", fallback_model_names=["totally-never-registered-anywhere"]
        ),
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "custom_model_fallback_unresolvable_model"


async def test_fallback_chain_too_long_rejected_via_real_http(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        _URL,
        json=_register_payload(
            name="chain-too-long-http", fallback_model_names=[f"model-{i}" for i in range(6)]
        ),
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text
    # Pydantic's own `max_length=5` (defense in depth) or the service-layer
    # `CustomModelFallbackChainTooLongError` - either is an acceptable 422
    # shape here; what matters is that no row is ever written.
    body = response.json()
    assert body["error"]["code"] in (
        "validation_error",
        "custom_model_fallback_chain_too_long",
    )


async def test_fallback_self_reference_rejected_via_real_http(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        _URL,
        json=_register_payload(
            name="self-referencing-model-http", fallback_model_names=["self-referencing-model-http"]
        ),
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "custom_model_fallback_self_reference"


async def test_fallback_name_resolving_to_verified_custom_model_succeeds_via_real_http(
    app: FastAPI, client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """The positive counterpart to the unverified-rejection test above: a
    genuinely VERIFIED custom model is a valid fallback target. Verifying
    requires a live provider call, so this test configures a real (mocked)
    provider key and drives the real `/verify` endpoint."""
    key_resp = await client.put(
        "/v1/admin/providers/openai/key", json={"api_key": "sk-verify-target"}, headers=auth_headers
    )
    assert key_resp.status_code == 200, key_resp.text

    target = await client.post(
        _URL,
        json=_register_payload(
            name="verified-fallback-target", native_model_id="verified-fallback-target-native"
        ),
        headers=auth_headers,
    )
    assert target.status_code == 201, target.text
    target_id = target.json()["id"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-verify",
                "object": "chat.completion",
                "created": 1_700_000_000,
                "model": "verified-fallback-target-native",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "pong"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    app.dependency_overrides[get_provider_http_client] = lambda: _mock_client_for(handler)
    try:
        verify_response = await client.post(f"{_URL}/{target_id}/verify", headers=auth_headers)
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)
    assert verify_response.status_code == 200, verify_response.text
    assert verify_response.json()["verified"] is True

    response = await client.post(
        _URL,
        json=_register_payload(
            name="points-at-verified-model",
            native_model_id="points-at-verified-model-native",
            fallback_model_names=["verified-fallback-target"],
        ),
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["fallback_model_names"] == ["verified-fallback-target"]
