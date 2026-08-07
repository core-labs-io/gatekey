"""Integration test (Phase 3, cross-cutting): the gateway pipeline's actual
call order (`common.py` module docstring / design doc section 3.3 and 5.3)
is `require_gateway_credential -> check_access_schedule -> resolve_route ->
check_model_policy -> check_residency -> DLP scan -> check_content_
classification -> check_budget_available -> fetch_credential`.

This test targets the specific regression class an ordering bug would
introduce: a request that's ALREADY rejected by an early, cheap check
(access schedule) must never pay for - or even reach - a later, more
expensive check (the DLP scan). If a future change accidentally reordered
these steps (e.g. ran DLP before the schedule check), this test fails by
asserting BOTH the error code AND the complete absence of any DLP scan
side-effect (no `dlp_scan_results` row, no `dlp.block` audit entry) for a
request that also would have tripped an aggressive `block`-everything DLP
policy, had it been reached.
"""

from __future__ import annotations

from datetime import datetime, timezone

import asyncpg
import pytest

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _clean_tables(migrated_database_url: str):
    async def _truncate() -> None:
        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            await conn.execute(
                "TRUNCATE TABLE access_schedules, holiday_dates, emergency_overrides, "
                "dlp_policies, dlp_custom_patterns, team_dlp_action_overrides, "
                "dlp_scan_results CASCADE"
            )
        finally:
            await conn.close()

    await _truncate()
    yield
    await _truncate()


async def _count(database_url: str, query: str, *args) -> int:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchval(query, *args)
    finally:
        await conn.close()


async def _make_service_account_secret(client, auth_headers, *, user_id: str, team_id: str) -> str:
    response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "pipeline-order-test-key", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["secret"]


async def test_access_schedule_block_short_circuits_before_dlp_scan_ever_runs(
    client, auth_headers, default_user_id, default_team_id, migrated_database_url
) -> None:
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    # `audit_entries` is a shared, never-truncated-between-files table (by
    # design - see `test_access_schedule_gateway.py`'s own fixture
    # docstring), so compare a delta rather than assuming a global zero.
    dlp_block_audits_before = await _count(
        migrated_database_url, "SELECT count(*) FROM audit_entries WHERE action = 'dlp.block'"
    )

    # A DLP policy that would BLOCK this exact prompt if the scan ever ran.
    dlp_resp = await client.put(
        "/v1/admin/dlp-policy",
        json={"ssn_detector_enabled": True, "default_action": "block"},
        headers=auth_headers,
    )
    assert dlp_resp.status_code == 200, dlp_resp.text

    # An org-wide schedule that excludes TODAY's weekday entirely.
    today_weekday = datetime.now(timezone.utc).isoweekday()
    blocked_weekday = 1 if today_weekday != 1 else 2
    schedule_resp = await client.put(
        "/v1/admin/access-schedule",
        json={"enabled": True, "allowed_days": [blocked_weekday]},
        headers=auth_headers,
    )
    assert schedule_resp.status_code == 200, schedule_resp.text

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "my SSN is 234-56-7890 today"}],
        },
        headers={"Authorization": f"Bearer {secret}"},
    )

    # The schedule block fires - never the DLP block, even though the
    # prompt would have tripped it.
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "outside_allowed_schedule"

    assert await _count(migrated_database_url, "SELECT count(*) FROM dlp_scan_results") == 0
    dlp_block_audits_after = await _count(
        migrated_database_url, "SELECT count(*) FROM audit_entries WHERE action = 'dlp.block'"
    )
    assert dlp_block_audits_after == dlp_block_audits_before
