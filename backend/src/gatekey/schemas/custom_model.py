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
    created_at: datetime
    updated_at: datetime


def is_shadowed_by_registry(name: str) -> bool:
    """Zero-I/O, plain-dict-`in`-check helper for `shadowed_by_registry`
    (technical design doc section 2.4b) - a thin, testable wrapper around
    `name in MODEL_REGISTRY` so the admin router's response builder (and
    the startup shadowing-log helper, a later task) share the exact same
    one-line definition of "shadowed" rather than two independently
    maintained copies of the same check."""
    return name in MODEL_REGISTRY
