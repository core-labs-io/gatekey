"""Unit tests for schemas/service_account_key.py."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from gatekey.schemas.service_account_key import (
    ServiceAccountKeyCreateRequest,
    ServiceAccountKeyCreateResponse,
    ServiceAccountKeyResponse,
)


def test_create_request_accepts_valid_name():
    model = ServiceAccountKeyCreateRequest(
        name="billing-service", user_id=uuid.uuid4(), team_id=uuid.uuid4()
    )
    assert model.name == "billing-service"


@pytest.mark.parametrize("bad_value", ["", "   "])
def test_create_request_rejects_blank_name(bad_value):
    with pytest.raises(ValidationError):
        ServiceAccountKeyCreateRequest(name=bad_value, user_id=uuid.uuid4(), team_id=uuid.uuid4())


def test_create_request_rejects_oversized_name():
    with pytest.raises(ValidationError):
        ServiceAccountKeyCreateRequest(name="x" * 5000, user_id=uuid.uuid4(), team_id=uuid.uuid4())


def test_create_request_forbids_extra_fields():
    with pytest.raises(ValidationError):
        ServiceAccountKeyCreateRequest(
            name="valid", user_id=uuid.uuid4(), team_id=uuid.uuid4(), unexpected_field="nope"
        )


def test_create_request_requires_user_id():
    with pytest.raises(ValidationError):
        ServiceAccountKeyCreateRequest(name="valid", team_id=uuid.uuid4())


def test_create_request_requires_team_id():
    # Phase 2 (design doc 1.7 / security review H-1): every NEW key needs a
    # team attribution - required at the API-schema layer.
    with pytest.raises(ValidationError):
        ServiceAccountKeyCreateRequest(name="valid", user_id=uuid.uuid4())


def test_create_response_is_the_only_schema_with_a_secret_field():
    assert "secret" in ServiceAccountKeyCreateResponse.model_fields
    assert "secret" not in ServiceAccountKeyResponse.model_fields
    assert "secret_hash" not in ServiceAccountKeyResponse.model_fields
    assert "secret_hash" not in ServiceAccountKeyCreateResponse.model_fields


def test_response_schema_has_no_secret_shaped_fields_defined_at_all():
    # Locks down the model shape itself - not just serialization exclusion -
    # so a future edit can't accidentally reintroduce a leaky field. Mirrors
    # the identical guard in test_schemas_provider_key.py.
    assert set(ServiceAccountKeyResponse.model_fields) == {
        "id",
        "name",
        "user_id",
        "team_id",
        "key_prefix",
        "created_at",
        "revoked_at",
        "active",
    }


class _FakeServiceAccountKeyRow:
    """Stands in for `gatekey.db.models.service_account_key.ServiceAccountKey`."""

    def __init__(self, *, revoked_at=None):
        self.id = uuid.uuid4()
        self.org_id = uuid.uuid4()
        self.user_id = uuid.uuid4()
        self.name = "billing-service"
        self.team_id = None
        self.key_prefix = "abcdefghijkl"
        self.secret_hash = b"\x00" * 32
        self.created_at = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
        self.revoked_at = revoked_at


def test_response_active_true_when_revoked_at_is_none():
    response = ServiceAccountKeyResponse.model_validate(_FakeServiceAccountKeyRow())
    assert response.active is True
    assert response.revoked_at is None


def test_response_active_false_when_revoked_at_is_set():
    revoked_at = datetime(2026, 7, 14, 13, 0, 0, tzinfo=timezone.utc)
    response = ServiceAccountKeyResponse.model_validate(
        _FakeServiceAccountKeyRow(revoked_at=revoked_at)
    )
    assert response.active is False
    assert response.revoked_at == revoked_at


def test_response_serialization_never_includes_secret_hash():
    response = ServiceAccountKeyResponse.model_validate(_FakeServiceAccountKeyRow())
    dumped_json = response.model_dump_json()
    assert "secret_hash" not in dumped_json
    assert "secret" not in response.model_dump()
