"""Unit tests for providers/registry.py.

Phase 1 addition (Ollama/OpenRouter): explicit contents/length assertions
on `SUPPORTED_PROVIDERS` and `build_validator_registry()`'s keys, so a
future provider addition/removal is caught here rather than only via the
generic `set(...)` comparison in `test_deps.py`.
"""

from __future__ import annotations

import pytest

from gatekey.providers.ollama import OllamaValidator
from gatekey.providers.openrouter import OpenRouterValidator
from gatekey.providers.registry import (
    SUPPORTED_PROVIDERS,
    UnknownProviderError,
    build_validator_registry,
    get_validator,
)


def test_supported_providers_contents_and_length():
    assert SUPPORTED_PROVIDERS == ("openai", "anthropic", "vertex_ai", "ollama", "openrouter")
    assert len(SUPPORTED_PROVIDERS) == 5


def test_build_validator_registry_covers_all_supported_providers():
    registry = build_validator_registry(timeout_seconds=3.0)
    assert set(registry.keys()) == set(SUPPORTED_PROVIDERS)
    assert isinstance(registry["ollama"], OllamaValidator)
    assert isinstance(registry["openrouter"], OpenRouterValidator)


def test_get_validator_unknown_provider_raises():
    registry = build_validator_registry()
    with pytest.raises(UnknownProviderError):
        get_validator("not-a-real-provider", registry)
