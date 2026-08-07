"""create degradation_policies table plus the degradation_scope_type enum

Phase 4 (Reliability & Cost Efficiency), DB-6. See
`docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.7 for
the full design rationale and
`gatekey.db.models.degradation_policy.DegradationPolicy` for the ORM side.
This migration is the source of truth for actual DDL.

Fifth application of the same one-row-per-scope partial-unique-index pattern
as `residency_rules`/`rotation_policies`/`access_schedules`/
`rate_limit_rules`. The `CHECK` constraint enforces `scope_team_id` is set
iff `scope_type = 'team'`. `downgrade_target_model` is validated as a
`MODEL_REGISTRY` key at the service-layer write time (mirrors
`ModelPolicy.models`) - not a DB constraint, since `MODEL_REGISTRY` is an
in-process registry, not a DB table.

No `created_at`/`updated_at` columns - not part of the design doc's column
list for this table (same precedent `team_dlp_action_overrides`, migration
`0014`, already establishes for a table the design doc lists without them).

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-05

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0028"
down_revision: Union[str, None] = "0027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEGRADATION_SCOPE_TYPE_VALUES = ("org", "team")


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(
        *DEGRADATION_SCOPE_TYPE_VALUES, name="degradation_scope_type", create_type=False
    ).create(bind, checkfirst=True)

    op.create_table(
        "degradation_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scope_type",
            postgresql.ENUM(
                *DEGRADATION_SCOPE_TYPE_VALUES, name="degradation_scope_type", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "scope_team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "threshold_pct_of_budget",
            sa.Numeric(5, 2),
            nullable=False,
            server_default=sa.text("10.0"),
        ),
        # Validated as a MODEL_REGISTRY key at write time - see module
        # docstring.
        sa.Column("downgrade_target_model", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "(scope_type = 'org' AND scope_team_id IS NULL) OR "
            "(scope_type = 'team' AND scope_team_id IS NOT NULL)",
            name="ck_degradation_policies_scope_type_matches_scope_team_id",
        ),
    )
    op.create_index(
        "uq_degradation_policies_org",
        "degradation_policies",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'org'"),
    )
    op.create_index(
        "uq_degradation_policies_team",
        "degradation_policies",
        ["scope_team_id"],
        unique=True,
        postgresql_where=sa.text("scope_team_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_degradation_policies_team", table_name="degradation_policies")
    op.drop_index("uq_degradation_policies_org", table_name="degradation_policies")
    op.drop_table("degradation_policies")

    bind = op.get_bind()
    postgresql.ENUM(*DEGRADATION_SCOPE_TYPE_VALUES, name="degradation_scope_type").drop(
        bind, checkfirst=True
    )
