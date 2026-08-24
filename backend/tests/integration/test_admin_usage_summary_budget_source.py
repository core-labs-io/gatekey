"""Regression tests for two live-observed budget-display bugs, both rooted in
the same cause: the admin console presented the legacy, dead `User.budget_usd`
field (never read by the gateway for any team-scoped key - see
`db/models/team_membership.py`'s docstring) as if it were live for every user.

1. `GET /v1/admin/usage/summary`'s `spend_by_user[].budget_usd` used to pull
   `User.budget_usd` unconditionally, so the dashboard could show e.g. "$0.02,
   Exhausted" for a user whose real, enforced `TeamMembership.budget_usd` was
   $1.00 with plenty of headroom - misleading admin UI, not an enforcement
   bypass (requests correctly kept succeeding against the real $1.00 ceiling).

2. `GET /v1/admin/users` gave the admin console's "Edit user" form no way to
   know a user's `budget_usd` field was inert - an admin could set it
   (exactly as this bug's original report described: "Budget (USD) 0.0200000000"
   via that form) with zero effect on what's actually enforced, and zero
   indication anything was wrong. `team_memberships` now exposes each active
   membership so the frontend can warn instead of silently no-opping."""

from __future__ import annotations

import uuid

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from gatekey.api.deps import get_provider_http_client
from gatekey.constants import DEFAULT_ORG_ID

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio


def _canned_response() -> dict:
    return {
        "id": "chatcmpl-budget-source-test",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "gpt-4o",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    }


async def test_spend_by_user_budget_reflects_team_membership_not_dead_user_field(
    app: FastAPI, client: httpx.AsyncClient, auth_headers: dict, default_user_id: str, default_team_id: str
) -> None:
    # Deliberately set the legacy, dead `User.budget_usd` LOWER than the
    # real, enforced `TeamMembership.budget_usd` - if the dashboard is still
    # reading the dead field, it'll surface the low number here instead.
    legacy_resp = await client.patch(
        f"/v1/admin/users/{default_user_id}", json={"budget_usd": "0.02"}, headers=auth_headers
    )
    assert legacy_resp.status_code == 200, legacy_resp.text

    membership_resp = await client.patch(
        f"/v1/teams/{default_team_id}/members/{default_user_id}",
        json={"budget_usd": "1.00"},
        headers=auth_headers,
    )
    assert membership_resp.status_code == 200, membership_resp.text

    key_resp = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-test-budget-source"},
        headers=auth_headers,
    )
    assert key_resp.status_code == 200, key_resp.text

    secret_resp = await client.post(
        "/v1/admin/service-accounts",
        json={
            "name": "budget-source-test-key",
            "user_id": default_user_id,
            "team_id": default_team_id,
        },
        headers=auth_headers,
    )
    assert secret_resp.status_code == 201, secret_resp.text
    secret = secret_resp.json()["secret"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_canned_response())

    app.dependency_overrides[get_provider_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        chat_resp = await client.post(
            "/v1/chat/completions",
            json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert chat_resp.status_code == 200, chat_resp.text
    finally:
        del app.dependency_overrides[get_provider_http_client]

    summary_resp = await client.get(
        "/v1/admin/usage/summary", params={"team_id": default_team_id}, headers=auth_headers
    )
    assert summary_resp.status_code == 200, summary_resp.text
    rows = summary_resp.json()["spend_by_user"]
    assert len(rows) == 1, rows
    row = rows[0]
    assert row["requests"] == 1
    assert float(row["budget_usd"]) == pytest.approx(1.00), (
        "expected the real, enforced TeamMembership budget ($1.00) - got the dead "
        f"legacy User.budget_usd field instead: {row}"
    )


async def test_spend_by_user_falls_back_to_legacy_field_for_memberless_user(
    client: httpx.AsyncClient, auth_headers: dict, migrated_database_url: str
) -> None:
    """A user with zero active team memberships is the one real case where
    `check_budget_available()` genuinely does use `User.budget_usd` (`team_id
    is None`) - the dashboard must still surface that number for them, not
    silently drop it to `None` just because team-scoped users exist elsewhere.

    No admin HTTP path issues a non-team-scoped chat request (every gateway
    key is either personal/team-scoped or a team-attributed service account -
    see `db/models/personal_api_key.py`), so this inserts the `usage_logs` row
    directly via `asyncpg` (same approach `test_phase4_usage_log_columns_e2e.
    py` uses for read verification) rather than trying to drive one through
    `/v1/chat/completions`."""
    user_resp = await client.post(
        "/v1/admin/users",
        json={"name": "memberless-budget-source-test-user", "budget_usd": "5.00"},
        headers=auth_headers,
    )
    assert user_resp.status_code == 201, user_resp.text
    user_id = user_resp.json()["id"]

    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute(
            """
            INSERT INTO usage_logs
                (id, org_id, user_id, team_id, request_id, endpoint, provider, model,
                 prompt_tokens, completion_tokens, cost_usd, latency_ms, stream, status, success)
            VALUES ($1, $2, $3, NULL, $4, '/v1/chat/completions', 'openai', 'gpt-4o',
                    4, 3, 0.0001, 10, false, 'ok', true)
            """,
            uuid.uuid4(),
            DEFAULT_ORG_ID,
            uuid.UUID(user_id),
            f"budget-source-test-{uuid.uuid4().hex[:8]}",
        )
    finally:
        await conn.close()

    summary_resp = await client.get("/v1/admin/usage/summary", params={"range": "90d"}, headers=auth_headers)
    assert summary_resp.status_code == 200, summary_resp.text
    rows = [r for r in summary_resp.json()["spend_by_user"] if r["user"] == "memberless-budget-source-test-user"]
    assert len(rows) == 1, summary_resp.json()["spend_by_user"]
    assert float(rows[0]["budget_usd"]) == pytest.approx(5.00), rows[0]


async def test_get_user_exposes_active_team_memberships_for_the_edit_form(
    client: httpx.AsyncClient, auth_headers: dict, default_user_id: str, default_team_id: str
) -> None:
    """`GET /v1/admin/users/{id}` and `GET /v1/admin/users` must both surface
    `team_memberships` - the admin console's "Edit user" form uses this list
    to warn that its own `budget_usd` field has no effect once populated
    (see this module's docstring, bug 2)."""
    membership_resp = await client.patch(
        f"/v1/teams/{default_team_id}/members/{default_user_id}",
        json={"budget_usd": "1.00"},
        headers=auth_headers,
    )
    assert membership_resp.status_code == 200, membership_resp.text

    get_resp = await client.get(f"/v1/admin/users/{default_user_id}", headers=auth_headers)
    assert get_resp.status_code == 200, get_resp.text
    memberships = get_resp.json()["team_memberships"]
    assert len(memberships) == 1, memberships
    assert memberships[0]["team_id"] == default_team_id
    assert float(memberships[0]["budget_usd"]) == pytest.approx(1.00)

    list_resp = await client.get("/v1/admin/users", headers=auth_headers)
    assert list_resp.status_code == 200, list_resp.text
    listed = next(u for u in list_resp.json() if u["id"] == default_user_id)
    assert len(listed["team_memberships"]) == 1, listed

    # A brand-new, never-added-to-a-team user must come back with an empty
    # list, not a missing field or an error.
    fresh_resp = await client.post(
        "/v1/admin/users", json={"name": "no-teams-yet-user"}, headers=auth_headers
    )
    assert fresh_resp.status_code == 201, fresh_resp.text
    assert fresh_resp.json()["team_memberships"] == []
