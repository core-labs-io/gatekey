"""Migration-verification tests for `0006_add_ollama_openrouter_providers`.

Encodes, as a permanent/re-runnable check, the verification protocol from
`docs/design/phase-1.1-1.2-1.4-ollama-openrouter-providers-design.md`
section 1.2 (US-G9 / AC-C1-3): that `ALTER TYPE provider_name ADD VALUE`
under Alembic's default transactional-DDL wrapping actually leaves the two
new enum values ('ollama', 'openrouter') usable by a real `INSERT` from a
genuinely separate connection/process once the migration has committed -
both for a fresh clone (`0001`-`0006` applied in one `alembic upgrade
head` invocation) and for the incremental-deploy case (a database already
at `0005`, with `0006` applied alone, in its own single-migration
transaction).

VERIFIED (manual run by database-admin, 2026-07-28, `postgres:16-alpine`,
Alembic against this repo's `alembic/env.py`):
  - Fresh DB, `alembic upgrade head` (0001->0006 in one invocation): exit 0.
    Separate-connection `INSERT ... provider = 'ollama'` /
    `provider = 'openrouter'`: both succeeded.
  - DB pre-migrated to `0005`, then `alembic upgrade head` (0006 alone, its
    own transaction): exit 0. Same separate-connection INSERT check: both
    succeeded.
  - `alembic downgrade -1` from `0006`: raises `NotImplementedError` as
    documented (does not silently no-op; `alembic_version` stays at `0006`).
This file makes that manual result permanent/re-runnable rather than a
one-time confirmation that evaporates.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config

from .conftest import BACKEND_ROOT, _free_port, _wait_for_postgres, to_asyncpg_dsn

pytestmark = pytest.mark.asyncio

DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"
NEW_PROVIDER_VALUES = ("ollama", "openrouter")

_INSERT_SQL = """
INSERT INTO provider_keys (id, org_id, provider, ciphertext, nonce, auth_tag, label)
VALUES (gen_random_uuid(), $1::uuid, $2::provider_name, $3::bytea, $3::bytea, $3::bytea, $4::text)
RETURNING id
"""

# `label` (Phase 4, migration `0023`) doesn't exist yet at revision `0006` -
# used by `test_incremental_deploy_from_0005_enables_new_provider_enum_
# values` below, which deliberately stops at `0006` to test THAT
# migration's own behavior, not head's current schema.
_INSERT_SQL_NO_LABEL = """
INSERT INTO provider_keys (id, org_id, provider, ciphertext, nonce, auth_tag)
VALUES (gen_random_uuid(), $1::uuid, $2::provider_name, $3::bytea, $3::bytea, $3::bytea)
RETURNING id
"""


def _alembic_config(database_url: str) -> Config:
    alembic_cfg = Config(str(BACKEND_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    os.environ["DATABASE_URL"] = database_url
    return alembic_cfg


async def _assert_new_enum_values_usable(database_url: str, *, with_label: bool = True) -> None:
    """INSERT one row per new provider value from a fresh connection, then clean up.

    Must be called with a connection opened *after* the migrating Alembic
    process has fully exited (per design doc section 1.2's "why a new
    connection/transaction matters" note) - callers here always run this
    after `command.upgrade(...)` has already returned.

    `with_label=False` for a caller that stopped migrating at `0006` (before
    Phase 4's `0023` added the `label` column) - see `_INSERT_SQL_NO_LABEL`.
    """
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        inserted_ids: list[uuid.UUID] = []
        for provider in NEW_PROVIDER_VALUES:
            if with_label:
                row_id = await conn.fetchval(
                    _INSERT_SQL, DEFAULT_ORG_ID, provider, b"\x00", "Default"
                )
            else:
                row_id = await conn.fetchval(_INSERT_SQL_NO_LABEL, DEFAULT_ORG_ID, provider, b"\x00")
            assert row_id is not None
            inserted_ids.append(row_id)
        for row_id in inserted_ids:
            await conn.execute("DELETE FROM provider_keys WHERE id = $1", row_id)
    finally:
        await conn.close()


async def test_fresh_clone_head_migration_enables_new_provider_enum_values(
    migrated_database_url: str,
) -> None:
    """0001-0006 applied in one `alembic upgrade head` invocation (fresh clone case).

    `migrated_database_url` (session-scoped, `conftest.py`) already ran
    `alembic upgrade head` against a real, empty `postgres:16-alpine`
    instance and returned successfully - a non-zero-exit/raising upgrade
    would have failed the fixture, and therefore this test, at setup time.
    """
    await _assert_new_enum_values_usable(migrated_database_url)


@pytest_asyncio.fixture
async def fresh_postgres_url() -> AsyncIterator[str]:
    """A dedicated, empty Postgres instance/database independent of the
    session-scoped `migrated_database_url` fixture.

    The incremental-deploy scenario below needs to control revision state
    precisely (stop at `0005`, then apply `0006` alone) - it cannot share
    the session fixture, which is always driven straight to `head`. If
    `GATEKEY_TEST_DATABASE_URL` is set (CI reusing an already-running
    instance), provisions a fresh throwaway database on that instance via
    `CREATE DATABASE` instead of spinning up Docker, mirroring
    `conftest.py`'s own env-var opt-out.
    """
    existing = os.environ.get("GATEKEY_TEST_DATABASE_URL")
    if existing:
        db_name = f"gatekey_migtest_{uuid.uuid4().hex[:10]}"
        admin_dsn = to_asyncpg_dsn(existing)
        admin_conn = await asyncpg.connect(admin_dsn)
        try:
            await admin_conn.execute(f'CREATE DATABASE "{db_name}"')
        finally:
            await admin_conn.close()

        base = existing.rsplit("/", 1)[0]
        database_url = f"{base}/{db_name}"
        try:
            yield database_url
        finally:
            cleanup_conn = await asyncpg.connect(admin_dsn)
            try:
                await cleanup_conn.execute(
                    f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'
                )
            finally:
                await cleanup_conn.close()
        return

    import subprocess

    port = _free_port()
    container_name = f"gatekey-migtest-pg-{uuid.uuid4().hex[:10]}"
    subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            container_name,
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-e",
            "POSTGRES_DB=gatekey_migtest",
            "-p",
            f"{port}:5432",
            "postgres:16-alpine",
        ],
        check=True,
        capture_output=True,
    )
    database_url = f"postgresql+asyncpg://postgres:postgres@localhost:{port}/gatekey_migtest"
    try:
        await _wait_for_postgres(to_asyncpg_dsn(database_url))
        yield database_url
    finally:
        subprocess.run(["docker", "stop", container_name], capture_output=True)


async def test_incremental_deploy_from_0005_enables_new_provider_enum_values(
    fresh_postgres_url: str,
) -> None:
    """An existing production DB already at `0005` getting only `0006`
    applied (its own single-migration transaction) - the incremental-deploy
    case, distinct from the fresh-clone-runs-everything-at-once case above.
    """
    alembic_cfg = _alembic_config(fresh_postgres_url)

    # Step 1: land on 0005 first, as a separate `alembic upgrade` invocation.
    await asyncio.to_thread(command.upgrade, alembic_cfg, "0005")

    # Step 2: apply 0006 alone, in its own transaction, exactly as an
    # incremental production deploy would.
    await asyncio.to_thread(command.upgrade, alembic_cfg, "0006")

    await _assert_new_enum_values_usable(fresh_postgres_url, with_label=False)


async def test_0006_downgrade_raises_not_implemented_rather_than_silently_no_opping(
    fresh_postgres_url: str,
) -> None:
    """Postgres has no DROP VALUE for enum types (design doc section 1.1) -
    `downgrade()` must raise, not silently succeed while leaving the values
    in place. Also confirms `alembic_version` is not moved by the failed
    attempt (the raise happens before any version-table update).
    """
    alembic_cfg = _alembic_config(fresh_postgres_url)
    # Pin to 0006, not "head": this test is about 0006's own downgrade.
    # ("head" moved to 0013 when Phase 2 landed, silently pointing the
    # `downgrade -1` below at the wrong migration.)
    await asyncio.to_thread(command.upgrade, alembic_cfg, "0006")

    with pytest.raises(NotImplementedError, match="cannot be downgraded"):
        await asyncio.to_thread(command.downgrade, alembic_cfg, "-1")

    conn = await asyncpg.connect(to_asyncpg_dsn(fresh_postgres_url))
    try:
        current_version = await conn.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()
    assert current_version == "0006"
