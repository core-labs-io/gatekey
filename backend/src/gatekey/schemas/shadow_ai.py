"""Pydantic v2 request/response models for Shadow AI Discovery (Phase 5 -
Differentiators, 5.1) - both the ingestion contract (`ShadowAiIngestBatchRequest`,
AC5.1.1/AC5.1.3) and the admin config/report/token-gen/hostname-CRUD surface
(`api/v1/admin/shadow_ai.py`).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Generous sanity bounds only (matches `schemas/self_hosted_provider.py`'s
# stated convention) - not format-specific validation.
_MAX_USER_IDENTIFIER_LENGTH = 320  # RFC 5321 max mailbox length
_MAX_HOSTNAME_LENGTH = 255
_MAX_TOOL_LABEL_LENGTH = 200
_MAX_WEBHOOK_URL_LENGTH = 2048
_MAX_EVENTS_PER_BATCH = 5000
_MAX_RAW_METADATA_BYTES = 4096

# ---------------------------------------------------------------------------
# Ingestion (AC5.1.1/AC5.1.3).
# ---------------------------------------------------------------------------


class ShadowAiIngestEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_identifier: str = Field(min_length=1, max_length=_MAX_USER_IDENTIFIER_LENGTH)
    destination_host: str = Field(min_length=1, max_length=_MAX_HOSTNAME_LENGTH)
    occurred_at: datetime
    source: Literal["sase_log", "proxy_log"]
    # Connection metadata only - never full URLs/query strings/bodies (module
    # docstring / AC5.1.1). Hardening pass item 7: `_MAX_RAW_METADATA_BYTES`
    # (previously a defined-but-never-enforced constant, a latent gap flagged
    # by the Phase 5 security review) is now enforced below via `_validate_
    # raw_metadata_size` - a clean 422 on an oversized payload, never a
    # silent truncation, so the "connection metadata only" claim in
    # `docs/policy/shadow-ai-data-handling.md` §2 is technically enforced,
    # not just documented convention.
    raw_metadata: dict | None = None

    @field_validator("raw_metadata")
    @classmethod
    def _validate_raw_metadata_size(cls, value: dict | None) -> dict | None:
        if value is None:
            return value
        # Compact (no extra whitespace) JSON encoding, same normalization
        # `services.response_cache.compute_prompt_hash` uses elsewhere in
        # this codebase for a "the serialized size actually stored" measure
        # - a generous few-KB sanity bound (not a strict schema), matching
        # this module's own stated convention for free-form fields.
        serialized_bytes = len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
        if serialized_bytes > _MAX_RAW_METADATA_BYTES:
            raise ValueError(
                f"raw_metadata is too large ({serialized_bytes} bytes serialized) - "
                f"the limit is {_MAX_RAW_METADATA_BYTES} bytes. This field is for small, "
                "non-content connection metadata only (see docs/policy/shadow-ai-data-"
                "handling.md); it is rejected outright, never silently truncated."
            )
        return value


class ShadowAiIngestBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[ShadowAiIngestEventRequest] = Field(
        min_length=1, max_length=_MAX_EVENTS_PER_BATCH
    )


class ShadowAiIngestResponse(BaseModel):
    received: int
    persisted: int
    dropped: int


# ---------------------------------------------------------------------------
# Config (detection source / enforcement mode / retention) + token issuance.
# ---------------------------------------------------------------------------


class ShadowAiConfigResponse(BaseModel):
    """`webhook_url` itself is never returned (defense-in-depth, even though
    the underlying column is currently plaintext at rest - see
    `services/shadow_ai.py`'s module docstring "webhook_url at rest" for the
    flagged gap) - only `webhook_configured` (mirrors `Team.
    webhook_configured`'s own "never echo the secret-equivalent URL back"
    discipline)."""

    detection_source: Literal["sase_log", "proxy_log"]
    enforcement_mode: Literal["detect_only", "notification", "webhook"]
    webhook_configured: bool
    shadow_ai_retention_days: int
    ingestion_configured: bool
    token_created_at: datetime | None


class ShadowAiConfigPutRequest(BaseModel):
    """Full-replace write (same convention as `services.compliance_settings.
    set_compliance_settings`) - every field must be supplied on every PUT.
    `confirm` gates AC5.1.7's intrusive-enforcement-mode transition - see
    `services.shadow_ai.set_shadow_ai_config`'s docstring for exactly when
    it's required."""

    model_config = ConfigDict(extra="forbid")

    detection_source: Literal["sase_log", "proxy_log"]
    enforcement_mode: Literal["detect_only", "notification", "webhook"]
    webhook_url: str | None = Field(default=None, max_length=_MAX_WEBHOOK_URL_LENGTH)
    shadow_ai_retention_days: int = Field(gt=0, le=3650)
    confirm: bool = False


class ShadowAiTokenRotateResponse(BaseModel):
    """One-time-reveal - same discipline as `ScimTokenRotateResponse`/
    `ServiceAccountKeyCreateResponse`: `token` is returned exactly once, by
    this endpoint, never persisted, never returned by any other endpoint."""

    token: str
    token_created_at: datetime


# ---------------------------------------------------------------------------
# Curated hostname allowlist CRUD (AC5.1.2).
# ---------------------------------------------------------------------------


class KnownAiToolHostnameResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    hostname: str
    tool_label: str
    enabled: bool


class KnownAiToolHostnameCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hostname: str = Field(min_length=1, max_length=_MAX_HOSTNAME_LENGTH)
    tool_label: str = Field(min_length=1, max_length=_MAX_TOOL_LABEL_LENGTH)
    enabled: bool = True


class KnownAiToolHostnameUpdateRequest(BaseModel):
    """Every field optional - omitted means "leave unchanged" (same
    partial-update discipline as `SelfHostedProviderUpdateRequest`)."""

    model_config = ConfigDict(extra="forbid")

    tool_label: str | None = Field(default=None, min_length=1, max_length=_MAX_TOOL_LABEL_LENGTH)
    enabled: bool | None = None


# ---------------------------------------------------------------------------
# Report (AC5.1.5/AC5.1.6/AC5.1.8).
# ---------------------------------------------------------------------------


class ShadowAiReportRowResponse(BaseModel):
    user_identifier: str
    matched_user_id: uuid.UUID | None
    linked: bool
    tool_label: str
    destination_host: str
    frequency_per_week: float
    last_seen: datetime
    repeat_violator: bool
