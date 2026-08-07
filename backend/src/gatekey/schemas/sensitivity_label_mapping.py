"""Pydantic v2 request/response models for the `sensitivity_label_mappings`
admin API (Phase 5 - Differentiators, 5.3 Content-Classification-Aware
Routing, AC5.3.5/AC5.3.8/AC5.3.6). See `gatekey/phase-5-technical-design.md`
section 2.4/3.1.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator

from gatekey.schemas.content_aware_rule import CONTENT_AWARE_CATEGORIES


class SensitivityLabelMappingRequest(BaseModel):
    """Request body for `POST`/`PUT .../sensitivity-label-mappings[/{id}]`."""

    model_config = ConfigDict(extra="forbid")

    external_label: str = Field(min_length=1, max_length=200)
    gatekey_category: str

    @field_validator("external_label")
    @classmethod
    def _non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank.")
        return value

    @field_validator("gatekey_category")
    @classmethod
    def _known_category(cls, value: str) -> str:
        if value not in CONTENT_AWARE_CATEGORIES:
            raise ValueError(f"gatekey_category must be one of: {', '.join(CONTENT_AWARE_CATEGORIES)}.")
        return value


class SensitivityLabelMappingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    external_label: str
    gatekey_category: str
