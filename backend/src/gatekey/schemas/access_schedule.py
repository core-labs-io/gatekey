"""Pydantic v2 request/response models for the access-schedule and
holiday-date admin/team-lead API (Phase 3, BD-17). See
`docs/design/phase-3-security-compliance-design.md` sections 5 and 9.7.
"""

from __future__ import annotations

import uuid
from datetime import date, time

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ISO_WEEKDAYS = frozenset(range(1, 8))


class AccessSchedulePutRequest(BaseModel):
    """Request body shared by the org/team/per-key access-schedule PUT
    endpoints. Default state is OFF (AC9.3) - `enabled` defaults to
    `False`, matching this phase's off-by-default posture."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    allowed_days: list[int] = Field(default_factory=list)
    allowed_hours_start: time | None = None
    allowed_hours_end: time | None = None

    @field_validator("allowed_days")
    @classmethod
    def _valid_weekdays(cls, value: list[int]) -> list[int]:
        for entry in value:
            if entry not in _ISO_WEEKDAYS:
                raise ValueError("allowed_days entries must be ISO weekday ints 1(Mon)-7(Sun).")
        return sorted(set(value))

    @model_validator(mode="after")
    def _hours_paired(self) -> "AccessSchedulePutRequest":
        if (self.allowed_hours_start is None) != (self.allowed_hours_end is None):
            raise ValueError(
                "allowed_hours_start and allowed_hours_end must be set together, or both omitted."
            )
        return self


class AccessScheduleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    allowed_days: list[int]
    allowed_hours_start: time | None
    allowed_hours_end: time | None


class EffectiveScheduleEntry(BaseModel):
    """One row of `GET /v1/admin/keys/schedules` (AC9.10)."""

    model_config = ConfigDict(from_attributes=True)

    service_account_id: uuid.UUID
    name: str
    team_id: uuid.UUID | None
    effective: str


class HolidayDateCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holiday_date: date
    label: str | None = None


class HolidayDateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    holiday_date: date
    label: str | None
