"""Shared fixtures/seed helpers for the Phase 2 (BD-21) integration tests.

Not a test module (no `test_` prefix). Test modules import the fixtures by
name (`from .phase2_helpers import sf, _clean_phase2_tables  # noqa: F401`)
- pytest picks fixtures up from the importing module's namespace.

Seeding goes through the real service layer / ORM against a throwaway
engine bound to the already-migrated test database, mirroring
`test_gateway_ollama_openrouter.py`'s established pattern.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

import asyncpg
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.team import Team, TeamPeriodEnd, TeamPeriodType
from gatekey.db.models.team_membership import TeamMembership, TeamRole
from gatekey.db.models.user import User, UserOrgRole
from gatekey.services.sessions import SESSION_COOKIE_NAME, create_session

from .conftest import to_asyncpg_dsn

# Every Phase 2 table (plus the Phase 1 tables they FK into) - truncated
# before each test in the importing modules for isolation. `orgs` is left
# alone (single seeded default org row).
_PHASE2_TRUNCATE_SQL = (
    "TRUNCATE TABLE audit_entries, join_requests, personal_api_keys, sessions, "
    "team_model_policies, team_memberships, teams, usage_logs, "
    "service_account_keys, users, model_policies, org_settings CASCADE"
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_phase2_tables(migrated_database_url: str):
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute(_PHASE2_TRUNCATE_SQL)
    finally:
        await conn.close()
    yield


@pytest_asyncio.fixture
async def sf(migrated_database_url: str):
    """A session factory on a throwaway engine, sized for the concurrency
    tests (each gathered task takes its own pooled connection)."""
    engine = create_async_engine(migrated_database_url, pool_size=10, max_overflow=20)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()


async def make_user(
    sf: async_sessionmaker, name: str, *, org_role: UserOrgRole | None = None
) -> uuid.UUID:
    async with sf() as session:
        user = User(org_id=DEFAULT_ORG_ID, name=name, org_role=org_role)
        session.add(user)
        await session.commit()
        return user.id


async def session_cookie_headers(
    sf: async_sessionmaker, user_id: uuid.UUID, *, ttl_hours: int = 12
) -> dict[str, str]:
    """Seed a real `sessions` row and return the `Cookie:` header carrying
    its raw token - the same thing `/v1/auth/sso/callback` would have set."""
    async with sf() as session:
        _, raw_token = await create_session(
            session, user_id=user_id, org_id=DEFAULT_ORG_ID, ttl_hours=ttl_hours
        )
    return {"Cookie": f"{SESSION_COOKIE_NAME}={raw_token}"}


async def make_team(
    sf: async_sessionmaker,
    name: str,
    *,
    ceiling: Decimal | None = None,
    period_type: TeamPeriodType = TeamPeriodType.MONTHLY,
    on_period_end: TeamPeriodEnd = TeamPeriodEnd.RESET,
    started_at: datetime | None = None,
) -> uuid.UUID:
    async with sf() as session:
        team = Team(
            org_id=DEFAULT_ORG_ID,
            name=name,
            budget_ceiling_usd=ceiling,
            period_type=period_type,
            on_period_end=on_period_end,
        )
        if started_at is not None:
            team.current_period_started_at = started_at
        session.add(team)
        await session.commit()
        return team.id


async def add_membership(
    sf: async_sessionmaker,
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    role: TeamRole = TeamRole.MEMBER,
    budget: Decimal | None = None,
    spend: Decimal = Decimal(0),
) -> uuid.UUID:
    async with sf() as session:
        membership = TeamMembership(
            team_id=team_id,
            user_id=user_id,
            role=role,
            budget_usd=budget,
            current_spend_usd=spend,
        )
        session.add(membership)
        await session.commit()
        return membership.id


async def set_team_spend(
    sf: async_sessionmaker, team_id: uuid.UUID, amount: Decimal
) -> None:
    """Set the ADR-7 denormalized team aggregate directly (simulating prior
    charges) so period-boundary tests start from a lockstep state."""
    async with sf() as session:
        await session.execute(
            update(Team).where(Team.id == team_id).values(current_spend_usd=amount)
        )
        await session.commit()


async def fetch_row(database_url: str, query: str, *args):
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchrow(query, *args)
    finally:
        await conn.close()


async def fetch_val(database_url: str, query: str, *args):
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchval(query, *args)
    finally:
        await conn.close()


def canned_chat_response(model: str, *, prompt_tokens: int = 4, completion_tokens: int = 3) -> dict:
    return {
        "id": "chatcmpl-bd21",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
