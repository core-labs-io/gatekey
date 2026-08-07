"""Unit tests for `services/audit.py` (Phase 2, BD-17) - actor-label
derivation for both actor shapes and JSONB-safe value conversion, with a
stub session (no DB)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from gatekey.api.deps import AdminContext
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.audit_entry import AuditEntry
from gatekey.db.models.team import TeamPeriodType
from gatekey.services.audit import write_audit_entry
from gatekey.services.sessions import SessionContext


class _StubResult:
    """Answers `write_audit_entry`'s own `compliance_settings.chain_enabled`
    read (Phase 5, 5.2) - always "no row" (ADR-2 default: chain disabled),
    so every test in this file exercises the unchained, byte-for-byte
    pre-Phase-5 write path unless a test opts into chaining explicitly."""

    def scalar_one_or_none(self):
        return None


class _StubSession:
    def __init__(self) -> None:
        self.added: list[AuditEntry] = []

    def add(self, row: AuditEntry) -> None:
        self.added.append(row)

    async def flush(self) -> None:
        pass

    async def execute(self, stmt):  # noqa: ANN001, ARG002
        return _StubResult()


def _session_ctx(user_id: uuid.UUID) -> SessionContext:
    return SessionContext(
        session_id=uuid.uuid4(),
        org_id=DEFAULT_ORG_ID,
        user_id=user_id,
        org_role=None,
        display_label="Ada Lovelace <ada@example.com>",
    )


@pytest.mark.asyncio
async def test_session_actor_uses_display_label_and_user_id() -> None:
    session = _StubSession()
    user_id = uuid.uuid4()
    await write_audit_entry(
        session,  # type: ignore[arg-type]
        actor=_session_ctx(user_id),
        action="team.create",
        target_type="team",
        target_id="t1",
        old_value=None,
        new_value={"name": "Platform"},
    )
    [entry] = session.added
    assert entry.actor_user_id == user_id
    assert entry.actor_label == "Ada Lovelace <ada@example.com>"
    assert entry.action == "team.create"
    assert entry.old_value is None
    assert entry.new_value == {"name": "Platform"}


@pytest.mark.asyncio
async def test_break_glass_admin_actor_uses_sentinel_label() -> None:
    session = _StubSession()
    await write_audit_entry(
        session,  # type: ignore[arg-type]
        actor=AdminContext(
            actor_user_id=None, actor_label="system:admin_token", org_id=DEFAULT_ORG_ID
        ),
        action="team.delete",
        target_type="team",
        target_id="t1",
        old_value={"name": "Platform"},
        new_value=None,
    )
    [entry] = session.added
    assert entry.actor_user_id is None
    assert entry.actor_label == "system:admin_token"


@pytest.mark.asyncio
async def test_values_are_converted_to_jsonb_safe_primitives() -> None:
    session = _StubSession()
    some_uuid = uuid.uuid4()
    when = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    await write_audit_entry(
        session,  # type: ignore[arg-type]
        actor=AdminContext(
            actor_user_id=None, actor_label="system:admin_token", org_id=DEFAULT_ORG_ID
        ),
        action="team.update",
        target_type="team",
        target_id="t1",
        old_value={"budget_usd": Decimal("12.50"), "nested": {"user_id": some_uuid}},
        new_value={"period_type": TeamPeriodType.MONTHLY, "at": when, "items": [Decimal("1")]},
    )
    [entry] = session.added
    # Decimals stringified (no float precision loss), UUID/datetime/enum
    # reduced to strings - everything json.dumps-able.
    assert entry.old_value == {
        "budget_usd": "12.50",
        "nested": {"user_id": str(some_uuid)},
    }
    assert entry.new_value == {
        "period_type": "monthly",
        "at": "2026-08-04T12:00:00+00:00",
        "items": ["1"],
    }


@pytest.mark.asyncio
async def test_break_glass_session_sentinel_context_writes_a4_actor() -> None:
    """The org_admin-equivalent BREAK_GLASS_SESSION_CONTEXT (used by the
    require_role/require_team_role factories) must audit exactly like the
    AdminContext break-glass shape: actor_user_id NULL, sentinel label."""
    from gatekey.services.sessions import BREAK_GLASS_SESSION_CONTEXT

    session = _StubSession()
    await write_audit_entry(
        session,  # type: ignore[arg-type]
        actor=BREAK_GLASS_SESSION_CONTEXT,
        action="team.member.add",
        target_type="team_membership",
        target_id="m1",
        old_value=None,
        new_value={"role": "member"},
    )
    [entry] = session.added
    assert entry.actor_user_id is None
    assert entry.actor_label == "system:admin_token"
    assert entry.org_id == DEFAULT_ORG_ID
