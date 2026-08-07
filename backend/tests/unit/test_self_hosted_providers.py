"""Unit tests for `providers.pricing.compute_self_hosted_cost` and
`services.self_hosted_providers.SelfHostedModelRouteCache` (Phase 5 -
Differentiators, 5.5 Unified Governance for BYOK + Self-Hosted OSS Models).
See `gatekey/phase-5-product-spec.md` AC5.5.5/AC5.5.7.

Also covers `_validate_model_ids`'s third guard (Custom Model Registry
feature, CMR-3): a self-hosted `models` entry colliding with an existing
`custom_models.name` for this org is rejected - this module's half of that
feature's bidirectional collision guard (see `gatekey/custom-model-registry-
technical-design.md` section 4.1 guard #2 / section 5 row 15, and
`services/custom_models.py`'s own mirror-image guard, tested in
`test_custom_models_service.py::test_name_colliding_with_self_hosted_model_id_rejected`).
Uses the same `_FakeSession`/queued-results pattern that file already
established, rather than a real DB (mirrors this project's established
unit-vs-integration split for `_validate_model_ids`'s DB-dependent guards).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from gatekey.providers.model_registry import MODEL_REGISTRY
from gatekey.providers.pricing import compute_self_hosted_cost
from gatekey.services.encryption import EnvKeyProvider
from gatekey.services.self_hosted_providers import (
    SelfHostedModelCustomModelCollisionError,
    SelfHostedModelRouteCache,
    SelfHostedRouteEntry,
    edit_self_hosted_provider,
    register_self_hosted_provider,
)


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


# ---------------------------------------------------------------------------
# _validate_model_ids's third guard (CMR-3): collision against
# `custom_models.name` for this org - this module's half of the Custom
# Model Registry feature's bidirectional collision guard.
# ---------------------------------------------------------------------------


def _key_provider() -> EnvKeyProvider:
    return EnvKeyProvider(os.urandom(32))


class _FakeScalars:
    def __init__(self, items: list) -> None:
        self._items = items

    def all(self) -> list:
        return self._items


class _FakeExecuteResult:
    def __init__(self, items: list) -> None:
        self._items = items

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


@dataclass
class _FakeCustomModelRow:
    """Stands in for a `db.models.custom_model.CustomModel` row - the new
    third guard only ever reads `.name` off whatever `_validate_model_ids`'
    `CustomModel` query returns, so a plain object with that one attribute
    is enough (mirrors `test_custom_models_service.py`'s identical
    `_FakeSelfHostedRow` pattern for that module's mirror-image guard)."""

    name: str = ""


@dataclass
class _FakeSelfHostedProviderRow:
    """Stands in for an existing `SelfHostedProvider` row returned by the
    guard's first (already-shipped) query - only `.models` is read."""

    models: list[str] = field(default_factory=list)


class _FakeSession:
    """Minimal in-process fake session (mirrors `test_custom_models_
    service.py`'s established `_FakeSession` pattern). `queued_results` is
    consumed in order, one per `execute()` call - lets a test model
    "first query returns other self-hosted-provider rows, second query
    returns custom_models rows" without inspecting compiled SQL text."""

    def __init__(self, queued_results: list[list] | None = None) -> None:
        self._queued_results = list(queued_results or [])
        self.execute_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.added: list[object] = []

    async def execute(self, stmt):  # noqa: ANN001
        self.execute_calls += 1
        items = self._queued_results.pop(0) if self._queued_results else []
        return _FakeExecuteResult(items)

    def add(self, obj) -> None:  # noqa: ANN001
        self.added.append(obj)

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1

    async def refresh(self, obj) -> None:  # noqa: ANN001
        pass


@pytest.mark.asyncio
async def test_register_self_hosted_provider_rejects_model_id_colliding_with_custom_model_name():
    """Guard #3 (module docstring "Model-id collision guard"): a
    self-hosted `models` entry that collides with an existing
    `custom_models.name` for this org must be rejected, regardless of
    which table was written first."""
    session = _FakeSession(
        queued_results=[
            [],  # CMR-14 org-settings lock: upsert
            [],  # CMR-14 org-settings lock: SELECT ... FOR UPDATE
            [],  # first collision query: no other self-hosted-provider rows claim it
            [_FakeCustomModelRow(name="my-custom-gpt")],  # second query: custom_models rows
        ]
    )
    with pytest.raises(SelfHostedModelCustomModelCollisionError) as exc_info:
        await register_self_hosted_provider(
            session,
            name="my-vllm-endpoint",
            base_url="https://vllm.internal.example.com",
            bearer_token=None,
            cost_basis_per_gpu_hour=Decimal("2.00"),
            models=["my-custom-gpt"],
            key_provider=_key_provider(),
        )
    assert "my-custom-gpt" in str(exc_info.value)
    # No DB write happened - collision guards run BEFORE the insert/commit.
    assert session.commit_calls == 0
    assert session.added == []


@pytest.mark.asyncio
async def test_register_self_hosted_provider_succeeds_when_no_custom_model_collision():
    session = _FakeSession(
        queued_results=[
            [],  # CMR-14 org-settings lock: upsert
            [],  # CMR-14 org-settings lock: SELECT ... FOR UPDATE
            [],  # other self-hosted-provider rows
            [_FakeCustomModelRow(name="some-other-model")],  # custom_models rows
        ]
    )
    row = await register_self_hosted_provider(
        session,
        name="my-vllm-endpoint",
        base_url="https://vllm.internal.example.com",
        bearer_token=None,
        cost_basis_per_gpu_hour=Decimal("2.00"),
        models=["my-custom-gpt"],
        key_provider=_key_provider(),
    )
    assert row.models == ["my-custom-gpt"]
    assert session.commit_calls == 1


@pytest.mark.asyncio
async def test_edit_self_hosted_provider_rejects_model_id_colliding_with_custom_model_name():
    """Same guard, exercised via the edit path - `edit_self_hosted_provider`
    re-runs `_validate_model_ids` whenever `models` is being changed."""
    existing = _FakeSelfHostedProviderRow(models=["old-model"])
    existing.id = uuid.uuid4()  # type: ignore[attr-defined]
    existing.org_id = uuid.uuid4()  # type: ignore[attr-defined]
    session = _FakeSession(
        queued_results=[
            [existing],  # get_self_hosted_provider_by_id
            [],  # CMR-14 org-settings lock: upsert
            [],  # CMR-14 org-settings lock: SELECT ... FOR UPDATE
            [],  # other self-hosted-provider rows
            [_FakeCustomModelRow(name="already-a-custom-model")],  # custom_models rows
        ]
    )
    with pytest.raises(SelfHostedModelCustomModelCollisionError) as exc_info:
        await edit_self_hosted_provider(
            session,
            existing.id,  # type: ignore[attr-defined]
            models=["already-a-custom-model"],
            key_provider=_key_provider(),
        )
    assert "already-a-custom-model" in str(exc_info.value)
