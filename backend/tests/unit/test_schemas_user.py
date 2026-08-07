"""Unit tests for schemas/user.py (Phase 1.4/1.6)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from gatekey.schemas.user import UserCreateRequest, UserUpdateRequest


def test_create_request_defaults_budget_to_none():
    model = UserCreateRequest(name="ana@acme.co")
    assert model.budget_usd is None


def test_create_request_accepts_explicit_zero_budget_distinct_from_none():
    model = UserCreateRequest(name="ana@acme.co", budget_usd=Decimal("0"))
    assert model.budget_usd == Decimal("0")


def test_create_request_rejects_negative_budget():
    with pytest.raises(ValidationError):
        UserCreateRequest(name="ana@acme.co", budget_usd=Decimal("-1"))


def test_create_request_rejects_blank_name():
    with pytest.raises(ValidationError):
        UserCreateRequest(name="   ")


def test_create_request_forbids_current_spend_usd_field():
    with pytest.raises(ValidationError):
        UserCreateRequest(name="ana@acme.co", current_spend_usd=Decimal("5"))


def test_update_request_distinguishes_omitted_from_explicit_null():
    omitted = UserUpdateRequest(name="new-name")
    explicit_null = UserUpdateRequest(budget_usd=None)

    assert "budget_usd" not in omitted.model_dump(exclude_unset=True)
    assert "budget_usd" in explicit_null.model_dump(exclude_unset=True)
    assert explicit_null.model_dump(exclude_unset=True)["budget_usd"] is None


def test_update_request_precise_decimal_round_trips_exactly():
    model = UserUpdateRequest(budget_usd=Decimal("12.3456789012"))
    dumped = model.model_dump_json()
    reloaded = UserUpdateRequest.model_validate_json(dumped)
    assert reloaded.budget_usd == Decimal("12.3456789012")
