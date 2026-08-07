"""Unit tests for `providers.pricing.compute_self_hosted_cost` and
`services.self_hosted_providers.SelfHostedModelRouteCache` (Phase 5 -
Differentiators, 5.5 Unified Governance for BYOK + Self-Hosted OSS Models).
See `gatekey/phase-5-product-spec.md` AC5.5.5/AC5.5.7.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from gatekey.providers.model_registry import MODEL_REGISTRY
from gatekey.providers.pricing import compute_self_hosted_cost
from gatekey.services.self_hosted_providers import SelfHostedModelRouteCache, SelfHostedRouteEntry


# ---------------------------------------------------------------------------
# compute_self_hosted_cost (AC5.5.7)
# ---------------------------------------------------------------------------


def test_compute_self_hosted_cost_exact_formula():
    """`cost_basis_per_gpu_hour * (wall_clock_latency_seconds / 3600)`,
    computed exactly - a $3.60/GPU-hour rate over exactly one hour (3600s)
    is exactly $3.60."""
    cost = compute_self_hosted_cost(Decimal("3.60"), wall_clock_latency_seconds=Decimal(3600))
    assert cost == Decimal("3.60")


def test_compute_self_hosted_cost_one_second_at_one_dollar_per_hour():
    cost = compute_self_hosted_cost(Decimal("1.00"), wall_clock_latency_seconds=Decimal(1))
    assert cost == Decimal(1) / Decimal(3600)


def test_compute_self_hosted_cost_zero_latency_is_zero():
    cost = compute_self_hosted_cost(Decimal("2.50"), wall_clock_latency_seconds=Decimal(0))
    assert cost == Decimal(0)


def test_compute_self_hosted_cost_accepts_float_latency_via_decimal_str_conversion():
    """`float` is accepted for caller convenience (`time.perf_counter()`
    deltas are floats) but always computed in `Decimal` via `str()` first -
    never a direct `Decimal(float)` construction (module docstring)."""
    cost_from_float = compute_self_hosted_cost(Decimal("3.60"), wall_clock_latency_seconds=1800.0)
    cost_from_decimal = compute_self_hosted_cost(
        Decimal("3.60"), wall_clock_latency_seconds=Decimal("1800.0")
    )
    assert cost_from_float == cost_from_decimal == Decimal("1.80")


def test_compute_self_hosted_cost_returns_decimal_type():
    cost = compute_self_hosted_cost(Decimal("5.00"), wall_clock_latency_seconds=120.0)
    assert isinstance(cost, Decimal)


def test_compute_self_hosted_cost_scales_linearly_with_latency():
    # 3600s/7200s (exact multiples of one hour) avoid Decimal division
    # rounding noise a non-exact-multiple pair (e.g. 60s/120s, which repeats
    # in base 10) would otherwise introduce into a strict equality check.
    basis = Decimal("4.00")
    short = compute_self_hosted_cost(basis, wall_clock_latency_seconds=Decimal(3600))
    long = compute_self_hosted_cost(basis, wall_clock_latency_seconds=Decimal(7200))
    assert long == short * 2


# ---------------------------------------------------------------------------
# SelfHostedModelRouteCache (AC5.5.5) - warm/invalidate behavior
# ---------------------------------------------------------------------------


def test_cache_starts_empty_when_constructed_with_no_initial_snapshot():
    cache = SelfHostedModelRouteCache()
    assert cache.get("vllm-internal-llama3") is None
    assert cache.known_model_ids() == frozenset()


def test_cache_get_returns_entry_after_set_all():
    provider_id = uuid.uuid4()
    entry = SelfHostedRouteEntry(provider_id=provider_id, cost_basis_per_gpu_hour=Decimal("2.00"))
    cache = SelfHostedModelRouteCache()
    cache.set_all({"vllm-internal-llama3": entry})
    fetched = cache.get("vllm-internal-llama3")
    assert fetched is not None
    assert fetched.provider_id == provider_id
    assert fetched.cost_basis_per_gpu_hour == Decimal("2.00")


def test_cache_unknown_model_returns_none():
    cache = SelfHostedModelRouteCache()
    cache.set_all(
        {"vllm-internal-llama3": SelfHostedRouteEntry(uuid.uuid4(), Decimal("1.00"))}
    )
    assert cache.get("not-a-registered-model") is None


def test_cache_known_model_ids_reflects_current_snapshot():
    cache = SelfHostedModelRouteCache()
    cache.set_all(
        {
            "model-a": SelfHostedRouteEntry(uuid.uuid4(), Decimal("1.00")),
            "model-b": SelfHostedRouteEntry(uuid.uuid4(), Decimal("2.00")),
        }
    )
    assert cache.known_model_ids() == frozenset({"model-a", "model-b"})


def test_cache_set_all_is_a_full_replace_not_a_merge():
    """Invalidation on write (design doc section 2.3) re-derives the FULL
    mapping and replaces the cache wholesale - a model present in the OLD
    snapshot but absent from the new one (e.g. removed at the DB layer)
    must disappear from the cache, not linger."""
    cache = SelfHostedModelRouteCache()
    cache.set_all({"model-a": SelfHostedRouteEntry(uuid.uuid4(), Decimal("1.00"))})
    assert cache.get("model-a") is not None

    cache.set_all({"model-b": SelfHostedRouteEntry(uuid.uuid4(), Decimal("2.00"))})
    assert cache.get("model-a") is None
    assert cache.get("model-b") is not None


def test_cache_set_all_copies_input_defensively():
    """`set_all` takes a defensive copy (`dict(snapshot)`) rather than
    storing the caller's dict object by reference - mutating the caller's
    dict AFTER calling `set_all` must never retroactively change what the
    cache serves (same GIL-atomic reference-swap contract as
    `ModelPolicyCache`/`ContentAwareRuleCache`)."""
    cache = SelfHostedModelRouteCache()
    caller_owned_snapshot = {"model-a": SelfHostedRouteEntry(uuid.uuid4(), Decimal("1.00"))}
    cache.set_all(caller_owned_snapshot)

    caller_owned_snapshot["model-b"] = SelfHostedRouteEntry(uuid.uuid4(), Decimal("2.00"))
    del caller_owned_snapshot["model-a"]

    assert cache.get("model-a") is not None
    assert cache.get("model-b") is None


def test_cache_never_collides_with_static_model_registry_keys():
    """Sanity guard for the fixture data below: none of the synthetic
    self-hosted model ids used in these tests happen to collide with a
    real static `MODEL_REGISTRY` key (which would make `resolve_route()`'s
    "static registry always wins" contract untestable in isolation here)."""
    synthetic_ids = {"vllm-internal-llama3", "model-a", "model-b", "not-a-registered-model"}
    assert not synthetic_ids & MODEL_REGISTRY.keys()
