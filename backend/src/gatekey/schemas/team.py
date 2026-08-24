"""Pydantic v2 request/response models for the Teams API (Phase 2, BD-14).

Design doc section 5.4. AC1.5 note: member `role` fields are typed
`Literal["member", "team_lead"]` - `org_admin`/`auditor` are structurally
inexpressible on these endpoints, so a Team Lead attempting to grant an
org-wide role is rejected by request validation before any authorization
logic runs (the "let the type system guarantee it" style from Phase 1.3's
ADR-2). Org-wide roles are granted only via the org-role admin endpoint.

The alert-config response never carries the webhook URL (bearer-equivalent
secret) in any form - only `webhook_configured` (see `services/teams.py`).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

_MIN_NAME_LENGTH = 1
_MAX_NAME_LENGTH = 256

TeamMemberRole = Literal["member", "team_lead"]


def _non_blank(value: str | None) -> str | None:
    if value is not None and not value.strip():
        raise ValueError("must not be blank.")
    return value


def _non_negative(value: Decimal | None) -> Decimal | None:
    if value is not None and value < 0:
        raise ValueError("must be non-negative.")
    return value


# --- Team CRUD ---------------------------------------------------------------


class TeamCreateRequest(BaseModel):
    """`POST /v1/teams` body."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=_MIN_NAME_LENGTH, max_length=_MAX_NAME_LENGTH)
    budget_ceiling_usd: Decimal | None = None

    _name_non_blank = field_validator("name")(_non_blank)
    _ceiling_non_negative = field_validator("budget_ceiling_usd")(_non_negative)


class TeamUpdateRequest(BaseModel):
    """`PATCH /v1/teams/{team_id}` body.

    Uses `model_fields_set` at the route to distinguish an omitted
    `budget_ceiling_usd` (leave unchanged) from an explicit `null` (clear to
    unmetered) - same pattern as `UserUpdateRequest`.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(
        default=None, min_length=_MIN_NAME_LENGTH, max_length=_MAX_NAME_LENGTH
    )
    budget_ceiling_usd: Decimal | None = None

    _name_non_blank = field_validator("name")(_non_blank)
    _ceiling_non_negative = field_validator("budget_ceiling_usd")(_non_negative)


class TeamPeriodConfigRequest(BaseModel):
    """`PATCH /v1/teams/{team_id}/period-config` body."""

    model_config = ConfigDict(extra="forbid")

    period_type: Literal["monthly", "quarterly"] | None = None
    on_period_end: Literal["rollover", "reset"] | None = None


class TeamResponse(BaseModel):
    id: UUID
    name: str
    budget_ceiling_usd: Decimal | None
    current_spend_usd: Decimal
    period_type: str
    on_period_end: str
    current_period_started_at: datetime
    created_at: datetime
    updated_at: datetime


# --- Members -----------------------------------------------------------------


class TeamMemberResponse(BaseModel):
    user_id: UUID
    name: str
    role: str
    budget_usd: Decimal | None
    current_spend_usd: Decimal
    created_at: datetime


class RemovedTeamMemberResponse(TeamMemberResponse):
    """`GET /v1/teams/{team_id}/members/removed` - the restore-UI
    counterpart to `TeamMemberResponse` (added by `0049`)."""

    removed_at: datetime


class TeamMemberAddRequest(BaseModel):
    """`POST /v1/teams/{team_id}/members` body. `budget_usd` is a required
    key (explicit `null` = unmetered - a deliberate choice, never a
    default)."""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    role: TeamMemberRole = "member"
    budget_usd: Decimal | None

    _budget_non_negative = field_validator("budget_usd")(_non_negative)


class TeamMemberUpdateRequest(BaseModel):
    """`PATCH /v1/teams/{team_id}/members/{user_id}` body - `model_fields_set`
    distinguishes omitted `budget_usd` from explicit `null`."""

    model_config = ConfigDict(extra="forbid")

    role: TeamMemberRole | None = None
    budget_usd: Decimal | None = None

    _budget_non_negative = field_validator("budget_usd")(_non_negative)


class ReassignBudgetRequest(BaseModel):
    """`POST /v1/teams/{team_id}/reassign-budget` body."""

    model_config = ConfigDict(extra="forbid")

    from_user_id: UUID
    to_user_id: UUID
    amount_usd: Decimal = Field(gt=0)


class ReassignBudgetResponse(BaseModel):
    from_user_id: UUID
    to_user_id: UUID
    amount_usd: Decimal
    from_new_budget_usd: Decimal
    to_new_budget_usd: Decimal


# --- Model restrictions ------------------------------------------------------


class TeamModelRestrictionsResponse(BaseModel):
    """`org_baseline` = every model (static registry, plus any verified
    self-hosted/custom model) the org policy currently allows;
    `team_restriction` = the team's narrowing allowlist, or null = no
    restriction row (org baseline applies unchanged)."""

    org_baseline: list[str]
    team_restriction: list[str] | None


class TeamModelRestrictionsPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[str]


class TeamMemberModelRestrictionsResponse(BaseModel):
    """`team_baseline` = every model this member's TEAM can currently use
    (org baseline intersected with the team's own restriction, if any) -
    the effective set a member overlay is allowed to narrow further, never
    widen beyond. `member_restriction` = this member's own narrowing
    allowlist, or null = no restriction row (the team baseline applies to
    them unchanged) - same "null = no further restriction" convention
    `TeamModelRestrictionsResponse.team_restriction` already establishes one
    layer up."""

    team_baseline: list[str]
    member_restriction: list[str] | None


class TeamMemberModelRestrictionsPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[str]


# --- Alert config ------------------------------------------------------------


class TeamAlertConfigResponse(BaseModel):
    """Never carries the webhook URL in any form - `webhook_configured` is
    the only signal (a Slack-style webhook URL is bearer-equivalent along
    its whole path, so even a masked tail would leak secret bits)."""

    threshold_80_enabled: bool
    threshold_100_enabled: bool
    webhook_enabled: bool
    webhook_configured: bool
    email_enabled: bool


class TeamAlertConfigPutRequest(BaseModel):
    """`PUT /v1/teams/{team_id}/alert-config` body. `webhook_url` semantics
    (via `model_fields_set`): omitted = keep the stored URL; a string =
    replace (re-encrypted at rest); explicit `null` = clear."""

    model_config = ConfigDict(extra="forbid")

    threshold_80_enabled: bool
    threshold_100_enabled: bool
    webhook_enabled: bool
    webhook_url: str | None = None
    email_enabled: bool

    @field_validator("webhook_url")
    @classmethod
    def _url_shape(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("https://", "http://")):
            raise ValueError("webhook_url must be an http(s) URL.")
        return value


# --- Team detail / usage -----------------------------------------------------


class TeamDetailResponse(TeamResponse):
    """`GET /v1/teams/{team_id}` - full detail (design doc 5.4)."""

    members: list[TeamMemberResponse]
    team_restriction: list[str] | None
    alert_config: TeamAlertConfigResponse


class TeamSpendByDayResponse(BaseModel):
    date: str
    spend_usd: Decimal


class TeamSpendByModelResponse(BaseModel):
    model: str
    spend_usd: Decimal


class TeamMemberUsageResponse(BaseModel):
    """Per-member breakdown for the Team Dashboard - every current member
    appears (zero rows included), with their membership budget state."""

    user_id: UUID
    name: str
    requests: int
    spend_usd: Decimal
    budget_usd: Decimal | None
    current_spend_usd: Decimal


class TeamUsageResponse(BaseModel):
    total_spend_usd: Decimal
    request_count: int
    spend_by_day: list[TeamSpendByDayResponse]
    spend_by_model: list[TeamSpendByModelResponse]
    spend_by_member: list[TeamMemberUsageResponse]
