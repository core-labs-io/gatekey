"""Unit tests for `services/compliance_settings.py`'s Phase 5 (5.2
Hash-Chained Audit Ledger) mutual-exclusivity guard (AC5.2.7) - the upfront,
DB-free rejection in `_apply_compliance_settings` when the REQUESTED final
state has both `chain_enabled=True` and a non-null `audit_retention_days`.

The lock/backfill/commit path itself needs a real Postgres `FOR UPDATE`
lock to exercise meaningfully (concurrency, real row state) - covered by
QA's integration pass, not here (same split `test_scheduler_service.py`'s
own docstring documents for `run_due_rotations`).
"""

from __future__ import annotations

import pytest

from gatekey.errors import ChainPurgeMutualExclusivityError
from gatekey.services.compliance_settings import set_chain_enabled, set_compliance_settings


class _NeverCalledSession:
    """Proves the mutual-exclusivity guard rejects BEFORE any database
    round trip (no lock taken, no backfill attempted, no partial commit
    possible) - any `execute`/`commit` call is a bug."""

    async def execute(self, stmt):  # noqa: ANN001, ARG002
        raise AssertionError("must not touch the database when the request is rejected upfront")

    async def commit(self) -> None:
        raise AssertionError("must not commit when the request is rejected upfront")

    async def rollback(self) -> None:
        raise AssertionError("must not roll back when the request was never applied")


@pytest.mark.asyncio
async def test_set_compliance_settings_rejects_chain_enabled_with_finite_retention() -> None:
    session = _NeverCalledSession()
    with pytest.raises(ChainPurgeMutualExclusivityError) as exc_info:
        await set_compliance_settings(
            session,  # type: ignore[arg-type]
            audit_retention_days=30,
            log_prompt_retention_days=30,
            access_schedule_timezone="UTC",
            chain_enabled=True,
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.code == "chain_purge_mutually_exclusive"


@pytest.mark.asyncio
async def test_set_compliance_settings_allows_finite_retention_when_chain_explicitly_disabled() -> None:
    """A well-formed request (chain_enabled=False, finite retention) must
    reach the database - so this test expects an AssertionError from the
    stub's `execute` (proving the guard did NOT reject it), not the
    mutual-exclusivity error."""
    session = _NeverCalledSession()
    with pytest.raises(AssertionError, match="must not touch the database"):
        await set_compliance_settings(
            session,  # type: ignore[arg-type]
            audit_retention_days=30,
            log_prompt_retention_days=30,
            access_schedule_timezone="UTC",
            chain_enabled=False,
        )


@pytest.mark.asyncio
async def test_set_chain_enabled_rejects_when_current_retention_is_finite() -> None:
    """`set_chain_enabled(enabled=True)` reads the org's CURRENT
    `audit_retention_days` first (one `execute` call) - a fake session
    reporting a finite current retention must reject before any further
    write."""

    class _CurrentRetentionRow:
        audit_retention_days = 30
        log_prompt_retention_days = 30
        access_schedule_timezone = "UTC"
        chain_enabled = False

    class _FakeResult:
        def scalar_one_or_none(self):
            return _CurrentRetentionRow()

    class _ReadOnlySession:
        async def execute(self, stmt):  # noqa: ANN001, ARG002
            return _FakeResult()

        async def commit(self) -> None:
            raise AssertionError("must not commit when the request is rejected")

    with pytest.raises(ChainPurgeMutualExclusivityError):
        await set_chain_enabled(_ReadOnlySession(), enabled=True)  # type: ignore[arg-type]
