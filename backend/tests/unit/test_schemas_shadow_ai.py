"""Unit tests for `schemas/shadow_ai.py` (hardening pass item 7): the
`raw_metadata` size cap, previously a defined-but-never-enforced constant
(`_MAX_RAW_METADATA_BYTES`) - flagged as a low-severity gap by the Phase 5
security review, now a real, enforced `field_validator`.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from gatekey.schemas.shadow_ai import ShadowAiIngestEventRequest

_BASE_KWARGS = dict(
    user_identifier="alice@example.com",
    destination_host="chat.openai.com",
    occurred_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
    source="sase_log",
)


def test_raw_metadata_none_is_accepted() -> None:
    event = ShadowAiIngestEventRequest(**_BASE_KWARGS, raw_metadata=None)
    assert event.raw_metadata is None


def test_raw_metadata_well_within_cap_is_accepted() -> None:
    event = ShadowAiIngestEventRequest(
        **_BASE_KWARGS, raw_metadata={"connection_type": "vpn", "client_version": "1.2.3"}
    )
    assert event.raw_metadata == {"connection_type": "vpn", "client_version": "1.2.3"}


def test_raw_metadata_over_4096_bytes_serialized_is_rejected_with_clean_422_shape() -> None:
    oversized = {"padding": "x" * 5000}
    with pytest.raises(ValidationError) as exc_info:
        ShadowAiIngestEventRequest(**_BASE_KWARGS, raw_metadata=oversized)
    # Rejected outright (a ValueError from the validator, surfaced by
    # Pydantic/FastAPI as a structured 422) - never silently truncated.
    assert "too large" in str(exc_info.value)


def test_raw_metadata_exactly_at_the_serialized_byte_boundary_is_accepted() -> None:
    """Binary-search a value whose compact-JSON serialization lands exactly
    at the 4096-byte cap, to pin the boundary (not just "clearly under" /
    "clearly over") - the cap is inclusive (<=), so exactly 4096 must pass."""
    import json

    # `{"padding":"..."}` - fixed overhead once compact-serialized (no
    # separators' whitespace): len('{"padding":""}') == 15.
    overhead = len(json.dumps({"padding": ""}, separators=(",", ":")).encode("utf-8"))
    filler_len = 4096 - overhead
    value = {"padding": "x" * filler_len}
    serialized_len = len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
    assert serialized_len == 4096  # sanity-check the arithmetic above

    event = ShadowAiIngestEventRequest(**_BASE_KWARGS, raw_metadata=value)
    assert event.raw_metadata == value


def test_raw_metadata_one_byte_over_the_boundary_is_rejected() -> None:
    import json

    overhead = len(json.dumps({"padding": ""}, separators=(",", ":")).encode("utf-8"))
    filler_len = 4096 - overhead + 1
    value = {"padding": "x" * filler_len}
    serialized_len = len(json.dumps(value, separators=(",", ":")).encode("utf-8"))
    assert serialized_len == 4097  # sanity-check the arithmetic above

    with pytest.raises(ValidationError):
        ShadowAiIngestEventRequest(**_BASE_KWARGS, raw_metadata=value)
