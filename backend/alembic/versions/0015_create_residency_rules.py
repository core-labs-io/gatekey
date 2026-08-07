"""create residency_rules table plus the residency_violation_behavior enum

Phase 3 (Security & Compliance Hardening), DB-2. See
`docs/design/phase-3-security-compliance-design.md` section 1.6 for the full
design rationale and `gatekey.db.models.residency_rule.ResidencyRule` for the
ORM side. This migration is the source of truth for actual DDL.

At most one rule per scope (org-wide, or per team) is a schema-level
invariant via the two partial unique indexes below - same "let the schema
guarantee the one-row-per-scope invariant" philosophy as `ModelPolicy`/
`TeamModelPolicy`, rather than an app-level pre-check-then-insert.
`violation_behavior` defaults to `hard_block` at the column level (AC3.2 -
the create path cannot silently default to `warn`).

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RESIDENCY_VIOLATION_BEHAVIOR_VALUES = ("hard_block", "warn")


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(
        *RESIDENCY_VIOLATION_BEHAVIOR_VALUES,
        name="residency_violation_behavior",
        create_type=False,
    ).create(bind, checkfirst=True)

    op.create_table(
        "residency_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # NULL = org-wide rule.
        sa.Column(
            "scope_team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="CASCADE"),
            nullable=True,
        ),
        # string[] drawn from `services.residency.SUPPORTED_REGIONS`.
        sa.Column("allowed_regions", postgresql.JSONB(), nullable=False),
        sa.Column(
            "violation_behavior",
            postgresql.ENUM(
                *RESIDENCY_VIOLATION_BEHAVIOR_VALUES,
                name="residency_violation_behavior",
                create_type=False,
            ),
            nullable=False,
            server_default=sa.text("'hard_block'"),
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
    op.create_index(
        "uq_residency_rules_org_wide",
        "residency_rules",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("scope_team_id IS NULL"),
    )
    op.create_index(
        "uq_residency_rules_team_scoped",
        "residency_rules",
        ["scope_team_id"],
        unique=True,
        postgresql_where=sa.text("scope_team_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_residency_rules_team_scoped", table_name="residency_rules")
    op.drop_index("uq_residency_rules_org_wide", table_name="residency_rules")
    op.drop_table("residency_rules")

    bind = op.get_bind()
    postgresql.ENUM(
        *RESIDENCY_VIOLATION_BEHAVIOR_VALUES, name="residency_violation_behavior"
    ).drop(bind, checkfirst=True)
