"""Unit tests for the pure team-resolution rules behind
`GET /v1/model-access` (Phase 2, BD-20) and the personal-key expiry rules
(BD-16) - no DB, no app."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from gatekey.api.v1.model_access import select_team_id
from gatekey.errors import ForbiddenError, GatekeyError
from gatekey.services.personal_keys import validate_expiry

_TEAM_A = uuid.uuid4()
_TEAM_B = uuid.uuid4()


# --- select_team_id (design doc 5.7) -----------------------------------------


def test_single_membership_auto_selects():
    assert select_team_id(None, [_TEAM_A], org_wide_role=False) == _TEAM_A


def test_zero_memberships_falls_back_to_org_baseline():
    assert select_team_id(None, [], org_wide_role=False) is None


def test_two_memberships_without_team_id_is_400_team_id_required():
    with pytest.raises(GatekeyError) as exc_info:
        select_team_id(None, [_TEAM_A, _TEAM_B], org_wide_role=False)
    assert exc_info.value.code == "team_id_required"
    assert exc_info.value.status_code == 400


def test_explicit_team_id_must_be_own_membership():
    assert select_team_id(_TEAM_B, [_TEAM_A, _TEAM_B], org_wide_role=False) == _TEAM_B


def test_explicit_foreign_team_id_is_generic_403():
    with pytest.raises(ForbiddenError):
        select_team_id(_TEAM_B, [_TEAM_A], org_wide_role=False)


def test_org_wide_role_may_view_any_team():
    assert select_team_id(_TEAM_B, [], org_wide_role=True) == _TEAM_B


# --- validate_expiry (design doc 1.1/5.6) ------------------------------------

_NOW = datetime(2026, 8, 4, 12, 0, 0, tzinfo=timezone.utc)


def test_no_expiry_allowed_when_org_has_no_max():
    validate_expiry(None, max_expiration_days=None, now=_NOW)  # no raise


def test_past_expiry_rejected():
    with pytest.raises(GatekeyError) as exc_info:
        validate_expiry(_NOW - timedelta(days=1), max_expiration_days=None, now=_NOW)
    assert exc_info.value.code == "personal_key_expiry_invalid"


def test_no_expiry_rejected_when_org_sets_a_max():
    with pytest.raises(GatekeyError) as exc_info:
        validate_expiry(None, max_expiration_days=30, now=_NOW)
    assert exc_info.value.code == "personal_key_expiry_required"


def test_expiry_within_max_accepted():
    validate_expiry(_NOW + timedelta(days=30), max_expiration_days=30, now=_NOW)


def test_expiry_beyond_max_rejected():
    with pytest.raises(GatekeyError) as exc_info:
        validate_expiry(_NOW + timedelta(days=31), max_expiration_days=30, now=_NOW)
    assert exc_info.value.code == "personal_key_expiry_too_long"
