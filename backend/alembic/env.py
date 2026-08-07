"""Alembic migration environment for Gatekey.

Async SQLAlchemy (asyncpg) is used both by the app and by migrations, so
the same `DATABASE_URL` format (`postgresql+asyncpg://...`) works in both
places without translation.

`DATABASE_URL` resolution order (see `_get_database_url` below):

1. `gatekey.config.Settings` (pydantic-settings) - the source of truth once
   the rest of the app depends on it, since it applies the same env-file
   loading / validation behavior the running app uses.
2. Fallback: read `DATABASE_URL` directly from the process environment.
   Migrations only need a database connection string, not the full app
   secret set - this fallback keeps `alembic upgrade`/`downgrade` runnable
   even when unrelated required `Settings` fields (`GATEKEY_ADMIN_TOKEN`,
   `GATEKEY_MASTER_KEY`) aren't set in the current shell/CI job, and also
   covers `Settings` being unavailable at all.

TODO(backend-developer): once every deployment/CI path reliably sets the
full Settings env (admin token + master key) alongside DATABASE_URL,
consider tightening this to require Settings and drop the raw-env
fallback, so config validation stays single-sourced. Not done now because
decoupling "can I run a migration" from "is the full app configured" is
also a reasonable permanent posture for a migration runner - revisit with
backend-developer's input rather than assuming it should change.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from gatekey.db.base import Base

# Import models so every table registers on Base.metadata before Alembic
# (autogenerate, or this env's target_metadata) inspects it.
from gatekey.db import models  # noqa: F401

# Alembic Config object, providing access to values within alembic.ini.
config = context.config

# Interpret the config file for Python logging (alembic.ini's [loggers]
# section etc.) unless no config file is present (e.g. invoked programmatically).
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_database_url() -> str:
    try:
        from gatekey.config import get_settings

        return get_settings().DATABASE_URL
    except Exception:
        # See module docstring for why we fall back rather than raise here.
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "DATABASE_URL is not set (checked gatekey.config.Settings "
                "and the DATABASE_URL environment variable). Set DATABASE_URL "
                "(e.g. postgresql+asyncpg://user:pass@host:5432/gatekey) before "
                "running Alembic."
            ) from None
        return url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine, so a DBAPI is
    not required. Calls to `context.execute()` here emit the given string to
    the script output.
    """
    url = _get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async Engine and associate a connection with the context."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _get_database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live async DB connection."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
