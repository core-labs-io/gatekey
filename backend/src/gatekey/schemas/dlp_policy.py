"""Pydantic v2 request/response models for the DLP admin API (Phase 3,
BD-2). See `docs/design/phase-3-security-compliance-design.md` section 9.2.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DlpActionLiteral = Literal["log", "redact", "block"]


def _coerce_enum(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


class DlpPolicyPutRequest(BaseModel):
    """Request body for `PUT /v1/admin/dlp-policy`. Full-replace, mirrors
    `ModelPolicyPutRequest`'s posture. Detector toggles/`default_action`
    default to the same off/log defaults as the DB row's own column
    defaults (`db/models/dlp_policy.py`) - a bare `{}` body is a no-op
    write, never a surprise."""

    model_config = ConfigDict(extra="forbid")

    ssn_detector_enabled: bool = False
    credit_card_detector_enabled: bool = False
    email_detector_enabled: bool = False
    phone_detector_enabled: bool = False
    default_action: DlpActionLiteral = "log"
    store_raw_flagged_content: bool = False
    scan_inbound_responses: bool = Field(
        default=False,
        description=(
            "NOT YET IMPLEMENTED - no code path scans provider responses; only inbound "
            "prompts are scanned. Must be false/absent; setting true is rejected with "
            "422 `inbound_scanning_not_implemented`."
        ),
    )


class DlpPolicyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ssn_detector_enabled: bool
    credit_card_detector_enabled: bool
    email_detector_enabled: bool
    phone_detector_enabled: bool
    default_action: DlpActionLiteral
    store_raw_flagged_content: bool
    scan_inbound_responses: bool = Field(
        description=(
            "NOT YET IMPLEMENTED - always false. Reserved column for scanning provider "
            "responses (not just inbound prompts); no code path acts on it yet."
        ),
    )

    @field_validator("default_action", mode="before")
    @classmethod
    def _coerce_action(cls, value: Any) -> Any:
        return _coerce_enum(value)


class DlpCustomPatternRequest(BaseModel):
    """Request body for `POST`/`PATCH .../custom-patterns[/{id}]`. `pattern`
    is validated compilable at the service layer (design doc section 1.4),
    not here - a regex string that happens to also be syntactically valid
    JSON/whatever isn't itself something Pydantic can check."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    pattern: str = Field(min_length=1, max_length=2000)
    action: DlpActionLiteral

    @field_validator("name", "pattern")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank.")
        return value


class DlpCustomPatternResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    pattern: str
    action: DlpActionLiteral

    @field_validator("action", mode="before")
    @classmethod
    def _coerce_action(cls, value: Any) -> Any:
        return _coerce_enum(value)


class TeamDlpOverrideRequest(BaseModel):
    """Request body for `PUT /v1/teams/{team_id}/dlp-override` - action
    override only (AC2.4's two-layer system; no per-key override, no
    per-team pattern authoring)."""

    model_config = ConfigDict(extra="forbid")

    action: DlpActionLiteral


class TeamDlpOverrideResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    action: DlpActionLiteral | None  # None = no override row, org default applies

    @field_validator("action", mode="before")
    @classmethod
    def _coerce_action(cls, value: Any) -> Any:
        return _coerce_enum(value)
