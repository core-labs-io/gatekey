"""Integration tests (Phase 3, BD-1/BD-6): a real `POST /v1/chat/
completions` request carrying synthetic PII round-trips through the ACTUAL
gateway pipeline (`check_residency -> run_dlp_scan -> check_content_
classification -> ... -> fetch_credential -> provider call`), not just
`services.dlp` in isolation (already covered by
`tests/unit/test_dlp_service.py`).

Exercises AC2.5's three action semantics end-to-end against a real Postgres
and a stubbed provider HTTP server (`httpx.MockTransport`, same pattern as
`test_gateway_ollama_openrouter.py`):

- redact: the PROVIDER-BOUND request body must never contain the flagged
  substring - proving redaction happens before the outbound call, not just
  that `scan_texts()` returns a redacted string somewhere.
- block: the mock provider handler must never be invoked at all (a call
  counter), and the block itself is durably recorded (`dlp_scan_results`
  row + `dlp.block` audit entry) even though the request never reached the
  provider.
- log: the request succeeds with the ORIGINAL (unredacted) content reaching
  the provider (AC2.5's "no redaction applied" for log-only), and a
  `dlp_scan_results` row still appears (`ran_sync=False`) once the
  background task has run - proving the scan happened without holding the
  request open (AC2.6).
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

_SSN = "234-56-7890"  # Presidio's UsSsnRecognizer invalidates the textbook "123-45-6789" placeholder.
_EMAIL = "jane.doe@example.com"


@pytest.fixture(autouse=True)
async def _clean_dlp_tables(migrated_database_url: str):
    async def _truncate() -> None:
        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            await conn.execute(
                "TRUNCATE TABLE dlp_policies, dlp_custom_patterns, "
                "team_dlp_action_overrides, dlp_scan_results CASCADE"
            )
        finally:
            await conn.close()

    await _truncate()
    yield
    await _truncate()


def _canned_response(model: str) -> dict:
    return {
        "id": "chatcmpl-dlp-test",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    }


async def _fetch_scan_result(database_url: str, *, model: str) -> asyncpg.Record | None:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchrow(
            "SELECT ran_sync, action_taken, findings FROM dlp_scan_results "
            "WHERE model = $1 ORDER BY created_at DESC LIMIT 1",
            model,
        )
    finally:
        await conn.close()


async def _count_dlp_block_audit_entries(database_url: str) -> int:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchval("SELECT count(*) FROM audit_entries WHERE action = 'dlp.block'")
    finally:
        await conn.close()


async def _make_service_account_secret(client, auth_headers, *, user_id: str, team_id: str) -> str:
    response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "dlp-test-key", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["secret"]


async def test_dlp_redact_mode_strips_pii_before_reaching_provider(
    app: FastAPI, client, auth_headers, default_user_id, default_team_id, migrated_database_url
) -> None:
    call_count = 0
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_canned_response("llama3.1"))

    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    put_resp = await client.put(
        "/v1/admin/dlp-policy",
        json={"email_detector_enabled": True, "default_action": "redact"},
        headers=auth_headers,
    )
    assert put_resp.status_code == 200, put_resp.text
    key_resp = await client.put(
        "/v1/admin/providers/ollama/key",
        json={"base_url": "http://ollama-dlp-stub.internal:11434"},
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
                "model": "ollama/llama3.1",
                "messages": [{"role": "user", "content": f"reach me at {_EMAIL} please"}],
            },
            headers={"Authorization": f"Bearer {secret}"},
        )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert response.status_code == 200, response.text
    assert call_count == 1
    # The core AC2.5 assertion: the PROVIDER never saw the raw email.
    sent_content = captured["body"]["messages"][0]["content"]
    assert _EMAIL not in sent_content
    assert "[REDACTED]" in sent_content

    scan_row = await _fetch_scan_result(migrated_database_url, model="ollama/llama3.1")
    assert scan_row is not None
    assert scan_row["ran_sync"] is True
    assert scan_row["action_taken"] == "redact"


async def test_dlp_block_mode_rejects_before_reaching_provider(
    app: FastAPI, client, auth_headers, default_user_id, default_team_id, migrated_database_url
) -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(200, json=_canned_response("llama3.1"))

    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    put_resp = await client.put(
        "/v1/admin/dlp-policy",
        json={"ssn_detector_enabled": True, "default_action": "block"},
        headers=auth_headers,
    )
    assert put_resp.status_code == 200, put_resp.text

    app.dependency_overrides[get_provider_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        # No provider key configured at all - proves the block happens
        # BEFORE `fetch_credential`, not merely before the HTTP call.
        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "ollama/llama3.1",
                "messages": [{"role": "user", "content": f"my SSN is {_SSN} today"}],
            },
            headers={"Authorization": f"Bearer {secret}"},
        )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "dlp_blocked"
    assert call_count == 0  # provider never called

    scan_row = await _fetch_scan_result(migrated_database_url, model="ollama/llama3.1")
    assert scan_row is not None
    assert scan_row["ran_sync"] is True
    assert scan_row["action_taken"] == "block"

    assert await _count_dlp_block_audit_entries(migrated_database_url) >= 1


async def test_dlp_log_mode_never_redacts_and_never_blocks_but_still_records(
    app: FastAPI, client, auth_headers, default_user_id, default_team_id, migrated_database_url
) -> None:
    call_count = 0
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_canned_response("llama3.1"))

    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    put_resp = await client.put(
        "/v1/admin/dlp-policy",
        json={"ssn_detector_enabled": True, "default_action": "log"},
        headers=auth_headers,
    )
    assert put_resp.status_code == 200, put_resp.text
    key_resp = await client.put(
        "/v1/admin/providers/ollama/key",
        json={"base_url": "http://ollama-dlp-stub.internal:11434"},
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
                "model": "ollama/llama3.1",
                "messages": [{"role": "user", "content": f"my SSN is {_SSN} today"}],
            },
            headers={"Authorization": f"Bearer {secret}"},
        )
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    assert response.status_code == 200, response.text
    assert call_count == 1
    # AC2.5: log never redacts - the provider saw the RAW SSN.
    assert _SSN in captured["body"]["messages"][0]["content"]

    scan_row = await _fetch_scan_result(migrated_database_url, model="ollama/llama3.1")
    assert scan_row is not None
    assert scan_row["ran_sync"] is False
    assert scan_row["action_taken"] == "log"


async def test_put_dlp_policy_rejects_scan_inbound_responses_true(
    client, auth_headers, migrated_database_url
) -> None:
    """Security review finding 4: `scan_inbound_responses` is a persisted
    field with no scanning implementation behind it - `true` must 422
    cleanly, not silently round-trip as if it did something."""
    response = await client.put(
        "/v1/admin/dlp-policy",
        json={"scan_inbound_responses": True},
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "inbound_scanning_not_implemented"

    # The rejected write must not have persisted (nor a stray audit row).
    get_resp = await client.get("/v1/admin/dlp-policy", headers=auth_headers)
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["scan_inbound_responses"] is False
