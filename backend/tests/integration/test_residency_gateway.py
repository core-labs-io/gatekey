"""Integration tests (Phase 3, BD-3/BD-4/BD-6): a real `POST /v1/chat/
completions` request against a real Postgres proves data-residency
enforcement is a genuine hard-block, not a silent reroute (AC3.6), and that
an explicit warn-only rule lets the request through while still logging the
violation (AC3.5/ratified #12) - exercised through the actual gateway
pipeline (`check_residency`), not just `services.residency.resolve_
residency` in isolation (already covered by `tests/unit/test_residency_
service.py`).

`openai` resolves to the static region "us" (`services.residency.
_PROVIDER_STATIC_REGION`) with zero DB/key lookup needed, so these tests
don't need a provider key configured at all for the hard-block case - the
request never gets far enough to need one. The warn case is proven the same
way `test_access_schedule_gateway.py` proves an override was consulted: the
request proceeds PAST residency and fails LATER with a different, later-
pipeline error (`provider_not_configured`), never `residency_violation`.
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


@pytest.fixture(autouse=True)
async def _clean_residency_tables(migrated_database_url: str):
    async def _truncate() -> None:
        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            await conn.execute("TRUNCATE TABLE residency_rules CASCADE")
        finally:
            await conn.close()

    await _truncate()
    yield
    await _truncate()


async def _count_residency_audit_entries(database_url: str, action: str) -> int:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchval(
            "SELECT count(*) FROM audit_entries WHERE action = $1", action
        )
    finally:
        await conn.close()


async def _make_service_account_secret(client, auth_headers, *, user_id: str, team_id: str) -> str:
    response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "residency-test-key", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["secret"]


async def test_hard_block_residency_rule_rejects_request_with_structured_error(
    client, auth_headers, default_user_id, default_team_id, migrated_database_url
) -> None:
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    # openai is statically "us" - an org rule allowing only "eu" always
    # violates it, with no provider key needed for the request to reach
    # this check.
    put_resp = await client.put(
        "/v1/admin/residency-rules",
        json={"allowed_regions": ["eu"], "violation_behavior": "hard_block"},
        headers=auth_headers,
    )
    assert put_resp.status_code == 200, put_resp.text
    assert put_resp.json()["violation_behavior"] == "hard_block"

    response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"]["code"] == "residency_violation"

    assert await _count_residency_audit_entries(migrated_database_url, "residency.hard_block") >= 1


async def test_warn_only_residency_rule_allows_request_through_but_logs_violation(
    client, auth_headers, default_user_id, default_team_id, migrated_database_url
) -> None:
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    put_resp = await client.put(
        "/v1/admin/residency-rules",
        json={"allowed_regions": ["eu"], "violation_behavior": "warn"},
        headers=auth_headers,
    )
    assert put_resp.status_code == 200, put_resp.text

    response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    # No provider key configured, so this fails LATER in the pipeline - the
    # important assertion is what it is NOT: never `residency_violation`,
    # proving "warn" let the request continue past the check.
    assert response.json()["error"]["code"] == "provider_not_configured"

    assert await _count_residency_audit_entries(migrated_database_url, "residency.warn") >= 1


async def test_residency_rule_creation_defaults_to_hard_block(client, auth_headers) -> None:
    """AC3.2: the create path must not silently default to warn."""
    put_resp = await client.put(
        "/v1/admin/residency-rules",
        json={"allowed_regions": ["us", "eu"]},
        headers=auth_headers,
    )
    assert put_resp.status_code == 200, put_resp.text
    assert put_resp.json()["violation_behavior"] == "hard_block"


async def test_downgrading_hard_block_to_warn_is_audited_as_weakened(
    client, auth_headers, migrated_database_url
) -> None:
    put_resp = await client.put(
        "/v1/admin/residency-rules",
        json={"allowed_regions": ["us"], "violation_behavior": "hard_block"},
        headers=auth_headers,
    )
    assert put_resp.status_code == 200, put_resp.text

    downgrade_resp = await client.put(
        "/v1/admin/residency-rules",
        json={"allowed_regions": ["us"], "violation_behavior": "warn"},
        headers=auth_headers,
    )
    assert downgrade_resp.status_code == 200, downgrade_resp.text

    assert await _count_residency_audit_entries(migrated_database_url, "residency_rule.weakened") >= 1


def _canned_openrouter_response() -> dict:
    return {
        "id": "chatcmpl-residency-test",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "openai/gpt-4o-mini",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    }


async def test_openrouter_without_trusted_providers_blocked_by_us_only_residency_rule(
    app: FastAPI, client, auth_headers, default_user_id, default_team_id
) -> None:
    """The exact scenario this feature was built for: an admin has a real
    OpenRouter key configured (no `trusted_provider_slugs`/`_region` set)
    and an active "us"-only hard-block rule - the request must still be
    blocked, because nothing has actually restricted where OpenRouter might
    route this specific call. Confirms the pre-feature behavior is
    unchanged for anyone who hasn't opted into the new fields."""
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    key_resp = await client.put(
        "/v1/admin/providers/openrouter/key",
        json={"api_key": "sk-or-test-no-restriction"},
        headers=auth_headers,
    )
    assert key_resp.status_code == 200, key_resp.text

    rule_resp = await client.put(
        "/v1/admin/residency-rules",
        json={"allowed_regions": ["us"], "violation_behavior": "hard_block"},
        headers=auth_headers,
    )
    assert rule_resp.status_code == 200, rule_resp.text

    response = await client.post(
        "/v1/chat/completions",
        json={"model": "openrouter/openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "residency_violation"


async def test_openrouter_with_trusted_providers_satisfies_us_only_residency_rule(
    app: FastAPI, client, auth_headers, default_user_id, default_team_id
) -> None:
    """With `trusted_provider_slugs`/`trusted_provider_region="us"`
    configured, the SAME rule that blocked the request above must now let
    it through - AND the actual outbound OpenRouter call must carry
    `provider.only` restricted to exactly that list, proving the region
    claim is enforced, not just asserted (see `services.residency.
    resolve_model_region`'s openrouter branch)."""
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    key_resp = await client.put(
        "/v1/admin/providers/openrouter/key",
        json={
            "api_key": "sk-or-test-restricted",
            "trusted_provider_slugs": ["openai", "anthropic"],
            "trusted_provider_region": "us",
        },
        headers=auth_headers,
    )
    assert key_resp.status_code == 200, key_resp.text
    assert key_resp.json()["metadata"] == {
        "trusted_provider_slugs": ["openai", "anthropic"],
        "trusted_provider_region": "us",
    }

    rule_resp = await client.put(
        "/v1/admin/residency-rules",
        json={"allowed_regions": ["us"], "violation_behavior": "hard_block"},
        headers=auth_headers,
    )
    assert rule_resp.status_code == 200, rule_resp.text

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_canned_openrouter_response())

    app.dependency_overrides[get_provider_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        response = await client.post(
            "/v1/chat/completions",
            json={"model": "openrouter/openai/gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {secret}"},
        )
    finally:
        del app.dependency_overrides[get_provider_http_client]

    assert response.status_code == 200, response.text
    assert captured["body"]["provider"] == {"only": ["openai", "anthropic"]}
