"""Pure hash-chain math + the historical backfill routine for Phase 5's
hash-chained audit ledger (5.2 - see `gatekey/phase-5-product-spec.md` AC5.2.x
and `gatekey/phase-5-technical-design.md` section 2.1).

Deliberately its own module - not folded into `services/audit.py` (the
write path) or `services/compliance_settings.py` (the enable-toggle/backfill
path): both of those need this exact hash formula and must never
independently reimplement it (that would risk write-time and backfill/
verify-time hashes silently diverging), and putting the shared formula in a
third, dependency-free module (no import of either `audit.py` or
`compliance_settings.py`) avoids a circular import between those two
(`audit.py` needs `compliance_settings.get_effective_compliance_settings`;
`compliance_settings.py` needs this module's backfill).

`build_chain_payload`/`compute_chain_hash` are pure functions - no DB, no
`async` - directly unit-testable, and also used by `verify_chain` (AC5.2.4)
to recompute-and-compare every row. `backfill_chain` and `verify_chain` are
the two functions here that touch the DB (AC5.2.6/AC5.2.4 respectively).

Deliberately does NOT know about `compliance_settings.chain_enabled` at all
(no "not configured" concept here) - whether the chain is enabled is a
`services.compliance_settings` question, and importing that module from
here would recreate the exact circular import this module's docstring
opening paragraph explains avoiding. The `not_enabled` verify-response
status (AC5.2.4/design doc section 7.1) is therefore the ADMIN ROUTER's
responsibility (`api/v1/admin/audit_chain.py`): it reads
`compliance_settings.chain_enabled` itself and only calls `verify_chain`
when chaining is actually enabled.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.db.models.audit_entry import AuditEntry

# Batches the backfill's per-round-trip payload size only - does NOT release
# the caller's row lock between batches. See `backfill_chain`'s docstring
# and design doc section 2.1's "batching bounds per-round-trip payload size,
# it does not release the lock between batches" note - releasing early would
# let a concurrent `write_audit_entry` call compute a `prev_hash` against a
# partially-backfilled tail.
_BACKFILL_BATCH_SIZE = 5000


def _source_ip_str(value: Any) -> str | None:
    """`AuditEntry.source_ip` is a Postgres `INET` - the ORM attribute comes
    back as an `ipaddress.IPv4Address`/`IPv6Address` instance (not `str`)
    once read from the database, but is a plain `str` (or `None`) at
    write-time, before the row is ever persisted. `str(...)` normalizes both
    shapes to the identical formatted string - the same coercion
    `api/v1/admin/audit_entries.py::_export_row_dict` already relies on for
    the same column."""
    return None if value is None else str(value)


def build_chain_payload(
    *,
    entry_id: uuid.UUID | str,
    org_id: uuid.UUID | str,
    actor_label: str,
    action: str,
    target_type: str,
    target_id: str,
    old_value: dict[str, Any] | None,
    new_value: dict[str, Any] | None,
    source_ip: Any,
    created_at: datetime,
) -> str:
    """AC5.2.3's canonical JSON payload - deterministic key order
    (`sort_keys=True`) and separators (no incidental whitespace), so the
    exact same logical row always serializes to the exact same bytes at
    write time, backfill time, and verify time alike."""
    payload = {
        "id": str(entry_id),
        "org_id": str(org_id),
        "actor_label": actor_label,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "old_value": old_value,
        "new_value": new_value,
        "source_ip": _source_ip_str(source_ip),
        "created_at": created_at.isoformat(),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def compute_chain_hash(prev_hash: str, **fields: Any) -> str:
    """AC5.2.3's exact formula:
    `SHA256(prev_hash_or_empty_string || canonical_json({...}))`.

    `prev_hash` is the empty string `""` at a chain's true genesis row -
    callers pass `tail.chain_hash if tail is not None else ""`, never
    `None` (unlike the STORED `prev_hash` *column*, which is `NULL` at
    genesis - see `AuditEntry.prev_hash`'s docstring; `NULL` and `""` are
    deliberately different things here). `**fields` are
    `build_chain_payload`'s keyword arguments.
    """
    canonical = build_chain_payload(**fields)
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


async def backfill_chain(session: AsyncSession, org_id: uuid.UUID) -> int:
    """AC5.2.6: compute `chain_seq`/`prev_hash`/`chain_hash` for EVERY
    existing `audit_entries` row belonging to `org_id`, ordered by
    `(created_at, id)` (deterministic tie-break) - true historical genesis
    at the org's actual first-ever row, not a fresh genesis at enable-time.

    Batched (`_BACKFILL_BATCH_SIZE` rows/round-trip, keyset-paginated over
    `(created_at, id)`) to bound per-round-trip payload size only - flushes
    each batch but never commits. The caller
    (`services.compliance_settings._apply_compliance_settings`) holds the
    `compliance_settings` row lock for this function's ENTIRE duration and
    commits once, after this returns, exactly as design doc section 2.1
    requires. This is therefore a genuinely long-running, lock-holding
    operation for a large pre-existing table - a known, explicitly
    documented v1 trade-off (design doc sections 6.3/11/12: "Hash-chain
    backfill is synchronous and lock-holding... no background-job infra for
    a resumable backfill in this phase"), not an oversight.

    Returns the number of rows backfilled (0 for a freshly-provisioned org
    with no pre-existing audit history - the common case).
    """
    tail_hash: str | None = None
    chain_seq = 0
    cursor: tuple[datetime, uuid.UUID] | None = None
    while True:
        stmt = select(AuditEntry).where(AuditEntry.org_id == org_id)
        if cursor is not None:
            cursor_created_at, cursor_id = cursor
            stmt = stmt.where(
                or_(
                    AuditEntry.created_at > cursor_created_at,
                    and_(
                        AuditEntry.created_at == cursor_created_at,
                        AuditEntry.id > cursor_id,
                    ),
                )
            )
        stmt = stmt.order_by(AuditEntry.created_at.asc(), AuditEntry.id.asc()).limit(
            _BACKFILL_BATCH_SIZE
        )
        rows = (await session.execute(stmt)).scalars().all()
        if not rows:
            break
        for row in rows:
            chain_seq += 1
            prev_hash_for_hash = tail_hash if tail_hash is not None else ""
            new_hash = compute_chain_hash(
                prev_hash_for_hash,
                entry_id=row.id,
                org_id=row.org_id,
                actor_label=row.actor_label,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                old_value=row.old_value,
                new_value=row.new_value,
                source_ip=row.source_ip,
                created_at=row.created_at,
            )
            row.chain_seq = chain_seq
            row.prev_hash = tail_hash
            row.chain_hash = new_hash
            tail_hash = new_hash
        cursor = (rows[-1].created_at, rows[-1].id)
        await session.flush()
        if len(rows) < _BACKFILL_BATCH_SIZE:
            break
    return chain_seq


@dataclass(frozen=True)
class ChainVerificationResult:
    """AC5.2.4's verify response shape, pre-serialization. `status="broken"`
    always populates the four `broken_at_*`/`*_prev_hash` fields; `status=
    "intact"` only ever populates `entries_verified`."""

    status: Literal["intact", "broken"]
    entries_verified: int = 0
    broken_at_entry_id: str | None = None
    broken_at_chain_seq: int | None = None
    expected_prev_hash: str | None = None
    actual_prev_hash: str | None = None


async def verify_chain(session: AsyncSession, org_id: uuid.UUID) -> ChainVerificationResult:
    """AC5.2.4: walk every chained row for `org_id` in `chain_seq` order,
    recompute each `chain_hash` from its own stored fields and compare
    against the stored value, and separately compare each row's stored
    `prev_hash` against the PRIOR row's stored `chain_hash` - two distinct
    checks (design doc section 2.1's verify description), either of which
    can independently catch a tamper: a row's own content fields being
    altered (caught by the first, recomputation, check even if that row's
    `prev_hash` pointer itself was left untouched) vs. the chain's linkage
    itself being broken (caught by the second, even if a row's own content/
    hash pair is internally self-consistent).

    Callers are responsible for the "hash chain not enabled at all" case
    (AC5.2.4/design doc section 7.1's `not_enabled` status) - see module
    docstring. An org with chaining enabled but zero chained rows yet
    (freshly enabled, no audit activity since) returns `status="intact"`,
    `entries_verified=0` - vacuously true, never a false negative.
    """
    rows = (
        (
            await session.execute(
                select(AuditEntry)
                .where(AuditEntry.org_id == org_id, AuditEntry.chain_seq.is_not(None))
                .order_by(AuditEntry.chain_seq.asc())
            )
        )
        .scalars()
        .all()
    )

    prev_stored_hash: str | None = None
    verified = 0
    for row in rows:
        prev_hash_for_hash = row.prev_hash if row.prev_hash is not None else ""
        recomputed = compute_chain_hash(
            prev_hash_for_hash,
            entry_id=row.id,
            org_id=row.org_id,
            actor_label=row.actor_label,
            action=row.action,
            target_type=row.target_type,
            target_id=row.target_id,
            old_value=row.old_value,
            new_value=row.new_value,
            source_ip=row.source_ip,
            created_at=row.created_at,
        )
        if recomputed != row.chain_hash or row.prev_hash != prev_stored_hash:
            return ChainVerificationResult(
                status="broken",
                broken_at_entry_id=str(row.id),
                broken_at_chain_seq=row.chain_seq,
                expected_prev_hash=prev_stored_hash or "",
                actual_prev_hash=row.prev_hash or "",
            )
        prev_stored_hash = row.chain_hash
        verified += 1
    return ChainVerificationResult(status="intact", entries_verified=verified)
