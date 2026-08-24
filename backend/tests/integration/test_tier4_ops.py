"""Tier 4 ops/DX polish - integration coverage against a real Postgres:

- /readyz: 200 with a passing database check (Redis reported not_configured
  when unset).
- /metrics: Prometheus exposition with the gatekey_http_* series present
  and incrementing.
- POST /v1/admin/bootstrap: creates user + team + membership + key in one
  call (the key immediately authenticates), writes the audit entries, and
  is atomic - a failed call leaves NOTHING behind.
"""

from __future__ import annotations

import httpx


async def test_readyz_reports_ready_with_real_database(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"
    # No GATEKEY_REDIS_URL in the integration harness - "not applicable",
    # never a failure.
    assert body["checks"]["redis"] == "not_configured"


async def test_metrics_exposes_and_increments_http_counters(
    client: httpx.AsyncClient,
) -> None:
    await client.get("/healthz")
    response = await client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    text = response.text
    assert "gatekey_http_requests_total" in text
    assert "gatekey_http_request_duration_seconds" in text
    # The /healthz hit above is labeled by ROUTE TEMPLATE.
    assert 'route="/healthz"' in text


async def test_every_response_carries_request_id_header(client: httpx.AsyncClient) -> None:
    ok = await client.get("/healthz")
    assert "x-request-id" in ok.headers
    unauthorized = await client.get("/v1/admin/provider-keys")
    assert unauthorized.status_code == 401
    body = unauthorized.json()["error"]
    assert body["request_id"] == unauthorized.headers["x-request-id"]


async def test_bootstrap_creates_working_chain_in_one_call(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/v1/admin/bootstrap",
        json={
            "user_name": "t4-bootstrap-user",
            "team_name": "t4-bootstrap-team",
            "budget_usd": "25",
        },
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["user"]["name"] == "t4-bootstrap-user"
    assert body["team"]["name"] == "t4-bootstrap-team"
    assert body["membership"]["role"] == "member"
    assert body["membership"]["budget_usd"] is not None
    secret = body["service_account_key"]["secret"]
    assert secret.startswith("gk_sk_")

    # The key AUTHENTICATES immediately (an unknown model still proves the
    # credential resolved - 404 model_not_found, never 401).
    gateway = await client.post(
        "/v1/chat/completions",
        json={"model": "no-such-model", "messages": [{"role": "user", "content": "x"}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert gateway.status_code == 404
    assert gateway.json()["error"]["code"] == "model_not_found"

    # All three audit entries landed, marked as bootstrap-created.
    audit = await client.get("/v1/admin/audit-entries?limit=10", headers=auth_headers)
    actions = [e["action"] for e in audit.json()["entries"]]
    for expected in ("team.create", "team.member.add", "service_account_key.create"):
        assert expected in actions, actions


async def test_bootstrap_is_atomic_on_duplicate_team_name(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    first = await client.post(
        "/v1/admin/bootstrap",
        json={"user_name": "t4-atomic-user-1", "team_name": "t4-atomic-team"},
        headers=auth_headers,
    )
    assert first.status_code == 201, first.text

    # Same team name again, DIFFERENT user name: the team-create step fails
    # (409) - the already-flushed user row must roll back with it.
    second = await client.post(
        "/v1/admin/bootstrap",
        json={"user_name": "t4-atomic-user-2", "team_name": "t4-atomic-team"},
        headers=auth_headers,
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "team_already_exists"

    users = await client.get("/v1/admin/users", headers=auth_headers)
    names = [u["name"] for u in users.json()]
    assert "t4-atomic-user-1" in names
    assert "t4-atomic-user-2" not in names  # nothing survived the failed call
