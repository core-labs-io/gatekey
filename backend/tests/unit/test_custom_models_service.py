"""Unit tests for `services/custom_models.py` (Custom Model Registry /
Admin-Managed BYOK Models, CMR-2).

DB-backed CRUD's actual SQL semantics (the `UNIQUE(org_id, name)` ->
`IntegrityError` -> 409 path, real collision queries against real rows,
etc.) are left to the integration suite against a real Postgres, matching
this project's established convention (see e.g. `test_model_policy_
service.py`'s module docstring, `test_self_hosted_providers.py`). This file
covers everything meaningfully unit-testable without a real database:

  - Every write-time validation guard (`_validate_custom_model_write`, via
    `register_custom_model`/`edit_custom_model`) - the PURE guards (ollama,
    unsupported provider, embeddings-provider, capability/pricing mismatch,
    invalid pricing, static-registry collision) are proven to reject
    BEFORE any DB access is attempted (an `_ExplodingSessionSentinel`,
    same pattern `test_provider_keys_service.py` already established, is
    enough to prove this); the one DB-dependent guard (self-hosted
    collision) is exercised with a minimal fake session.
  - `CustomModelRouteCache`'s warm/invalidate contract (mirrors
    `test_self_hosted_providers.py`'s cache tests almost verbatim).
  - `load_custom_model_route_snapshot()`'s query-level `verified = true`
    filter (asserted against the compiled SQL, since a real DB isn't
    available here) and its row-to-cache-entry mapping.
  - `compute_custom_model_cost()`'s exact per-token arithmetic.
  - `verify_custom_model()`'s success/failure/cooldown/not-configured
    paths, with the real provider-client functions monkeypatched out.
  - `edit_custom_model()`'s `verified`-reset semantics: `native_model_id`/
    `provider` edits reset it, a pricing-only edit does not.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

import pytest

import gatekey.providers.openai as openai_provider_module
import gatekey.providers.vertex_ai as vertex_ai_provider_module
import gatekey.services.custom_models as custom_models
from gatekey.db.models.custom_model import CustomModel
from gatekey.errors import ProviderNotConfiguredError, ProviderUpstreamError
from gatekey.providers.base import ProviderCallError
from gatekey.providers.model_registry import MODEL_REGISTRY, ModelCapability
from gatekey.services.custom_models import (
    CustomModelCacheEntry,
    CustomModelCapabilityPricingMismatchError,
    CustomModelEmbeddingsProviderUnsupportedError,
    CustomModelNameRegistryCollisionError,
    CustomModelNameSelfHostedCollisionError,
    CustomModelOllamaProviderError,
    CustomModelPricingInvalidError,
    CustomModelRouteCache,
    CustomModelUnsupportedProviderError,
    CustomModelVerifyCooldownError,
    compute_custom_model_cost,
    edit_custom_model,
    load_custom_model_route_snapshot,
    register_custom_model,
    verify_custom_model,
)
from gatekey.services.proxy_keys import ApiKeyCredential, ProviderKeyNotConfiguredError


# ---------------------------------------------------------------------------
# Fake sessions
# ---------------------------------------------------------------------------


class _ExplodingSessionSentinel:
    """Stands in for `session` in tests that must never touch the DB - any
    attribute access raises immediately. Same pattern already established
    by `test_provider_keys_service.py`."""

    def __getattr__(self, name: str):
        raise AssertionError(f"session.{name} must not be accessed for a pure-guard rejection")


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
class _FakeSelfHostedRow:
    models: list[str] = field(default_factory=list)


class _FakeSession:
    """Minimal in-process fake session (mirrors `test_call_provider_with_
    failover.py`'s established `_FakeSession` pattern). `queued_results` is
    consumed in order, one per `execute()` call - lets a test model "first
    query returns the row being edited, second query (if any) returns the
    self-hosted collision rows" without inspecting compiled SQL text.
    """

    def __init__(self, queued_results: list[list] | None = None) -> None:
        self._queued_results = list(queued_results or [])
        self.execute_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0
        self.added: list[object] = []
        self.deleted: list[object] = []

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

    async def delete(self, obj) -> None:  # noqa: ANN001
        self.deleted.append(obj)


def _make_row(
    *,
    row_id: uuid.UUID | None = None,
    name: str = "my-custom-gpt",
    provider: str = "openai",
    native_model_id: str = "gpt-4o-2024-preview",
    capability: ModelCapability = ModelCapability.CHAT,
    input_price: Decimal = Decimal("1.00"),
    output_price: Decimal | None = Decimal("2.00"),
    verified: bool = False,
) -> CustomModel:
    return CustomModel(
        id=row_id if row_id is not None else uuid.uuid4(),
        org_id=uuid.uuid4(),
        name=name,
        provider=provider,
        native_model_id=native_model_id,
        capability=capability,
        input_price_per_million_usd=input_price,
        output_price_per_million_usd=output_price,
        pricing_source=None,
        pricing_as_of=date.today(),
        verified=verified,
    )


async def _register(session, **overrides):
    kwargs = dict(
        name="my-custom-gpt",
        provider="openai",
        native_model_id="gpt-4o-2024-preview",
        capability=ModelCapability.CHAT,
        input_price_per_million_usd=Decimal("1.00"),
        output_price_per_million_usd=Decimal("2.00"),
        pricing_source=None,
    )
    kwargs.update(overrides)
    return await register_custom_model(session, **kwargs)


# ---------------------------------------------------------------------------
# Write-time validation guards - pure (reject before any DB access)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_provider_rejected_without_touching_db():
    with pytest.raises(CustomModelOllamaProviderError):
        await _register(_ExplodingSessionSentinel(), provider="ollama")


@pytest.mark.asyncio
async def test_unsupported_provider_rejected_without_touching_db():
    with pytest.raises(CustomModelUnsupportedProviderError):
        await _register(_ExplodingSessionSentinel(), provider="not-a-real-provider")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["anthropic", "openrouter"])
async def test_embeddings_capability_rejected_for_non_embeddings_providers(provider: str):
    """Design doc section 2.5a's spec correction: only `openai`/`vertex_ai`
    actually implement `create_embeddings` - confirmed by direct inspection
    of `providers/anthropic.py`/`providers/openrouter.py` (neither module
    defines that function at all)."""
    with pytest.raises(CustomModelEmbeddingsProviderUnsupportedError):
        await _register(
            _ExplodingSessionSentinel(),
            provider=provider,
            capability=ModelCapability.EMBEDDINGS,
            output_price_per_million_usd=None,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "vertex_ai"])
async def test_embeddings_capability_allowed_for_supported_providers_reaches_db_step(provider: str):
    """The inverse of the guard above: `openai`/`vertex_ai` must pass this
    particular guard (and proceed through the CMR-14 org-settings lock
    upsert+select, then the self-hosted-collision DB query, which we let
    return no rows here)."""
    session = _FakeSession(queued_results=[[], [], []])
    row = await _register(
        session,
        provider=provider,
        native_model_id="text-embedding-custom",
        capability=ModelCapability.EMBEDDINGS,
        output_price_per_million_usd=None,
    )
    assert row.provider == provider
    assert row.capability is ModelCapability.EMBEDDINGS
    assert row.verified is False


@pytest.mark.asyncio
async def test_chat_capability_without_output_price_rejected_without_touching_db():
    with pytest.raises(CustomModelCapabilityPricingMismatchError):
        await _register(
            _ExplodingSessionSentinel(),
            capability=ModelCapability.CHAT,
            output_price_per_million_usd=None,
        )


@pytest.mark.asyncio
async def test_embeddings_capability_with_output_price_rejected_without_touching_db():
    with pytest.raises(CustomModelCapabilityPricingMismatchError):
        await _register(
            _ExplodingSessionSentinel(),
            provider="openai",
            capability=ModelCapability.EMBEDDINGS,
            output_price_per_million_usd=Decimal("1.00"),
        )


@pytest.mark.asyncio
async def test_zero_input_price_rejected_without_touching_db():
    with pytest.raises(CustomModelPricingInvalidError):
        await _register(_ExplodingSessionSentinel(), input_price_per_million_usd=Decimal("0"))


@pytest.mark.asyncio
async def test_negative_output_price_rejected_without_touching_db():
    with pytest.raises(CustomModelPricingInvalidError):
        await _register(_ExplodingSessionSentinel(), output_price_per_million_usd=Decimal("-1"))


@pytest.mark.asyncio
async def test_name_colliding_with_static_registry_rejected_without_touching_db():
    """Guard #1 (technical design doc section 2.1/4.1's stated NFR): the
    static registry always wins, provably, BEFORE any DB write."""
    static_name = next(iter(MODEL_REGISTRY))
    with pytest.raises(CustomModelNameRegistryCollisionError):
        await _register(_ExplodingSessionSentinel(), name=static_name)


# ---------------------------------------------------------------------------
# Write-time validation guards - the one DB-dependent guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_name_colliding_with_self_hosted_model_id_rejected():
    """Guard #2 (technical design doc section 2.1 / section 5 row 16) -
    this module's own half of the bidirectional collision guard: queries
    `SelfHostedProvider` rows directly (a plain object with a `.models`
    list is enough - `_self_hosted_model_ids_for_org` only reads that
    attribute). The two leading `[]` entries are the CMR-14 org-settings
    lock's upsert + `SELECT ... FOR UPDATE` calls (see
    `_lock_org_settings_for_model_name_guard`), which now run before the
    self-hosted-collision query itself."""
    session = _FakeSession(
        queued_results=[[], [], [_FakeSelfHostedRow(models=["vllm-internal-llama3"])]]
    )
    with pytest.raises(CustomModelNameSelfHostedCollisionError):
        await _register(session, name="vllm-internal-llama3")


@pytest.mark.asyncio
async def test_name_not_colliding_with_self_hosted_model_id_succeeds():
    session = _FakeSession(
        queued_results=[[], [], [_FakeSelfHostedRow(models=["some-other-model"])]]
    )
    row = await _register(session, name="my-custom-gpt")
    assert row.name == "my-custom-gpt"
    assert session.commit_calls == 1


# ---------------------------------------------------------------------------
# CustomModelRouteCache - warm/invalidate behavior (mirrors
# SelfHostedModelRouteCache's own test file almost verbatim)
# ---------------------------------------------------------------------------


def _entry(**overrides) -> CustomModelCacheEntry:
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


def test_cache_starts_empty_when_constructed_with_no_initial_snapshot():
    cache = CustomModelRouteCache()
    assert cache.get("my-custom-gpt") is None
    assert cache.known_model_ids() == frozenset()


def test_cache_get_returns_entry_after_set_all():
    entry = _entry()
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-gpt": entry})
    fetched = cache.get("my-custom-gpt")
    assert fetched is not None
    assert fetched.id == entry.id
    assert fetched.provider == "openai"


def test_cache_unknown_model_returns_none():
    cache = CustomModelRouteCache()
    cache.set_all({"my-custom-gpt": _entry()})
    assert cache.get("not-registered") is None


def test_cache_known_model_ids_reflects_current_snapshot():
    cache = CustomModelRouteCache()
    cache.set_all({"model-a": _entry(), "model-b": _entry()})
    assert cache.known_model_ids() == frozenset({"model-a", "model-b"})


def test_cache_set_all_is_a_full_replace_not_a_merge():
    cache = CustomModelRouteCache()
    cache.set_all({"model-a": _entry()})
    assert cache.get("model-a") is not None

    cache.set_all({"model-b": _entry()})
    assert cache.get("model-a") is None
    assert cache.get("model-b") is not None


def test_cache_set_all_copies_input_defensively():
    cache = CustomModelRouteCache()
    caller_owned = {"model-a": _entry()}
    cache.set_all(caller_owned)
    caller_owned["model-b"] = _entry()
    del caller_owned["model-a"]

    assert cache.get("model-a") is not None
    assert cache.get("model-b") is None


# ---------------------------------------------------------------------------
# load_custom_model_route_snapshot - query-level verified filter + mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_snapshot_query_filters_on_verified_true():
    """`verified = true` is a QUERY-LEVEL filter (`.where(CustomModel.
    verified.is_(True))`), never a second runtime check on the entries
    themselves - see `CustomModelRouteCache`'s module docstring. Asserted
    against the compiled SQL text since a real Postgres isn't available at
    the unit-test tier (mirrors this project's established DB-vs-unit test
    split)."""
    captured: dict[str, object] = {}

    class _CapturingSession:
        async def execute(self, stmt):  # noqa: ANN001
            captured["stmt"] = stmt
            return _FakeExecuteResult([])

    await load_custom_model_route_snapshot(_CapturingSession())
    compiled_sql = str(captured["stmt"]).lower()
    assert "verified" in compiled_sql


@pytest.mark.asyncio
async def test_load_snapshot_maps_rows_to_cache_entries_by_name():
    row_id = uuid.uuid4()
    row = _make_row(
        row_id=row_id,
        name="my-custom-gpt",
        provider="openai",
        native_model_id="gpt-4o-2024-preview",
        input_price=Decimal("3.50"),
        output_price=Decimal("7.00"),
        verified=True,
    )

    class _FakeVerifiedOnlySession:
        async def execute(self, stmt):  # noqa: ANN001
            # Simulates the DB already having applied `verified = true` -
            # this test's job is the row->entry mapping, not the SQL.
            return _FakeExecuteResult([row])

    snapshot = await load_custom_model_route_snapshot(_FakeVerifiedOnlySession())
    assert set(snapshot) == {"my-custom-gpt"}
    entry = snapshot["my-custom-gpt"]
    assert entry.id == row_id
    assert entry.provider == "openai"
    assert entry.native_model_id == "gpt-4o-2024-preview"
    assert entry.input_price_per_million_usd == Decimal("3.50")
    assert entry.output_price_per_million_usd == Decimal("7.00")


# ---------------------------------------------------------------------------
# compute_custom_model_cost - exact per-token arithmetic
# ---------------------------------------------------------------------------


def test_compute_cost_chat_exact_formula():
    entry = _entry(
        input_price_per_million_usd=Decimal("2.00"), output_price_per_million_usd=Decimal("8.00")
    )
    cost = compute_custom_model_cost(entry, prompt_tokens=1_000_000, completion_tokens=500_000)
    assert cost == Decimal("2.00") + Decimal("4.00")  # 2.00*1 + 8.00*0.5


def test_compute_cost_chat_zero_completion_tokens_still_uses_chat_formula():
    """`completion_tokens=0` (not `None`) still selects the chat formula -
    only `None` selects the embeddings formula (mirrors `services.budget.
    compute_cost()`'s identical `completion_tokens=None` convention)."""
    entry = _entry(
        input_price_per_million_usd=Decimal("1.00"), output_price_per_million_usd=Decimal("5.00")
    )
    cost = compute_custom_model_cost(entry, prompt_tokens=1_000_000, completion_tokens=0)
    assert cost == Decimal("1.00")


def test_compute_cost_embeddings_formula_has_no_output_term():
    entry = _entry(
        capability=ModelCapability.EMBEDDINGS,
        input_price_per_million_usd=Decimal("0.02"),
        output_price_per_million_usd=None,
    )
    cost = compute_custom_model_cost(entry, prompt_tokens=500_000, completion_tokens=None)
    assert cost == Decimal("0.01")


def test_compute_cost_small_token_counts_exact():
    entry = _entry(
        input_price_per_million_usd=Decimal("2.50"), output_price_per_million_usd=Decimal("10.00")
    )
    cost = compute_custom_model_cost(entry, prompt_tokens=100, completion_tokens=50)
    expected = (Decimal("2.50") * 100) / Decimal(1_000_000) + (Decimal("10.00") * 50) / Decimal(
        1_000_000
    )
    assert cost == expected


def test_compute_cost_returns_decimal_type():
    entry = _entry()
    cost = compute_custom_model_cost(entry, prompt_tokens=10, completion_tokens=10)
    assert isinstance(cost, Decimal)


# ---------------------------------------------------------------------------
# verify_custom_model
# ---------------------------------------------------------------------------


class _FakeVerifySession:
    def __init__(self, row: CustomModel) -> None:
        self._row = row
        self.commit_calls = 0

    async def execute(self, stmt):  # noqa: ANN001
        return _FakeExecuteResult([self._row])

    async def commit(self) -> None:
        self.commit_calls += 1

    async def refresh(self, obj) -> None:  # noqa: ANN001
        pass


@pytest.mark.asyncio
async def test_verify_success_sets_verified_true_and_commits(monkeypatch: pytest.MonkeyPatch):
    row = _make_row(provider="openai", capability=ModelCapability.CHAT, verified=False)
    session = _FakeVerifySession(row)

    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        return ApiKeyCredential(provider="openai", api_key="sk-test")

    async def _fake_create_chat_completion(*args, **kwargs):
        return object()

    monkeypatch.setattr(custom_models, "get_decrypted_provider_credential", _fake_get_credential)
    monkeypatch.setattr(
        openai_provider_module, "create_chat_completion", _fake_create_chat_completion
    )

    result = await verify_custom_model(
        session,
        row.id,
        key_provider=object(),
        http_client=object(),
        vertex_token_cache=object(),
    )
    assert result.verified is True
    assert session.commit_calls == 1


@pytest.mark.asyncio
async def test_verify_provider_call_error_leaves_verified_false_and_surfaces_message(
    monkeypatch: pytest.MonkeyPatch,
):
    row = _make_row(provider="openai", capability=ModelCapability.CHAT, verified=False)
    session = _FakeVerifySession(row)

    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        return ApiKeyCredential(provider="openai", api_key="sk-test")

    async def _fake_create_chat_completion(*args, **kwargs):
        raise ProviderCallError("OpenAI returned HTTP 400 during inference.", status_code=400)

    monkeypatch.setattr(custom_models, "get_decrypted_provider_credential", _fake_get_credential)
    monkeypatch.setattr(
        openai_provider_module, "create_chat_completion", _fake_create_chat_completion
    )

    with pytest.raises(ProviderUpstreamError) as exc_info:
        await verify_custom_model(
            session,
            row.id,
            key_provider=object(),
            http_client=object(),
            vertex_token_cache=object(),
        )
    # The real provider error message is surfaced verbatim, never swallowed.
    assert "OpenAI returned HTTP 400" in exc_info.value.message
    assert row.verified is False
    assert session.commit_calls == 1  # still committed the (unchanged) False


@pytest.mark.asyncio
async def test_verify_no_provider_key_configured_raises_not_configured_without_committing(
    monkeypatch: pytest.MonkeyPatch,
):
    row = _make_row(provider="anthropic", capability=ModelCapability.CHAT, verified=False)
    session = _FakeVerifySession(row)

    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        raise ProviderKeyNotConfiguredError(provider)

    monkeypatch.setattr(custom_models, "get_decrypted_provider_credential", _fake_get_credential)

    with pytest.raises(ProviderNotConfiguredError):
        await verify_custom_model(
            session,
            row.id,
            key_provider=object(),
            http_client=object(),
            vertex_token_cache=object(),
        )
    assert session.commit_calls == 0


@pytest.mark.asyncio
async def test_verify_embeddings_capability_dispatches_to_create_embeddings(
    monkeypatch: pytest.MonkeyPatch,
):
    row = _make_row(
        provider="vertex_ai",
        native_model_id="gemini-embedding-001",
        capability=ModelCapability.EMBEDDINGS,
        output_price=None,
        verified=False,
    )
    session = _FakeVerifySession(row)

    from gatekey.services.proxy_keys import ServiceAccountCredential

    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        return ServiceAccountCredential(
            provider="vertex_ai",
            service_account_json={"type": "service_account"},
            project_id="proj",
            location="us-central1",
        )

    calls: list[str] = []

    async def _fake_create_embeddings(*args, **kwargs):
        calls.append("embeddings")
        return object()

    async def _fake_create_chat_completion(*args, **kwargs):
        calls.append("chat")  # pragma: no cover - must never be called
        return object()

    monkeypatch.setattr(custom_models, "get_decrypted_provider_credential", _fake_get_credential)
    monkeypatch.setattr(vertex_ai_provider_module, "create_embeddings", _fake_create_embeddings)
    monkeypatch.setattr(
        vertex_ai_provider_module, "create_chat_completion", _fake_create_chat_completion
    )

    result = await verify_custom_model(
        session,
        row.id,
        key_provider=object(),
        http_client=object(),
        vertex_token_cache=object(),
    )
    assert result.verified is True
    assert calls == ["embeddings"]


@pytest.mark.asyncio
async def test_verify_second_attempt_within_cooldown_raises_429(monkeypatch: pytest.MonkeyPatch):
    row = _make_row(provider="openai", capability=ModelCapability.CHAT, verified=False)
    session = _FakeVerifySession(row)

    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        return ApiKeyCredential(provider="openai", api_key="sk-test")

    async def _fake_create_chat_completion(*args, **kwargs):
        return object()

    monkeypatch.setattr(custom_models, "get_decrypted_provider_credential", _fake_get_credential)
    monkeypatch.setattr(
        openai_provider_module, "create_chat_completion", _fake_create_chat_completion
    )

    await verify_custom_model(
        session, row.id, key_provider=object(), http_client=object(), vertex_token_cache=object()
    )
    with pytest.raises(CustomModelVerifyCooldownError):
        await verify_custom_model(
            session,
            row.id,
            key_provider=object(),
            http_client=object(),
            vertex_token_cache=object(),
        )


# ---------------------------------------------------------------------------
# edit_custom_model - verified-reset semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_native_model_id_resets_verified():
    existing = _make_row(verified=True)
    session = _FakeSession(queued_results=[[existing]])

    updated = await edit_custom_model(session, existing.id, native_model_id="a-different-model-id")
    assert updated.verified is False
    assert updated.native_model_id == "a-different-model-id"


@pytest.mark.asyncio
async def test_edit_provider_resets_verified():
    existing = _make_row(provider="openai", verified=True)
    # provider is changing -> revalidate=True -> the CMR-14 org-settings
    # lock's upsert+select (two `[]` placeholders) then the
    # self-hosted-collision query runs once.
    session = _FakeSession(queued_results=[[existing], [], [], []])

    updated = await edit_custom_model(session, existing.id, provider="openrouter")
    assert updated.verified is False
    assert updated.provider == "openrouter"


@pytest.mark.asyncio
async def test_edit_pricing_only_does_not_reset_verified():
    existing = _make_row(verified=True, input_price=Decimal("1.00"))
    # input price changed -> revalidate=True -> the CMR-14 org-settings
    # lock's upsert+select (two `[]` placeholders) then one
    # self-hosted-collision query.
    session = _FakeSession(queued_results=[[existing], [], [], []])

    updated = await edit_custom_model(
        session, existing.id, input_price_per_million_usd=Decimal("4.00")
    )
    assert updated.verified is True
    assert updated.input_price_per_million_usd == Decimal("4.00")
    assert updated.pricing_as_of == date.today()


@pytest.mark.asyncio
async def test_edit_pricing_source_only_does_not_query_db_beyond_the_initial_fetch():
    """`pricing_source` alone changing needs no guard re-validation at all -
    only the initial `get_custom_model_by_id` fetch should execute."""
    existing = _make_row(verified=True)
    session = _FakeSession(queued_results=[[existing]])

    updated = await edit_custom_model(session, existing.id, pricing_source="https://example.com/pricing")
    assert updated.verified is True
    assert updated.pricing_source == "https://example.com/pricing"
    assert session.execute_calls == 1


@pytest.mark.asyncio
async def test_edit_capability_to_embeddings_clears_output_price_when_explicitly_provided():
    existing = _make_row(
        provider="openai", capability=ModelCapability.CHAT, output_price=Decimal("2.00")
    )
    # capability is changing -> revalidate=True -> the CMR-14 org-settings
    # lock's upsert+select (two `[]` placeholders) then one
    # self-hosted-collision query.
    session = _FakeSession(queued_results=[[existing], [], [], []])

    updated = await edit_custom_model(
        session,
        existing.id,
        capability=ModelCapability.EMBEDDINGS,
        output_price_per_million_usd=None,
        output_price_per_million_usd_provided=True,
    )
    assert updated.capability is ModelCapability.EMBEDDINGS
    assert updated.output_price_per_million_usd is None


@pytest.mark.asyncio
async def test_edit_capability_to_embeddings_without_clearing_price_rejected():
    """The capability/price consistency guard re-runs on a `capability`
    edit (technical design doc section 2.1) - omitting the price-clear
    leaves a stale non-null output price, correctly rejected."""
    existing = _make_row(
        provider="openai", capability=ModelCapability.CHAT, output_price=Decimal("2.00")
    )
    session = _FakeSession(queued_results=[[existing]])

    with pytest.raises(CustomModelCapabilityPricingMismatchError):
        await edit_custom_model(session, existing.id, capability=ModelCapability.EMBEDDINGS)


@pytest.mark.asyncio
async def test_edit_capability_resets_verified():
    """A row verified via a chat completion call and then edited to
    `embeddings` (same native_model_id) must NOT stay `verified=True` - the
    prior verification proved nothing about the new capability."""
    existing = _make_row(
        provider="openai",
        capability=ModelCapability.CHAT,
        output_price=Decimal("2.00"),
        verified=True,
    )
    # capability is changing -> revalidate=True -> the CMR-14 org-settings
    # lock's upsert+select (two `[]` placeholders) then one
    # self-hosted-collision query.
    session = _FakeSession(queued_results=[[existing], [], [], []])

    updated = await edit_custom_model(
        session,
        existing.id,
        capability=ModelCapability.EMBEDDINGS,
        output_price_per_million_usd=None,
        output_price_per_million_usd_provided=True,
    )
    assert updated.verified is False
