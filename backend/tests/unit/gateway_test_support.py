"""Shared test helpers for `tests/unit/test_gateway_*.py`.

Not itself a test module (no `test_` prefix - pytest won't collect it).

Builds the real `create_app()` FastAPI app (so the actual router
registration / lifespan wiring under test in `main.py` is exercised) but
never touches a real database or a real provider API:

  - `get_db_session` is always overridden to yield `_ExplodingSessionSentinel`
    - a stand-in that raises `AssertionError` on any attribute access -
      which proves a test's code path never tries to run a real query
      (every gateway route's actual credential lookup goes through
      `services.proxy_keys.get_decrypted_provider_credential`, which
      individual tests monkeypatch at
      `gatekey.api.v1.gateway.common.get_decrypted_provider_credential`).
  - `require_gateway_credential` (Phase 2's unified gateway auth, which
    replaced `require_service_account` on the gateway routes) is overridden
    to a canned `GatewayCallerContext` for tests that want to exercise
    already-authenticated behavior (`build_authenticated_app`); tests that
    want to exercise the real auth dependency's 401 paths instead use
    `build_app_with_real_auth` and monkeypatch the DB lookup it calls
    through, mirroring `tests/unit/test_deps.py`'s pattern.
  - `resolve_model()` is left real (it's a pure, zero-I/O module - see
    `providers/model_registry.py`'s docstring) - tests use real gateway-
    facing model names from `MODEL_REGISTRY` rather than mocking resolution
    itself.
  - Phase 1.4 (Budget - Basic): `services.budget.get_budget_state`,
    `services.budget.record_usage_charge`, and
    `services.usage_logs.record_usage_log` are monkeypatched to
    DB-free defaults by `build_authenticated_app` (unmetered budget state,
    a fixed charge, a no-op log write) - same "replace the whole DB-touching
    function" pattern already used for `get_decrypted_provider_credential`,
    so the exploding session sentinel still proves no *unexpected* DB access
    happens. Individual tests override these again via the same
    `monkeypatch` fixture where they need to exercise non-default budget
    behavior (e.g. an exhausted budget).
  - Phase 3 (DLP/residency): `services.dlp.load_dlp_policy`/`load_custom_
    patterns`/`get_team_dlp_override` are monkeypatched to DB-free
    "nothing configured" defaults (every detector off, no custom patterns,
    no team override) by `build_authenticated_app` - `run_dlp_scan()`'s own
    `has_any_scanning_enabled()` fast-path then skips Presidio and every
    further DB read entirely, same discipline as the budget fakes above.
    `check_residency()` needs no fake: its own zero-I/O fast path already
    skips all DB access when the (test-default, DB-unreachable-at-startup)
    `ResidencyRuleCache`/`ContentAwareRuleCache` on `app.state` are empty.
  - Phase 4 (Reliability & Cost Efficiency): each gateway route handler
    (`api/v1/gateway/{chat,completions,embeddings}.py`) now calls
    `common.call_provider_with_failover()` instead of the old bare
    `fetch_credential()` + provider call - and does so via a plain
    `from ... import call_provider_with_failover`, so it holds its own
    module-level binding rather than looking it up through `common.` at
    call time (unlike `budget_service.get_budget_state`/etc above, which
    genuinely are looked up via module-attribute access and so are
    patchable at the `..._service` module itself). `call_provider_with_
    failover()`'s first step, `provider_key_health.select_provider_key()`,
    does a real `session.execute()` - which would hit the exploding
    sentinel before a test's own credential fake ever gets a chance to
    run. `build_authenticated_app` therefore monkeypatches
    `call_provider_with_failover` directly on each of the three route
    modules (not on `common`, which not one of them actually reads it
    from at call time) to a DB-free fake that skips straight to `common.
    fetch_credential()` (itself unpatched - a thin, zero-I/O wrapper
    around the module-level `common.get_decrypted_provider_credential`
    name, which per-test/per-file fakes already monkeypatch, same as
    before Phase 4) and then invokes `call_fn` once - i.e. byte-for-byte
    the pre-Phase-4 behavior (now wrapped in a `FailoverCallResult` with
    `attempt=0`/`used_key_id=None`, matching `call_provider_with_failover`'s
    real Phase-4 return shape - see `common.FailoverCallResult`). This
    intentionally does not exercise failover retry/health-store
    bookkeeping; no test in `test_gateway_{chat,completions,embeddings}.py`
    asserts on that today - see `tests/integration/test_phase4_reliability_
    cost.py` for that coverage instead.
  - Phase 4 gateway-pipeline wiring (rate limiting/caching/degradation):
    `check_rate_limit()`/`check_response_cache()`/`check_and_apply_
    degradation()` read `RateLimitCache`/`CachingSettingsCache`/
    `DegradationPolicyCache` off `app.state` (Fix 6, NFR gap - these are
    now warmed by the real lifespan `create_app()` runs, same as
    `app.state.model_policy_cache`). No monkeypatch needed for the default
    "nothing configured" case: the real lifespan's DB warm fails harmlessly
    against this harness's unreachable DSN and leaves each cache at its
    empty (= unconfigured/permissive) default, same fail-open contract
    `model_policy_cache` already relies on. Individual tests that want
    non-default Phase 4 behavior push directly into the relevant
    `app.state.*_cache` (only reachable once the real lifespan has run,
    i.e. inside `with TestClient(app) as client:`) - see e.g.
    `test_gateway_phase4_pipeline.py`.
"""

from __future__ import annotations

import base64
import os
import uuid
from decimal import Decimal

import pytest
from fastapi import FastAPI

from gatekey.api.deps import GatewayCallerContext, get_db_session, require_gateway_credential
from gatekey.api.v1.gateway import chat as gateway_chat
from gatekey.api.v1.gateway import common as gateway_common
from gatekey.api.v1.gateway import completions as gateway_completions
from gatekey.api.v1.gateway import embeddings as gateway_embeddings
from gatekey.config import Settings
from gatekey.db.session import get_db_session as db_session_get_db_session
from gatekey.main import create_app
from gatekey.services import budget as budget_service
from gatekey.services import dlp as dlp_service
from gatekey.services import usage_logs as usage_logs_service


def make_settings(**overrides) -> Settings:
    defaults = dict(
        _env_file=None,
        DATABASE_URL="postgresql+asyncpg://user:pass@localhost:5432/gatekey",
        GATEKEY_ADMIN_TOKEN="test-token",
        GATEKEY_MASTER_KEY=base64.b64encode(os.urandom(32)).decode(),
    )
    defaults.update(overrides)
    return Settings(**defaults)


class _ExplodingSessionSentinel:
    """Session stand-in that errors on any attribute access.

    Proves a test's code path never touches the database directly - mirrors
    the identical pattern in `tests/unit/test_deps.py`.
    """

    def __getattr__(self, name: str):
        raise AssertionError(f"session.{name} must not be accessed in this test")


async def _override_get_db_session():
    yield _ExplodingSessionSentinel()


_DEFAULT_USER_ID = uuid.uuid4()


def _default_unmetered_state(user_id: uuid.UUID) -> budget_service.UserBudgetState:
    return budget_service.UserBudgetState(
        id=user_id, name="test-user", budget_usd=None, current_spend_usd=Decimal("0")
    )


def build_authenticated_app(
    monkeypatch: pytest.MonkeyPatch, *, org_id: uuid.UUID | None = None, user_id: uuid.UUID | None = None
) -> FastAPI:
    """Build the real gateway app with auth pre-satisfied.

    `require_gateway_credential` is overridden to always return a canned
    `GatewayCallerContext` (a `gk_sk_`-shaped, legacy `team_id=None`
    caller) - use this for tests that are exercising model-resolution /
    credential-fetch / provider-dispatch behavior, not the auth dependency
    itself.

    `monkeypatch` is required (not optional) so this helper can install the
    Phase 1.4 budget-check/charge/log defaults described in the module
    docstring - callers that need non-default budget behavior re-patch the
    same targets afterward via their own `monkeypatch` fixture instance
    (the same instance passed in here).
    """
    app = create_app(settings=make_settings())
    resolved_user_id = user_id or _DEFAULT_USER_ID
    context = GatewayCallerContext(
        org_id=org_id or uuid.uuid4(),
        credential_id=uuid.uuid4(),
        credential_type="service_account",
        user_id=resolved_user_id,
        team_id=None,
        name="test-service-account",
    )
    app.dependency_overrides[get_db_session] = _override_get_db_session
    app.dependency_overrides[db_session_get_db_session] = _override_get_db_session
    app.dependency_overrides[require_gateway_credential] = lambda: context

    async def _fake_get_budget_state(session, user_id):  # noqa: ANN001, ARG001
        return _default_unmetered_state(user_id)

    async def _fake_record_usage_charge(session, **kwargs):  # noqa: ANN001, ARG001
        return Decimal("0.000001")

    async def _fake_record_usage_log(session, **kwargs):  # noqa: ANN001, ARG001
        return None

    async def _fake_load_dlp_policy(session):  # noqa: ANN001, ARG001
        return dlp_service._DEFAULT_POLICY

    async def _fake_load_custom_patterns(session):  # noqa: ANN001, ARG001
        return []

    async def _fake_get_team_dlp_override(session, team_id):  # noqa: ANN001, ARG001
        return None

    async def _fake_call_provider_with_failover(  # noqa: ANN001
        session,
        app,  # noqa: ARG001
        *,
        route,
        org_id,  # noqa: ARG001
        team_id,  # noqa: ARG001
        request_id,  # noqa: ARG001
        key_provider,
        health_store,  # noqa: ARG001
        team_override_cache,  # noqa: ARG001
        call_fn,
    ):
        """DB-free stand-in for Phase 4's `common.call_provider_with_
        failover` - see module docstring's "Phase 4" paragraph for why
        this must be patched per-route-module rather than on `common`,
        and why delegating to `common.fetch_credential()` keeps every
        existing per-test-file `get_decrypted_provider_credential` fake
        working unmodified. Wraps the result in a `FailoverCallResult`
        (`attempt=0`, `used_key_id=None`) matching the real function's
        Phase-4 return shape - see module docstring's "Phase 4" bullet."""
        credential = await gateway_common.fetch_credential(session, route.provider, key_provider=key_provider)
        result = await call_fn(credential)
        return gateway_common.FailoverCallResult(result=result, attempt=0, used_key_id=None)

    monkeypatch.setattr(budget_service, "get_budget_state", _fake_get_budget_state)
    monkeypatch.setattr(budget_service, "record_usage_charge", _fake_record_usage_charge)
    monkeypatch.setattr(usage_logs_service, "record_usage_log", _fake_record_usage_log)
    monkeypatch.setattr(dlp_service, "load_dlp_policy", _fake_load_dlp_policy)
    monkeypatch.setattr(dlp_service, "load_custom_patterns", _fake_load_custom_patterns)
    monkeypatch.setattr(dlp_service, "get_team_dlp_override", _fake_get_team_dlp_override)
    monkeypatch.setattr(gateway_chat, "call_provider_with_failover", _fake_call_provider_with_failover)
    monkeypatch.setattr(gateway_completions, "call_provider_with_failover", _fake_call_provider_with_failover)
    monkeypatch.setattr(gateway_embeddings, "call_provider_with_failover", _fake_call_provider_with_failover)
    # Fix 6 (NFR gap): rate limiting/caching/degradation no longer read
    # through `load_effective_rate_limit_rules`/`load_effective_caching_
    # config`/`load_effective_degradation_policy` on the hot path at all -
    # see module docstring's "Phase 4 gateway-pipeline wiring" bullet. The
    # real lifespan (run by `create_app()` above) already constructs
    # `app.state.rate_limit_cache`/`caching_settings_cache`/
    # `degradation_policy_cache` empty (its DB warm fails harmlessly
    # against this harness's unreachable DSN, same fail-open contract
    # `app.state.model_policy_cache` already relies on), which is exactly
    # the "nothing configured" default every test here needs - no
    # monkeypatch required. Tests that want non-default behavior push
    # directly into the relevant `app.state.*_cache` (only reachable once
    # the real lifespan has run, i.e. inside `with TestClient(app) as
    # client:` - see e.g. `test_gateway_phase4_pipeline.py`).

    return app


def build_app_with_real_auth() -> FastAPI:
    """Build the real gateway app with the real `require_gateway_credential`
    dependency still in force (not overridden).

    Use this for 401 tests. The DB session it depends on is still
    overridden to the exploding sentinel; pair this with monkeypatching
    `gatekey.api.deps.get_active_service_account_by_hash` (see
    `test_deps.py`'s identical pattern) so no real database is needed.
    """
    app = create_app(settings=make_settings())
    app.dependency_overrides[get_db_session] = _override_get_db_session
    app.dependency_overrides[db_session_get_db_session] = _override_get_db_session
    return app
