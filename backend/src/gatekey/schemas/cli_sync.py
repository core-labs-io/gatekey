"""Pydantic v2 request/response models for the CLI-sync device-code auth
flow + `GET /v1/me/current-key` (Phase 3, BD-25, design doc section 8.2 and
API contract section 9.8).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DeviceStartRequest(BaseModel):
    """`POST /v1/auth/device/start` optional body (added by `0047`) -
    `device_label` is self-reported by the CLI (the machine being paired
    knows its own hostname; the approving browser doesn't) and later
    stamped onto the minted `PersonalApiKey.device_label`. Every field
    optional / the body itself optional - existing CLI-sync clients that
    send no body at all must keep working unchanged."""

    model_config = ConfigDict(extra="forbid")

    device_label: str | None = Field(default=None, max_length=255)


class DeviceStartResponse(BaseModel):
    """`POST /v1/auth/device/start` - standard OAuth 2.0 Device
    Authorization Grant shape (design doc 8.2, AC8a.2)."""

    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


class DeviceApproveRequest(BaseModel):
    """`POST /v1/auth/device/approve` (session auth). `team_id` is always
    required - see `services.cli_refresh_credentials.
    resolve_team_id_for_device_approval`'s docstring for why this mirrors
    `PersonalApiKeyCreateRequest.team_id` rather than the design doc's
    auto-select wording."""

    model_config = ConfigDict(extra="forbid")

    user_code: str = Field(min_length=1, max_length=32)
    team_id: UUID


class DeviceApproveResponse(BaseModel):
    """No secret material - the refresh credential is delivered to the CLI
    exactly once, via `poll`, never echoed back to the approving browser."""

    status: Literal["approved"] = "approved"


class DevicePollRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_code: str


class DevicePollResponse(BaseModel):
    """`202` (`status="pending"`) while the user hasn't approved yet; `200`
    (`status="approved"`, `refresh_credential` populated) exactly once, on
    the first poll to observe the approval - see `services.
    cli_refresh_credentials.DeviceAuthStore.poll`'s docstring."""

    status: Literal["pending", "approved"]
    refresh_credential: str | None = None


class CurrentKeyResponse(BaseModel):
    """`GET /v1/me/current-key` - the freshly-rotated personal key's
    plaintext (fork #3: every fetch rotates) plus a server-computed
    `valid_until` hint (never client-trusted for enforcement - design doc
    8.2)."""

    secret: str
    valid_until: datetime
