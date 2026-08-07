"""Effective compliance-settings read/write helpers (Phase 3, BD-10; Phase 5
5.2 Hash-Chained Audit Ledger extends this with `chain_enabled`).

Mirrors `services.org_settings`'s ADR-2 shape exactly - `compliance_settings`
is a single-row-per-org table, absence of a row = the default state (no
audit purge, 30-day usage/prompt retention, UTC access-schedule timezone,
chain disabled). See `db/models/compliance_settings.py` and
`docs/design/phase-3-security-compliance-design.md` section 1.2/9.1.

`services.scheduler.run_audit_purge_if_due` and `services.rotation.
compute_next_rotation`'s callers read the effective settings via
`get_effective_compliance_settings` - never a raw `ComplianceSettings`
query duplicated elsewhere.

Phase 5 (5.2) - chain_enabled / AC5.2.6 backfill / AC5.2.7 mutual exclusivity
------------------------------------------------------------------------------
See `gatekey/phase-5-technical-design.md` section 2.1. Every WRITE to this
table (`set_compliance_settings`, `set_chain_enabled`) now funnels through
one private, lock-holding, atomic function (`_apply_compliance_settings`) so
there is exactly one place that (a) validates AC5.2.7's mutual exclusivity
against the REQUESTED final state - never a partially-applied intermediate
one, which would risk leaving a committed-but-inconsistent row if a caller
tried to change `audit_retention_days` and `chain_enabled` in two separate
transactions - and (b) runs the AC5.2.6 historical backfill, inside the same
transaction as the lock and the final commit, exactly once, the first time
`chain_enabled` transitions `false -> true`. `write_audit_entry`
(`services/audit.py`) takes the identical `SELECT ... FOR UPDATE` lock on
this table's row before computing a chained hash - see that module's
docstring for why this specific row (not a new lock primitive) is safe to
share between the two code paths.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.compliance_settings import ComplianceSettings
from gatekey.errors import ChainPurgeMutualExclusivityError
from gatekey.services.audit_chain import backfill_chain

DEFAULT_LOG_PROMPT_RETENTION_DAYS = 30
DEFAULT_ACCESS_SCHEDULE_TIMEZONE = "UTC"

_CHAIN_ENABLE_REJECTED_REASON = (
    "Cannot enable the hash chain while a finite audit_retention_days purge "
    "window is configured - disable the purge window first (AC5.2.7)."
)
_RETENTION_SET_REJECTED_REASON = (
    "Cannot set a finite audit_retention_days while the hash chain is "
    "enabled - disable the hash chain first (AC5.2.7)."
)


@dataclass(frozen=True)
class EffectiveComplianceSettings:
    """The org's compliance settings with absence-of-row defaults applied."""

    audit_retention_days: int | None
    log_prompt_retention_days: int
    access_schedule_timezone: str
    chain_enabled: bool


_DEFAULTS = EffectiveComplianceSettings(
    audit_retention_days=None,
    log_prompt_retention_days=DEFAULT_LOG_PROMPT_RETENTION_DAYS,
    access_schedule_timezone=DEFAULT_ACCESS_SCHEDULE_TIMEZONE,
    chain_enabled=False,
)


async def get_effective_compliance_settings(
    session: AsyncSession,
) -> EffectiveComplianceSettings:
    """Return the org's compliance-settings row, or the ADR-2 defaults if
    none exists."""
    row = (
        await session.execute(
            select(ComplianceSettings).where(ComplianceSettings.org_id == DEFAULT_ORG_ID)
        )
    ).scalar_one_or_none()
    if row is None:
        return _DEFAULTS
    return EffectiveComplianceSettings(
        audit_retention_days=row.audit_retention_days,
        log_prompt_retention_days=row.log_prompt_retention_days,
        access_schedule_timezone=row.access_schedule_timezone,
        chain_enabled=row.chain_enabled,
    )


async def _lock_or_create_compliance_settings(
    session: AsyncSession, org_id: uuid.UUID
) -> ComplianceSettings:
    """Ensure a `compliance_settings` row exists for `org_id`, then
    `SELECT ... FOR UPDATE` it - the ADR-5-style lock this codebase already
    uses (`services/team_budget.py::_lock_team`), applied to the "guaranteed
    to exist once chain_enabled can ever be true" parent config row (design
    doc section 2.1). The insert-if-absent step is what makes that guarantee
    hold even before this org's very first settings write."""
    insert_stmt = postgresql.insert(ComplianceSettings).values(org_id=org_id).on_conflict_do_nothing(
        index_elements=[ComplianceSettings.org_id]
    )
    await session.execute(insert_stmt)
    return (
        await session.execute(
            select(ComplianceSettings).where(ComplianceSettings.org_id == org_id).with_for_update()
        )
    ).scalar_one()


async def _apply_compliance_settings(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    audit_retention_days: int | None,
    log_prompt_retention_days: int,
    access_schedule_timezone: str,
    chain_enabled: bool,
    reason_for_conflict: str,
) -> ComplianceSettings:
    """The one atomic, locked mutation both `set_compliance_settings` and
    `set_chain_enabled` funnel through - see module docstring. Commits.

    `reason_for_conflict` lets the two public callers surface a message
    naming which direction of AC5.2.7's mutual exclusivity was violated
    (attempting to enable chaining vs. attempting to set a finite retention
    window), even though both ultimately hit this same shared guard."""
    if chain_enabled and audit_retention_days is not None:
        raise ChainPurgeMutualExclusivityError(reason_for_conflict)

    row = await _lock_or_create_compliance_settings(session, org_id)
    if chain_enabled and not row.chain_enabled:
        # AC5.2.6 - full historical backfill, inside this same locked
        # transaction, exactly once, on the false->true transition.
        await backfill_chain(session, org_id)

    row.audit_retention_days = audit_retention_days
    row.log_prompt_retention_days = log_prompt_retention_days
    row.access_schedule_timezone = access_schedule_timezone
    row.chain_enabled = chain_enabled
    try:
        await session.commit()
    except IntegrityError:
        # Defense-in-depth backstop against the `chk_chain_purge_mutually_
        # exclusive` CHECK (migration 0038) - the app-layer check above
        # should always catch this first, but a client must never see a
        # raw IntegrityError/500 either way.
        await session.rollback()
        raise ChainPurgeMutualExclusivityError(reason_for_conflict) from None
    return row


async def set_compliance_settings(
    session: AsyncSession,
    *,
    audit_retention_days: int | None,
    log_prompt_retention_days: int,
    access_schedule_timezone: str,
    chain_enabled: bool | None = None,
) -> ComplianceSettings:
    """Full-replace write for compliance settings (Phase 3's original three
    fields; Phase 5 adds `chain_enabled`).

    `chain_enabled=None` (the default) preserves the org's current value
    unchanged - every pre-Phase-5 caller that doesn't know about chaining
    keeps working unmodified. AC5.2.6's full historical backfill runs
    automatically, inside this same call, the first time `chain_enabled`
    transitions `false -> true`. Raises `ChainPurgeMutualExclusivityError`
    (422) rather than silently violating AC5.2.7."""
    if chain_enabled is None:
        current = await get_effective_compliance_settings(session)
        chain_enabled = current.chain_enabled
    return await _apply_compliance_settings(
        session,
        org_id=DEFAULT_ORG_ID,
        audit_retention_days=audit_retention_days,
        log_prompt_retention_days=log_prompt_retention_days,
        access_schedule_timezone=access_schedule_timezone,
        chain_enabled=chain_enabled,
        reason_for_conflict=_RETENTION_SET_REJECTED_REASON,
    )


async def set_chain_enabled(session: AsyncSession, *, enabled: bool) -> ComplianceSettings:
    """Toggle ONLY `chain_enabled`, preserving the org's existing retention/
    timezone settings - a convenience entrypoint (design doc section 2.1's
    "`services/compliance_settings.py` gains `set_chain_enabled`") for
    callers that only want to flip the chain toggle without resupplying the
    other three fields. Same atomic lock/backfill/commit contract as
    `set_compliance_settings` (they share `_apply_compliance_settings`)."""
    current = await get_effective_compliance_settings(session)
    return await _apply_compliance_settings(
        session,
        org_id=DEFAULT_ORG_ID,
        audit_retention_days=current.audit_retention_days,
        log_prompt_retention_days=current.log_prompt_retention_days,
        access_schedule_timezone=current.access_schedule_timezone,
        chain_enabled=enabled,
        reason_for_conflict=_CHAIN_ENABLE_REJECTED_REASON,
    )
