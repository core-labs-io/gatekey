"""Integration tests for CMR-6's `main.py` startup wiring: the REAL, DB-
backed warm of `CustomModelRouteCache` (`_warm_custom_model_route_cache`,
mirroring `_warm_self_hosted_model_route_cache`) and the shadowing
cross-reference startup log (`_log_custom_model_shadowing`) - see
`gatekey/custom-model-registry-technical-design.md` sections 2.4a/5 row
3-4/6.3.

Unlike `test_custom_models_gateway_wiring.py`/`test_custom_model_policy_
wiring.py` (which stand in for "register + verify" by overriding the
`get_custom_model_route_cache` FastAPI dependency with a pre-built,
in-memory `CustomModelRouteCache`), this file's whole point is proving the
REAL startup path: rows are inserted directly into Postgres via the ORM
(bypassing `services.custom_models.register_custom_model()`'s write-time
collision guard entirely, on purpose - the guard prevents this exact
collision from ever being CREATED going forward, but cannot retroactively
protect an already-registered row against a NEW static registry key shipped
in a later release; simulating that inverse-order scenario is the whole
point of this test, per design doc section 6.3), then the app's real
lifespan (`main.py::_lifespan`) is driven directly via `app.router.
lifespan_context(app)` - the same mechanism `conftest.py`'s `client`
fixture uses, just invoked manually here so rows can be inserted BEFORE
startup runs.

Log assertions use a monkeypatched spy on `gatekey.main.logger.error`
directly, NOT pytest's `caplog` fixture - `tests/unit/test_main.py::test_
model_policy_bootstrap_failure_fails_open_permissive`'s docstring already
documents why `caplog` is unreliable against the `"gatekey"` logger in this
codebase: `alembic/env.py` calls `logging.config.fileConfig()` (default
`disable_existing_loggers=True`) on every Alembic invocation, which
permanently disables `logging.getLogger("gatekey")` for the rest of the
process. This test's own `migrated_database_url` fixture runs an Alembic
upgrade, so by the time this test body executes, the `"gatekey"` logger has
already been disabled in-process - `caplog` would silently observe zero
records here even though the log line fired correctly. A direct monkeypatch
spy on `main.logger.error` bypasses the `disabled` flag entirely (it never
goes through `Logger.error`'s own enablement check) and is the established,
robust pattern this codebase already uses for exactly this reason.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI

import gatekey.main as main_module
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.custom_model import CustomModel
from gatekey.db.session import create_engine as db_create_engine
from gatekey.db.session import create_session_factory
from gatekey.providers.model_registry import MODEL_REGISTRY, ModelCapability

pytestmark = pytest.mark.asyncio


async def _insert_custom_model_rows(database_url: str, rows: list[CustomModel]) -> None:
    class _StubSettings:
        DATABASE_URL = database_url

    engine = db_create_engine(_StubSettings())  # type: ignore[arg-type]
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            session.add_all(rows)
            await session.commit()
    finally:
        await engine.dispose()


async def _delete_custom_model_rows(database_url: str, row_ids: list[uuid.UUID]) -> None:
    class _StubSettings:
        DATABASE_URL = database_url

    engine = db_create_engine(_StubSettings())  # type: ignore[arg-type]
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            for row_id in row_ids:
                row = await session.get(CustomModel, row_id)
                if row is not None:
                    await session.delete(row)
            await session.commit()
    finally:
        await engine.dispose()


def _make_row(
    *,
    row_id: uuid.UUID,
    name: str,
    verified: bool,
    native_model_id: str,
) -> CustomModel:
    return CustomModel(
        id=row_id,
        org_id=DEFAULT_ORG_ID,
        name=name,
        provider="openai",
        native_model_id=native_model_id,
        capability=ModelCapability.CHAT,
        input_price_per_million_usd=Decimal("1.00"),
        output_price_per_million_usd=Decimal("2.00"),
        pricing_source=None,
        pricing_as_of=date.today(),
        verified=verified,
    )


async def test_startup_warms_cache_with_only_verified_rows_and_logs_shadowing(
    app: FastAPI,
    migrated_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The design doc's own CMR-6 acceptance scenario: given a mix of
    verified/unverified `custom_models` rows, PLUS one deliberately
    colliding with a real static `MODEL_REGISTRY` key (`"gpt-4o"`, inserted
    directly via the ORM - `register_custom_model()`'s write-time guard #1
    would normally reject this exact collision, so this row simulates an
    already-registered custom model later shadowed by a NEW static registry
    entry shipped in a subsequent Gatekey release, per design doc section
    6.3), real app startup:

      (a) warms `CustomModelRouteCache` with ONLY the verified rows -
          `known_model_ids()` includes the verified + shadowed rows'
          names, never the unverified row's name;
      (b) logs the shadowing collision at ERROR level EXACTLY once, naming
          the org and the colliding row's `custom_model_id` - asserted via
          a monkeypatched spy on `main.logger.error` (see module docstring
          for why not `caplog`), not just "the app didn't crash".
    """
    assert "gpt-4o" in MODEL_REGISTRY, (
        "test fixture assumption: 'gpt-4o' must be a real static MODEL_REGISTRY "
        "key for this test to actually simulate a shadowing collision"
    )

    verified_id = uuid.uuid4()
    unverified_id = uuid.uuid4()
    shadowed_id = uuid.uuid4()

    error_calls: list[tuple[str, dict]] = []

    def _fake_error(msg, *args, **kwargs):  # noqa: ANN001
        error_calls.append((msg, kwargs))

    monkeypatch.setattr(main_module.logger, "error", _fake_error)

    await _insert_custom_model_rows(
        migrated_database_url,
        [
            _make_row(
                row_id=verified_id,
                name="my-verified-custom-model-cmr6",
                verified=True,
                native_model_id="gpt-4o-verified-cmr6",
            ),
            _make_row(
                row_id=unverified_id,
                name="my-unverified-custom-model-cmr6",
                verified=False,
                native_model_id="gpt-4o-unverified-cmr6",
            ),
            # Deliberately colliding with the static registry - see module
            # docstring for why this bypasses the normal write-time guard.
            _make_row(
                row_id=shadowed_id,
                name="gpt-4o",
                verified=True,
                native_model_id="gpt-4o-shadowed-native-cmr6",
            ),
        ],
    )

    try:
        async with app.router.lifespan_context(app):
            cache = app.state.custom_model_route_cache
            known = cache.known_model_ids()

            # (a) Only verified rows are in the warmed cache.
            assert "my-verified-custom-model-cmr6" in known
            assert "my-unverified-custom-model-cmr6" not in known
            # The shadowed row IS verified=true, so it IS present in the
            # cache (cache membership only ever checks `verified`, per
            # design doc section 2.4c's "no auto-remediation" - the static
            # registry wins at REQUEST time via resolve_route()'s ordering,
            # never by removing the row from this cache).
            assert "gpt-4o" in known
            shadowed_entry = cache.get("gpt-4o")
            assert shadowed_entry is not None
            assert shadowed_entry.id == shadowed_id
    finally:
        await _delete_custom_model_rows(
            migrated_database_url, [verified_id, unverified_id, shadowed_id]
        )

    # (b) The shadowing collision was logged at ERROR level EXACTLY once,
    # naming the org and the colliding row's custom_model_id.
    shadow_calls = [
        (msg, kwargs)
        for msg, kwargs in error_calls
        if msg == "custom_model_shadowed_by_static_registry"
    ]
    assert len(shadow_calls) == 1, (
        f"expected exactly one shadowing ERROR log call, got {len(shadow_calls)}: {shadow_calls}"
    )
    _msg, kwargs = shadow_calls[0]
    extra = kwargs.get("extra", {})
    assert extra.get("model") == "gpt-4o"
    assert extra.get("custom_model_id") == str(shadowed_id)
    assert extra.get("org_id") == str(DEFAULT_ORG_ID)

    # And the OTHER two rows must never have triggered a (spurious) shadowing
    # log line of their own - only the real collision does.
    all_shadowed_models = {kwargs.get("extra", {}).get("model") for _msg, kwargs in shadow_calls}
    assert "my-verified-custom-model-cmr6" not in all_shadowed_models
    assert "my-unverified-custom-model-cmr6" not in all_shadowed_models


async def test_startup_cache_warm_is_fail_open_on_no_matching_rows(
    app: FastAPI,
    migrated_database_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard / sanity check for the empty-DB case (no
    `custom_models` rows at all for the default org, the normal state for
    every OTHER integration test module in this suite that never touches
    this table): the cache warms to an empty snapshot, and the shadowing
    check logs nothing at ERROR - a real assertion that this feature's new
    startup code is a no-op/silent-pass for every org that never registers
    a custom model, matching design doc section 9.3's explicit regression
    gate."""
    error_calls: list[tuple[str, dict]] = []

    def _fake_error(msg, *args, **kwargs):  # noqa: ANN001
        error_calls.append((msg, kwargs))

    monkeypatch.setattr(main_module.logger, "error", _fake_error)

    async with app.router.lifespan_context(app):
        cache = app.state.custom_model_route_cache
        assert cache.known_model_ids() == frozenset()

    shadow_calls = [
        (msg, kwargs)
        for msg, kwargs in error_calls
        if msg == "custom_model_shadowed_by_static_registry"
    ]
    assert shadow_calls == []
