"""create failover_events table

Phase 4 (Reliability & Cost Efficiency), DB-3. See
`docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.4 for
the full design rationale and `gatekey.db.models.failover_event.FailoverEvent`
for the ORM side. This migration is the source of truth for actual DDL.
Depends on `0023`'s `provider_keys` table existing for its two FKs.

`ON DELETE SET NULL` (not `CASCADE`) on both `from_provider_key_id`/
`to_provider_key_id` - a failover event is history that must survive a later
key deletion, same "never lose history" posture as `audit_entries.target_id`.
`detected_at`/`switched_at` are stored as two timestamps, not a precomputed
duration - the admin API computes detection-to-switch duration at read time
(design doc section 1.4, "compute, don't store, when it's cheap to compute").

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-05

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0025"
down_revision: Union[str, None] = "0024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "failover_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "from_provider_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_keys.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "to_provider_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_keys.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("request_id", sa.Text(), nullable=False),
        # When the primary's failing call returned.
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        # When the backup call succeeded.
        sa.Column("switched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_failover_events_org_id_created_at", "failover_events", ["org_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_failover_events_org_id_created_at", table_name="failover_events")
    op.drop_table("failover_events")
