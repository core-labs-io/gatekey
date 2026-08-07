"""Pydantic v2 request/response models for the compliance-settings admin API
(Phase 3, BD-10; Phase 5 5.2 Hash-Chained Audit Ledger adds `chain_enabled`).
See `docs/design/phase-3-security-compliance-design.md` section 9.1 and
`gatekey/phase-5-technical-design.md` section 3.1 ("PUT /v1/admin/
compliance-settings (extended) - adds chain_enabled: bool").
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ComplianceSettingsPutRequest(BaseModel):
    """Full-replace PUT. `audit_retention_days=None` (the default) means
    "never auto-purge" (ratified #1) - a client must explicitly set a
    finite value to opt in to the purge job.

    `chain_enabled` (Phase 5, AC5.2.2/AC5.2.7): a full-replace field, exactly
    like the three fields above - a client that never sends it explicitly
    gets `False` (chain disabled), same "PUT always states the complete
    desired state" contract this endpoint has always had. Setting this
    `True` together with a non-null `audit_retention_days` in the same
    request is rejected (422) - never silently accepted."""

    model_config = ConfigDict(extra="forbid")

    audit_retention_days: int | None = Field(default=None, ge=1)
    log_prompt_retention_days: int = Field(default=30, ge=1)
    access_schedule_timezone: str = "UTC"
    chain_enabled: bool = False

    @field_validator("access_schedule_timezone")
    @classmethod
    def _valid_iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            raise ValueError(f"'{value}' is not a recognized IANA timezone name.") from None
        return value


class ComplianceSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    audit_retention_days: int | None
    log_prompt_retention_days: int
    access_schedule_timezone: str
    chain_enabled: bool
