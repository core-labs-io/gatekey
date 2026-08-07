"""Unit tests for schemas/model_policy.py (Phase 1.3, BD-9)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gatekey.schemas.model_policy import ModelPolicyPutRequest, ModelPolicyResponse


def test_put_request_accepts_allowlist() -> None:
    model = ModelPolicyPutRequest(mode="allowlist", models=["gpt-4o", "gpt-4o-mini"])
    assert model.mode == "allowlist"
    assert model.models == ["gpt-4o", "gpt-4o-mini"]


def test_put_request_accepts_denylist() -> None:
    model = ModelPolicyPutRequest(mode="denylist", models=["gpt-4o"])
    assert model.mode == "denylist"


def test_put_request_defaults_models_to_empty_list() -> None:
    model = ModelPolicyPutRequest(mode="allowlist")
    assert model.models == []


def test_put_request_rejects_unconfigured_mode() -> None:
    """AC-7: `mode="unconfigured"` is not a member of the `Literal` - this
    must 422 via ordinary Pydantic/FastAPI validation, not custom app code.
    """
    with pytest.raises(ValidationError):
        ModelPolicyPutRequest(mode="unconfigured", models=[])


@pytest.mark.parametrize("bad_mode", ["ALLOWLIST", "allow", "deny_list", "", None, 1])
def test_put_request_rejects_any_other_invalid_mode(bad_mode) -> None:
    with pytest.raises(ValidationError):
        ModelPolicyPutRequest(mode=bad_mode, models=[])


def test_put_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelPolicyPutRequest(mode="allowlist", models=[], unexpected_field="nope")


@pytest.mark.parametrize("bad_entry", ["", "   ", None, 123, [], {}])
def test_put_request_rejects_non_empty_string_violation_in_models(bad_entry) -> None:
    """AC-9-adjacent validation: every `models` entry must be a non-empty string."""
    with pytest.raises(ValidationError):
        ModelPolicyPutRequest(mode="allowlist", models=["gpt-4o", bad_entry])


def test_put_request_accepts_empty_models_list() -> None:
    model = ModelPolicyPutRequest(mode="allowlist", models=[])
    assert model.models == []


def test_response_accepts_unconfigured_mode() -> None:
    response = ModelPolicyResponse(mode="unconfigured", models=[])
    assert response.mode == "unconfigured"


def test_response_accepts_allowlist_and_denylist_modes() -> None:
    ModelPolicyResponse(mode="allowlist", models=["gpt-4o"])
    ModelPolicyResponse(mode="denylist", models=["gpt-4o"])


def test_response_rejects_unknown_mode() -> None:
    with pytest.raises(ValidationError):
        ModelPolicyResponse(mode="not-a-real-mode", models=[])


def test_response_has_exactly_the_two_documented_fields() -> None:
    # Locks the schema shape down - see design doc section 4.1: a single
    # `models` field, no `allowlist_models`/`denylist_models` pair (AC-9).
    assert set(ModelPolicyResponse.model_fields) == {"mode", "models"}
