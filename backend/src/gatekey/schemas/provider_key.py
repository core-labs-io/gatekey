"""Pydantic v2 request/response models for the provider-key admin API.

Request bodies deliberately apply only minimal sanity bounds (non-empty,
reasonable length) rather than provider-specific format validation - key
formats vary across providers and change over time; the real check is the
live `validate()` call in `providers/*`, not shape-sniffing here.

`ProviderKeyResponse` is intentionally narrow: it has no field that could
ever hold ciphertext/nonce/auth_tag/plaintext, so there is no way for a
future change to this file to accidentally leak secret material by adding
an `exclude=...` somewhere and forgetting it - the fields simply don't
exist on the model.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Reasonable sanity bounds only - see module docstring. Not provider-format-specific.
_MIN_API_KEY_LENGTH = 1
_MAX_API_KEY_LENGTH = 4096
_MAX_PROJECT_ID_LENGTH = 256
_MAX_LOCATION_LENGTH = 128
_MAX_BASE_URL_LENGTH = 2048  # generous bound for a scheme+host[:port][/path] string

# Phase 4 (Reliability & Cost Efficiency, multi-key/failover - AC4.1.1/
# AC4.1.2): every `PUT .../key` request schema below carries an optional
# `label` field so an admin can add a genuine SECOND key for a provider
# (`services.provider_keys.add_or_replace_key` already upserts by
# `(org_id, provider, label)`, not `(org_id, provider)` - see that
# function's docstring). Defaults to `"Default"` - the exact same value
# migration `0023` backfilled onto every pre-existing row - so every caller
# that never sets `label` (the overwhelming majority, unchanged from before
# this field existed) keeps upserting the same single row it always has.
_MAX_LABEL_LENGTH = 200
_DEFAULT_KEY_LABEL = "Default"


class OpenAIKeyRequest(BaseModel):
    """Request body for `PUT /v1/admin/providers/openai/key`."""

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=_MIN_API_KEY_LENGTH, max_length=_MAX_API_KEY_LENGTH)
    label: str = Field(default=_DEFAULT_KEY_LABEL, max_length=_MAX_LABEL_LENGTH)

    @field_validator("api_key")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("api_key must not be blank.")
        return value

    @field_validator("label")
    @classmethod
    def _non_blank_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("label must not be blank.")
        return value


class AnthropicKeyRequest(BaseModel):
    """Request body for `PUT /v1/admin/providers/anthropic/key`."""

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=_MIN_API_KEY_LENGTH, max_length=_MAX_API_KEY_LENGTH)
    label: str = Field(default=_DEFAULT_KEY_LABEL, max_length=_MAX_LABEL_LENGTH)

    @field_validator("api_key")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("api_key must not be blank.")
        return value

    @field_validator("label")
    @classmethod
    def _non_blank_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("label must not be blank.")
        return value


class VertexAIKeyRequest(BaseModel):
    """Request body for `PUT /v1/admin/providers/vertex_ai/key`."""

    model_config = ConfigDict(extra="forbid")

    service_account_json: dict[str, Any]
    project_id: str = Field(min_length=1, max_length=_MAX_PROJECT_ID_LENGTH)
    location: str = Field(min_length=1, max_length=_MAX_LOCATION_LENGTH)
    label: str = Field(default=_DEFAULT_KEY_LABEL, max_length=_MAX_LABEL_LENGTH)

    @field_validator("service_account_json")
    @classmethod
    def _non_empty(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("service_account_json must not be empty.")
        return value

    @field_validator("project_id", "location", "label")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank.")
        return value


_MAX_TRUSTED_PROVIDER_SLUGS = 20
_MAX_PROVIDER_SLUG_LENGTH = 128


class OpenRouterKeyRequest(BaseModel):
    """Request body for `PUT /v1/admin/providers/openrouter/key`. Same
    `api_key`/`label` shape as `OpenAIKeyRequest` (AC-D1-1), plus two
    optional residency fields.

    `trusted_provider_slugs`/`trusted_provider_region`: OpenRouter is a
    multi-provider aggregator with no single knowable hosting region (see
    `services.residency.resolve_model_region`'s module-level rationale) - by
    default a request through it always resolves to an unknown region, which
    an active residency rule blocks. These two fields let an admin make an
    ENFORCED (not merely claimed) exception: OpenRouter's own `provider:
    {"only": [...]}` request field (confirmed live, see `providers/
    openrouter.py::create_chat_completion`) restricts which underlying
    provider(s) OpenRouter is allowed to route to - if the admin names a set
    of provider slugs they personally vouch for as being hosted in
    `trusted_provider_region`, every outbound OpenRouter call is
    UNCONDITIONALLY constrained to only that set (never just on the calls
    where a residency rule happens to be active - see `providers/
    openrouter.py`), which is what makes the resulting region claim true by
    construction rather than an assumption. Both fields are optional and
    mutually required (set together or not at all) - a region with no
    provider restriction, or a restriction with no declared region, would
    not actually mean anything."""

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(min_length=_MIN_API_KEY_LENGTH, max_length=_MAX_API_KEY_LENGTH)
    label: str = Field(default=_DEFAULT_KEY_LABEL, max_length=_MAX_LABEL_LENGTH)
    # Underlying-provider slugs (OpenRouter's own vocabulary, e.g. "openai",
    # "anthropic", "fireworks") the admin vouches for as hosted in
    # `trusted_provider_region` - see class docstring. Not validated against
    # a known-slugs list (OpenRouter's own catalog is the only source of
    # truth for what's real, and it changes over time - same "minimal sanity
    # bounds only" philosophy this whole module already follows).
    trusted_provider_slugs: list[str] = Field(default_factory=list, max_length=_MAX_TRUSTED_PROVIDER_SLUGS)
    trusted_provider_region: str | None = Field(default=None)

    @field_validator("api_key")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("api_key must not be blank.")
        return value

    @field_validator("label")
    @classmethod
    def _non_blank_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("label must not be blank.")
        return value

    @field_validator("trusted_provider_slugs")
    @classmethod
    def _non_blank_slugs(cls, value: list[str]) -> list[str]:
        cleaned = [slug.strip() for slug in value]
        if any(not slug for slug in cleaned):
            raise ValueError("trusted_provider_slugs entries must not be blank.")
        if any(len(slug) > _MAX_PROVIDER_SLUG_LENGTH for slug in cleaned):
            raise ValueError(f"trusted_provider_slugs entries must be at most {_MAX_PROVIDER_SLUG_LENGTH} characters.")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("trusted_provider_slugs must not contain duplicates.")
        return cleaned

    @field_validator("trusted_provider_region")
    @classmethod
    def _valid_region(cls, value: str | None) -> str | None:
        # Local import - see `OllamaKeyRequest._valid_region`'s identical
        # rationale for why this one field imports `services.residency`
        # locally rather than at module level.
        from gatekey.services.residency import SUPPORTED_REGIONS

        if value is not None and value not in SUPPORTED_REGIONS:
            raise ValueError(f"trusted_provider_region must be one of: {', '.join(sorted(SUPPORTED_REGIONS))}.")
        return value

    @model_validator(mode="after")
    def _slugs_and_region_together(self) -> "OpenRouterKeyRequest":
        if bool(self.trusted_provider_slugs) != bool(self.trusted_provider_region):
            raise ValueError(
                "trusted_provider_slugs and trusted_provider_region must be set together, or not at all."
            )
        return self


class OllamaKeyRequest(BaseModel):
    """Request body for `PUT /v1/admin/providers/ollama/key`."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=1, max_length=_MAX_BASE_URL_LENGTH)
    bearer_token: str | None = Field(default=None, max_length=_MAX_API_KEY_LENGTH)
    # Phase 3 (design doc section 1.13, ratified #5): Ollama is self-hosted,
    # so there is no provider-side region to read - this is the one
    # org-admin-settable region field `services.residency.resolve_model_region`
    # reads back out of the same non-secret `key_metadata` column. `None`
    # (the default - never set) means "unknown", which a residency rule
    # treats as a violation by default (hard-block-by-default's own intent).
    region: str | None = Field(default=None)
    label: str = Field(default=_DEFAULT_KEY_LABEL, max_length=_MAX_LABEL_LENGTH)

    @field_validator("label")
    @classmethod
    def _non_blank_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("label must not be blank.")
        return value

    @field_validator("base_url")
    @classmethod
    def _valid_base_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("base_url must not be blank.")
        # AC-D2-2: minimal scheme sanity check only, generic to any
        # admin-configured endpoint URL - not Ollama-specific format
        # validation. Consistent with this module's stated "minimal sanity
        # bounds, not provider-specific format validation" philosophy.
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("base_url must start with http:// or https://.")
        return value

    @field_validator("region")
    @classmethod
    def _valid_region(cls, value: str | None) -> str | None:
        # Local import - `services.residency` is Phase 3-specific and this
        # is the only field in this module that needs it.
        from gatekey.services.residency import SUPPORTED_REGIONS

        if value is not None and value not in SUPPORTED_REGIONS:
            raise ValueError(f"region must be one of: {', '.join(sorted(SUPPORTED_REGIONS))}.")
        return value

    @field_validator("bearer_token")
    @classmethod
    def _normalize_blank_to_none(cls, value: str | None) -> str | None:
        # AC-D2-1: an empty/whitespace-only bearer_token normalizes to
        # None - exactly one representation of "not configured", never a
        # distinct "empty but present" state.
        if value is not None and not value.strip():
            return None
        return value


class ProviderKeyResponse(BaseModel):
    """Safe-to-return view of a configured provider key.

    No ciphertext/nonce/auth_tag/plaintext field exists on this model - see
    module docstring.
    """

    model_config = ConfigDict(from_attributes=True)

    provider: str
    configured: Literal[True] = True
    # Nullable at the DB level (see `ProviderKey.validated_at`), but every
    # row this service writes has already passed live validation before
    # insert, so it is always populated on any row this API can return.
    validated_at: datetime
    created_at: datetime
    updated_at: datetime
    metadata: dict[str, Any] = Field(validation_alias="key_metadata")

    @field_validator("provider", mode="before")
    @classmethod
    def _coerce_provider_to_str(cls, value: Any) -> Any:
        # `ProviderKey.provider` comes back from SQLAlchemy as a
        # `ProviderName` (str, Enum) instance. `.value` guarantees the
        # plain string ("openai") regardless of Enum.__str__ quirks.
        if isinstance(value, Enum):
            return value.value
        return value


class ProviderKeyListItemResponse(BaseModel):
    """Safe-to-return view of one individual `ProviderKey` ROW (as opposed
    to `ProviderKeyResponse`'s one-per-provider aggregate view) - backs
    `GET /v1/admin/provider-keys` (Phase 4, AC4.1.7).

    Same "no field that could ever hold ciphertext/nonce/auth_tag/
    plaintext" discipline as `ProviderKeyResponse` above - this model has no
    such field, so there is no way to leak secret material by adding an
    `exclude=...` somewhere and forgetting it.

    Hardening pass item 3: `failover_enabled`/`failover_target_id` added so
    the admin console can show a key's CURRENT failover configuration on a
    plain page load/list refresh, not only immediately after a `PUT
    .../failover-config` call (previously the only endpoint that ever
    returned these two fields, meaning the console lost visibility across a
    reload). Both are plain, non-secret routing/config columns already
    present on `db.models.provider_key.ProviderKey` (the same row this
    response is validated from) - `failover_enabled` mirrors that column's
    own `server_default=false`/`nullable=False`, so it is never `None`;
    `failover_target_id` stays nullable, matching the column.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    provider: str
    label: str
    is_primary: bool
    backup_group_id: uuid.UUID | None
    health_status: str
    last_health_check: datetime | None
    last_error: str | None
    availability_24h: float | None
    failover_enabled: bool = False
    failover_target_id: uuid.UUID | None = None

    @field_validator("provider", mode="before")
    @classmethod
    def _coerce_provider_to_str(cls, value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        return value
