"""Integration test (Phase 3, BD-20..26, known gap #4): a full SCIM 2.0
lifecycle against a real Postgres, via `TestClient`-shaped `httpx.AsyncClient`
calls - not the fake-session unit tests in `tests/unit/test_scim_service.py`.

Covers:
  - `POST /scim/v2/Users` actually provisions a `User` row reachable via the
    bearer-token-authenticated SCIM surface (AC5.2/AC5.3).
  - `POST /scim/v2/Groups` (with an initial member) creates a `Team` +
    `TeamMembership` (AC5.1/AC5.4).
  - `PATCH /scim/v2/Users/{id} {active: false}` cascades: personal key,
    team-attributed service-account key, session, and `cli_refresh_
    credentials` row are ALL revoked in the real database, with one
    `AuditEntry` per revoked credential, actor "system:scim" (AC5.5, design
    doc section 6.4's extension).
  - The SSO-identity-reconciliation path (`services.users.resolve_or_
    create_sso_user`, design doc section 6.3): a later "OIDC callback" for
    the SAME SCIM-provisioned user (matched by email, no `sso_subject` yet)
    claims the existing row instead of creating a duplicate - exercised
    directly against the real DB (a live OIDC flow isn't practical here),
    per the task's own guidance.
"""

from __future__ import annotations

import uuid

import asyncpg
import pytest

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.services.cli_refresh_credentials import create_cli_refresh_credential
from gatekey.services.users import resolve_or_create_sso_user

from .conftest import to_asyncpg_dsn
from .phase2_helpers import session_cookie_headers, sf  # noqa: F401 - fixture

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _clean_scim_tables(migrated_database_url: str):
    async def _truncate() -> None:
        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            await conn.execute("TRUNCATE TABLE scim_config CASCADE")
        finally:
            await conn.close()

    await _truncate()
    yield
    await _truncate()


async def _enable_scim_and_get_token(client, auth_headers) -> str:
    enable_resp = await client.put(
        "/v1/admin/scim-config", json={"enabled": True}, headers=auth_headers
    )
    assert enable_resp.status_code == 200, enable_resp.text
    rotate_resp = await client.post("/v1/admin/scim-config/rotate-token", headers=auth_headers)
    assert rotate_resp.status_code == 200, rotate_resp.text
    return rotate_resp.json()["token"]


async def _fetch_val(database_url: str, query: str, *args):
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchval(query, *args)
    finally:
        await conn.close()


async def _fetch_vals(database_url: str, query: str, *args) -> list:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return [row[0] for row in await conn.fetch(query, *args)]
    finally:
        await conn.close()


async def test_scim_provisioning_deactivation_cascade_and_sso_reconciliation(
    client, auth_headers, migrated_database_url, sf
) -> None:
    scim_token = await _enable_scim_and_get_token(client, auth_headers)
    scim_headers = {"Authorization": f"Bearer {scim_token}"}

    # --- AC5.2: a wrong/rotated-away token is rejected -----------------------
    stale_check = await client.get(
        "/scim/v2/Users", headers={"Authorization": "Bearer gk_scim_wrong-token"}
    )
    assert stale_check.status_code == 401, stale_check.text

    # --- AC5.3: POST /Users provisions a user, no org_role, no team yet ------
    create_resp = await client.post(
        "/scim/v2/Users",
        json={"userName": "scim-e2e@example.com", "name": {"formatted": "SCIM E2E User"}},
        headers=scim_headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    user_id = create_resp.json()["id"]
    assert create_resp.json()["active"] is True

    # --- AC5.1/AC5.4: Group push creates a Team + zero-budget membership -----
    group_resp = await client.post(
        "/scim/v2/Groups",
        json={"displayName": "scim-e2e-team", "members": [{"value": user_id}]},
        headers=scim_headers,
    )
    assert group_resp.status_code == 201, group_resp.text
    team_id = group_resp.json()["id"]
    membership_budget = await _fetch_val(
        migrated_database_url,
        "SELECT budget_usd FROM team_memberships WHERE team_id = $1 AND user_id = $2",
        uuid.UUID(team_id),
        uuid.UUID(user_id),
    )
    assert membership_budget is None  # unmetered, not $0 (ratified #7)

    # --- Set up the four credential types the deactivation cascade covers ---
    sa_resp = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "scim-e2e-sa-key", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert sa_resp.status_code == 201, sa_resp.text
    sa_key_id = sa_resp.json()["id"]

    cookie_headers = await session_cookie_headers(sf, uuid.UUID(user_id))
    key_resp = await client.post(
        "/v1/keys",
        json={"name": "scim-e2e-personal-key", "team_id": team_id},
        headers=cookie_headers,
    )
    assert key_resp.status_code == 201, key_resp.text
    personal_key_id = key_resp.json()["id"]

    async with sf() as session:
        _row, _secret = await create_cli_refresh_credential(
            session,
            org_id=DEFAULT_ORG_ID,
            user_id=uuid.UUID(user_id),
            bound_personal_key_id=uuid.UUID(personal_key_id),
        )
        await session.commit()

    # Sanity: everything active before deactivation.
    assert await _fetch_val(
        migrated_database_url, "SELECT revoked_at FROM personal_api_keys WHERE id = $1", uuid.UUID(personal_key_id)
    ) is None
    assert await _fetch_val(
        migrated_database_url, "SELECT revoked_at FROM service_account_keys WHERE id = $1", uuid.UUID(sa_key_id)
    ) is None

    audit_before = await _fetch_val(
        migrated_database_url, "SELECT count(*) FROM audit_entries WHERE actor_label = 'system:scim'"
    )

    # --- AC5.5: PATCH active:false cascades every credential type -----------
    patch_resp = await client.patch(
        f"/scim/v2/Users/{user_id}",
        json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
        headers=scim_headers,
    )
    assert patch_resp.status_code == 200, patch_resp.text
    assert patch_resp.json()["active"] is False

    assert await _fetch_val(
        migrated_database_url, "SELECT revoked_at FROM personal_api_keys WHERE id = $1", uuid.UUID(personal_key_id)
    ) is not None
    assert await _fetch_val(
        migrated_database_url, "SELECT revoked_at FROM service_account_keys WHERE id = $1", uuid.UUID(sa_key_id)
    ) is not None
    assert await _fetch_val(
        migrated_database_url,
        "SELECT count(*) FROM sessions WHERE user_id = $1 AND revoked_at IS NOT NULL",
        uuid.UUID(user_id),
    ) >= 1
    assert await _fetch_val(
        migrated_database_url,
        "SELECT count(*) FROM cli_refresh_credentials WHERE user_id = $1 AND revoked_at IS NOT NULL",
        uuid.UUID(user_id),
    ) == 1

    # One AuditEntry per revoked credential (4: personal key, SA key,
    # session, CLI refresh credential) PLUS the route's own
    # `scim_user.deactivate` entry for the deactivation itself (5 total),
    # all actor "system:scim".
    audit_after = await _fetch_val(
        migrated_database_url, "SELECT count(*) FROM audit_entries WHERE actor_label = 'system:scim'"
    )
    assert audit_after - audit_before == 5
    new_actions = await _fetch_vals(
        migrated_database_url,
        "SELECT action FROM audit_entries WHERE actor_label = 'system:scim' ORDER BY created_at "
        "OFFSET $1",
        audit_before,
    )
    assert sorted(new_actions) == sorted(
        [
            "personal_key.revoke",
            "service_account_key.revoke",
            "session.revoke",
            "cli_refresh_credential.revoke",
            "scim_user.deactivate",
        ]
    )

    # AC5.6: the User row itself is never deleted.
    assert await _fetch_val(
        migrated_database_url, "SELECT count(*) FROM users WHERE id = $1", uuid.UUID(user_id)
    ) == 1

    # --- design doc section 6.3: SSO-identity reconciliation -----------------
    async with sf() as session:
        resolved = await resolve_or_create_sso_user(
            session,
            org_id=DEFAULT_ORG_ID,
            sub="new-oidc-subject-for-scim-user",
            email="scim-e2e@example.com",
            name="SCIM E2E User",
        )
        assert str(resolved.id) == user_id  # claimed the SCIM row, not a new one
        assert resolved.sso_subject == "new-oidc-subject-for-scim-user"

    duplicate_count = await _fetch_val(
        migrated_database_url, "SELECT count(*) FROM users WHERE sso_email = $1", "scim-e2e@example.com"
    )
    assert duplicate_count == 1


async def test_scim_disabled_by_default_rejects_any_token(client) -> None:
    """AC5.7: off by default - no config row exists yet in a fresh org."""
    response = await client.post(
        "/scim/v2/Users", json={"userName": "x@example.com"}, headers={"Authorization": "Bearer gk_scim_anything"}
    )
    assert response.status_code == 401, response.text


async def test_scim_cannot_set_org_role_via_custom_attribute(client, auth_headers, migrated_database_url) -> None:
    """AC5.8: even a crafted payload with an org-role-shaped attribute must
    never grant org_admin/auditor - the field simply has no mapping."""
    scim_token = await _enable_scim_and_get_token(client, auth_headers)
    scim_headers = {"Authorization": f"Bearer {scim_token}"}
    create_resp = await client.post(
        "/scim/v2/Users",
        json={
            "userName": "scim-privesc-attempt@example.com",
            "org_role": "org_admin",
            "orgRole": "org_admin",
            "role": "org_admin",
        },
        headers=scim_headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    user_id = create_resp.json()["id"]
    org_role = await _fetch_val(
        migrated_database_url, "SELECT org_role FROM users WHERE id = $1", uuid.UUID(user_id)
    )
    assert org_role is None
