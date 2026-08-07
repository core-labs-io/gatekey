"""Unit tests for `services/scheduler.py`'s purge-job behavior (Phase 3,
AC1.6/AC1.7, AC6.2) with a fake session - no DB.

The atomic claim-and-advance rotation-firing path (`run_due_rotations`)
needs a real Postgres row lock to exercise meaningfully (the whole point is
concurrent-worker behavior) - covered by QA's integration pass, not here.
"""

from __future__ import annotations

import pytest

from gatekey.services.scheduler import run_audit_purge_if_due, run_log_prompt_purge_if_due


class _FakeResult:
    def __init__(self, value, rowcount: int = 0):
        self._value = value
        self.rowcount = rowcount

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Stands in for `AsyncSession` - `execute()` only ever needs to answer
    the `compliance_settings` SELECT for this test; a DELETE would be a bug
    (asserted via `delete_called`)."""

    def __init__(self) -> None:
        self.delete_called = False
        self.execute_count = 0

    async def execute(self, stmt):  # noqa: ANN001
        self.execute_count += 1
        # Any statement whose compiled form contains "DELETE" would mean the
        # NULL-skip guard failed to short-circuit.
        if "DELETE" in str(stmt).upper():
            self.delete_called = True
        return _FakeResult(None)  # no compliance_settings row -> ADR-2 default (NULL)

    async def commit(self) -> None:
        pass


@pytest.mark.asyncio
async def test_purge_never_fires_when_audit_retention_days_is_null() -> None:
    session = _FakeSession()
    deleted = await run_audit_purge_if_due(session)  # type: ignore[arg-type]
    assert deleted == 0
    assert session.delete_called is False
    # Exactly one query (the compliance-settings read) - returns immediately
    # per the NULL-skip guard, never proceeds to build/issue a DELETE.
    assert session.execute_count == 1


class _ChainEnabledRow:
    """A `compliance_settings` row with `chain_enabled=True` - the Phase 5
    (5.2, AC5.2.7) guard must short-circuit BEFORE the `audit_retention_days
    is None` check, regardless of what `audit_retention_days` itself holds
    (the DB `CHECK` constraint means the two are never both non-default in
    practice, but the app-layer guard must not depend on that for
    correctness - see `services/scheduler.py::run_audit_purge_if_due`)."""

    audit_retention_days = 30
    log_prompt_retention_days = 30
    access_schedule_timezone = "UTC"
    chain_enabled = True


class _ChainEnabledSession:
    def __init__(self) -> None:
        self.delete_called = False
        self.execute_count = 0

    async def execute(self, stmt):  # noqa: ANN001
        self.execute_count += 1
        if "DELETE" in str(stmt).upper():
            self.delete_called = True
        return _FakeResult(_ChainEnabledRow())

    async def commit(self) -> None:
        pass


@pytest.mark.asyncio
async def test_purge_never_fires_when_chain_enabled_even_with_finite_retention_days() -> None:
    """Phase 5 (5.2, AC5.2.7): the purge job is a no-op whenever
    `chain_enabled = true`, regardless of `audit_retention_days` - deleting
    a row structurally breaks a hash chain."""
    session = _ChainEnabledSession()
    deleted = await run_audit_purge_if_due(session)  # type: ignore[arg-type]
    assert deleted == 0
    assert session.delete_called is False
    assert session.execute_count == 1


class _FakeLogPromptSession:
    """Stands in for `AsyncSession` for `run_log_prompt_purge_if_due`.
    Unlike `audit_retention_days`, `log_prompt_retention_days` is DB-level
    `NOT NULL` (default 30) - there is no NULL-skip path to test here, this
    exercises the "always fires, one batch per table, stops once a batch
    returns fewer rows than the batch size" shape instead."""

    def __init__(self, *, usage_log_rowcounts: list[int], dlp_scan_result_rowcounts: list[int]) -> None:
        self._usage_log_rowcounts = list(usage_log_rowcounts)
        self._dlp_scan_result_rowcounts = list(dlp_scan_result_rowcounts)
        self.delete_statements: list[str] = []

    async def execute(self, stmt):  # noqa: ANN001
        compiled = str(stmt).upper()
        if "SELECT" in compiled and "COMPLIANCE_SETTINGS" in compiled:
            return _FakeResult(None)  # no row -> ADR-2 default (log_prompt_retention_days=30)
        if "DELETE" in compiled:
            self.delete_statements.append(compiled)
            if "USAGE_LOGS" in compiled:
                rowcount = self._usage_log_rowcounts.pop(0)
            else:
                rowcount = self._dlp_scan_result_rowcounts.pop(0)
            return _FakeResult(None, rowcount=rowcount)
        raise AssertionError(f"unexpected statement: {compiled}")

    async def commit(self) -> None:
        pass


@pytest.mark.asyncio
async def test_log_prompt_purge_deletes_from_both_tables_and_sums_rowcounts() -> None:
    session = _FakeLogPromptSession(usage_log_rowcounts=[3], dlp_scan_result_rowcounts=[2])
    deleted = await run_log_prompt_purge_if_due(session)  # type: ignore[arg-type]
    assert deleted == 5
    assert len(session.delete_statements) == 2
    assert any("USAGE_LOGS" in s for s in session.delete_statements)
    assert any("DLP_SCAN_RESULTS" in s for s in session.delete_statements)
