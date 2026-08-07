"""Async SQLAlchemy engine/session plumbing.

The engine and session factory are built once at app startup (see
`gatekey.main.create_app`) and stashed on `app.state` - constructing a new
engine (and therefore a new connection pool) per request would defeat
pooling entirely and is not how SQLAlchemy's async engine is meant to be
used.

`get_db_session` is the FastAPI dependency every DB-backed endpoint should
depend on; it yields a single `AsyncSession` scoped to the request and
always closes it afterward (including on an exception, since fixed uses
`async with`).
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from gatekey.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine from `Settings.DATABASE_URL`.

    `pool_pre_ping=True` so a stale/dropped connection (e.g. after a DB
    restart) is detected and replaced rather than surfacing as an opaque
    connection error on the next request.
    """
    return create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session factory bound to `engine`.

    `expire_on_commit=False`: service functions that `INSERT ... RETURNING`
    and then return the resulting ORM object (see
    `services/provider_keys.py`) need those attributes to stay populated
    after `session.commit()` without triggering an extra round trip.
    """
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def get_db_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an `AsyncSession` scoped to one request.

    Reads the session factory stashed on `app.state` at startup rather than
    constructing a new engine/session factory per call.
    """
    session_factory: async_sessionmaker[AsyncSession] = request.app.state.db_session_factory
    async with session_factory() as session:
        yield session
