"""Pydantic v2 request/response models for the service-account-key admin API.

`ServiceAccountKeyCreateResponse` is the *only* schema in this module (or
anywhere else) with a `secret` field - it is returned exactly once, by the
create endpoint, and never again. `ServiceAccountKeyResponse` (used by
list/get) is intentionally narrow: it has no field that could ever hold
`secret`/`secret_hash`, so there is no way for a future change to this file
to accidentally leak secret material by adding an `exclude=...` somewhere
and forgetting it - the fields simply don't exist on the model. This
mirrors the same rationale as `ProviderKeyResponse` in
`schemas/provider_key.py`.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Reasonable sanity bounds only, not format-specific - mirrors
# `schemas/provider_key.py`'s `OpenAIKeyRequest` pattern.
_MIN_NAME_LENGTH = 1
_MAX_NAME_LENGTH = 256


class ServiceAccountKeyCreateRequest(BaseModel):
    """Request body for `POST /v1/admin/service-accounts`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=_MIN_NAME_LENGTH, max_length=_MAX_NAME_LENGTH)
    # Phase 1.4 (Budget - Basic): the budget-owning cost-center this key
    # charges against. Required - every key must be attributed to a user.
    user_id: UUID
    # Phase 2 (design doc section 1.7 / security review H-1): required for
    # every NEW key - the API contract, not a column constraint, is what
    # closes the "new keys require team_id" gap (the column stays nullable
    # for pre-Phase-2 legacy rows). The target user must hold a
    # TeamMembership on this team (validated at create time).
    team_id: UUID

    @field_validator("name")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank.")
        return value


class ServiceAccountKeyCreateResponse(BaseModel):
    """Response body for `POST /v1/admin/service-accounts`.

    The ONLY schema with a `secret` field. `secret` holds the plaintext
    credential (`gk_sk_...`) and is shown to the caller exactly this one
    time - it is never persisted and never returned by any other endpoint
    (see `ServiceAccountKeyResponse`, used by list/get).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    user_id: UUID
    team_id: UUID | None
    key_prefix: str
    secret: str
    created_at: datetime


class ServiceAccountKeyResponse(BaseModel):
    """Safe-to-return view of a service-account key, used by list/get.

    No `secret`/`secret_hash` field exists on this model - see module
    docstring.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    user_id: UUID
    # None = pre-Phase-2 legacy row (flat User-budget path).
    team_id: UUID | None
    key_prefix: str
    created_at: datetime
    revoked_at: datetime | None
    active: bool = True

    @model_validator(mode="after")
    def _compute_active(self) -> "ServiceAccountKeyResponse":
        self.active = self.revoked_at is None
        return self
