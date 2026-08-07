"""Pydantic v2 request/response models for the residency-rule admin API
(Phase 3, BD-4). See `docs/design/phase-3-security-compliance-design.md`
sections 3.1/9.3.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ViolationBehaviorLiteral = Literal["hard_block", "warn"]


class ResidencyRulePutRequest(BaseModel):
    """Request body for `PUT /v1/admin/residency-rules` and `PUT
    /v1/teams/{team_id}/residency-rule`. `violation_behavior` defaults to
    `"hard_block"` (AC3.2: the create path cannot silently default to
    `warn`) - a client must explicitly opt down."""

    model_config = ConfigDict(extra="forbid")

    allowed_regions: list[str] = Field(min_length=1)
    violation_behavior: ViolationBehaviorLiteral = "hard_block"

    @field_validator("allowed_regions")
    @classmethod
    def _entries_non_empty_strings(cls, value: list[str]) -> list[str]:
        for entry in value:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError("allowed_regions entries must be non-empty strings.")
        return value


class ResidencyRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    allowed_regions: list[str]
    violation_behavior: ViolationBehaviorLiteral

    @field_validator("violation_behavior", mode="before")
    @classmethod
    def _coerce_behavior(cls, value: Any) -> Any:
        return value.value if isinstance(value, Enum) else value
