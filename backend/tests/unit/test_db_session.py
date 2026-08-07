"""Unit tests for db/session.py and constants.py that don't require a live DB.

Actual query execution against `create_engine`/`create_session_factory` is
covered by the integration suite (`tests/integration/`), which runs against
real Postgres.
"""

from __future__ import annotations

import base64
import os
import uuid

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from gatekey.config import Settings
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.session import create_engine, create_session_factory


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/gatekey",
        GATEKEY_ADMIN_TOKEN="test-token",
        GATEKEY_MASTER_KEY=base64.b64encode(os.urandom(32)).decode(),
    )


def test_default_org_id_matches_migration_seeded_uuid():
    # Must match the literal in alembic/versions/0001_create_orgs_and_provider_keys.py.
    assert DEFAULT_ORG_ID == uuid.UUID("00000000-0000-0000-0000-000000000001")


def test_create_engine_builds_async_engine_from_settings():
    engine = create_engine(_settings())
    assert isinstance(engine, AsyncEngine)
    assert engine.url.drivername == "postgresql+asyncpg"


def test_create_session_factory_builds_sessionmaker_with_expire_on_commit_false():
    engine = create_engine(_settings())
    factory = create_session_factory(engine)
    assert isinstance(factory, async_sessionmaker)
    session = factory()
    try:
        assert isinstance(session, AsyncSession)
        # expire_on_commit=False is required so `add_or_replace_key`'s
        # returned ORM object stays populated after `session.commit()`.
        assert session.sync_session.expire_on_commit is False
    finally:
        session.sync_session.close()
