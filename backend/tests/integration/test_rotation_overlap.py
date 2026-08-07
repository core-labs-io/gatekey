"""Integration test for the dual-secret rotation overlap mechanism (Phase 3,
design doc sections 1.11/4.3, AC7.4) against a real Postgres.

Proves the actual NFR: a rotated service-account key's PREVIOUS secret
still authenticates during the overlap window, and stops authenticating
once `previous_secret_valid_until` has passed - exercised via `services.
service_accounts.get_active_service_account_by_hash` (the same function
`api.deps.require_gateway_credential`/`require_service_account` call on the
gateway hot path), not a full end-to-end gateway request (provider/model
setup would be unrelated overhead for what this test needs to prove).
"""

from __future__ import annotations

import base64
import os

import asyncpg
import pytest

from gatekey.services.service_accounts import get_active_service_account_by_hash, hash_secret

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _truncate_service_account_keys(migrated_database_url: str):
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute("TRUNCATE TABLE service_account_keys CASCADE")
    finally:
        await conn.close()
    yield


async def test_rotated_key_previous_secret_authenticates_within_overlap_then_expires(
    client, auth_headers, migrated_database_url, default_user_id, default_team_id
):
    create_response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "rotation-test-key", "user_id": default_user_id, "team_id": default_team_id},
        headers=auth_headers,
    )
    assert create_response.status_code == 201, create_response.text
    key_id = create_response.json()["id"]
    old_secret = create_response.json()["secret"]

    rotate_response = await client.post(
        f"/v1/admin/keys/{key_id}/rotate-now", headers=auth_headers
    )
    assert rotate_response.status_code == 200, rotate_response.text
    new_secret = rotate_response.json()["secret"]
    assert new_secret != old_secret

    from gatekey.config import Settings
    from gatekey.db.session import create_engine, create_session_factory

    settings = Settings(
        _env_file=None,
        DATABASE_URL=migrated_database_url,
        GATEKEY_ADMIN_TOKEN="unused",
        GATEKEY_MASTER_KEY=base64.b64encode(os.urandom(32)).decode(),
    )
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        # Still within the overlap window (default 5 minutes): BOTH the new
        # secret and the OLD (rotated-away) secret authenticate.
        async with session_factory() as session:
            new_row = await get_active_service_account_by_hash(session, hash_secret(new_secret))
            assert new_row is not None
            assert str(new_row.id) == key_id

            old_row = await get_active_service_account_by_hash(session, hash_secret(old_secret))
            assert old_row is not None
            assert str(old_row.id) == key_id

        # Force the overlap window into the past (simulates "5 minutes
        # later") - same effect as waiting out overlap_buffer_minutes.
        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            await conn.execute(
                "UPDATE service_account_keys SET previous_secret_valid_until = now() - interval '1 minute' "
                "WHERE id = $1",
                key_id,
            )
        finally:
            await conn.close()

        async with session_factory() as session:
            old_row_after_expiry = await get_active_service_account_by_hash(
                session, hash_secret(old_secret)
            )
            assert old_row_after_expiry is None

            new_row_after_expiry = await get_active_service_account_by_hash(
                session, hash_secret(new_secret)
            )
            assert new_row_after_expiry is not None
    finally:
        await engine.dispose()
