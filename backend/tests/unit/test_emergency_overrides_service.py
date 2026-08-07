"""Unit tests for `services/emergency_overrides.py` (Phase 3, BD-16/BD-18).

AC9.7: `reason` is required and non-empty, enforced SERVER-SIDE - this is
tested here with `session=None` to prove the validation raises before any
DB I/O is attempted (a session that were actually touched would blow up
immediately on a `None.add(...)` call).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from gatekey.errors import GatekeyError
from gatekey.services.emergency_overrides import (
    EmergencyOverrideReasonRequiredError,
    grant_emergency_override,
)


@pytest.mark.asyncio
async def test_grant_rejects_blank_reason_before_any_db_write() -> None:
    with pytest.raises(EmergencyOverrideReasonRequiredError):
        await grant_emergency_override(
            None,  # type: ignore[arg-type]
            service_account_id=uuid.uuid4(),
            granted_by_user_id=uuid.uuid4(),
            reason="   ",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )


@pytest.mark.asyncio
async def test_grant_rejects_empty_reason_before_any_db_write() -> None:
    with pytest.raises(EmergencyOverrideReasonRequiredError):
        await grant_emergency_override(
            None,  # type: ignore[arg-type]
            service_account_id=uuid.uuid4(),
            granted_by_user_id=uuid.uuid4(),
            reason="",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )


def test_reason_required_error_is_a_422_gatekey_error() -> None:
    assert issubclass(EmergencyOverrideReasonRequiredError, GatekeyError)
    err = EmergencyOverrideReasonRequiredError()
    assert err.status_code == 422
    assert err.code == "emergency_override_reason_required"
