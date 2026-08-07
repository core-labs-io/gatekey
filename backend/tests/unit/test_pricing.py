"""Unit tests for providers/pricing.py (Phase 1.4 - Budget Basic, AC-3)."""

from __future__ import annotations

from decimal import Decimal

from gatekey.providers.model_registry import MODEL_REGISTRY, ModelCapability
from gatekey.providers.pricing import PRICING_TABLE, PricingEntryMissingError, get_pricing_entry


def test_pricing_table_covers_every_registry_model():
    assert PRICING_TABLE.keys() == MODEL_REGISTRY.keys()


def test_pricing_shape_matches_capability():
    for model, route in MODEL_REGISTRY.items():
        entry = PRICING_TABLE[model]
        assert isinstance(entry.input_price_per_million_usd, Decimal)
        if route.capability is ModelCapability.CHAT:
            assert entry.output_price_per_million_usd is not None
            assert isinstance(entry.output_price_per_million_usd, Decimal)
        else:
            assert entry.output_price_per_million_usd is None


def test_get_pricing_entry_returns_known_model():
    entry = get_pricing_entry("gpt-4o-mini")
    assert entry.input_price_per_million_usd == Decimal("0.15")


def test_get_pricing_entry_raises_for_unknown_model():
    import pytest

    with pytest.raises(PricingEntryMissingError):
        get_pricing_entry("not-a-real-model")


def test_every_entry_has_as_of_and_source():
    for entry in PRICING_TABLE.values():
        assert entry.as_of
        assert entry.source


def test_ollama_entries_price_at_exactly_zero():
    """AC-E3-1/AC-E3-4: every Ollama entry is present at exactly
    $0.00/$0.00 - not None, not missing."""
    ollama_models = [model for model, route in MODEL_REGISTRY.items() if route.provider == "ollama"]
    assert ollama_models, "expected at least one ollama MODEL_REGISTRY entry"
    for model in ollama_models:
        entry = PRICING_TABLE[model]
        assert entry.input_price_per_million_usd == Decimal("0.00")
        assert entry.output_price_per_million_usd == Decimal("0.00")


def test_ollama_entries_source_is_explanatory_not_url_shaped():
    """AC-E3-2: Ollama's source is an explanatory string, not a provider
    price-page URL (since none exists for a self-hosted target)."""
    ollama_models = [model for model, route in MODEL_REGISTRY.items() if route.provider == "ollama"]
    for model in ollama_models:
        source = PRICING_TABLE[model].source
        assert not source.startswith("http://") and not source.startswith("https://")
        assert "no per-token provider charge" in source


def test_openrouter_entries_source_is_url_shaped():
    """US-G7: OpenRouter entries carry a non-empty, URL-shaped `source`
    string, unlike Ollama's explanatory-string source."""
    openrouter_models = [
        model for model, route in MODEL_REGISTRY.items() if route.provider == "openrouter"
    ]
    assert openrouter_models, "expected at least one openrouter MODEL_REGISTRY entry"
    for model in openrouter_models:
        source = PRICING_TABLE[model].source
        assert source.startswith("https://") or source.startswith("http://")
