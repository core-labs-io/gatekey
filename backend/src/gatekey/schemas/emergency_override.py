"""Pydantic v2 request/response models for emergency access-schedule
overrides (Phase 3, BD-18). See
`docs/design/phase-3-security-compliance-design.md` section 5.3 and the
product spec's AC9.7-AC9.9.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator


class EmergencyOverrideGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1)
    expires_at: AwareDatetime

    @field_validator("reason")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        # AC9.7: server-side non-empty, not just a UI required-field hint -
        # a whitespace-only string passes Pydantic's bare `min_length=1`,
        # so this closes that gap explicitly.
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason must not be blank.")
        return stripped


class EmergencyOverrideResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    service_account_id: uuid.UUID
    granted_by_user_id: uuid.UUID
    reason: str
    granted_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
