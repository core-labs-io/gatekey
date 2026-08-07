"""Maps provider identifiers to their `ProviderValidator` implementation.

Provider identifiers match the `ProviderKey.provider` enum values agreed
with database-admin: `"openai" | "anthropic" | "vertex_ai" | "ollama" |
"openrouter"`.
"""

from __future__ import annotations

from gatekey.providers.anthropic import AnthropicValidator
from gatekey.providers.base import ProviderValidator
from gatekey.providers.ollama import OllamaValidator
from gatekey.providers.openai import OpenAIValidator
from gatekey.providers.openrouter import OpenRouterValidator
from gatekey.providers.vertex_ai import VertexAIValidator

SUPPORTED_PROVIDERS = ("openai", "anthropic", "vertex_ai", "ollama", "openrouter")


class UnknownProviderError(ValueError):
    def __init__(self, provider: str) -> None:
        super().__init__(f"Unknown provider: {provider!r}")
        self.provider = provider


def build_validator_registry(timeout_seconds: float = 8.0) -> dict[str, ProviderValidator]:
    """Construct a fresh provider -> validator mapping.

    A factory function (rather than a module-level singleton) so the
    configured validation timeout - which comes from `Settings` - can be
    threaded through at app-startup time.
    """
    return {
        "openai": OpenAIValidator(timeout_seconds=timeout_seconds),
        "anthropic": AnthropicValidator(timeout_seconds=timeout_seconds),
        "vertex_ai": VertexAIValidator(timeout_seconds=timeout_seconds),
        "ollama": OllamaValidator(timeout_seconds=timeout_seconds),
        "openrouter": OpenRouterValidator(timeout_seconds=timeout_seconds),
    }


def get_validator(
    provider: str, registry: dict[str, ProviderValidator]
) -> ProviderValidator:
    """Look up a validator by provider name, raising `UnknownProviderError` if absent."""
    try:
        return registry[provider]
    except KeyError:
        raise UnknownProviderError(provider) from None
