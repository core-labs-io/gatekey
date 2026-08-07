"""Unit tests for `api/v1/gateway/common.py::resolve_route()`'s Custom Model
Registry (Admin-Managed BYOK Models, CMR-4) fallback branch.

See `gatekey/custom-model-registry-technical-design.md` section 2.2 (the
`ModelRoute` discriminator problem / the resolve_route() pseudocode) and
section 5 row 6 (the wiring checklist entry this module proves). Mirrors
`test_self_hosted_providers.py`'s established "no real DB, in-process cache
only" unit-test tier for `resolve_route()`'s sibling self-hosted fallback -
`resolve_route()` itself is zero-I/O once the caches are warmed, so nothing
here needs a database.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from gatekey.api.v1.gateway.common import resolve_route
from gatekey.errors import ModelNotFoundError
from gatekey.providers.model_registry import MODEL_REGISTRY, ModelCapability
from gatekey.services.custom_models import CustomModelCacheEntry, CustomModelRouteCache
from gatekey.services.self_hosted_providers import SelfHostedModelRouteCache, SelfHostedRouteEntry


def _custom_entry(**overrides) -> CustomModelCacheEntry:
    kwargs = dict(
        id=uuid.uuid4(),
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id="gpt-4o-2024-preview",
        input_price_per_million_usd=Decimal("1.00"),
        output_price_per_million_usd=Decimal("2.00"),
    )
    kwargs.update(overrides)
    return CustomModelCacheEntry(**kwargs)


# ---------------------------------------------------------------------------
# Custom-model cache hit
# ---------------------------------------------------------------------------


def test_custom_model_cache_hit_builds_route_with_real_provider_and_custom_model_id():
    """The technical design doc's central discriminator decision (section
    2.2): `route.provider` carries the REAL BYOK provider value (never a
    sentinel), and `custom_model_id` - not `provider` - is what marks this
    as a custom-model route."""
    entry = _custom_entry(provider="anthropic", native_model_id="claude-3-5-sonnet-vendor-id")
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-claude": entry})

    route = resolve_route("my-custom-claude", custom_model_cache=cache)

    assert route.provider == "anthropic"
    assert route.native_model_id == "claude-3-5-sonnet-vendor-id"
    assert route.custom_model_id == entry.id
    assert route.self_hosted_provider_id is None


def test_custom_model_cache_hit_carries_the_row_own_capability_not_hardcoded_chat():
    """Section 2.2: capability comes from the row itself, never hardcoded -
    this is what lets ONE resolve_route() correctly serve both chat.py
    (capability=chat) and embeddings.py (capability=embeddings) call
    sites."""
    entry = _custom_entry(capability=ModelCapability.EMBEDDINGS, output_price_per_million_usd=None)
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-embedder": entry})

    route = resolve_route("my-custom-embedder", custom_model_cache=cache)

    assert route.capability is ModelCapability.EMBEDDINGS


def test_custom_model_cache_miss_falls_through_to_self_hosted():
    custom_cache = CustomModelRouteCache()
    custom_cache.set_all({"some-other-custom-model": _custom_entry()})

    self_hosted_entry = SelfHostedRouteEntry(
        provider_id=uuid.uuid4(), cost_basis_per_gpu_hour=Decimal("1.00")
    )
    self_hosted_cache = SelfHostedModelRouteCache()
    self_hosted_cache.set_all({"vllm-internal-llama3": self_hosted_entry})

    route = resolve_route(
        "vllm-internal-llama3",
        self_hosted_cache=self_hosted_cache,
        custom_model_cache=custom_cache,
    )

    assert route.provider == "self_hosted"
    assert route.self_hosted_provider_id == self_hosted_entry.provider_id
    assert route.custom_model_id is None


def test_custom_model_cache_miss_and_self_hosted_cache_miss_raises_model_not_found():
    custom_cache = CustomModelRouteCache()
    self_hosted_cache = SelfHostedModelRouteCache()

    with pytest.raises(ModelNotFoundError):
        resolve_route(
            "totally-unknown-model",
            self_hosted_cache=self_hosted_cache,
            custom_model_cache=custom_cache,
        )


def test_custom_model_cache_none_behaves_exactly_like_pre_cmr4_default():
    """`custom_model_cache=None` (the default, e.g. `completions.py`'s call
    site) must never fall back to a custom model, even if one happens to be
    registered under this name in some OTHER cache instance the caller
    simply didn't pass here."""
    with pytest.raises(ModelNotFoundError):
        resolve_route("my-custom-gpt")


# ---------------------------------------------------------------------------
# Static registry always wins - provably, even against a defensively
# malformed custom-model cache holding a colliding name (registration itself
# should already prevent this, per the technical design doc's guard #1, but
# resolve_route() must never trust that invariant blindly - section 10's
# explicit NFR).
# ---------------------------------------------------------------------------


def test_static_registry_wins_over_a_colliding_custom_model_cache_entry():
    static_name = next(iter(MODEL_REGISTRY))
    static_route = MODEL_REGISTRY[static_name]

    colliding_entry = _custom_entry(provider="openrouter", native_model_id="should-never-be-reached")
    cache = CustomModelRouteCache()
    cache.set_all({static_name: colliding_entry})

    route = resolve_route(static_name, custom_model_cache=cache)

    assert route.provider == static_route.provider
    assert route.native_model_id == static_route.native_model_id
    assert route.custom_model_id is None


def test_static_registry_wins_over_a_colliding_self_hosted_cache_entry():
    static_name = next(iter(MODEL_REGISTRY))
    static_route = MODEL_REGISTRY[static_name]

    self_hosted_cache = SelfHostedModelRouteCache()
    self_hosted_cache.set_all(
        {static_name: SelfHostedRouteEntry(provider_id=uuid.uuid4(), cost_basis_per_gpu_hour=Decimal("1.00"))}
    )

    route = resolve_route(static_name, self_hosted_cache=self_hosted_cache)

    assert route.provider == static_route.provider
    assert route.self_hosted_provider_id is None


def test_static_registry_wins_over_both_caches_simultaneously():
    """All three cache combinations, in one assertion (section 9.1's
    explicit unit-test scenario: "static > custom > self-hosted precedence,
    all three cache combinations")."""
    static_name = next(iter(MODEL_REGISTRY))
    static_route = MODEL_REGISTRY[static_name]

    custom_cache = CustomModelRouteCache()
    custom_cache.set_all({static_name: _custom_entry()})
    self_hosted_cache = SelfHostedModelRouteCache()
    self_hosted_cache.set_all(
        {static_name: SelfHostedRouteEntry(provider_id=uuid.uuid4(), cost_basis_per_gpu_hour=Decimal("1.00"))}
    )

    route = resolve_route(
        static_name, self_hosted_cache=self_hosted_cache, custom_model_cache=custom_cache
    )

    assert route.provider == static_route.provider
    assert route.custom_model_id is None
    assert route.self_hosted_provider_id is None


# ---------------------------------------------------------------------------
# Custom-model fallback is checked before the self-hosted fallback (the
# order the technical design doc's section 5 row 6 documents as
# "implemented custom-then-self-hosted", even though the two caches' key
# sets are disjoint by construction via the bidirectional collision guard).
# ---------------------------------------------------------------------------


def test_custom_model_cache_checked_before_self_hosted_cache_for_a_defensively_colliding_name():
    """Registration-time guards should already make this collision
    impossible, but `resolve_route()` itself must not silently depend on
    that - proving the actual precedence order defensively, mirroring the
    static-registry-collision tests above."""
    name = "defensively-colliding-name"
    custom_entry = _custom_entry(provider="vertex_ai", native_model_id="custom-wins-here")
    custom_cache = CustomModelRouteCache()
    custom_cache.set_all({name: custom_entry})

    self_hosted_cache = SelfHostedModelRouteCache()
    self_hosted_cache.set_all(
        {name: SelfHostedRouteEntry(provider_id=uuid.uuid4(), cost_basis_per_gpu_hour=Decimal("1.00"))}
    )

    route = resolve_route(name, self_hosted_cache=self_hosted_cache, custom_model_cache=custom_cache)

    assert route.provider == "vertex_ai"
    assert route.custom_model_id == custom_entry.id
    assert route.self_hosted_provider_id is None
