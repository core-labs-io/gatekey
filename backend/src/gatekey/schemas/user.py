"""Pydantic v2 request/response models for the `User` (budget cost-center)
admin API (Phase 1.4 / 1.6).

`User` is not an authentication principal (see `db/models/user.py`) - these
schemas carry no password/session field. `UserResponse.org_role` (Phase 2)
is the one role field: it is read-only here and mutated exclusively via
`PATCH /v1/admin/users/{id}/org-role`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MIN_NAME_LENGTH = 1
_MAX_NAME_LENGTH = 256


class UserCreateRequest(BaseModel):
    """Request body for `POST /v1/admin/users`."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=_MIN_NAME_LENGTH, max_length=_MAX_NAME_LENGTH)
    budget_usd: Decimal | None = None

    @field_validator("name")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank.")
        return value

    @field_validator("budget_usd")
    @classmethod
    def _non_negative(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value < 0:
            raise ValueError("budget_usd must be non-negative.")
        return value


class UserUpdateRequest(BaseModel):
    """`PATCH /v1/admin/users/{id}` body.

    Uses `model_fields_set`/`exclude_unset` (see `api/v1/admin/users.py`) to
    distinguish an omitted `budget_usd` key (leave unchanged) from an
    explicit `budget_usd: null` (clear to unmetered) - the standard,
    correct FastAPI/Pydantic pattern for this exact problem.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=_MIN_NAME_LENGTH, max_length=_MAX_NAME_LENGTH)
    budget_usd: Decimal | None = None

    @field_validator("name")
    @classmethod
    def _non_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("name must not be blank.")
        return value

    @field_validator("budget_usd")
    @classmethod
    def _non_negative(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value < 0:
            raise ValueError("budget_usd must be non-negative.")
        return value


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    budget_usd: Decimal | None
    current_spend_usd: Decimal
    org_role: Literal["org_admin", "auditor"] | None = None
    created_at: datetime
    updated_at: datetime
