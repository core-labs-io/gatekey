"""Pydantic v2 request/response models for personal API keys (Phase 2, BD-6).

Mirrors `schemas/service_account_key.py`'s discipline exactly:
`PersonalApiKeyCreateResponse` is the ONLY schema with a `secret` field
(returned exactly once by create/regenerate, never persisted, never returned
elsewhere); `PersonalApiKeyResponse` (list/get) structurally cannot leak
secret material because the fields don't exist on it.

The endpoints consuming these land in a later task (BD-16) - this file only
locks the shapes per the API contract (design doc 5.6).
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# Same sanity bounds as `schemas/service_account_key.py`.
_MIN_NAME_LENGTH = 1
_MAX_NAME_LENGTH = 256


class PersonalApiKeyCreateRequest(BaseModel):
    """Request body for `POST /v1/keys` (self-serve create).

    `team_id` is always required in the body - the frontend auto-selects it
    when the user belongs to exactly one team (A1), but the server never
    infers it (design doc 5.6).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=_MIN_NAME_LENGTH, max_length=_MAX_NAME_LENGTH)
    team_id: UUID
    # AwareDatetime (security review L-3): a naive timestamp would blow up
    # with a TypeError (500) when compared against timezone-aware DB/`now()`
    # values downstream - reject it as a clean 422 at the trust boundary.
    expires_at: AwareDatetime | None = None

    @field_validator("name")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank.")
        return value


class PersonalApiKeyCreateResponse(BaseModel):
    """Response body for create/regenerate.

    The ONLY schema with a `secret` field - the plaintext `gk_pk_...`
    credential, shown exactly this one time (AC5.2/5.3).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    owner_user_id: UUID
    team_id: UUID
    key_prefix: str
    secret: str
    expires_at: datetime | None
    created_at: datetime


class PersonalApiKeyResponse(BaseModel):
    """Safe-to-return view of a personal key (list/get) - no `secret`/
    `secret_hash` field exists on this model."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    owner_user_id: UUID
    created_by_user_id: UUID
    team_id: UUID
    key_prefix: str
    expires_at: datetime | None
    created_at: datetime
    revoked_at: datetime | None
    active: bool = True

    @model_validator(mode="after")
    def _compute_active(self) -> "PersonalApiKeyResponse":
        expired = self.expires_at is not None and self.expires_at <= datetime.now(timezone.utc)
        self.active = self.revoked_at is None and not expired
        return self
