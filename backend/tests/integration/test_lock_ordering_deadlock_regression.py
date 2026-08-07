"""Regression tests for CMR-14's cross-endpoint lock-ordering deadlock fix.

See `docs/design/custom-model-registry-security-review.md`'s "Finding -
genuine deadlock between the new lock and the pre-existing `org_settings.py`
PUT endpoint" for the full write-up. Summary: `services/audit.py::write_
audit_entry`, when an org's hash-chained audit ledger is enabled
(`compliance_settings.chain_enabled=True`), takes `SELECT ... FOR UPDATE`
on the org's `compliance_settings` row. Several admin endpoints ALSO lock a
second config row (`org_settings`, or a `teams` row) in the same
transaction. Before this fix, two different endpoints acquired those two
locks in OPPOSITE orders:

  - `api/v1/admin/custom_models.py`'s POST/PUT and `api/v1/admin/
    self_hosted_providers.py`'s POST/PUT: audit (`compliance_settings`)
    FIRST, then `org_settings` (via the CMR-12.5 collision-guard lock).
  - `api/v1/admin/org_settings.py`'s PUT: `org_settings` (via `set_org_
    budget_ceiling`) FIRST, then audit (`compliance_settings`) SECOND.

Run genuinely concurrently against real Postgres with chaining enabled,
this reliably produced `DeadlockDetectedError`, surfacing as an unhandled
500 on whichever transaction Postgres aborted, instead of both requests
succeeding. The fix reorders `org_settings.py::put_org_settings_endpoint`
to acquire `compliance_settings` (via the audit write) before `org_
settings` - matching the `custom_models.py`/`self_hosted_providers.py`
convention.

Broader systemic finding (not in the original security review, surfaced
while implementing the fix above): `services/team_budget.py::set_team_
budget_ceiling` locks `org_settings` THEN `teams` in ONE call, used by
`api/v1/teams.py`'s `create_team_endpoint`/`update_team_endpoint`. Fixing
THOSE two endpoints' `org_settings` ordering unavoidably also moves their
`teams`-row lock to AFTER the audit/`compliance_settings` lock - which
would have introduced a NEW deadlock against every OTHER `teams`-row-
locking + audit-writing endpoint in that router (`add_member_endpoint`/
`update_member_endpoint`/`approve_join_request_endpoint`/`reassign_budget_
endpoint`, plus `services/scim.py::add_scim_group_members`), which
previously locked `teams` BEFORE writing their audit entry. All of those
were reordered the same way (audit first) to stay globally consistent.
`test_concurrent_team_budget_ceiling_and_member_budget_update_no_deadlock`
below proves that reordering didn't just move the deadlock elsewhere.

Harness style deliberately mirrors `test_custom_model_collision_race_
condition.py` (CMR-12.5's own concurrency-regression test), not an HTTP/
ASGI-level test: a throwaway session-factory-per-task, calling the exact
service functions each fixed route handler calls, in the exact order the
fixed handler now calls them, run genuinely concurrently via `asyncio.
gather`. This targets the actual unit under test (Postgres transaction
lock-acquisition order) directly, without an unrelated ASGI-middleware
concurrency dependency in the loop.
"""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gatekey.api.deps import AdminContext
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.team import Team
from gatekey.db.models.team_membership import TeamMembership, TeamRole
from gatekey.db.models.user import User
from gatekey.providers.model_registry import ModelCapability
from gatekey.services.audit import write_audit_entry
from gatekey.services.compliance_settings import set_chain_enabled
from gatekey.services.custom_models import register_custom_model
from gatekey.services.team_budget import (
    set_org_budget_ceiling,
    set_team_budget_ceiling,
    update_team_membership_budget,
)

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio

_TRUNCATE_SQL = (
    "TRUNCATE TABLE audit_entries, compliance_settings, org_settings, "
    "custom_models, self_hosted_providers, team_memberships, teams, users "
    "CASCADE"
)


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables(migrated_database_url: str):
    """Truncate before AND after (not just before) - same precedent as
    `test_custom_model_collision_race_condition.py`'s fixture: a genuine
    concurrency test's outcome is deliberately not fully deterministic, so
    state can't be cleaned up from inside the test body alone."""

    async def _truncate() -> None:
        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            await conn.execute(_TRUNCATE_SQL)
        finally:
            await conn.close()

    await _truncate()
    yield
    await _truncate()


def _actor() -> AdminContext:
    """A lightweight, session-free actor (same shape `api/deps.require_
    admin`'s break-glass path constructs) - all `write_audit_entry` needs is
    `.org_id`/`.actor_user_id`/`.actor_label`."""
    return AdminContext(actor_user_id=None, actor_label="system:admin_token", org_id=DEFAULT_ORG_ID)


@pytest.fixture
def _race_sf(migrated_database_url: str):
    engine = create_async_engine(migrated_database_url, pool_size=10, max_overflow=20)
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False), engine


def _assert_no_failures(results: list) -> None:
    failures = [r for r in results if isinstance(r, BaseException)]
    assert not failures, (
        f"expected every concurrent request to succeed (chaining enabled, "
        f"lock ordering fixed) - got {len(failures)} failure(s): {failures}"
    )


# --- the blocking finding: org_settings.py vs. custom_models.py -------------


async def test_concurrent_org_settings_put_and_custom_model_register_no_deadlock(
    _race_sf,
) -> None:
    """`PUT /v1/admin/org-settings` concurrently with `POST /v1/admin/
    custom-models`, chaining enabled. Before the fix this reliably raised
    `DeadlockDetectedError` on one side (opposite lock-acquisition order);
    after the fix every request must complete cleanly."""
    sf, engine = _race_sf
    actor = _actor()

    async with sf() as session:
        await set_chain_enabled(session, enabled=True)

    async def _put_org_settings(i: int) -> None:
        async with sf() as session:
            # Mirrors the FIXED `api/v1/admin/org_settings.py::put_org_
            # settings_endpoint` order: audit (compliance_settings lock)
            # BEFORE `set_org_budget_ceiling` (org_settings lock).
            await write_audit_entry(
                session,
                actor=actor,
                action="org_settings.update",
                target_type="org_settings",
                target_id=str(DEFAULT_ORG_ID),
                old_value=None,
                new_value={"budget_ceiling_usd": str(1000 + i)},
            )
            await set_org_budget_ceiling(session, budget_ceiling_usd=Decimal(1000 + i))
            await session.commit()

    async def _register_custom_model(i: int) -> None:
        name = f"cmr14-deadlock-model-{i}"
        async with sf() as session:
            # Mirrors `api/v1/admin/custom_models.py::register_custom_
            # model_endpoint`'s existing order: audit (compliance_settings
            # lock) BEFORE `register_custom_model` (org_settings lock,
            # CMR-12.5's collision guard). `register_custom_model` commits
            # internally.
            custom_model_id = uuid.uuid4()
            await write_audit_entry(
                session,
                actor=actor,
                action="custom_model.register",
                target_type="custom_model",
                target_id=str(custom_model_id),
                old_value=None,
                new_value={"name": name},
            )
            await register_custom_model(
                session,
                custom_model_id=custom_model_id,
                name=name,
                provider="openai",
                native_model_id=f"{name}-native",
                capability=ModelCapability.CHAT,
                input_price_per_million_usd=Decimal("1.0"),
                output_price_per_million_usd=Decimal("2.0"),
                pricing_source=None,
            )

    concurrency = 6
    tasks = [_put_org_settings(i) for i in range(concurrency)] + [
        _register_custom_model(i) for i in range(concurrency)
    ]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await engine.dispose()

    _assert_no_failures(results)


# --- the broader systemic finding: teams.py's own internal consistency -----


async def test_concurrent_team_budget_ceiling_and_member_budget_update_no_deadlock(
    _race_sf,
) -> None:
    """`PATCH /v1/teams/{id}` (budget-ceiling edit - locks `org_settings`
    then `teams` via `set_team_budget_ceiling`) concurrently with `PATCH
    /v1/teams/{id}/members/{user_id}` (member-budget edit - locks `teams`
    via `update_team_membership_budget`) on the SAME team, chaining
    enabled. Proves the broader reordering (module docstring) is
    internally consistent: both now acquire `compliance_settings` before
    `teams`, so this pair cannot deadlock either - fixing the reported bug
    didn't just relocate it."""
    sf, engine = _race_sf
    actor = _actor()

    async with sf() as session:
        await set_chain_enabled(session, enabled=True)
        user = User(org_id=DEFAULT_ORG_ID, name="cmr14-deadlock-member")
        session.add(user)
        await session.flush()
        team = Team(org_id=DEFAULT_ORG_ID, name="cmr14-deadlock-team")
        session.add(team)
        await session.flush()
        membership = TeamMembership(
            team_id=team.id, user_id=user.id, role=TeamRole.MEMBER, budget_usd=Decimal("5")
        )
        session.add(membership)
        await session.commit()
        team_id, user_id = team.id, user.id

    async def _update_team_ceiling(i: int) -> None:
        async with sf() as session:
            # Mirrors the FIXED `update_team_endpoint`: audit
            # (compliance_settings lock) BEFORE `set_team_budget_ceiling`
            # (org_settings THEN teams lock).
            await write_audit_entry(
                session,
                actor=actor,
                action="team.update",
                target_type="team",
                target_id=str(team_id),
                old_value=None,
                new_value={"budget_ceiling_usd": str(500 + i)},
            )
            await set_team_budget_ceiling(
                session, team_id=team_id, budget_ceiling_usd=Decimal(500 + i)
            )
            await session.commit()

    async def _update_member_budget(i: int) -> None:
        async with sf() as session:
            # Mirrors the FIXED `update_member_endpoint`: audit
            # (compliance_settings lock) BEFORE `update_team_membership_
            # budget` (teams lock).
            await write_audit_entry(
                session,
                actor=actor,
                action="team.member.update",
                target_type="team_membership",
                target_id=str(user_id),
                old_value=None,
                new_value={"budget_usd": str(10 + i)},
            )
            await update_team_membership_budget(
                session, team_id=team_id, user_id=user_id, budget_usd=Decimal(10 + i)
            )
            await session.commit()

    concurrency = 6
    tasks = [_update_team_ceiling(i) for i in range(concurrency)] + [
        _update_member_budget(i) for i in range(concurrency)
    ]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await engine.dispose()

    _assert_no_failures(results)
