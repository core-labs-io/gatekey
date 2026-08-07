"""Integration tests for the service-account-key admin API against a real Postgres.

See `conftest.py` for the Postgres/Docker/migration/lifespan plumbing these
tests build on. This file additionally exercises the DB-backed parts of
`services/service_accounts.py` that aren't meaningfully mockable (hash-based
lookup against the real unique index, revoke idempotency/atomicity) directly
against the service functions, not just through the HTTP layer.

`user_id` note: every `POST /v1/admin/service-accounts` call below passes
`"user_id": default_user_id` (a fixture in `conftest.py`) because `user_id`
became a required field on that endpoint when Phase 1.4 (Budget - Basic)
landed - see `schemas/service_account_key.py`. This file's calls (and its
two `set(...keys()) == {...}` response-shape assertions) were not updated
when that requirement landed, which is why every test here was erroring at
setup or failing outright before this fix.
"""

from __future__ import annotations

import asyncio

import asyncpg
import httpx
import pytest

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.services.service_accounts import (
    get_active_service_account_by_hash,
    get_service_account,
    hash_secret,
    revoke_service_account,
)

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _truncate_service_account_keys(migrated_database_url: str):
    """Ensure each test starts with an empty `service_account_keys` table.

    `CASCADE` is required as of the Phase 1.5 `usage_logs` table (migration
    0005), which has an FK referencing `service_account_keys` - a bare
    `TRUNCATE` now fails with Postgres's `FeatureNotSupportedError` ("cannot
    truncate a table referenced in a foreign key constraint") rather than
    truncating. `usage_logs` being emptied alongside it is the correct
    behavior for test isolation, not just a side effect to tolerate - a
    fresh test should start with clean usage logs too, not leak rows from
    whatever service-account key a prior test created and then deleted.
    """
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute("TRUNCATE TABLE service_account_keys CASCADE")
    finally:
        await conn.close()
    yield


async def _row_count(database_url: str) -> int:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchval("SELECT count(*) FROM service_account_keys")
    finally:
        await conn.close()


# --- admin CRUD endpoints -----------------------------------------------------


async def test_create_returns_secret_exactly_once(
    client: httpx.AsyncClient, auth_headers: dict[str, str], default_user_id: str, default_team_id: str
) -> None:
    response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "billing-service", "user_id": default_user_id, "team_id": default_team_id},
        headers=auth_headers,
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "billing-service"
    assert body["user_id"] == default_user_id
    assert body["secret"].startswith("gk_sk_")
    assert body["key_prefix"] == body["secret"][len("gk_sk_") :][:12]
    assert set(body.keys()) == {
        "id",
        "name",
        "user_id",
        "team_id",
        "key_prefix",
        "secret",
        "created_at",
    }


async def test_create_persists_only_hash_not_plaintext(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    migrated_database_url: str,
    default_user_id: str,
    default_team_id: str,
) -> None:
    response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "billing-service", "user_id": default_user_id, "team_id": default_team_id},
        headers=auth_headers,
    )
    assert response.status_code == 201
    secret = response.json()["secret"]

    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        row = await conn.fetchrow(
            "SELECT secret_hash FROM service_account_keys WHERE org_id = $1", DEFAULT_ORG_ID
        )
    finally:
        await conn.close()

    assert row is not None
    assert bytes(row["secret_hash"]) == hash_secret(secret)
    # The plaintext secret does not appear anywhere in the DB row's bytes.
    assert secret.encode("utf-8") not in bytes(row["secret_hash"])


async def test_list_never_includes_secret_fields(
    client: httpx.AsyncClient, auth_headers: dict[str, str], default_user_id: str, default_team_id: str
) -> None:
    create_response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "app-one", "user_id": default_user_id, "team_id": default_team_id},
        headers=auth_headers,
    )
    secret = create_response.json()["secret"]

    list_response = await client.get("/v1/admin/service-accounts", headers=auth_headers)
    assert list_response.status_code == 200
    raw_text = list_response.text
    assert "secret_hash" not in raw_text
    assert secret not in raw_text

    body = list_response.json()
    assert len(body) == 1
    entry = body[0]
    assert set(entry.keys()) == {
        "id",
        "name",
        "user_id",
        "team_id",
        "key_prefix",
        "created_at",
        "revoked_at",
        "active",
    }
    assert entry["active"] is True
    assert entry["revoked_at"] is None


async def test_get_single_key_never_includes_secret(
    client: httpx.AsyncClient, auth_headers: dict[str, str], default_user_id: str, default_team_id: str
) -> None:
    create_response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "app-one", "user_id": default_user_id, "team_id": default_team_id},
        headers=auth_headers,
    )
    key_id = create_response.json()["id"]
    secret = create_response.json()["secret"]

    get_response = await client.get(f"/v1/admin/service-accounts/{key_id}", headers=auth_headers)
    assert get_response.status_code == 200
    assert secret not in get_response.text
    body = get_response.json()
    assert body["id"] == key_id
    assert body["active"] is True


async def test_get_unknown_id_returns_404(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    import uuid

    response = await client.get(
        f"/v1/admin/service-accounts/{uuid.uuid4()}", headers=auth_headers
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_delete_revokes_and_is_idempotent(
    client: httpx.AsyncClient, auth_headers: dict[str, str], default_user_id: str, default_team_id: str
) -> None:
    create_response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "app-one", "user_id": default_user_id, "team_id": default_team_id},
        headers=auth_headers,
    )
    key_id = create_response.json()["id"]

    first_delete = await client.delete(f"/v1/admin/service-accounts/{key_id}", headers=auth_headers)
    assert first_delete.status_code == 204

    get_response = await client.get(f"/v1/admin/service-accounts/{key_id}", headers=auth_headers)
    assert get_response.status_code == 200
    body = get_response.json()
    assert body["active"] is False
    assert body["revoked_at"] is not None

    # Revoking again is an idempotent no-op success, not an error.
    second_delete = await client.delete(
        f"/v1/admin/service-accounts/{key_id}", headers=auth_headers
    )
    assert second_delete.status_code == 204


async def test_delete_unknown_id_returns_404(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    import uuid

    response = await client.delete(
        f"/v1/admin/service-accounts/{uuid.uuid4()}", headers=auth_headers
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_create_requires_admin_token(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/admin/service-accounts", json={"name": "app-one"})
    assert response.status_code == 401


async def test_list_requires_admin_token(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/admin/service-accounts")
    assert response.status_code == 401


async def test_create_rejects_blank_name(
    client: httpx.AsyncClient, auth_headers: dict[str, str], default_user_id: str, default_team_id: str
) -> None:
    response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "   ", "user_id": default_user_id, "team_id": default_team_id},
        headers=auth_headers,
    )
    assert response.status_code == 422


async def test_create_rejects_team_the_user_is_not_a_member_of(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    default_user_id: str,
    migrated_database_url: str,
) -> None:
    """Phase 2 H-1: `team_id` is required and the target user must hold a
    TeamMembership on that team - a team without that membership is a 404,
    and no key row is written."""
    team_response = await client.post(
        "/v1/teams", json={"name": "no-membership-team"}, headers=auth_headers
    )
    assert team_response.status_code == 201, team_response.text
    response = await client.post(
        "/v1/admin/service-accounts",
        json={
            "name": "orphan-app",
            "user_id": default_user_id,
            "team_id": team_response.json()["id"],
        },
        headers=auth_headers,
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert await _row_count(migrated_database_url) == 0


async def test_create_requires_team_id_field(
    client: httpx.AsyncClient, auth_headers: dict[str, str], default_user_id: str
) -> None:
    response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "no-team-app", "user_id": default_user_id},
        headers=auth_headers,
    )
    assert response.status_code == 422


# --- service-layer DB behavior (hash lookup, revoke idempotency/atomicity) ---


async def test_get_active_service_account_by_hash_finds_active_key(
    client: httpx.AsyncClient, auth_headers: dict[str, str], app, default_user_id: str, default_team_id: str
) -> None:
    create_response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "app-one", "user_id": default_user_id, "team_id": default_team_id},
        headers=auth_headers,
    )
    secret = create_response.json()["secret"]

    session_factory = app.state.db_session_factory
    async with session_factory() as session:
        row = await get_active_service_account_by_hash(session, hash_secret(secret))
        assert row is not None
        assert row.name == "app-one"


async def test_get_active_service_account_by_hash_excludes_revoked_key(
    client: httpx.AsyncClient, auth_headers: dict[str, str], app, default_user_id: str, default_team_id: str
) -> None:
    create_response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "app-one", "user_id": default_user_id, "team_id": default_team_id},
        headers=auth_headers,
    )
    key_id = create_response.json()["id"]
    secret = create_response.json()["secret"]

    await client.delete(f"/v1/admin/service-accounts/{key_id}", headers=auth_headers)

    session_factory = app.state.db_session_factory
    async with session_factory() as session:
        row = await get_active_service_account_by_hash(session, hash_secret(secret))
        assert row is None


async def test_get_active_service_account_by_hash_no_match_for_unknown_hash(
    client: httpx.AsyncClient, app
) -> None:
    # `client` is requested (even though unused directly) so the app
    # lifespan has run and `app.state.db_session_factory` is populated -
    # see conftest.py's `client` fixture.
    session_factory = app.state.db_session_factory
    async with session_factory() as session:
        row = await get_active_service_account_by_hash(session, b"\x00" * 32)
        assert row is None


async def test_revoke_service_account_idempotent_at_service_layer(
    client: httpx.AsyncClient, auth_headers: dict[str, str], app, default_user_id: str, default_team_id: str
) -> None:
    create_response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "app-one", "user_id": default_user_id, "team_id": default_team_id},
        headers=auth_headers,
    )
    import uuid

    key_id = uuid.UUID(create_response.json()["id"])

    session_factory = app.state.db_session_factory
    async with session_factory() as session:
        first = await revoke_service_account(session, key_id)
        assert first is True

    async with session_factory() as session:
        second = await revoke_service_account(session, key_id)
        assert second is False

    async with session_factory() as session:
        row = await get_service_account(session, key_id)
        assert row is not None
        assert row.revoked_at is not None


async def test_revoke_service_account_unknown_id_returns_false(
    client: httpx.AsyncClient, app
) -> None:
    import uuid

    session_factory = app.state.db_session_factory
    async with session_factory() as session:
        result = await revoke_service_account(session, uuid.uuid4())
        assert result is False


async def test_concurrent_revoke_race_only_one_call_reports_state_change(
    client: httpx.AsyncClient, auth_headers: dict[str, str], app, default_user_id: str, default_team_id: str
) -> None:
    """Two concurrent revokes of the same key must not both report `True`.

    `revoke_service_account`'s single `UPDATE ... WHERE revoked_at IS NULL
    ... RETURNING id` (see `services/service_accounts.py`) means Postgres
    serializes the two statements: only the one that actually flips
    `revoked_at` from NULL sees a matching row.
    """
    create_response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "app-one", "user_id": default_user_id, "team_id": default_team_id},
        headers=auth_headers,
    )
    import uuid

    key_id = uuid.UUID(create_response.json()["id"])
    session_factory = app.state.db_session_factory

    async def _revoke() -> bool:
        async with session_factory() as session:
            return await revoke_service_account(session, key_id)

    results = await asyncio.gather(_revoke(), _revoke())
    assert sorted(results) == [False, True]
