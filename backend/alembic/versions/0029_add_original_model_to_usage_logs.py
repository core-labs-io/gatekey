"""add original_model to usage_logs

Phase 4 (Reliability & Cost Efficiency), DB-7. See
`docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.8 for
the full design rationale and `gatekey.db.models.usage_log.UsageLog` for the
ORM side. This migration is the source of truth for actual DDL.

`NULL` on every non-degraded request (the overwhelming majority); populated
with the originally-requested model only when `check_degradation`
substituted a different one (AC4.7). `model` (existing column) always holds
the model actually used/charged. This is also what the dashboard's "cost
saved via degradation" aggregation queries against - no new table needed for
degradation history.

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-05

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0029"
down_revision: Union[str, None] = "0028"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("usage_logs", sa.Column("original_model", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("usage_logs", "original_model")
