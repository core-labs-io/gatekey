"""Org-wide budget safeguard integration tests (added alongside migration
`0045`) - "one minor typo in team budget can cost an org millions."

Mirrors `test_phase2_budget_concurrency.py`'s established pattern one level
up: real Postgres, atomic-increment concurrency proof, and the live
gateway-hot-path enforcement check. Unlike the team/user counters, there is
NO period-boundary behavior to test here (migration `0045`'s docstring:
deliberately no automatic reset) - `reset_org_spend` is tested directly as
the one, explicit way the counter ever goes back to zero.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from gatekey.api.v1.gateway.common import check_budget_available
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.org_settings import OrgSettings
from gatekey.errors import BudgetExhaustedError, OrgBudgetExhaustedError
from gatekey.services import budget as budget_service

from .phase2_helpers import (  # noqa: F401 - fixtures resolved by name
    _clean_phase2_tables,
    add_membership,
    fetch_row,
    make_team,
    make_user,
    sf,
)

pytestmark = pytest.mark.asyncio


async def _set_org_ceiling(sf, ceiling: Decimal | None) -> None:
    from sqlalchemy.dialects import postgresql

    async with sf() as session:
        await session.execute(
            postgresql.insert(OrgSettings)
            .values(org_id=DEFAULT_ORG_ID, budget_ceiling_usd=ceiling)
            .on_conflict_do_update(
                index_elements=[OrgSettings.org_id], set_={"budget_ceiling_usd": ceiling}
            )
        )
        await session.commit()


# --- no-row default state -----------------------------------------------------


async def test_org_budget_state_defaults_when_no_row_exists(sf) -> None:
    """ADR-2: absence of an `org_settings` row is the normal "never
    configured" state - unmetered, zero spend, never exhausted."""
    async with sf() as session:
        state = await budget_service.get_org_budget_state(session)
    assert state.budget_usd is None
    assert state.current_spend_usd == Decimal(0)
    assert budget_service.is_budget_exhausted(state) is False


# --- atomic concurrency (mirrors test_concurrent_charges_lose_no_updates...) --


async def test_concurrent_org_charges_lose_no_updates(sf, migrated_database_url: str) -> None:
    """20 fully concurrent org charges, starting from NO `org_settings` row
    (first-ever spend): the upsert-increment must create the row exactly
    once and never lose an update - final total is exactly 20 * cost."""
    cost = Decimal("0.75")
    n = 20

    async def _one_charge() -> None:
        async with sf() as session:
            await budget_service.record_org_usage_charge(session, cost=cost)

    await asyncio.gather(*(_one_charge() for _ in range(n)))

    row = await fetch_row(
        migrated_database_url,
        "SELECT current_spend_usd FROM org_settings WHERE org_id = $1",
        DEFAULT_ORG_ID,
    )
    assert row is not None
    assert Decimal(row["current_spend_usd"]) == cost * n


# --- live enforcement: org ceiling blocks even with team/user headroom -------


async def test_org_ceiling_blocks_request_even_with_team_and_user_headroom(
    sf, migrated_database_url: str
) -> None:
    """The whole point: a team/user with plenty of their OWN budget headroom
    must still be blocked once the ORG total is exhausted - the org check is
    a real circuit breaker, independent of any lower-level budget."""
    user_id = await make_user(sf, "org-safeguard-user")
    team_id = await make_team(sf, "org-safeguard-team", ceiling=Decimal(1_000_000))
    await add_membership(sf, team_id, user_id, budget=Decimal(1_000_000))  # huge headroom

    await _set_org_ceiling(sf, Decimal("10"))
    async with sf() as session:
        await budget_service.record_org_usage_charge(session, cost=Decimal("10"))

    with pytest.raises(OrgBudgetExhaustedError):
        async with sf() as session:
            await check_budget_available(session, user_id, team_id=team_id)


async def test_org_ceiling_unmetered_never_blocks(sf) -> None:
    """`budget_ceiling_usd IS NULL` (never configured, or explicitly
    cleared) - unmetered, same as every other budget field's NULL
    semantics in this codebase. Only the team/user checks below it can
    still block."""
    user_id = await make_user(sf, "org-unmetered-user")
    team_id = await make_team(sf, "org-unmetered-team")
    await add_membership(sf, team_id, user_id, budget=None)

    async with sf() as session:
        await budget_service.record_org_usage_charge(session, cost=Decimal("999999"))

    async with sf() as session:
        await check_budget_available(session, user_id, team_id=team_id)  # must not raise


async def test_org_check_runs_even_on_legacy_flat_user_path(sf) -> None:
    """`team_id=None` (legacy flat-user path) must ALSO be caught by the
    org-wide check - the safeguard exists to catch total spend regardless
    of which path a request took."""
    user_id = await make_user(sf, "org-legacy-user")
    await _set_org_ceiling(sf, Decimal("5"))
    async with sf() as session:
        await budget_service.record_org_usage_charge(session, cost=Decimal("5"))

    with pytest.raises(OrgBudgetExhaustedError):
        async with sf() as session:
            await check_budget_available(session, user_id, team_id=None)


async def test_user_level_block_still_takes_priority_message_when_org_has_headroom(
    sf,
) -> None:
    """Sanity check the two checks don't interfere: with org headroom intact,
    a user's OWN exhausted budget still raises the ORIGINAL per-user error,
    not the org one."""
    user_id = await make_user(sf, "org-headroom-user")
    team_id = await make_team(sf, "org-headroom-team")
    await add_membership(sf, team_id, user_id, budget=Decimal(0))  # exhausted immediately
    await _set_org_ceiling(sf, Decimal("1000"))  # org has plenty of headroom

    with pytest.raises(BudgetExhaustedError):
        async with sf() as session:
            await check_budget_available(session, user_id, team_id=team_id)


# --- explicit reset -------------------------------------------------------------


async def test_reset_org_spend_zeroes_counter_keeps_ceiling(
    sf, migrated_database_url: str
) -> None:
    await _set_org_ceiling(sf, Decimal("50"))
    async with sf() as session:
        await budget_service.record_org_usage_charge(session, cost=Decimal("50"))

    async with sf() as session:
        row = await budget_service.reset_org_spend(session)
        await session.commit()
    assert row.current_spend_usd == Decimal(0)
    assert row.budget_ceiling_usd == Decimal("50")  # reset never touches the ceiling

    async with sf() as session:
        state = await budget_service.get_org_budget_state(session)
    assert state.current_spend_usd == Decimal(0)
    assert budget_service.is_budget_exhausted(state) is False


async def test_reset_org_spend_creates_row_if_absent(sf) -> None:
    """Reset on an org that has never touched org settings (no row at all)
    must not error - it upserts a fresh, zeroed row."""
    async with sf() as session:
        row = await budget_service.reset_org_spend(session)
        await session.commit()
    assert row.current_spend_usd == Decimal(0)
    assert row.budget_ceiling_usd is None
