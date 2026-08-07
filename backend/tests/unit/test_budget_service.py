"""Unit tests for services/budget.py (Phase 1.4 - Budget Basic)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from gatekey.db.models.team import TeamPeriodType
from gatekey.services.budget import (
    TeamMembershipBudgetState,
    UserBudgetState,
    compute_cost,
    is_budget_exhausted,
)
from gatekey.services.team_periods import TeamPeriodInfo
from gatekey.providers.pricing import PricingEntryMissingError


def _state(*, budget_usd, current_spend_usd) -> UserBudgetState:
    return UserBudgetState(
        id=uuid.uuid4(), name="test-user", budget_usd=budget_usd, current_spend_usd=current_spend_usd
    )


def test_is_budget_exhausted_null_budget_never_exhausted():
    state = _state(budget_usd=None, current_spend_usd=Decimal("999999"))
    assert is_budget_exhausted(state) is False


def test_is_budget_exhausted_zero_budget_exhausted_immediately():
    state = _state(budget_usd=Decimal("0"), current_spend_usd=Decimal("0"))
    assert is_budget_exhausted(state) is True


def test_is_budget_exhausted_below_budget_not_exhausted():
    state = _state(budget_usd=Decimal("10"), current_spend_usd=Decimal("5"))
    assert is_budget_exhausted(state) is False


def test_is_budget_exhausted_exactly_at_budget_is_exhausted():
    state = _state(budget_usd=Decimal("10"), current_spend_usd=Decimal("10"))
    assert is_budget_exhausted(state) is True


def test_is_budget_exhausted_over_budget_is_exhausted():
    state = _state(budget_usd=Decimal("10"), current_spend_usd=Decimal("10.01"))
    assert is_budget_exhausted(state) is True


def _team_state(*, budget_usd, current_spend_usd) -> TeamMembershipBudgetState:
    team_id = uuid.uuid4()
    return TeamMembershipBudgetState(
        membership_id=uuid.uuid4(),
        team_id=team_id,
        user_id=uuid.uuid4(),
        name="test-user",
        budget_usd=budget_usd,
        current_spend_usd=current_spend_usd,
        period=TeamPeriodInfo(
            id=team_id,
            period_type=TeamPeriodType.MONTHLY,
            current_period_started_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        ),
    )


def test_is_budget_exhausted_same_semantics_for_team_membership_state():
    """Phase 2 (BD-8): one shared predicate - NULL unmetered, `>=` exhausted
    - for both the legacy flat counter and a TeamMembership counter."""
    assert is_budget_exhausted(_team_state(budget_usd=None, current_spend_usd=Decimal("999"))) is False
    assert is_budget_exhausted(_team_state(budget_usd=Decimal("0"), current_spend_usd=Decimal("0"))) is True
    assert is_budget_exhausted(_team_state(budget_usd=Decimal("10"), current_spend_usd=Decimal("9.99"))) is False
    assert is_budget_exhausted(_team_state(budget_usd=Decimal("10"), current_spend_usd=Decimal("10"))) is True


def test_compute_cost_chat_formula():
    # gpt-4o-mini: input $0.15/M, output $0.60/M
    cost = compute_cost("gpt-4o-mini", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert cost == Decimal("0.15") + Decimal("0.60")


def test_compute_cost_embeddings_formula_has_no_output_term():
    cost = compute_cost("text-embedding-3-small", prompt_tokens=1_000_000, completion_tokens=None)
    assert cost == Decimal("0.02")


def test_compute_cost_zero_tokens_is_legitimate_zero():
    cost = compute_cost("gpt-4o-mini", prompt_tokens=0, completion_tokens=0)
    assert cost == Decimal("0")


def test_compute_cost_raises_for_unpriced_model():
    with pytest.raises(PricingEntryMissingError):
        compute_cost("not-a-real-model", prompt_tokens=10, completion_tokens=10)
