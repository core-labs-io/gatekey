"""`AuditEntry` - the plain, append-only governance audit trail (Phase 2 -
Multi-Tenant Governance).

See `docs/design/phase-2-multi-tenant-governance-design.md` section 1.10 for
the full design rationale.

Append-only (AC4.2): service-layer code (`services.audit.write_audit_entry`)
only ever INSERTs rows - never UPDATE, never DELETE. Nothing in normal
operation mutates or removes a row once written.

**The one sanctioned exception (Phase 3, AC1.6/AC1.7)**: the only DELETE
ever issued against this table is the config-driven, scheduled purge job
`services.scheduler.run_audit_purge_if_due`, and only when an org has
explicitly configured a finite `compliance_settings.audit_retention_days`
(`NULL`, the default, means the purge job never fires for that org - see
`db/models/compliance_settings.py`). This purge is never reachable via any
mutating API endpoint an admin or auditor can invoke directly - it only
runs from the scheduler loop. See
`docs/design/phase-3-security-compliance-design.md` section 7.3 for the
full rationale; Phase 5's hash-chained ledger will need to revisit this
exception (deleting a row breaks a hash chain unless the purge is made
chain-aware), per that design doc's section 12 forward-looking note.

Snapshots, not live joins: `actor_label` captures the actor's name/email at
write time (or the `"system:admin_token"` break-glass sentinel, A4), so a
later rename/delete of the acting user never rewrites history -
`actor_user_id` is `SET NULL` for the same reason. `target_id` is text, not
a typed FK: `target_type` varies row to row (a genuinely polymorphic
reference), and this table deliberately never blocks deletion of anything
it references.

Forward-compat (Phase 5): the hash-chained ledger adds
`chain_hash`/`prev_hash` columns to this same table as an additive
migration - do not reshape this table for it, and do not add those columns
early.

Hash-chain columns (Phase 5 - Differentiators, 5.2)
----------------------------------------------------
`chain_hash`/`prev_hash`/`chain_seq` are additive-only, per the note above -
see `alembic/versions/0037_add_chain_columns_to_audit_entries.py` for the
DDL and `gatekey/phase-5-technical-design.md` section 2.1 for the write-path
design (backend-developer task). All three are `NULL` on every row written
while `compliance_settings.chain_enabled` is `false` (the default) - byte-
for-byte the pre-Phase-5 shape for any org that never enables chaining.
`chain_seq` is a per-`org_id` monotonic sequence (1-based) once assigned;
`prev_hash` is `NULL` only at a chain's true genesis row for that org.
`chain_hash` is computed by `services.audit.write_audit_entry` as
`SHA256(prev_hash_for_hash + canonical_json({id, org_id, actor_label,
action, target_type, target_id, old_value, new_value, source_ip,
created_at}))` - this model does not compute or validate the hash itself,
it only persists it.

`source_ip` (Phase 3 - Security & Compliance Hardening)
----------------------------------------------------------
Native Postgres `INET`, nullable - AC1.2's best-effort contract: an audit
write must never fail because a source IP genuinely isn't available (e.g.
an internal service call with no request context). See
`docs/design/phase-3-security-compliance-design.md` section 1.8/7.1.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0012_create_audit_entries.py`,
`alembic/versions/0017_add_source_ip_to_audit_entries.py`, and
`alembic/versions/0037_add_chain_columns_to_audit_entries.py` - those
migrations, not `Base.metadata.create_all()`, are the source of truth for
actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org


class AuditEntry(Base):
    __tablename__ = "audit_entries"
    # See module docstring "Migration ownership" - must match `0012` exactly.
    __table_args__ = (
        Index("ix_audit_entries_org_id_created_at", "org_id", "created_at"),
        Index("ix_audit_entries_actor_user_id", "actor_user_id"),
        Index("ix_audit_entries_action", "action"),
        # Phase 5 hash-chain indexes - see module docstring "Hash-chain
        # columns" and `0037_add_chain_columns_to_audit_entries.py`. Both
        # partial (only cover chained rows, `chain_seq IS NOT NULL`).
        Index(
            "uq_audit_entries_org_id_chain_seq",
            "org_id",
            "chain_seq",
            unique=True,
            postgresql_where=text("chain_seq IS NOT NULL"),
        ),
        Index(
            "ix_audit_entries_org_id_chain_seq_desc",
            "org_id",
            text("chain_seq DESC"),
            postgresql_where=text("chain_seq IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    # NULL for break-glass admin-token actions (A4) or after the acting
    # user's deletion - `actor_label` is the durable record.
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Name/email snapshot, or the "system:admin_token" sentinel - see module
    # docstring.
    actor_label: Mapped[str] = mapped_column(String, nullable=False)
    # Fixed vocabulary - see the design doc section 5's action-type table.
    action: Mapped[str] = mapped_column(String, nullable=False)
    target_type: Mapped[str] = mapped_column(String, nullable=False)
    # Stringified id; deliberately not a typed/polymorphic FK - see module
    # docstring.
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    old_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Phase 3 - best-effort, never blocks the write. See module docstring.
    source_ip: Mapped[str | None] = mapped_column(INET, nullable=True)

    # Phase 5 - hash-chain columns. See module docstring "Hash-chain
    # columns". NULL/NULL/NULL for every row written while
    # `compliance_settings.chain_enabled` is false (the default).
    chain_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    prev_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    chain_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    org: Mapped["Org"] = relationship("Org")
