"""Direct unit tests for `api.v1.gateway.common.dispatch_with_model_fallback`
(Model Catalog + Cross-Provider Fallback Chains, Part B - see
`gatekey/model-catalog-fallback-chains-technical-design.md` section 2.5).

Mirrors `test_call_provider_with_failover.py`'s established "drive the real
function directly with a minimal fake session + monkeypatched dispatch
point" pattern (not the full HTTP/TestClient stack - `dispatch_with_model_
fallback()` is a plain, directly-callable async function with no FastAPI
dependency-injection surface of its own).

`gateway_common.call_provider_with_failover` is monkeypatched to a thin,
DB-free fake (identical in spirit to `gateway_test_support.py`'s own fake)
that just invokes `call_fn` - success/failure behavior for each hop is
controlled per test via a `native_model_id -> outcome` map, so these tests
exercise `dispatch_with_model_fallback()`'s OWN control flow (which
candidate gets tried, in what order, what gets skipped vs. surfaced) without
re-testing `call_provider_with_failover`'s own retry mechanic (already
covered by `test_call_provider_with_failover.py`).

`check_residency`/`check_budget_available` are exercised for real against an
empty (= permissive, zero-I/O) `ResidencyRuleCache` and a monkeypatched
no-op `check_budget_available` respectively - `check_residency`'s own fast
path never touches `session` when no rule is configured (see its
docstring), and `check_budget_available` is monkeypatched directly (its own
DB-backed mechanics are covered by other test files, not the concern here).
`session` is therefore an exploding sentinel throughout - proving none of
these tests accidentally depends on live DB behavior.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from gatekey.api.deps import GatewayCallerContext
from gatekey.api.v1.gateway import common as gateway_common
from gatekey.errors import ModelDeniedError, ProviderNotConfiguredError
from gatekey.providers.base import ProviderCallError
from gatekey.providers.model_registry import ModelCapability, ModelRoute
from gatekey.services.custom_models import CustomModelCacheEntry, CustomModelRouteCache
from gatekey.services.model_policy import (
    ContentAwareRuleCache,
    MemberModelPolicyCache,
    ModelPolicyCache,
    ModelPolicySnapshot,
    TeamModelPolicyCache,
)
from gatekey.services.provider_key_health import TeamFailoverOverrideCache
from gatekey.services.residency import ResidencyRuleCache
from gatekey.services.shared_state import InProcessSharedStateStore


class _ExplodingSessionSentinel:
    """Proves no code path here needs a real database - see module
    docstring. Mirrors `tests/unit/gateway_test_support.py`'s identical
    sentinel."""

    def __getattr__(self, name: str):
        raise AssertionError(f"session.{name} must not be accessed in this test")


def _ctx(*, team_id: uuid.UUID | None = None) -> GatewayCallerContext:
    return GatewayCallerContext(
        org_id=uuid.uuid4(),
        credential_id=uuid.uuid4(),
        credential_type="service_account",
        user_id=uuid.uuid4(),
        team_id=team_id,
        name="test-caller",
    )


def _custom_entry(**overrides: Any) -> CustomModelCacheEntry:
    kwargs: dict[str, Any] = dict(
        id=uuid.uuid4(),
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id="my-custom-native-id",
        input_price_per_million_usd=Decimal("1.00"),
        output_price_per_million_usd=Decimal("2.00"),
        fallback_model_names=(),
    )
    kwargs.update(overrides)
    return CustomModelCacheEntry(**kwargs)


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    original_route: ModelRoute,
    original_model: str,
    custom_model_cache: CustomModelRouteCache,
    build_call_fn: Callable[[ModelRoute], Callable[[Any], Any]],
    model_policy_cache: ModelPolicyCache | None = None,
    budget_trips_first_n_calls: int = 0,
) -> gateway_common.ModelFallbackResult:
    from gatekey.errors import BudgetExhaustedError

    async def _fake_call_provider_with_failover(session, app, *, route, org_id, team_id, request_id, key_provider, health_store, team_override_cache, call_fn):  # noqa: ANN001, ARG001
        result = await call_fn(object())
        return gateway_common.FailoverCallResult(result=result, attempt=0, used_key_id=None)

    budget_calls = {"count": 0}

    async def _fake_check_budget_available(session, user_id, team_id=None):  # noqa: ANN001, ARG001
        budget_calls["count"] += 1
        if budget_calls["count"] <= budget_trips_first_n_calls:
            raise BudgetExhaustedError(
                name="test-user", budget_usd=Decimal("10"), current_spend_usd=Decimal("10")
            )

    monkeypatch.setattr(gateway_common, "call_provider_with_failover", _fake_call_provider_with_failover)
    monkeypatch.setattr(gateway_common, "check_budget_available", _fake_check_budget_available)

    return await gateway_common.dispatch_with_model_fallback(
        _ExplodingSessionSentinel(),
        None,  # app - never read by the faked call_provider_with_failover
        _ctx(),
        original_route=original_route,
        original_model=original_model,
        custom_model_cache=custom_model_cache,
        self_hosted_cache=None,
        model_policy_cache=model_policy_cache if model_policy_cache is not None else ModelPolicyCache(),
        team_model_policy_cache=TeamModelPolicyCache(),
        member_model_policy_cache=MemberModelPolicyCache(),
        content_aware_cache=ContentAwareRuleCache(),
        residency_cache=ResidencyRuleCache(),
        category_findings=frozenset(),
        source_ip=None,
        request_id="req-fallback-test",
        key_provider=object(),
        health_store=InProcessSharedStateStore(),
        team_override_cache=TeamFailoverOverrideCache(),
        build_call_fn=build_call_fn,
    )


def _outcome_based_call_fn(
    outcomes: dict[str, str], calls: list[str]
) -> Callable[[ModelRoute], Callable[[Any], Any]]:
    """`outcomes[native_model_id]` is `"ok"` or `"fail"` - builds a
    `build_call_fn` for `dispatch_with_model_fallback()` that records every
    hop attempted (by native_model_id, in order) into `calls`."""

    def _build(route: ModelRoute):
        async def _call(credential: Any) -> str:
            calls.append(route.native_model_id)
            outcome = outcomes.get(route.native_model_id, "fail")
            if outcome == "fail":
                raise ProviderCallError(f"{route.native_model_id} failed", status_code=500)
            return f"response-from-{route.native_model_id}"

        return _call

    return _build


# ---------------------------------------------------------------------------
# (a) primary succeeds -> chain never touched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_primary_success_chain_never_touched(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _custom_entry(fallback_model_names=("gpt-4o-mini", "claude-sonnet-5"))
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-model": entry})
    original_route = ModelRoute(
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id=entry.native_model_id,
        custom_model_id=entry.id,
    )
    calls: list[str] = []
    build_call_fn = _outcome_based_call_fn({entry.native_model_id: "ok"}, calls)

    result = await _run(
        monkeypatch,
        original_route=original_route,
        original_model="my-custom-model",
        custom_model_cache=cache,
        build_call_fn=build_call_fn,
    )

    assert result.fallback_attempt == 0
    assert result.fallback_from_model is None
    assert result.served_route == original_route
    assert result.served_model == "my-custom-model"
    assert calls == [entry.native_model_id]  # candidates never dispatched


# ---------------------------------------------------------------------------
# (b) primary fails, candidate 1 fails, candidate 2 succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_primary_fails_second_candidate_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _custom_entry(fallback_model_names=("gpt-4o-mini", "claude-sonnet-5"))
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-model": entry})
    original_route = ModelRoute(
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id=entry.native_model_id,
        custom_model_id=entry.id,
    )
    calls: list[str] = []
    build_call_fn = _outcome_based_call_fn(
        {entry.native_model_id: "fail", "gpt-4o-mini": "fail", "claude-sonnet-5": "ok"}, calls
    )

    result = await _run(
        monkeypatch,
        original_route=original_route,
        original_model="my-custom-model",
        custom_model_cache=cache,
        build_call_fn=build_call_fn,
    )

    assert calls == [entry.native_model_id, "gpt-4o-mini", "claude-sonnet-5"]
    assert result.fallback_attempt == 2  # 1-indexed - the second chain entry
    assert result.fallback_from_model == "my-custom-model"
    assert result.served_model == "claude-sonnet-5"
    assert result.served_route.provider == "anthropic"
    assert result.served_route.native_model_id == "claude-sonnet-5"
    assert result.failover.result == "response-from-claude-sonnet-5"


# ---------------------------------------------------------------------------
# (c) every candidate fails -> the PRIMARY's original error, unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_candidates_fail_reraises_primary_error_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _custom_entry(fallback_model_names=("gpt-4o-mini", "claude-sonnet-5"))
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-model": entry})
    original_route = ModelRoute(
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id=entry.native_model_id,
        custom_model_id=entry.id,
    )
    calls: list[str] = []
    build_call_fn = _outcome_based_call_fn(
        {entry.native_model_id: "fail", "gpt-4o-mini": "fail", "claude-sonnet-5": "fail"}, calls
    )

    with pytest.raises(ProviderCallError) as excinfo:
        await _run(
            monkeypatch,
            original_route=original_route,
            original_model="my-custom-model",
            custom_model_cache=cache,
            build_call_fn=build_call_fn,
        )

    # The PRIMARY's own message, not either candidate's.
    assert f"{entry.native_model_id} failed" in str(excinfo.value)
    assert calls == [entry.native_model_id, "gpt-4o-mini", "claude-sonnet-5"]


@pytest.mark.asyncio
async def test_no_fallback_chain_configured_reraises_primary_error_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`fallback_model_names == ()` (the default) - byte-for-byte pre-Part-B
    behavior: the primary's failure propagates immediately, no candidates
    exist to walk."""
    entry = _custom_entry()  # no fallback_model_names
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-model": entry})
    original_route = ModelRoute(
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id=entry.native_model_id,
        custom_model_id=entry.id,
    )
    calls: list[str] = []
    build_call_fn = _outcome_based_call_fn({entry.native_model_id: "fail"}, calls)

    with pytest.raises(ProviderCallError):
        await _run(
            monkeypatch,
            original_route=original_route,
            original_model="my-custom-model",
            custom_model_cache=cache,
            build_call_fn=build_call_fn,
        )
    assert calls == [entry.native_model_id]


@pytest.mark.asyncio
async def test_non_custom_model_route_never_walks_a_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """`original_route.custom_model_id is None` (a plain `MODEL_REGISTRY`
    route) - `candidates` is always `()`, regardless of what any cache
    happens to hold, since step 2 only ever looks up a chain for a
    custom-model route."""
    original_route = ModelRoute(provider="openai", capability=ModelCapability.CHAT, native_model_id="gpt-4o")
    calls: list[str] = []
    build_call_fn = _outcome_based_call_fn({"gpt-4o": "fail"}, calls)

    with pytest.raises(ProviderCallError):
        await _run(
            monkeypatch,
            original_route=original_route,
            original_model="gpt-4o",
            custom_model_cache=CustomModelRouteCache(),
            build_call_fn=build_call_fn,
        )
    assert calls == ["gpt-4o"]


# ---------------------------------------------------------------------------
# (d) a policy-denied candidate is skipped, not surfaced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_denied_candidate_is_skipped_not_surfaced(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _custom_entry(fallback_model_names=("gpt-4o-mini", "claude-sonnet-5"))
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-model": entry})
    original_route = ModelRoute(
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id=entry.native_model_id,
        custom_model_id=entry.id,
    )
    # Denies "gpt-4o-mini" (the first candidate) specifically.
    policy_cache = ModelPolicyCache(ModelPolicySnapshot(mode="denylist", models=frozenset({"gpt-4o-mini"})))
    calls: list[str] = []
    build_call_fn = _outcome_based_call_fn(
        {entry.native_model_id: "fail", "gpt-4o-mini": "ok", "claude-sonnet-5": "ok"}, calls
    )

    result = await _run(
        monkeypatch,
        original_route=original_route,
        original_model="my-custom-model",
        custom_model_cache=cache,
        build_call_fn=build_call_fn,
        model_policy_cache=policy_cache,
    )

    # "gpt-4o-mini" is never actually dispatched (denied before the call) -
    # the walk moves straight to "claude-sonnet-5", which succeeds.
    assert entry.native_model_id in calls
    assert "gpt-4o-mini" not in calls
    assert calls[-1] == "claude-sonnet-5"
    assert result.fallback_attempt == 2
    assert result.served_model == "claude-sonnet-5"


# ---------------------------------------------------------------------------
# (e) a candidate that no longer resolves to anything is silently skipped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unresolvable_candidate_is_silently_skipped_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate verified/resolvable at write time can have since been
    deleted/un-verified - `resolve_route()` raising `ModelNotFoundError` for
    it must not propagate, matching `CustomModelFallbackUnresolvableModel
    Error`'s documented write-vs-runtime distinction."""
    entry = _custom_entry(fallback_model_names=("since-deleted-model", "claude-sonnet-5"))
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-model": entry})
    original_route = ModelRoute(
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id=entry.native_model_id,
        custom_model_id=entry.id,
    )
    calls: list[str] = []
    build_call_fn = _outcome_based_call_fn(
        {entry.native_model_id: "fail", "claude-sonnet-5": "ok"}, calls
    )

    result = await _run(
        monkeypatch,
        original_route=original_route,
        original_model="my-custom-model",
        custom_model_cache=cache,
        build_call_fn=build_call_fn,
    )

    # "since-deleted-model" resolves to nothing (not in MODEL_REGISTRY, not
    # in any cache) - `resolve_route()` raises `ModelNotFoundError`, which
    # is swallowed; the walk never even attempts to dispatch it.
    assert "since-deleted-model" not in calls
    assert result.fallback_attempt == 2
    assert result.served_model == "claude-sonnet-5"


# ---------------------------------------------------------------------------
# Single-level-only enforcement - the one invariant the whole feature's
# safety rests on (technical design doc section 2.2/2.5).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_candidates_own_fallback_chain_is_never_walked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`candidates` is bound ONCE, from the ORIGINAL model's own
    `fallback_model_names`, before the loop starts - a candidate that is
    ITSELF a custom model with its own (non-empty) `fallback_model_names`
    must never have THAT second-order list consulted, even when the
    candidate itself fails."""
    inner_entry = _custom_entry(
        native_model_id="candidate-one-native-id",
        fallback_model_names=("should-never-be-tried",),
    )
    original_entry = _custom_entry(fallback_model_names=("candidate-one",))
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-model": original_entry, "candidate-one": inner_entry})
    original_route = ModelRoute(
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id=original_entry.native_model_id,
        custom_model_id=original_entry.id,
    )
    calls: list[str] = []
    build_call_fn = _outcome_based_call_fn(
        {original_entry.native_model_id: "fail", "candidate-one-native-id": "fail"}, calls
    )

    with pytest.raises(ProviderCallError) as excinfo:
        await _run(
            monkeypatch,
            original_route=original_route,
            original_model="my-custom-model",
            custom_model_cache=cache,
            build_call_fn=build_call_fn,
        )

    assert f"{original_entry.native_model_id} failed" in str(excinfo.value)
    assert calls == [original_entry.native_model_id, "candidate-one-native-id"]
    # The critical assertion: "should-never-be-tried" (candidate-one's OWN
    # fallback target) was never dispatched, and never even resolved -
    # nothing in the walk looked up a second-order chain.
    assert "should-never-be-tried" not in calls


# ---------------------------------------------------------------------------
# Budget re-check per hop - trips mid-chain -> skip, not chain-level abort.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_trip_on_a_candidate_is_skip_and_continue_not_abort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The FIRST candidate's own budget re-check trips (simulating a
    concurrent request pushing the caller over budget in the intervening
    round trip) - it is skipped, exactly like any other per-candidate
    rejection reason (step 3's uniform skip list), and the SECOND candidate
    is still tried and succeeds. Total exhaustion is NOT what happens here -
    this proves a budget trip on an early candidate doesn't abort the whole
    chain."""
    entry = _custom_entry(fallback_model_names=("gpt-4o-mini", "claude-sonnet-5"))
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-model": entry})
    original_route = ModelRoute(
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id=entry.native_model_id,
        custom_model_id=entry.id,
    )
    calls: list[str] = []
    build_call_fn = _outcome_based_call_fn(
        {entry.native_model_id: "fail", "gpt-4o-mini": "ok", "claude-sonnet-5": "ok"}, calls
    )

    result = await _run(
        monkeypatch,
        original_route=original_route,
        original_model="my-custom-model",
        custom_model_cache=cache,
        build_call_fn=build_call_fn,
        budget_trips_first_n_calls=1,
    )

    # "gpt-4o-mini" is never actually dispatched (its own budget check
    # tripped before the call) - the walk moves straight to
    # "claude-sonnet-5", which succeeds.
    assert entry.native_model_id in calls
    assert "gpt-4o-mini" not in calls
    assert result.served_model == "claude-sonnet-5"
    assert result.fallback_attempt == 2


@pytest.mark.asyncio
async def test_budget_trip_on_every_candidate_reraises_primary_error_not_budget_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Total exhaustion (every candidate's budget check trips) still
    surfaces the PRIMARY's original error - never a budget error the client
    never asked about (technical design doc section 2.5's explicit budget
    rationale)."""
    entry = _custom_entry(fallback_model_names=("gpt-4o-mini", "claude-sonnet-5"))
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-model": entry})
    original_route = ModelRoute(
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id=entry.native_model_id,
        custom_model_id=entry.id,
    )
    calls: list[str] = []
    build_call_fn = _outcome_based_call_fn(
        {entry.native_model_id: "fail", "gpt-4o-mini": "ok", "claude-sonnet-5": "ok"}, calls
    )

    with pytest.raises(ProviderCallError) as excinfo:
        await _run(
            monkeypatch,
            original_route=original_route,
            original_model="my-custom-model",
            custom_model_cache=cache,
            build_call_fn=build_call_fn,
            budget_trips_first_n_calls=2,
        )
    assert f"{entry.native_model_id} failed" in str(excinfo.value)
    # Neither candidate's call_fn ever ran - both were skipped at the
    # budget-check step, before dispatch.
    assert calls == [entry.native_model_id]


# ---------------------------------------------------------------------------
# Provider-not-configured on a candidate is skip-and-continue too.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_not_configured_candidate_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = _custom_entry(fallback_model_names=("gpt-4o-mini", "claude-sonnet-5"))
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-model": entry})
    original_route = ModelRoute(
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id=entry.native_model_id,
        custom_model_id=entry.id,
    )
    calls: list[str] = []

    def _build(route: ModelRoute):
        async def _call(credential: Any) -> str:
            calls.append(route.native_model_id)
            if route.native_model_id == entry.native_model_id:
                raise ProviderCallError("primary failed", status_code=500)
            if route.native_model_id == "gpt-4o-mini":
                raise ProviderNotConfiguredError("no key configured for openai")
            return f"response-from-{route.native_model_id}"

        return _call

    result = await _run(
        monkeypatch,
        original_route=original_route,
        original_model="my-custom-model",
        custom_model_cache=cache,
        build_call_fn=_build,
    )
    assert result.served_model == "claude-sonnet-5"
    assert result.fallback_attempt == 2


# ---------------------------------------------------------------------------
# Denial exception classes imported above must actually be the ones caught -
# a cheap regression guard against a future signature drift silently
# widening/narrowing the skip list.
# ---------------------------------------------------------------------------


def test_model_denied_error_and_provider_not_configured_error_are_gatekey_errors() -> None:
    assert issubclass(ModelDeniedError, Exception)
    assert issubclass(ProviderNotConfiguredError, Exception)
