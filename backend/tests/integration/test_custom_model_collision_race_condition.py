"""QA finding (CMR-12): the bidirectional custom_model <-> self_hosted_
provider name-collision guard (`gatekey/custom-model-registry-technical-
design.md` section 4.1 guards #2/#15/#16, product spec section 10's
"Collision guards are bidirectional across all three model-name sources"
NFR) is a plain SELECT-then-write check with NO row lock and NO cross-table
DB constraint backing it - unlike the SAME-table case (`custom_models.
UNIQUE(org_id, name)`), which genuinely is race-proof (see the passing
control test below).

Under READ COMMITTED (this codebase's default isolation level - no
`isolation_level=`/`SELECT ... FOR UPDATE` anywhere in this guard's path,
unlike e.g. `services/team_budget.py`'s assignment-time lock or Phase 5.2's
audit-chain-tail lock), two concurrent registrations - one
`register_custom_model(name=X, ...)`, one `register_self_hosted_provider
(models=[X], ...)` - can both run their collision SELECT before either
COMMITs, see no collision (because neither has committed yet), and both
succeed. The result: `X` is simultaneously claimed by a `custom_models` row
AND a `self_hosted_providers` row, silently violating the exact "bidirectional,
independently tested, not just one direction and assumed symmetric" guarantee
`gatekey/custom-model-registry-technical-design.md` section 8.1 (security-
reviewer mandatory flag list item 2) and section 9.1's mandatory test-scenario
table both require.

Consequence, concretely: `resolve_route()` checks `custom_model_cache`
BEFORE `self_hosted_cache` (section 2.2), so every gateway request for `X`
silently and permanently routes to the CUSTOM model's provider/pricing/
native-model-id - the self-hosted row's `X` entry becomes a dead, unreachable
route the admin has no way of discovering (no error was ever raised at
registration time for either side, and nothing today cross-checks the two
tables again after the fact - `shadowed_by_registry` only ever compares
against the STATIC `MODEL_REGISTRY`, never against `self_hosted_providers`).
This is the same flavor of "silent reroute to a different provider/pricing
config" risk section 4.2/8.1 spends three mitigations on for the
static-registry shadowing case, but reachable here via ordinary concurrent
admin usage (e.g. two Org Admins in the same org racing to register a model,
or one admin double-clicking) with NO detection mechanism at all.

Reproduction: run this test file directly - `test_cross_table_race_...`
FAILS in the current (unfixed) implementation because BOTH registrations
succeed; `test_same_table_custom_vs_custom_race_is_safe` PASSES, proving the
DB `UNIQUE(org_id, name)` constraint alone (no app-layer help needed) is
sufficient when both writers target the SAME table, and that the cross-table
case's exposure is real, not a general "concurrency is hard" artifact.

CMR-14 FIX (landed): both `services/custom_models.py::_validate_custom_model_
write` and `services/self_hosted_providers.py::_validate_model_ids` now take
`SELECT ... FOR UPDATE` on the org's `org_settings` row
(`_lock_org_settings_for_model_name_guard`, one copy per module, both
locking the same physical row) before running their respective collision
SELECT - mirroring `services/team_budget.py`'s ADR-5-style
lock-then-check-then-write pattern. This serializes the two guards across a
commit boundary, so the race this file reproduces can no longer let both
registrations succeed. The `xfail(strict=True)` marker has been removed -
this test now asserts the real, fixed behavior.
"""

from __future__ import annotations

import asyncio
import os
from decimal import Decimal

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gatekey.providers.model_registry import ModelCapability
from gatekey.services.custom_models import register_custom_model
from gatekey.services.encryption import EnvKeyProvider
from gatekey.services.self_hosted_providers import (
    edit_self_hosted_provider,
    register_self_hosted_provider,
)

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _truncate_race_tables(migrated_database_url: str):
    """This file's tests deliberately let a race resolve however it resolves
    (that is the whole point) - unlike every other `custom_models`-touching
    test file, the rows this produces cannot be cleaned up via a `finally`
    block inside the test itself (both branches of the race may commit a row
    that must be removed regardless of which one "won"). Truncate BOTH
    before AND after each test so this file can never leak a row into (or
    inherit one from) any other test file sharing this session-scoped
    database - same per-file isolation precedent as `test_custom_models_api.
    py`'s `_truncate_custom_models`, just also run as a teardown here since
    the leftover-row risk is this file's own deliberate design, not an
    oversight to fix away."""

    async def _truncate() -> None:
        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            await conn.execute("TRUNCATE TABLE custom_models CASCADE")
            await conn.execute("TRUNCATE TABLE self_hosted_providers CASCADE")
        finally:
            await conn.close()

    await _truncate()
    yield
    await _truncate()


@pytest.fixture
def _race_sf(migrated_database_url: str):
    engine = create_async_engine(migrated_database_url, pool_size=10, max_overflow=20)
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False), engine


async def test_cross_table_race_both_registrations_should_not_both_succeed(_race_sf) -> None:
    sf, engine = _race_sf
    key_provider = EnvKeyProvider(os.urandom(32))
    name = "race-collision-name-cmr12"

    async def _register_custom():
        async with sf() as session:
            return await register_custom_model(
                session,
                name=name,
                provider="openai",
                native_model_id="race-native-id",
                capability=ModelCapability.CHAT,
                input_price_per_million_usd=Decimal("1.0"),
                output_price_per_million_usd=Decimal("2.0"),
                pricing_source=None,
            )

    async def _register_self_hosted():
        async with sf() as session:
            return await register_self_hosted_provider(
                session,
                name="race-self-hosted-endpoint-cmr12",
                base_url="http://race-stub.internal:8000",
                bearer_token="token",
                cost_basis_per_gpu_hour=Decimal("1.0"),
                models=[name],
                key_provider=key_provider,
            )

    try:
        results = await asyncio.gather(
            _register_custom(), _register_self_hosted(), return_exceptions=True
        )
    finally:
        await engine.dispose()

    successes = [r for r in results if not isinstance(r, Exception)]
    # The bidirectional guard's whole point: `name` may only ever be claimed
    # by ONE of the two tables - now enforced by the shared `org_settings`
    # row lock (CMR-14 fix, see module docstring).
    assert len(successes) <= 1, (
        f"bidirectional collision guard bypassed under concurrency - both "
        f"registrations succeeded for colliding name {name!r}: {results}"
    )


async def test_edit_self_hosted_race_against_register_custom_model(_race_sf) -> None:
    """Different angle from the register-vs-register race above (added
    alongside the CMR-14 fix, not part of QA's original xfail): proves the
    lock also serializes `services.self_hosted_providers.
    edit_self_hosted_provider` - not just `register_self_hosted_provider` -
    against a concurrent `services.custom_models.register_custom_model`.
    `edit_self_hosted_provider` re-runs `_validate_model_ids` identically
    when `models` is provided, and must be covered by the same lock as the
    register path, not just it."""
    sf, engine = _race_sf
    key_provider = EnvKeyProvider(os.urandom(32))
    name = "race-edit-collision-name-cmr12"

    async with sf() as session:
        existing_provider = await register_self_hosted_provider(
            session,
            name="race-self-hosted-endpoint-edit-cmr12",
            base_url="http://race-stub-edit.internal:8000",
            bearer_token="token",
            cost_basis_per_gpu_hour=Decimal("1.0"),
            models=["race-edit-unrelated-model-cmr12"],
            key_provider=key_provider,
        )
        provider_id = existing_provider.id

    async def _register_custom():
        async with sf() as session:
            return await register_custom_model(
                session,
                name=name,
                provider="openai",
                native_model_id="race-native-id-edit",
                capability=ModelCapability.CHAT,
                input_price_per_million_usd=Decimal("1.0"),
                output_price_per_million_usd=Decimal("2.0"),
                pricing_source=None,
            )

    async def _edit_self_hosted():
        async with sf() as session:
            return await edit_self_hosted_provider(
                session,
                provider_id,
                models=[name],
                key_provider=key_provider,
            )

    try:
        results = await asyncio.gather(
            _register_custom(), _edit_self_hosted(), return_exceptions=True
        )
    finally:
        await engine.dispose()

    successes = [r for r in results if not isinstance(r, Exception)]
    assert len(successes) <= 1, (
        f"bidirectional collision guard bypassed under concurrency (edit "
        f"path) - both operations succeeded for colliding name {name!r}: "
        f"{results}"
    )


async def test_same_table_custom_vs_custom_race_is_safe(_race_sf) -> None:
    """Control: the SAME-table case is genuinely race-proof, backed by the
    real `UNIQUE(org_id, name)` DB constraint (not app-layer help) - proving
    the cross-table gap above is real and specific, not a general artifact of
    this test harness's concurrency setup."""
    sf, engine = _race_sf
    name = "race-custom-vs-custom-cmr12"

    async def _register():
        async with sf() as session:
            return await register_custom_model(
                session,
                name=name,
                provider="openai",
                native_model_id="race-native-id",
                capability=ModelCapability.CHAT,
                input_price_per_million_usd=Decimal("1.0"),
                output_price_per_million_usd=Decimal("2.0"),
                pricing_source=None,
            )

    try:
        results = await asyncio.gather(_register(), _register(), return_exceptions=True)
    finally:
        await engine.dispose()

    successes = [r for r in results if not isinstance(r, Exception)]
    assert len(successes) == 1, f"expected exactly one winner, got: {results}"
