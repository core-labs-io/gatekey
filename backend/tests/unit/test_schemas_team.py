"""Unit tests for `schemas/team.py` / `schemas/join_request.py` (Phase 2,
BD-14/BD-15) - focused on the schema-level invariants the design makes
load-bearing: AC1.5's `Literal["member", "team_lead"]` role restriction and
the numeric guards."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from gatekey.schemas.join_request import JoinRequestCreateRequest
from gatekey.schemas.team import (
    ReassignBudgetRequest,
    TeamAlertConfigPutRequest,
    TeamCreateRequest,
    TeamMemberAddRequest,
)


def test_member_role_accepts_member_and_team_lead() -> None:
    for role in ("member", "team_lead"):
        req = TeamMemberAddRequest(user_id=uuid.uuid4(), role=role, budget_usd=None)
        assert req.role == role


@pytest.mark.parametrize("role", ["org_admin", "auditor", "owner", ""])
def test_member_role_rejects_org_wide_roles_structurally(role: str) -> None:
    # AC1.5: org_admin/auditor are not expressible on the members endpoint at
    # all - rejected by request validation before any authorization logic.
    with pytest.raises(ValidationError):
        TeamMemberAddRequest(user_id=uuid.uuid4(), role=role, budget_usd=None)


def test_member_budget_usd_is_a_required_key() -> None:
    with pytest.raises(ValidationError):
        TeamMemberAddRequest(user_id=uuid.uuid4(), role="member")


def test_member_budget_must_be_non_negative() -> None:
    with pytest.raises(ValidationError):
        TeamMemberAddRequest(user_id=uuid.uuid4(), role="member", budget_usd=Decimal("-1"))


def test_reassign_amount_must_be_positive() -> None:
    kwargs = {"from_user_id": uuid.uuid4(), "to_user_id": uuid.uuid4()}
    with pytest.raises(ValidationError):
        ReassignBudgetRequest(amount_usd=Decimal("0"), **kwargs)
    with pytest.raises(ValidationError):
        ReassignBudgetRequest(amount_usd=Decimal("-5"), **kwargs)
    assert ReassignBudgetRequest(amount_usd=Decimal("5"), **kwargs).amount_usd == 5


def test_team_create_rejects_blank_name_and_negative_ceiling() -> None:
    with pytest.raises(ValidationError):
        TeamCreateRequest(name="   ")
    with pytest.raises(ValidationError):
        TeamCreateRequest(name="ok", budget_ceiling_usd=Decimal("-1"))


def test_alert_config_webhook_url_must_be_http() -> None:
    kwargs = {
        "threshold_80_enabled": True,
        "threshold_100_enabled": True,
        "webhook_enabled": True,
        "email_enabled": False,
    }
    with pytest.raises(ValidationError):
        TeamAlertConfigPutRequest(webhook_url="ftp://x", **kwargs)
    ok = TeamAlertConfigPutRequest(webhook_url="https://hooks.slack.com/x", **kwargs)
    assert "webhook_url" in ok.model_fields_set


def test_join_request_create_rejects_blank_name() -> None:
    with pytest.raises(ValidationError):
        JoinRequestCreateRequest(full_name="  ", team_id=uuid.uuid4())
