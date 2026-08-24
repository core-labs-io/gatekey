"""Static registry mapping gateway-facing model names to provider routes.

Phase 1.2 (BD-1) scope: this is the pilot model list agreed in the design
doc (section 2) for `/v1/chat/completions`, `/v1/completions`, and
`/v1/embeddings`. It is intentionally a small, hand-curated allowlist of
models Gatekey has validated end-to-end - not a mirror of every model each
provider exposes.

Single lookup point
--------------------
`resolve_model()` is the *only* sanctioned way to look up a model. No other
module should do inline `MODEL_REGISTRY[...]` or `MODEL_REGISTRY.get(...)`
access - Phase 1.3 (a later phase) needs to wrap this exact call site with
an org-level allow/denylist check, and centralizing the lookup here now
means that later change touches one function instead of every call site
that reads the registry.

Pure module
-----------
Zero I/O, zero DB access. `MODEL_REGISTRY` is a plain in-memory dict built
at import time.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass


class ModelCapability(str, enum.Enum):
    """What a model route can be used for."""

    CHAT = "chat"
    EMBEDDINGS = "embeddings"


@dataclass(frozen=True)
class ModelRoute:
    """Where a gateway-facing model name routes to.

    `provider` values match `providers.registry.SUPPORTED_PROVIDERS` /
    `db.models.provider_key.ProviderName` exactly (`"openai"`,
    `"anthropic"`, `"vertex_ai"`, `"ollama"`, `"openrouter"`), PLUS the
    literal string `"self_hosted"` (Phase 5 - Differentiators, 5.5) - see
    `self_hosted_provider_id` below. `"self_hosted"` is deliberately NOT
    added to `providers.registry.SUPPORTED_PROVIDERS`/`ProviderName` - it
    has no `provider_keys` row (a wholly separate table,
    `self_hosted_providers`, backs it) and never participates in that
    registry's validator-based BYOK-key-CRUD surface.
    """

    provider: str
    capability: ModelCapability
    native_model_id: str
    # Phase 5 (5.5): populated ONLY when `provider == "self_hosted"` - the
    # owning `SelfHostedProvider.id`, threaded through to the credential
    # fetch (`api.v1.gateway.common.call_self_hosted_provider`) and the
    # `usage_logs.self_hosted_provider_id` column. `None` for every
    # BYOK-provider route (the overwhelming majority) - see
    # `api.v1.gateway.common.resolve_route`'s self-hosted fallback.
    self_hosted_provider_id: uuid.UUID | None = None
    # Custom Model Registry (Admin-Managed BYOK Models): populated ONLY when
    # this route was produced by `CustomModelRouteCache`'s fallback in
    # `resolve_route()` - the owning `CustomModel.id`. Unlike self-hosted,
    # a custom model's `provider` carries the REAL BYOK provider value
    # (`"openai"`/etc.), not a synthetic sentinel - `route.provider` alone
    # therefore cannot distinguish "a static route to this provider" from
    # "a custom-model route to this provider" (see `services.custom_models`
    # module docstring / the Custom Model Registry technical design doc
    # section 2.2). `custom_model_id` is the ONE field every downstream
    # cost/audit branch must test for that distinction - never
    # `route.provider`. `None` for every static route and every self-hosted
    # route. A route can never have both `self_hosted_provider_id` and
    # `custom_model_id` set (the two caches' key sets are disjoint by
    # construction, via the bidirectional collision guards in
    # `services.custom_models`/`services.self_hosted_providers`).
    custom_model_id: uuid.UUID | None = None


class UnknownModelError(ValueError):
    """Raised by `resolve_model()` when the requested model isn't registered.

    The requested model name is included in the message - this is caller
    input, not secret material, so it's safe to surface to an API caller
    and to log.
    """

    def __init__(self, model: str) -> None:
        super().__init__(f"Unknown model: {model!r}")
        self.model = model


# Pilot model list (design doc section 2). Keys are the gateway-facing model
# names accepted in request bodies (the OpenAI-compatible `model` field);
# `native_model_id` is what gets passed to the underlying provider's API.
#
# Verified current/active (not deprecated) as of 2026-07-28 - see
# `providers/pricing.py`'s module docstring for the full sourcing note. The
# original pilot list (claude-3-5-sonnet-20241022, claude-3-5-haiku-20241022,
# claude-3-opus-20240229, gemini-1.5-pro, gemini-1.5-flash, text-embedding-004)
# has since been fully retired by both providers - Anthropic's oldest of
# those retired October 2025, Google's Gemini 1.5 family returns a bare 404
# on Vertex AI as of this date - and has been replaced below with each
# provider's current equivalent tier (fastest/cheapest, mid, and
# highest-capability chat model per provider, plus one current embeddings
# model), preserving the registry's own stated intent of a small, validated
# allowlist rather than a full model mirror.
MODEL_REGISTRY: dict[str, ModelRoute] = {
    # OpenAI - chat
    "gpt-4o": ModelRoute(
        provider="openai", capability=ModelCapability.CHAT, native_model_id="gpt-4o"
    ),
    "gpt-4o-mini": ModelRoute(
        provider="openai", capability=ModelCapability.CHAT, native_model_id="gpt-4o-mini"
    ),
    # OpenAI - embeddings
    "text-embedding-3-small": ModelRoute(
        provider="openai",
        capability=ModelCapability.EMBEDDINGS,
        native_model_id="text-embedding-3-small",
    ),
    "text-embedding-3-large": ModelRoute(
        provider="openai",
        capability=ModelCapability.EMBEDDINGS,
        native_model_id="text-embedding-3-large",
    ),
    # Anthropic - chat only (Anthropic has no embeddings API). Dateless IDs
    # here (`claude-sonnet-5`, `claude-opus-5`) are themselves pinned
    # snapshots per Anthropic's 4.6+ generation versioning scheme, not
    # evergreen aliases - see providers/pricing.py for the pricing source.
    "claude-haiku-4-5-20251001": ModelRoute(
        provider="anthropic",
        capability=ModelCapability.CHAT,
        native_model_id="claude-haiku-4-5-20251001",
    ),
    "claude-sonnet-5": ModelRoute(
        provider="anthropic",
        capability=ModelCapability.CHAT,
        native_model_id="claude-sonnet-5",
    ),
    "claude-opus-5": ModelRoute(
        provider="anthropic",
        capability=ModelCapability.CHAT,
        native_model_id="claude-opus-5",
    ),
    # Vertex AI - chat (GA, not preview/experimental)
    "gemini-2.5-flash": ModelRoute(
        provider="vertex_ai", capability=ModelCapability.CHAT, native_model_id="gemini-2.5-flash"
    ),
    "gemini-2.5-pro": ModelRoute(
        provider="vertex_ai", capability=ModelCapability.CHAT, native_model_id="gemini-2.5-pro"
    ),
    # Vertex AI - embeddings (gemini-embedding-001 is Google's current
    # recommended embeddings model, superseding text-embedding-004/005)
    "gemini-embedding-001": ModelRoute(
        provider="vertex_ai",
        capability=ModelCapability.EMBEDDINGS,
        native_model_id="gemini-embedding-001",
    ),
    # --- Ollama - chat only (self-hosted; Ollama's OpenAI-compat layer has
    # no embeddings endpoint). Example tags only, functional only if the
    # admin's Ollama instance has actually pulled that exact model tag - an
    # unpulled model fails at Ollama, surfaced as ProviderCallError (a real,
    # expected failure mode, not a Gatekey bug). Gateway-facing keys are
    # `ollama/`-prefixed per ADR-1 (design doc section 8) - native_model_id
    # stays the bare tag actually sent to Ollama.
    # NOT built this pass: dynamic per-org model discovery
    # (GET {base_url}/api/tags) - deliberate, explicit follow-up (see
    # design doc section 10, forward-looking flags).
    "ollama/llama3.1": ModelRoute(
        provider="ollama", capability=ModelCapability.CHAT, native_model_id="llama3.1"
    ),
    "ollama/mistral": ModelRoute(
        provider="ollama", capability=ModelCapability.CHAT, native_model_id="mistral"
    ),
    "ollama/qwen2.5": ModelRoute(
        provider="ollama", capability=ModelCapability.CHAT, native_model_id="qwen2.5"
    ),
    # --- OpenRouter - chat only, small curated allowlist (not a mirror of
    # OpenRouter's full multi-hundred-model catalog, matching this
    # registry's existing stated philosophy). native_model_id uses
    # OpenRouter's own `vendor/model` slug convention verbatim; only the
    # gateway-facing key gets the `openrouter/` prefix (ADR-1).
    "openrouter/openai/gpt-4o-mini": ModelRoute(
        provider="openrouter",
        capability=ModelCapability.CHAT,
        native_model_id="openai/gpt-4o-mini",
    ),
    # Meta's reasoning/agentic model, 1M-token context - verified live on
    # openrouter.ai/meta/muse-spark-1.2 (real, not a typo) before adding.
    "openrouter/meta/muse-spark-1.2": ModelRoute(
        provider="openrouter",
        capability=ModelCapability.CHAT,
        native_model_id="meta/muse-spark-1.2",
    ),
}


def resolve_model(model: str) -> ModelRoute:
    """Resolve a gateway-facing model name to its `ModelRoute`.

    Exact-match dict lookup - no fuzzy/prefix matching. Raises
    `UnknownModelError` if `model` isn't in `MODEL_REGISTRY`. See the
    module docstring: this is the single sanctioned lookup point.
    """
    try:
        return MODEL_REGISTRY[model]
    except KeyError:
        raise UnknownModelError(model) from None
