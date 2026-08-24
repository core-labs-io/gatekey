"""Unit tests for `services/model_catalog.py` (Model Catalog technical
design doc, Part A).

Mirrors `test_custom_models_service.py`'s `verify_custom_model()` test
style: the real provider-client `list_models()` functions are monkeypatched
out, and `get_decrypted_provider_credential` is monkeypatched on the
`gatekey.services.model_catalog` module (this module's own imported
reference), the same pattern that file already establishes for
`gatekey.services.custom_models`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

import gatekey.providers.anthropic as anthropic_provider_module
import gatekey.providers.openai as openai_provider_module
import gatekey.providers.openrouter as openrouter_provider_module
import gatekey.services.model_catalog as model_catalog
from gatekey.errors import ProviderNotConfiguredError, ProviderUpstreamError
from gatekey.providers.base import ProviderCallError
from gatekey.schemas.custom_model import AvailableModelEntry
from gatekey.services.model_catalog import (
    CustomModelLiveListingUnsupportedError,
    list_available_models,
)
from gatekey.services.proxy_keys import ApiKeyCredential, ProviderKeyNotConfiguredError


class _ExplodingSessionSentinel:
    """Stands in for `session` in tests that must never touch the DB - see
    `test_custom_models_service.py`'s identical sentinel."""

    def __getattr__(self, name: str):
        raise AssertionError(f"session.{name} must not be accessed for the vertex_ai zero-I/O path")


@pytest.fixture(autouse=True)
def _no_verified_custom_models(monkeypatch: pytest.MonkeyPatch):
    """`list_available_models()` now also queries verified custom models
    (for `routable_as` - see `test_routable_as_*` below, which override this
    default locally) to compute `routable_as`. Every OTHER test in this file
    predates that and passes a plain `object()`/`_ExplodingSessionSentinel`
    for `session` - autouse-patched to a real async no-DB stand-in so none
    of them need to know about this unrelated lookup."""

    async def _fake_verified_custom_model_names_by_native_id(session, provider):  # noqa: ANN001
        return {}

    monkeypatch.setattr(
        model_catalog, "_verified_custom_model_names_by_native_id", _fake_verified_custom_model_names_by_native_id
    )


# ---------------------------------------------------------------------------
# vertex_ai - zero-I/O 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vertex_ai_raises_immediately_with_no_io():
    with pytest.raises(CustomModelLiveListingUnsupportedError):
        await list_available_models(
            _ExplodingSessionSentinel(),
            "vertex_ai",
            key_provider=object(),
            http_client=object(),
        )


def test_vertex_ai_error_shape():
    exc = CustomModelLiveListingUnsupportedError()
    assert exc.status_code == 422
    assert exc.code == "custom_model_live_listing_unsupported"


# ---------------------------------------------------------------------------
# provider-not-configured translation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_not_configured_translates_to_gatekey_error(monkeypatch: pytest.MonkeyPatch):
    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        raise ProviderKeyNotConfiguredError(provider)

    monkeypatch.setattr(model_catalog, "get_decrypted_provider_credential", _fake_get_credential)

    with pytest.raises(ProviderNotConfiguredError):
        await list_available_models(
            object(), "openai", key_provider=object(), http_client=object()
        )


# ---------------------------------------------------------------------------
# ProviderCallError translation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provider_call_error_translates_to_provider_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        return ApiKeyCredential(provider="openai", api_key="sk-test")

    async def _fake_list_models(*args, **kwargs):
        raise ProviderCallError("OpenAI returned HTTP 401 during inference.", status_code=401)

    monkeypatch.setattr(model_catalog, "get_decrypted_provider_credential", _fake_get_credential)
    monkeypatch.setattr(openai_provider_module, "list_models", _fake_list_models)

    with pytest.raises(ProviderUpstreamError) as exc_info:
        await list_available_models(
            object(), "openai", key_provider=object(), http_client=object()
        )
    assert "OpenAI returned HTTP 401" in exc_info.value.message


# ---------------------------------------------------------------------------
# OpenAI/Anthropic - "known static price" reverse-index lookup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_entry_matching_registry_gets_prices_filled_in(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        return ApiKeyCredential(provider="openai", api_key="sk-test")

    async def _fake_list_models(client, credential):  # noqa: ANN001
        return [
            # "gpt-4o" is a real MODEL_REGISTRY/PRICING_TABLE key for
            # provider="openai" - must get real prices filled in.
            AvailableModelEntry(
                native_model_id="gpt-4o",
                display_name="gpt-4o",
                input_price_per_million_usd=None,
                output_price_per_million_usd=None,
            ),
            # Not a MODEL_REGISTRY entry at all - must stay unpriced.
            AvailableModelEntry(
                native_model_id="gpt-4o-2024-preview-not-in-registry",
                display_name="gpt-4o-2024-preview-not-in-registry",
                input_price_per_million_usd=None,
                output_price_per_million_usd=None,
            ),
        ]

    monkeypatch.setattr(model_catalog, "get_decrypted_provider_credential", _fake_get_credential)
    monkeypatch.setattr(openai_provider_module, "list_models", _fake_list_models)

    entries = await list_available_models(
        object(), "openai", key_provider=object(), http_client=object()
    )

    by_id = {e.native_model_id: e for e in entries}
    known = by_id["gpt-4o"]
    assert known.input_price_per_million_usd == Decimal("2.50")
    assert known.output_price_per_million_usd == Decimal("10.00")

    unknown = by_id["gpt-4o-2024-preview-not-in-registry"]
    assert unknown.input_price_per_million_usd is None
    assert unknown.output_price_per_million_usd is None


@pytest.mark.asyncio
async def test_anthropic_entry_matching_registry_gets_prices_filled_in(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        return ApiKeyCredential(provider="anthropic", api_key="sk-ant-test")

    async def _fake_list_models(client, credential):  # noqa: ANN001
        return [
            AvailableModelEntry(
                native_model_id="claude-sonnet-5",
                display_name="Claude Sonnet 5",
                input_price_per_million_usd=None,
                output_price_per_million_usd=None,
            ),
            AvailableModelEntry(
                native_model_id="claude-not-a-registry-model",
                display_name="Claude Not A Registry Model",
                input_price_per_million_usd=None,
                output_price_per_million_usd=None,
            ),
        ]

    monkeypatch.setattr(model_catalog, "get_decrypted_provider_credential", _fake_get_credential)
    monkeypatch.setattr(anthropic_provider_module, "list_models", _fake_list_models)

    entries = await list_available_models(
        object(), "anthropic", key_provider=object(), http_client=object()
    )

    by_id = {e.native_model_id: e for e in entries}
    known = by_id["claude-sonnet-5"]
    assert known.input_price_per_million_usd == Decimal("3.00")
    assert known.output_price_per_million_usd == Decimal("15.00")

    unknown = by_id["claude-not-a-registry-model"]
    assert unknown.input_price_per_million_usd is None
    assert unknown.output_price_per_million_usd is None


# ---------------------------------------------------------------------------
# OpenRouter - live pricing passed through untouched (no reverse-index join)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openrouter_entries_keep_their_own_live_pricing(monkeypatch: pytest.MonkeyPatch):
    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        return ApiKeyCredential(provider="openrouter", api_key="sk-or-test")

    async def _fake_list_models(client):  # noqa: ANN001
        return [
            AvailableModelEntry(
                native_model_id="openai/gpt-4o-mini",
                display_name="OpenAI: GPT-4o-mini",
                input_price_per_million_usd=Decimal("0.150000"),
                output_price_per_million_usd=Decimal("0.600000"),
            )
        ]

    monkeypatch.setattr(model_catalog, "get_decrypted_provider_credential", _fake_get_credential)
    monkeypatch.setattr(openrouter_provider_module, "list_models", _fake_list_models)

    entries = await list_available_models(
        object(), "openrouter", key_provider=object(), http_client=object()
    )

    assert len(entries) == 1
    assert entries[0].input_price_per_million_usd == Decimal("0.150000")
    assert entries[0].output_price_per_million_usd == Decimal("0.600000")


# ---------------------------------------------------------------------------
# sorted by native_model_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returned_entries_sorted_by_native_model_id(monkeypatch: pytest.MonkeyPatch):
    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        return ApiKeyCredential(provider="openai", api_key="sk-test")

    async def _fake_list_models(client, credential):  # noqa: ANN001
        return [
            AvailableModelEntry(
                native_model_id="zeta-model",
                display_name="zeta-model",
                input_price_per_million_usd=None,
                output_price_per_million_usd=None,
            ),
            AvailableModelEntry(
                native_model_id="alpha-model",
                display_name="alpha-model",
                input_price_per_million_usd=None,
                output_price_per_million_usd=None,
            ),
        ]

    monkeypatch.setattr(model_catalog, "get_decrypted_provider_credential", _fake_get_credential)
    monkeypatch.setattr(openai_provider_module, "list_models", _fake_list_models)

    entries = await list_available_models(
        object(), "openai", key_provider=object(), http_client=object()
    )

    assert [e.native_model_id for e in entries] == ["alpha-model", "zeta-model"]


# ---------------------------------------------------------------------------
# routable_as - the Model Policy "select models to enable" picker's own
# "is this already routable, and under what name" signal.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routable_as_matches_registry_key_for_openai(monkeypatch: pytest.MonkeyPatch):
    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        return ApiKeyCredential(provider="openai", api_key="sk-test")

    async def _fake_list_models(client, credential):  # noqa: ANN001
        return [
            AvailableModelEntry(
                native_model_id="gpt-4o",
                display_name="gpt-4o",
                input_price_per_million_usd=None,
                output_price_per_million_usd=None,
            )
        ]

    monkeypatch.setattr(model_catalog, "get_decrypted_provider_credential", _fake_get_credential)
    monkeypatch.setattr(openai_provider_module, "list_models", _fake_list_models)

    entries = await list_available_models(object(), "openai", key_provider=object(), http_client=object())
    assert entries[0].routable_as == "gpt-4o"


@pytest.mark.asyncio
async def test_routable_as_none_when_neither_registry_nor_custom_model_matches(
    monkeypatch: pytest.MonkeyPatch,
):
    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        return ApiKeyCredential(provider="openai", api_key="sk-test")

    async def _fake_list_models(client, credential):  # noqa: ANN001
        return [
            AvailableModelEntry(
                native_model_id="a-brand-new-openai-model",
                display_name="A Brand New OpenAI Model",
                input_price_per_million_usd=None,
                output_price_per_million_usd=None,
            )
        ]

    monkeypatch.setattr(model_catalog, "get_decrypted_provider_credential", _fake_get_credential)
    monkeypatch.setattr(openai_provider_module, "list_models", _fake_list_models)

    entries = await list_available_models(object(), "openai", key_provider=object(), http_client=object())
    assert entries[0].routable_as is None


@pytest.mark.asyncio
async def test_routable_as_matches_a_verified_custom_model_by_native_id(monkeypatch: pytest.MonkeyPatch):
    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        return ApiKeyCredential(provider="openai", api_key="sk-test")

    async def _fake_list_models(client, credential):  # noqa: ANN001
        return [
            AvailableModelEntry(
                native_model_id="gpt-5.5-preview",
                display_name="gpt-5.5-preview",
                input_price_per_million_usd=None,
                output_price_per_million_usd=None,
            )
        ]

    async def _fake_verified_custom_model_names_by_native_id(session, provider):  # noqa: ANN001
        return {"gpt-5.5-preview": "my-custom-gpt-5.5"}

    monkeypatch.setattr(model_catalog, "get_decrypted_provider_credential", _fake_get_credential)
    monkeypatch.setattr(openai_provider_module, "list_models", _fake_list_models)
    monkeypatch.setattr(
        model_catalog, "_verified_custom_model_names_by_native_id", _fake_verified_custom_model_names_by_native_id
    )

    entries = await list_available_models(object(), "openai", key_provider=object(), http_client=object())
    assert entries[0].routable_as == "my-custom-gpt-5.5"


@pytest.mark.asyncio
async def test_routable_as_resolves_to_registry_key_not_native_id_for_openrouter(
    monkeypatch: pytest.MonkeyPatch,
):
    """OpenRouter is the one provider whose `MODEL_REGISTRY` key differs
    from its `native_model_id` (ADR-1's `openrouter/` gateway-facing
    prefix - `providers/model_registry.py`'s `"openrouter/openai/gpt-4o-mini"`
    entry carries `native_model_id="openai/gpt-4o-mini"`, no prefix).
    `routable_as` must resolve to the REGISTRY KEY (what `set_policy()`/the
    gateway's `model` field actually accept), never the bare native id -
    the two strings are not interchangeable and a caller staging the wrong
    one would 422 on `PUT /v1/admin/model-policy`."""

    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        return ApiKeyCredential(provider="openrouter", api_key="sk-or-test")

    async def _fake_list_models(client):  # noqa: ANN001
        return [
            AvailableModelEntry(
                # Real OpenRouter slug, matches the registry route's
                # native_model_id exactly - no "openrouter/" prefix here.
                native_model_id="openai/gpt-4o-mini",
                display_name="OpenAI: GPT-4o-mini",
                input_price_per_million_usd=Decimal("0.150000"),
                output_price_per_million_usd=Decimal("0.600000"),
            )
        ]

    monkeypatch.setattr(model_catalog, "get_decrypted_provider_credential", _fake_get_credential)
    monkeypatch.setattr(openrouter_provider_module, "list_models", _fake_list_models)

    entries = await list_available_models(object(), "openrouter", key_provider=object(), http_client=object())

    assert len(entries) == 1
    assert entries[0].native_model_id == "openai/gpt-4o-mini"
    # Must be the prefixed REGISTRY KEY, not the bare native id.
    assert entries[0].routable_as == "openrouter/openai/gpt-4o-mini"
    assert entries[0].routable_as != entries[0].native_model_id


@pytest.mark.asyncio
async def test_routable_as_none_for_openrouter_entry_with_no_registry_or_custom_match(
    monkeypatch: pytest.MonkeyPatch,
):
    """Sanity companion to the above: an OpenRouter entry that is NOT one of
    the hand-curated registry slugs (and has no verified custom-model match)
    must report `routable_as: None`, not accidentally match on a substring
    or a prefix-stripped comparison."""

    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        return ApiKeyCredential(provider="openrouter", api_key="sk-or-test")

    async def _fake_list_models(client):  # noqa: ANN001
        return [
            AvailableModelEntry(
                native_model_id="mistralai/mixtral-8x22b",
                display_name="Mistral: Mixtral 8x22B",
                input_price_per_million_usd=Decimal("0.900000"),
                output_price_per_million_usd=Decimal("0.900000"),
            )
        ]

    monkeypatch.setattr(model_catalog, "get_decrypted_provider_credential", _fake_get_credential)
    monkeypatch.setattr(openrouter_provider_module, "list_models", _fake_list_models)

    entries = await list_available_models(object(), "openrouter", key_provider=object(), http_client=object())

    assert entries[0].routable_as is None


@pytest.mark.asyncio
async def test_routable_as_registry_wins_over_a_same_native_id_custom_model_match(
    monkeypatch: pytest.MonkeyPatch,
):
    """Genuinely reachable, not just a defensive invariant: `services.
    custom_models._validate_custom_model_write`'s write-time collision guard
    (`if name in MODEL_REGISTRY: raise CustomModelNameRegistryCollisionError`)
    only checks the custom model's own `name` against registry KEYS - it
    never checks `native_model_id`. Nothing stops an admin from registering
    a custom model with an unrelated `name` whose `native_model_id`
    duplicates a registry route's `native_model_id` for the same provider
    (confirmed live: `POST /v1/admin/custom-models` with
    `{"name": "my-custom-name", "provider": "openai", "native_model_id":
    "gpt-4o", ...}` is accepted - 201, no rejection). `_routable_as()`'s
    "registry always wins" rule is what actually adjudicates that case
    correctly at read time, and this test is the only thing proving it."""

    async def _fake_get_credential(session, provider, *, key_provider):  # noqa: ANN001
        return ApiKeyCredential(provider="openai", api_key="sk-test")

    async def _fake_list_models(client, credential):  # noqa: ANN001
        return [
            AvailableModelEntry(
                native_model_id="gpt-4o",
                display_name="gpt-4o",
                input_price_per_million_usd=None,
                output_price_per_million_usd=None,
            )
        ]

    async def _fake_verified_custom_model_names_by_native_id(session, provider):  # noqa: ANN001
        return {"gpt-4o": "should-never-win"}

    monkeypatch.setattr(model_catalog, "get_decrypted_provider_credential", _fake_get_credential)
    monkeypatch.setattr(openai_provider_module, "list_models", _fake_list_models)
    monkeypatch.setattr(
        model_catalog, "_verified_custom_model_names_by_native_id", _fake_verified_custom_model_names_by_native_id
    )

    entries = await list_available_models(object(), "openai", key_provider=object(), http_client=object())
    assert entries[0].routable_as == "gpt-4o"
