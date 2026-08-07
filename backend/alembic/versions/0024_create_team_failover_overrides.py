"""create team_failover_overrides table

Phase 4 (Reliability & Cost Efficiency), DB-2. See
`docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.3 for
the full design rationale and
`gatekey.db.models.team_failover_override.TeamFailoverOverride` for the ORM
side. This migration is the source of truth for actual DDL.

`team_id` is the primary key - at most one override row per team, same
`team_id`-as-PK shape as `team_dlp_action_overrides`/`team_model_policies`.
`failover_disabled` can only ever *disable* the org/key-level
`provider_keys.failover_enabled` default (design doc section 1.3) - there is
structurally no "enable" value, so no write-time narrowing check is needed
against this column the way `residency_rules`/`team_model_policies` need one.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-05

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0024"
down_revision: Union[str, None] = "0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "team_failover_overrides",
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "failover_disabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("team_failover_overrides")
