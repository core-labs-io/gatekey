"""Pydantic v2 request/response models for the onboarding / join-request
workflow (Phase 2, BD-15) - design doc sections 5.2 and 5.3.

`OnboardingTeamResponse` is deliberately id+name only: a pre-onboarding
user must never see budget/member data (design doc 5.2).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MIN_NAME_LENGTH = 1
_MAX_NAME_LENGTH = 256


class OnboardingTeamResponse(BaseModel):
    id: UUID
    name: str


class JoinRequestCreateRequest(BaseModel):
    """`POST /v1/onboarding/join-requests` body - `full_name` is snapshotted
    onto the request (AC6.2's editable IdP claim), independent of
    `users.name`."""

    model_config = ConfigDict(extra="forbid")

    full_name: str = Field(min_length=_MIN_NAME_LENGTH, max_length=_MAX_NAME_LENGTH)
    team_id: UUID

    @field_validator("full_name")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("full_name must not be blank.")
        return value


class JoinRequestResponse(BaseModel):
    id: UUID
    team_id: UUID
    team_name: str | None = None
    requester_user_id: UUID
    requester_name: str
    status: str
    routed_to: str
    requested_at: datetime
    resolved_at: datetime | None
    resolved_by_user_id: UUID | None
    approved_budget_usd: Decimal | None
    rejection_reason: str | None


class JoinRequestApproveRequest(BaseModel):
    """`POST .../approve` body - `budget_usd` is a required key; explicit
    `null` = unmetered (a deliberate approver choice, never a default)."""

    model_config = ConfigDict(extra="forbid")

    budget_usd: Decimal | None

    @field_validator("budget_usd")
    @classmethod
    def _non_negative(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value < 0:
            raise ValueError("budget_usd must be non-negative.")
        return value


class JoinRequestRejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2000)


class AdminJoinRequestQueueEntryResponse(JoinRequestResponse):
    """`GET /v1/admin/join-requests/queue` entry - why this request needs
    org-admin attention (computed live at query time, design doc 5.3)."""

    escalation_reason: Literal["no_team_lead", "pending_over_5_business_days"]
