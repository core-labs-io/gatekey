"""Pydantic v2 request/response models for the content-aware-rules admin API
(Phase 3, BD-5). See `docs/design/phase-3-security-compliance-design.md`
sections 1.7/9.4.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

# Ratified #6 (Phase 3) + Phase 5 (5.3, AC5.3.1/AC5.3.4): all four rows are
# ship-able/administrable AND functionally equivalent - every category is
# now wired to a real classifier signal (`services.dlp.py`'s
# `category_findings` - see `services.model_policy.resolve_content_
# classification`'s module docstring addition). "legal" (Phase 5, new) has
# no separate schema/scaffolding history - it's validated identically to
# the other three here, at the API layer (not the DB, which stays free-text
# for forward-compat) - a PUT naming any other category is rejected.
CONTENT_AWARE_CATEGORIES = ("pii", "source_code", "financial_data", "legal")


class ContentAwareRuleItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    enabled: bool = False
    allowed_models: list[str] = []

    @field_validator("category")
    @classmethod
    def _known_category(cls, value: str) -> str:
        if value not in CONTENT_AWARE_CATEGORIES:
            raise ValueError(f"category must be one of: {', '.join(CONTENT_AWARE_CATEGORIES)}.")
        return value


class ContentAwareRulesPutRequest(BaseModel):
    """Request body for `PUT /v1/admin/content-aware-rules` - the full set
    (up to one row per category); any category omitted from the list is
    left unchanged."""

    model_config = ConfigDict(extra="forbid")

    rules: list[ContentAwareRuleItem]


class ContentAwareRuleResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    category: str
    enabled: bool
    allowed_models: list[str]
