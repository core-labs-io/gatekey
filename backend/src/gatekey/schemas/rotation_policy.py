"""Pydantic v2 request/response models for the rotation-policy admin API
(Phase 3, BD-15). See `docs/design/phase-3-security-compliance-design.md`
section 9.6.

`mode` is never a client-writable field on any of these - AC7.1 (locked):
`service_account` scope is always `"automatic"`, `provider_key` scope is
always `"manual_guided"`. It is server-determined and appears read-only on
responses.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

RotationModeLiteral = Literal["automatic", "manual_guided"]

# UI default (AC7.4) - never long/multi-day by default; upper bound is a
# sanity guard, not a spec'd number.
_MAX_OVERLAP_BUFFER_MINUTES = 24 * 60


def _coerce_enum(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


class RotationPolicyPutRequest(BaseModel):
    """Request body shared by the org-default, per-service-account-key, and
    per-provider-key rotation-policy PUT endpoints. `interval_days=None`
    with `enabled=True` means "inherit the org default's interval" for the
    per-key/per-provider-key endpoints (AC7.1: "any key can override it");
    it is a validation error for the org-default endpoint itself (there is
    nothing further up the chain to inherit from)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval_days: int | None = Field(default=None, ge=1)
    rotate_at_local_time: time | None = None
    overlap_buffer_minutes: int = Field(default=5, ge=1, le=_MAX_OVERLAP_BUFFER_MINUTES)


class RotationPolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    interval_days: int | None
    rotate_at_local_time: time | None
    overlap_buffer_minutes: int
    next_rotation_at: datetime | None
    last_rotated_at: datetime | None
    mode: RotationModeLiteral

    @field_validator("mode", mode="before")
    @classmethod
    def _coerce_mode(cls, value: Any) -> Any:
        return _coerce_enum(value)


class RotateNowResponse(BaseModel):
    """Response for `POST /v1/admin/keys/{id}/rotate-now` - the ONLY
    rotation-policy-adjacent schema with a `secret` field (one-time-reveal,
    same discipline as `ServiceAccountKeyCreateResponse`)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key_prefix: str
    secret: str
    overlap_expires_at: datetime


class ProviderKeyRotateRequest(BaseModel):
    """Request body for `POST /v1/admin/provider-keys/{provider}/rotate`
    (AC7.7) - the new key's secret payload, same shape as the provider's
    existing `PUT .../key` request schema (validated live before anything
    is written)."""

    model_config = ConfigDict(extra="forbid")

    payload: dict[str, Any]
