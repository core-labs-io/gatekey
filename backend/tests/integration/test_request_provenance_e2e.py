"""Request-provenance logging integration tests (added by migration `0047`)
- "which system did each user use" for off-network-usage/leaked-key
monitoring: `source_ip`/`client_user_agent` on every `usage_logs` row, and
`personal_api_keys.device_label` self-reported via the CLI-sync
device-code flow.

Mirrors `test_phase4_usage_log_columns_e2e.py`'s established pattern: a
REAL request through the actual HTTP gateway route (provider call
mocked via `httpx.MockTransport`), verified against real Postgres rows -
not just that `record_usage_log()`'s signature accepts the new params
(every unit test fakes that function away entirely).
"""

from __future__ import annotations

import uuid

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from gatekey.api.deps import get_provider_http_client

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio


def _canned_response(model: str) -> dict:
    return {
        "id": "chatcmpl-provenance-test",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    }


async def _fetch_usage_log_provenance(database_url: str, *, team_id: str) -> asyncpg.Record:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchrow(
            "SELECT source_ip, client_user_agent FROM usage_logs WHERE team_id = $1 "
            "ORDER BY created_at DESC LIMIT 1",
            uuid.UUID(team_id),
        )
    finally:
        await conn.close()


async def _make_service_account_secret(client, auth_headers, *, user_id: str, team_id: str) -> str:
    response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "provenance-test-key", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["secret"]


async def test_real_gateway_request_populates_source_ip_and_user_agent(
    app: FastAPI, client, auth_headers, default_user_id, default_team_id, migrated_database_url
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_canned_response("gpt-4o"))

    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    key_resp = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-test-provenance"},
        headers=auth_headers,
    )
    assert key_resp.status_code == 200, key_resp.text

    app.dependency_overrides[get_provider_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "provenance e2e test"}],
            },
            headers={
                "Authorization": f"Bearer {secret}",
                "User-Agent": "kilo-code-test/1.0",
            },
        )
        assert response.status_code == 200, response.text
    finally:
        del app.dependency_overrides[get_provider_http_client]

    row = await _fetch_usage_log_provenance(migrated_database_url, team_id=default_team_id)
    assert row is not None
    # httpx's ASGITransport test client presents as a loopback address -
    # the real signal under test is that SOME IP was captured at all, not
    # its exact value (that's `get_source_ip`'s own, already-tested job).
    assert row["source_ip"] is not None
    assert row["client_user_agent"] == "kilo-code-test/1.0"


# --- GET /v1/admin/usage/requests --------------------------------------------


async def test_usage_requests_endpoint_returns_provenance_fields(
    app: FastAPI, client, auth_headers, default_user_id, default_team_id, migrated_database_url
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_canned_response("gpt-4o"))

    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-test-provenance-2"},
        headers=auth_headers,
    )

    app.dependency_overrides[get_provider_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        chat_resp = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "requests-endpoint test"}],
            },
            headers={"Authorization": f"Bearer {secret}", "User-Agent": "listing-test/2.0"},
        )
        assert chat_resp.status_code == 200, chat_resp.text
    finally:
        del app.dependency_overrides[get_provider_http_client]

    listing = await client.get(
        "/v1/admin/usage/requests",
        params={"team_id": default_team_id, "range": "24h"},
        headers=auth_headers,
    )
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] >= 1
    matching = [e for e in body["entries"] if e["client_user_agent"] == "listing-test/2.0"]
    assert len(matching) == 1
    entry = matching[0]
    assert entry["source_ip"] is not None
    assert entry["team_id"] == default_team_id
    assert entry["model"] == "gpt-4o"
    assert entry["success"] is True
    # This key wasn't minted through CLI-sync device pairing.
    assert entry["device_label"] is None


async def test_usage_requests_endpoint_filters_by_user_id(
    client, auth_headers, default_user_id
) -> None:
    response = await client.get(
        "/v1/admin/usage/requests",
        params={"user_id": str(uuid.uuid4()), "range": "24h"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json()["entries"] == []
    assert response.json()["total"] == 0


# --- CLI-sync device_label end-to-end ----------------------------------------


async def test_cli_sync_device_label_reaches_personal_api_key_row(
    app: FastAPI, client, default_user_id, default_team_id, migrated_database_url
) -> None:
    """The full device-code dance: `start` (CLI, self-reports a label) ->
    `approve` (browser session) -> the minted `PersonalApiKey.device_label`
    carries it through. No pre-existing integration coverage of `/approve`
    existed before this - `default_user_id`/`default_team_id` already give
    a user with a real team membership, so only the session cookie needs
    building by hand."""
    from gatekey.constants import DEFAULT_ORG_ID
    from gatekey.services.sessions import SESSION_COOKIE_NAME, create_session

    async with app.state.db_session_factory() as session:
        _, raw_token = await create_session(
            session, user_id=uuid.UUID(default_user_id), org_id=DEFAULT_ORG_ID, ttl_hours=12
        )
        await session.commit()
    cookies = {SESSION_COOKIE_NAME: raw_token}

    start_resp = await client.post(
        "/v1/auth/device/start", json={"device_label": "amits-macbook-pro"}
    )
    assert start_resp.status_code == 200, start_resp.text
    user_code = start_resp.json()["user_code"]

    approve_resp = await client.post(
        "/v1/auth/device/approve",
        json={"user_code": user_code, "team_id": default_team_id},
        cookies=cookies,
    )
    assert approve_resp.status_code == 200, approve_resp.text

    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        row = await conn.fetchrow(
            "SELECT device_label FROM personal_api_keys WHERE owner_user_id = $1",
            uuid.UUID(default_user_id),
        )
    finally:
        await conn.close()
    assert row is not None
    assert row["device_label"] == "amits-macbook-pro"
