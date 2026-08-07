"""create degradation_events table

Phase 4 (Reliability & Cost Efficiency) schema/code-drift fix. See
`gatekey.db.models.degradation_event.DegradationEvent` for the ORM side.
That model was never imported by `db/models/__init__.py` (so it sat outside
`Base.metadata` entirely) and no migration ever created this table -
`services.degradation.DegradationEventLogger.log_degradation` additionally
had its own broken, non-`Base`-derived duplicate `DegradationEvent` class
used with `sqlalchemy.insert()`, which is not a valid SQLAlchemy insertable
and would raise at runtime. Both the model registration and the service-
layer duplicate were fixed alongside this migration (see
`db/models/__init__.py` and `services/degradation.py`) - this migration is
the schema half of that three-part fix.

The model's `request_id` FK was also corrected as part of this same fix: it
previously pointed at `request_logs.id`, a table that has never existed in
this codebase (Phase 1's actual persisted per-request record is
`usage_logs`, not `request_logs`). This migration's DDL reflects the
corrected FK target.

`(team_id, created_at)` index is for AC4.5's dashboard "cost saved via
degradation, over a selectable time range, per team" aggregation queries -
same shape as `usage_logs.ix_usage_logs_org_id_created_at`.

`original_cost`/`degraded_cost` are `NUMERIC(12, 4)`, matching the ORM
model exactly. Note this is a narrower precision than `usage_logs.cost_usd`'s
`NUMERIC(20, 10)` (see that table's Phase 2 "Cost normalization" docstring) -
this migration does not change that, since widening it is a schema decision
for whoever owns cost-normalization precision, not something to silently
fix as a side effect of just making this table exist.

No `updated_at` column - not part of the ORM model's column list (this is
an append-only event-history table, like `failover_events`/
`rate_limit_rejection_events`/`cache_lookup_events`).

Downgrade is fully reversible and non-data-dependent: nothing else
references this table by FK, so dropping the index then the table is safe
regardless of row count.

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-05

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0032"
down_revision: Union[str, None] = "0031"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "degradation_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # See module docstring "request_id FK target correction" -
        # `usage_logs`, not the nonexistent `request_logs`.
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("usage_logs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("original_model", sa.Text(), nullable=False),
        sa.Column("degraded_model", sa.Text(), nullable=False),
        sa.Column("original_cost", sa.Numeric(12, 4), nullable=False),
        sa.Column("degraded_cost", sa.Numeric(12, 4), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_degradation_events_team_id_created_at",
        "degradation_events",
        ["team_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_degradation_events_team_id_created_at", table_name="degradation_events"
    )
    op.drop_table("degradation_events")
