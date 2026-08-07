"""create audit_entries table

Phase 2 (Multi-Tenant Governance), DB-6. See
`docs/design/phase-2-multi-tenant-governance-design.md` section 1.10 for the
full design rationale and `gatekey.db.models.audit_entry.AuditEntry` for the
ORM side. This migration is the source of truth for actual DDL.

Plain, append-only (AC4.2): service-layer code only ever INSERTs here, never
UPDATE/DELETE. `actor_label` is a snapshot (never a live join to
`users.name`); `actor_user_id` is `SET NULL` so history survives deletion of
the acting user. `target_id` is text, not a typed FK - `target_type` varies
row to row (a genuinely polymorphic reference), and this table must never
block deletion of anything it references.

Forward-compat: Phase 5's hash-chained ledger adds `chain_hash`/`prev_hash`
columns to this same table as an additive migration - nothing here needs
reshaping for that.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "audit_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # NULL for break-glass admin-token actions (A4) or after the acting
        # user's deletion (SET NULL) - `actor_label` below is the durable
        # record of who acted.
        sa.Column(
            "actor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Name/email snapshot, or the "system:admin_token" sentinel.
        sa.Column("actor_label", sa.String(), nullable=False),
        # Fixed vocabulary - see design doc section 5's action-type table.
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        # Stringified id; deliberately not a typed/polymorphic FK.
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("old_value", postgresql.JSONB(), nullable=True),
        sa.Column("new_value", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_audit_entries_org_id_created_at", "audit_entries", ["org_id", "created_at"])
    op.create_index("ix_audit_entries_actor_user_id", "audit_entries", ["actor_user_id"])
    op.create_index("ix_audit_entries_action", "audit_entries", ["action"])


def downgrade() -> None:
    op.drop_index("ix_audit_entries_action", table_name="audit_entries")
    op.drop_index("ix_audit_entries_actor_user_id", table_name="audit_entries")
    op.drop_index("ix_audit_entries_org_id_created_at", table_name="audit_entries")
    op.drop_table("audit_entries")
