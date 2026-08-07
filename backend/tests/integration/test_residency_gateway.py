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

import asyncpg
import pytest

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
