"""Integration test (Phase 3, BD-16/17/18/19): a real `POST /v1/chat/
completions` request is blocked outside a configured org-wide access
schedule (`outside_allowed_schedule`, AC9.6), and an emergency override
lifts the block (AC9.7-AC9.9) - against a real Postgres, exercising the
actual admin-write -> cache-refresh -> gateway-check round trip, not just
the pure `services.access_schedules` unit-level logic.

The override-lifted request is not expected to fully succeed end-to-end (no
provider key is configured) - proving it fails LATER, with a DIFFERENT
error (`provider_not_configured`, never `outside_allowed_schedule` again)
is sufficient to prove the schedule check itself was passed.

Emergency-override grant/revoke use a REAL org_admin session cookie, not
the break-glass bearer token - `emergency_overrides.granted_by_user_id` is
`NOT NULL` (design doc section 1.12), which the break-glass actor
(`user_id=None`) structurally cannot satisfy - see `services.
emergency_overrides.EmergencyOverrideRequiresUserSessionError`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

from gatekey.db.models.user import UserOrgRole

from .conftest import to_asyncpg_dsn
from .phase2_helpers import make_user, session_cookie_headers, sf  # noqa: F401 - fixture

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _clean_access_schedule_tables(migrated_database_url: str):
    """Truncate before AND after each test in this module - `access_
    schedules`/`emergency_overrides` are process-wide singletons (an
    org-wide schedule row applies to every other test file's gateway
    requests too, via each test's own fresh `AccessScheduleCache` warm at
    app-lifespan startup), so a row left behind here would silently start
    blocking unrelated gateway requests in test files that run later in
    the same session against the same shared Postgres instance."""

    async def _truncate() -> None:
        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            await conn.execute(
                "TRUNCATE TABLE access_schedules, holiday_dates, emergency_overrides CASCADE"
            )
        finally:
            await conn.close()

    await _truncate()
    yield
    await _truncate()


async def _count_access_schedule_block_audit_entries(database_url: str) -> int:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchval(
            "SELECT count(*) FROM audit_entries WHERE action = 'access_schedule.block'"
        )
    finally:
        await conn.close()


async def _create_service_account(client, auth_headers, *, user_id: str, team_id: str) -> tuple[str, str]:
    response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "access-schedule-test-key", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return body["id"], body["secret"]


async def test_gateway_request_blocked_outside_configured_schedule_and_lifted_by_override(
    client, auth_headers, default_user_id, default_team_id, migrated_database_url, sf
):
    key_id, secret = await _create_service_account(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )

    # An org-wide schedule that excludes TODAY's weekday entirely (any hour)
    # - guarantees the very next request is outside the window regardless
    # of wall-clock time-of-day.
    today_weekday = datetime.now(timezone.utc).isoweekday()
    blocked_weekday = 1 if today_weekday != 1 else 2
    put_response = await client.put(
        "/v1/admin/access-schedule",
        json={"enabled": True, "allowed_days": [blocked_weekday]},
        headers=auth_headers,
    )
    assert put_response.status_code == 200, put_response.text

    blocked_response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert blocked_response.status_code == 403, blocked_response.text
    assert blocked_response.json()["error"]["code"] == "outside_allowed_schedule"

    # AC9.6: the block itself is audited. Queried directly against the DB
    # (not via `GET /v1/admin/audit-entries`) - that endpoint has a known,
    # pre-existing, unrelated bug serializing a real `source_ip` value
    # (`inet` -> `IPv4Address`, not `str`) flagged separately to the
    # audit-gap-closure track owning that file; not this test's concern.
    assert await _count_access_schedule_block_audit_entries(migrated_database_url) >= 1

    # AC9.7/AC9.8: a real Org Admin session grants a time-boxed override
    # with a required reason, scoped under the key's own team.
    admin_id = await make_user(sf, "access-schedule-org-admin", org_role=UserOrgRole.ORG_ADMIN)
    admin_cookie = await session_cookie_headers(sf, admin_id)
    grant_response = await client.post(
        f"/v1/teams/{default_team_id}/service-account-keys/{key_id}/emergency-override",
        json={
            "reason": "incident response - need off-hours access",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
        headers=admin_cookie,
    )
    assert grant_response.status_code == 201, grant_response.text

    lifted_response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    # No provider key is configured, so this fails LATER in the pipeline -
    # the important assertion is what it is NOT: no longer the schedule
    # block, proving the active override was consulted and applied.
    assert lifted_response.json()["error"]["code"] == "provider_not_configured"


async def test_break_glass_token_cannot_grant_emergency_override(
    client, auth_headers, default_user_id, default_team_id
):
    """The break-glass bearer token is a valid org_admin-equivalent caller
    for every other admin surface, but `granted_by_user_id` structurally
    requires a real user - this must be a clean, structured 400, never a
    raw DB constraint violation surfacing as an unhandled 500."""
    key_id, _secret = await _create_service_account(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    response = await client.post(
        f"/v1/teams/{default_team_id}/service-account-keys/{key_id}/emergency-override",
        json={
            "reason": "incident response",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
        headers=auth_headers,
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "emergency_override_requires_user_session"


async def test_org_admin_reason_required_server_side_for_emergency_override(
    client, auth_headers, default_user_id, default_team_id, sf
):
    key_id, _secret = await _create_service_account(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    admin_id = await make_user(sf, "access-schedule-org-admin-2", org_role=UserOrgRole.ORG_ADMIN)
    admin_cookie = await session_cookie_headers(sf, admin_id)
    response = await client.post(
        f"/v1/teams/{default_team_id}/service-account-keys/{key_id}/emergency-override",
        json={
            "reason": "   ",
            "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        },
        headers=admin_cookie,
    )
    assert response.status_code == 422, response.text
