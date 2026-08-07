"""Pydantic v2 request/response models for the self-hosted-provider admin
API (Phase 5 - Differentiators, 5.5). Mirrors `schemas/provider_key.py`'s
"minimal sanity bounds only, no secret-bearing field on any response model"
conventions - see that module's docstring.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

# Same bounds as `schemas/provider_key.py`'s `OllamaKeyRequest` - see that
# module's docstring for why these are generous sanity bounds only, not
# format-specific validation.
_MAX_BASE_URL_LENGTH = 2048
_MAX_BEARER_TOKEN_LENGTH = 4096
_MAX_NAME_LENGTH = 200
_MAX_MODEL_ID_LENGTH = 256
_MAX_MODELS_PER_PROVIDER = 100


class SelfHostedProviderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=_MAX_NAME_LENGTH)
    base_url: str = Field(min_length=1, max_length=_MAX_BASE_URL_LENGTH)
    bearer_token: str | None = Field(default=None, max_length=_MAX_BEARER_TOKEN_LENGTH)
    cost_basis_per_gpu_hour: Decimal = Field(gt=0)
    models: list[str] = Field(min_length=1, max_length=_MAX_MODELS_PER_PROVIDER)


class SelfHostedProviderUpdateRequest(BaseModel):
    """Every field is optional - omitted means "leave unchanged". See
    `services.self_hosted_providers.edit_self_hosted_provider`'s
    `bearer_token_provided` docstring for why `bearer_token` needs its own
    explicit presence flag (`model_fields_set`) rather than a bare
    `is not None` check at the call site."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=_MAX_NAME_LENGTH)
    base_url: str | None = Field(default=None, min_length=1, max_length=_MAX_BASE_URL_LENGTH)
    bearer_token: str | None = Field(default=None, max_length=_MAX_BEARER_TOKEN_LENGTH)
    cost_basis_per_gpu_hour: Decimal | None = Field(default=None, gt=0)
    models: list[str] | None = Field(default=None, min_length=1, max_length=_MAX_MODELS_PER_PROVIDER)


class SelfHostedProviderResponse(BaseModel):
    """Safe-to-return view of a registered self-hosted provider - no
    ciphertext/nonce/auth_tag/plaintext `bearer_token` field exists on this
    model, matching `ProviderKeyResponse`'s discipline."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    base_url: str
    cost_basis_per_gpu_hour: Decimal
    verified: bool
    models: list[str]
    created_at: datetime
    updated_at: datetime
