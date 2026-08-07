"""Pydantic v2 request/response models for the SCIM-config admin API (Phase
3, BD-24) - design doc `phase-3-security-compliance-design.md` sections
6.2/9.5. NOT the SCIM protocol surface itself (`/scim/v2/...` uses raw
dicts/RFC 7644 shapes - see `services/scim.py`'s module docstring) - this is
Gatekey's own admin console API for issuing/rotating the SCIM bearer token,
so it follows this codebase's usual pydantic-schema convention.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ScimConfigResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    enabled: bool
    token_created_at: datetime | None
    base_url: str


class ScimConfigPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool


class ScimTokenRotateResponse(BaseModel):
    """The ONLY schema in this module with a `token` field - one-time-reveal,
    same discipline as `ServiceAccountKeyCreateResponse`
    (`schemas/service_account_key.py`) - returned exactly once, by the
    rotate endpoint, never persisted."""

    model_config = ConfigDict(from_attributes=True)

    token: str
    token_created_at: datetime
