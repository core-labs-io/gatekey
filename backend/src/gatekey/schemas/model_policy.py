"""Pydantic v2 request/response models for the model-policy admin API.

Phase 1.3 (Model Access Governance - Basic). See
`docs/design/phase-1.3-model-governance.md` section 4.1.

`ModelPolicyPutRequest.mode` is a two-member `Literal` (`"allowlist"` /
`"denylist"`) - it deliberately cannot express `"unconfigured"`, so a client
`PUT` with `mode="unconfigured"` 422s via ordinary FastAPI/Pydantic request
validation rather than needing app-level defensive code (AC-7; mirrors
`ModelPolicyMode`'s two-valued DB enum - see `db/models/model_policy.py`'s
ADR-2).

A single `models: list[str]` field (no `allowlist_models`/`denylist_models`
pair) makes "both lists populated" structurally unrepresentable (AC-9) -
there is only ever one list, and `mode` says how to interpret it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator


class ModelPolicyPutRequest(BaseModel):
    """Request body for `PUT /v1/admin/model-policy`."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["allowlist", "denylist"]
    models: list[str] = []

    @field_validator("models")
    @classmethod
    def _entries_non_empty_strings(cls, value: list[str]) -> list[str]:
        for entry in value:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError("models entries must be non-empty strings.")
        return value


class ModelPolicyResponse(BaseModel):
    """Response body for `GET`/`PUT /v1/admin/model-policy`."""

    model_config = ConfigDict(from_attributes=True)

    mode: Literal["unconfigured", "allowlist", "denylist"]
    models: list[str]
