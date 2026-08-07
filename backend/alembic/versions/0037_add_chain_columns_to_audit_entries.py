"""add chain_hash/prev_hash/chain_seq columns to audit_entries

Phase 5 (Differentiators), 5.2 Hash-Chained Audit Ledger. See
`gatekey.db.models.audit_entry.AuditEntry` for the ORM side and
`gatekey/phase-5-technical-design.md` section 4.1 for the full design
rationale. This migration is the source of truth for actual DDL.

Additive only, per `audit_entry.py`'s own forward-compat docstring note
("the hash-chained ledger adds `chain_hash`/`prev_hash` columns to this
same table as an additive migration - do not reshape this table for it") -
no existing column is touched, no existing row's meaning changes.

All three new columns are nullable at the schema level:
- `chain_hash`/`prev_hash` are `NULL` for every row written while
  `compliance_settings.chain_enabled` is `false` (the default) - the
  overwhelming majority of pre-Phase-5 and non-adopting-org rows.
- `prev_hash` is additionally `NULL` at true chain genesis even for a
  chain-enabled org (the first-ever chained row for that org has no
  predecessor to reference) - see design doc section 2.1.
- `chain_seq` is `NULL` for every unchained row; once assigned (backend-
  developer's `write_audit_entry`/backfill logic), it is a per-org
  monotonic sequence starting at 1.

The partial unique index enforces "at most one row per `(org_id,
chain_seq)` value, among rows that participate in the chain" without
constraining the (majority) unchained rows, which all share `chain_seq =
NULL` and would otherwise collide under a non-partial unique index (`NULL`
values ordinarily don't collide in a Postgres unique index either, but the
partial form is used anyway so the index only has to cover/size the
chained subset, not the whole table). The second, non-unique partial index
supports the "read the current tail" query
(`ORDER BY chain_seq DESC LIMIT 1 WHERE org_id = :org_id`) `write_audit_
entry` performs on every chained write (design doc section 2.1) - a plain
ascending index would still work for equality lookups but not for an
efficient descending scan, hence the explicit `DESC` index.

Downgrade is fully reversible and non-data-dependent: no other table
references these columns by FK, so dropping the two indexes then the three
columns is safe regardless of row count. Downgrading a chain-enabled org
that has real `chain_hash`/`prev_hash`/`chain_seq` data loses that data
irreversibly (expected for a column drop) - this is a schema rollback, not
a data-preserving operation.

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-06

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0037"
down_revision: Union[str, None] = "0036"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("audit_entries", sa.Column("chain_hash", sa.Text(), nullable=True))
    op.add_column("audit_entries", sa.Column("prev_hash", sa.Text(), nullable=True))
    op.add_column("audit_entries", sa.Column("chain_seq", sa.BigInteger(), nullable=True))

    op.create_index(
        "uq_audit_entries_org_id_chain_seq",
        "audit_entries",
        ["org_id", "chain_seq"],
        unique=True,
        postgresql_where=sa.text("chain_seq IS NOT NULL"),
    )
    op.create_index(
        "ix_audit_entries_org_id_chain_seq_desc",
        "audit_entries",
        ["org_id", sa.text("chain_seq DESC")],
        unique=False,
        postgresql_where=sa.text("chain_seq IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_audit_entries_org_id_chain_seq_desc", table_name="audit_entries")
    op.drop_index("uq_audit_entries_org_id_chain_seq", table_name="audit_entries")
    op.drop_column("audit_entries", "chain_seq")
    op.drop_column("audit_entries", "prev_hash")
    op.drop_column("audit_entries", "chain_hash")
