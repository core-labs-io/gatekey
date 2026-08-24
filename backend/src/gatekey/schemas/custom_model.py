"""Pydantic v2 request/response models for the Custom Model Registry admin
API (Custom Model Registry / Admin-Managed BYOK Models). Mirrors
`schemas/self_hosted_provider.py`'s "generous sanity bounds only, no
secret-bearing field on any response model" conventions exactly - see that
module's docstring. No secret-bearing field exists anywhere on this model at
all (a real simplification versus `SelfHostedProviderResponse`): a custom
model rides the org's existing, already-encrypted `provider_keys` row for
its `provider` rather than storing any credential of its own - see
`db.models.custom_model.CustomModel`'s module docstring.

See `gatekey/custom-model-registry-technical-design.md` section 3.3 for the
authoritative field list/bounds this module implements.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from gatekey.providers.model_registry import MODEL_REGISTRY

# Same bounds class as `schemas/self_hosted_provider.py` - generous sanity
# bounds only, not format-specific validation (that module's docstring).
_MAX_NAME_LENGTH = 200
_MAX_NATIVE_MODEL_ID_LENGTH = 256
_MAX_PRICING_SOURCE_LENGTH = 2048

# Strict subset of `providers.registry.SUPPORTED_PROVIDERS` - deliberately
# excludes `"ollama"` (self-hosted has its own registration mechanism, see
# `db.models.custom_model.CustomModel`'s module docstring) - mirrors the
# `custom_models` table's own `chk_custom_models_provider` CHECK constraint
# exactly, so a request that fails Pydantic validation here would also have
# failed the DB CHECK, just earlier and with a clearer error.
CustomModelProvider = Literal["openai", "anthropic", "vertex_ai", "openrouter"]
CustomModelCapabilityLiteral = Literal["chat", "embeddings"]


class RegistryModelEntry(BaseModel):
    """One static `MODEL_REGISTRY` entry - `name` (Gatekey's own registry
    key) paired with `provider`, so a caller can group/filter registry
    models by provider without hand-maintaining its own copy of that
    mapping. Distinct from the plain `list[str]` `registry-model-names`
    endpoint above: that one exists for the fallback-chain picker (which
    only ever needs names, never provider grouping); this one exists for
    Model Policy's provider-scoped checklist, specifically for `vertex_ai`
    (the one BYOK provider with no live-listing support - `services.
    model_catalog.CustomModelLiveListingUnsupportedError` - so its Model
    Policy checklist has no live alternative and must be sourced from this
    always-current, zero-I/O registry dump instead of a hand-typed
    frontend list)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    provider: str


class CustomModelCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    provider: CustomModelProvider
    native_model_id: str = Field(min_length=1, max_length=_MAX_NATIVE_MODEL_ID_LENGTH)
    capability: CustomModelCapabilityLiteral
    input_price_per_million_usd: Decimal = Field(gt=0)
    # `None` only for `capability == "embeddings"` - enforced by
    # `services.custom_models`'s write-time guard (#4) and, redundantly, by
    # the DB's own `chk_custom_models_capability_output_price` CHECK
    # (defense in depth, matching this codebase's established convention).
    output_price_per_million_usd: Decimal | None = Field(default=None, gt=0)
    pricing_source: str | None = Field(default=None, max_length=_MAX_PRICING_SOURCE_LENGTH)
    # Model Catalog + Cross-Provider Fallback Chains (Part B) - see
    # `gatekey/model-catalog-fallback-chains-technical-design.md` section
    # 2.4. `max_length=5` is a cheap, redundant early rejection in front of
    # `services.custom_models.CustomModelFallbackChainTooLongError` - same
    # "defense in depth" posture `input_price_per_million_usd: Field(gt=0)`
    # already has in front of `CustomModelPricingInvalidError`.
    fallback_model_names: list[str] = Field(default_factory=list, max_length=5)


class CustomModelUpdateRequest(BaseModel):
    """Every field is optional - omitted means "leave unchanged", identical
    discipline to `SelfHostedProviderUpdateRequest`.

    `output_price_per_million_usd` is the one field with the same
    None-vs-omitted ambiguity `SelfHostedProviderUpdateRequest.bearer_token`
    has (a `capability` edit from `"chat"` to `"embeddings"` must be able to
    explicitly clear a previously-required price to `null`) - callers must
    inspect `model_fields_set` (same as that field's `bearer_token_provided`
    precedent) rather than a bare `is not None` check, and pass the result
    to `services.custom_models.edit_custom_model`'s
    `output_price_per_million_usd_provided` parameter.

    `fallback_model_names` (Model Catalog + Cross-Provider Fallback Chains,
    Part B) has the identical `model_fields_set`-based provided-vs-omitted
    disambiguation - an edit must be able to explicitly clear a chain back to
    `[]`, which a bare `is not None` check can't distinguish from "omitted".
    Threaded through to `services.custom_models.edit_custom_model`'s
    `fallback_model_names_provided` parameter.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=_MAX_NAME_LENGTH)
    provider: CustomModelProvider | None = None
    native_model_id: str | None = Field(
        default=None, min_length=1, max_length=_MAX_NATIVE_MODEL_ID_LENGTH
    )
    capability: CustomModelCapabilityLiteral | None = None
    input_price_per_million_usd: Decimal | None = Field(default=None, gt=0)
    output_price_per_million_usd: Decimal | None = Field(default=None, gt=0)
    pricing_source: str | None = Field(default=None, max_length=_MAX_PRICING_SOURCE_LENGTH)
    fallback_model_names: list[str] | None = Field(default=None, max_length=5)


class CustomModelResponse(BaseModel):
    """Safe-to-return view of a registered custom model - no secret-bearing
    field exists on this model at all (see module docstring).

    `shadowed_by_registry` is NEVER a DB column and NEVER cached - it must
    be computed fresh, at response-build time, against the CURRENTLY
    RUNNING process's live `MODEL_REGISTRY` (see `is_shadowed_by_registry`
    below and the technical design doc section 2.4) - the whole point of
    this field is that it reflects the running code, not a value that could
    go stale between a deploy and the next write to this row. Because it
    has no ORM-model counterpart, `model_validate(row)` (`from_attributes`)
    alone can never populate it - callers must always construct this model
    explicitly, supplying `shadowed_by_registry` themselves.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    provider: str
    native_model_id: str
    capability: str
    input_price_per_million_usd: Decimal
    output_price_per_million_usd: Decimal | None
    pricing_source: str | None
    pricing_as_of: date
    verified: bool
    shadowed_by_registry: bool
    # Model Catalog + Cross-Provider Fallback Chains (Part B) - plain
    # passthrough, unlike `shadowed_by_registry` (no computed-field
    # complexity here; `CustomModel.fallback_model_names` maps directly).
    fallback_model_names: list[str]
    created_at: datetime
    updated_at: datetime


class AvailableModelEntry(BaseModel):
    """One entry in a provider's live "what models does it actually have"
    catalog - returned by `GET /v1/admin/custom-models/available/{provider}`
    (`services.model_catalog.list_available_models()`). See the Model
    Catalog technical design doc section 1.2 for the full "known static
    price" rationale this shape exists to support.

    `input_price_per_million_usd`/`output_price_per_million_usd` are
    non-`None` whenever the backend has ANY authoritative price for this
    entry (a live OpenRouter figure, or a `MODEL_REGISTRY`/`PRICING_TABLE`
    match for OpenAI/Anthropic) and `None` otherwise - deliberately not a
    separate "is this known" boolean; see the design doc section 1.2 for why
    that's a strictly worse contract for the frontend. This module must
    never import `providers.pricing.PRICING_TABLE` (unlike `MODEL_REGISTRY`,
    already imported above for `is_shadowed_by_registry`) - the
    `MODEL_REGISTRY`-to-`PRICING_TABLE` reverse-index join that fills these
    two fields in for OpenAI/Anthropic lives entirely in
    `services/model_catalog.py`, not here; this schema only describes the
    response shape.

    `routable_as` (Model Policy "select models to enable" picker): the
    Gatekey-facing model NAME this entry is already routable under - a
    `MODEL_REGISTRY` key, or a verified Custom Model's `name` - or `None` if
    this entry has never been registered/priced in Gatekey at all, so it
    cannot yet be added to org model policy (`PUT /v1/admin/model-policy`
    only accepts already-routable names). `None` here means "register this
    as a Custom Model first," not "unavailable" - the live provider itself
    offers it, Gatekey just doesn't have a price for it yet. Computed
    entirely in `services/model_catalog.py` (same "no PRICING_TABLE/DB
    logic in this schema module" discipline as the two price fields above).
    """

    model_config = ConfigDict(extra="forbid")

    native_model_id: str
    display_name: str
    input_price_per_million_usd: Decimal | None
    output_price_per_million_usd: Decimal | None
    routable_as: str | None = None


def is_shadowed_by_registry(name: str) -> bool:
    """Zero-I/O, plain-dict-`in`-check helper for `shadowed_by_registry`
    (technical design doc section 2.4b) - a thin, testable wrapper around
    `name in MODEL_REGISTRY` so the admin router's response builder (and
    the startup shadowing-log helper, a later task) share the exact same
    one-line definition of "shadowed" rather than two independently
    maintained copies of the same check."""
    return name in MODEL_REGISTRY
