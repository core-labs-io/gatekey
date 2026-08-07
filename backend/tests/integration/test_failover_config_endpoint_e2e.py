"""Fix 1 (QA finding, SEVERE): before this fix, `services.provider_keys.
set_failover_config()` had zero callers anywhere under `src/gatekey/api/` -
no admin HTTP surface could ever set `ProviderKey.failover_enabled`/
`failover_target_id`, so `api.v1.gateway.common.call_provider_with_
failover()`'s reactive retry path (proven correct in isolation by
`tests/unit/test_call_provider_with_failover.py`) could never fire for a
single real request - the entire §4.1 headline feature was inert in
production.

This test drives the actual product surface end-to-end: configure a real
second provider key, enable failover on the primary via the new
`PUT /v1/admin/provider-keys/{id}/failover-config` endpoint, then send a
real `POST /v1/chat/completions` request whose primary key returns a
transient 5xx - and confirms the backup key was used, with no trace of the
primary's failure surfaced in the response (AC1.8), the `X-Failover-*`
headers reflect the backup, and a `failover_events` row was persisted.
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

_PRIMARY_KEY = "sk-test-failover-primary"
_BACKUP_KEY = "sk-test-failover-backup"


def _canned_response(model: str, content: str = "served by backup") -> dict:
    return {
        "id": "chatcmpl-failover-test",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    }


async def _make_service_account_secret(client, auth_headers, *, user_id: str, team_id: str) -> str:
    response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "failover-config-e2e-test-key", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["secret"]


async def _fetch_failover_events(database_url: str) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetch(
            "SELECT from_provider_key_id, to_provider_key_id, request_id FROM failover_events "
            "ORDER BY created_at DESC LIMIT 1"
        )
    finally:
        await conn.close()


async def test_admin_configured_failover_actually_routes_around_a_failing_primary(
    app: FastAPI, client, auth_headers, default_user_id, default_team_id, migrated_database_url
) -> None:
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )

    # Two real keys for the same provider: the first PUT becomes the
    # primary (`is_primary=True`, `services.provider_keys.add_or_replace_
    # key`'s auto-assignment - see that function's docstring), the second
    # (a distinct `label`) is the backup.
    primary_resp = await client.put(
        "/v1/admin/providers/openai/key", json={"api_key": _PRIMARY_KEY}, headers=auth_headers
    )
    assert primary_resp.status_code == 200, primary_resp.text
    backup_resp = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": _BACKUP_KEY, "label": "backup"},
        headers=auth_headers,
    )
    assert backup_resp.status_code == 200, backup_resp.text

    list_resp = await client.get("/v1/admin/provider-keys?provider=openai", headers=auth_headers)
    assert list_resp.status_code == 200, list_resp.text
    rows = list_resp.json()
    assert len(rows) == 2, rows
    primary_row = next(r for r in rows if r["is_primary"] is True)
    backup_row = next(r for r in rows if r["is_primary"] is False)

    # Fix 1: the actual product surface an admin would use - PUT the
    # failover-config endpoint, exact request/response shape asserted here.
    failover_config_resp = await client.put(
        f"/v1/admin/provider-keys/{primary_row['id']}/failover-config",
        json={"failover_enabled": True, "failover_target_id": backup_row["id"]},
        headers=auth_headers,
    )
    assert failover_config_resp.status_code == 200, failover_config_resp.text
    failover_config_body = failover_config_resp.json()
    assert failover_config_body == {
        "id": primary_row["id"],
        "provider": "openai",
        "label": primary_row["label"],
        "failover_enabled": True,
        "failover_target_id": backup_row["id"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if auth == f"Bearer {_PRIMARY_KEY}":
            # A transient upstream failure on the primary.
            return httpx.Response(503, json={"error": {"message": "primary is down"}})
        if auth == f"Bearer {_BACKUP_KEY}":
            return httpx.Response(200, json=_canned_response("gpt-4o"))
        raise AssertionError(f"unexpected Authorization header: {auth!r}")

    app.dependency_overrides[get_provider_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "failover-config e2e test"}],
            },
            headers={"Authorization": f"Bearer {secret}"},
        )
    finally:
        del app.dependency_overrides[get_provider_http_client]

    # AC1.8: no trace of the primary's failure surfaced - a plain 200 with
    # the backup's content, exactly as if the backup had been the only key.
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "served by backup"
    assert response.headers["X-Failover-Attempt"] == "1"
    assert response.headers["X-Failover-Used-Key"] == backup_row["id"]

    event_rows = await _fetch_failover_events(migrated_database_url)
    assert len(event_rows) == 1, event_rows
    assert str(event_rows[0]["from_provider_key_id"]) == primary_row["id"]
    assert str(event_rows[0]["to_provider_key_id"]) == backup_row["id"]


async def test_failover_target_must_be_a_different_key_for_the_same_provider(
    app: FastAPI, client, auth_headers, default_user_id, default_team_id
) -> None:
    """AC4.1.9 (same-provider constraint - see `provider_key_health.py`'s
    module docstring for why "same model(s)" collapses to "same provider"
    at this layer): `failover_target_id` must reference a real, different
    key for the SAME provider - 422, no DB write, straight through from
    `services.provider_keys.set_failover_config()`."""
    primary_resp = await client.put(
        "/v1/admin/providers/openai/key", json={"api_key": _PRIMARY_KEY}, headers=auth_headers
    )
    assert primary_resp.status_code == 200, primary_resp.text
    list_resp = await client.get("/v1/admin/provider-keys?provider=openai", headers=auth_headers)
    primary_id = list_resp.json()[0]["id"]

    # A key cannot be its own failover target.
    self_target_resp = await client.put(
        f"/v1/admin/provider-keys/{primary_id}/failover-config",
        json={"failover_enabled": True, "failover_target_id": primary_id},
        headers=auth_headers,
    )
    assert self_target_resp.status_code == 422, self_target_resp.text

    # A target that doesn't exist at all.
    missing_target_resp = await client.put(
        f"/v1/admin/provider-keys/{primary_id}/failover-config",
        json={"failover_enabled": True, "failover_target_id": str(uuid.uuid4())},
        headers=auth_headers,
    )
    assert missing_target_resp.status_code == 422, missing_target_resp.text

    # A different provider's key is not a valid target either.
    anthropic_resp = await client.put(
        "/v1/admin/providers/anthropic/key", json={"api_key": "sk-ant-other-provider"}, headers=auth_headers
    )
    assert anthropic_resp.status_code == 200, anthropic_resp.text
    anthropic_list = await client.get("/v1/admin/provider-keys?provider=anthropic", headers=auth_headers)
    anthropic_id = anthropic_list.json()[0]["id"]
    cross_provider_resp = await client.put(
        f"/v1/admin/provider-keys/{primary_id}/failover-config",
        json={"failover_enabled": True, "failover_target_id": anthropic_id},
        headers=auth_headers,
    )
    assert cross_provider_resp.status_code == 422, cross_provider_resp.text
