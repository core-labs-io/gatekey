"""create team_memberships table

Phase 2 (Multi-Tenant Governance), DB-2. See
`docs/design/phase-2-multi-tenant-governance-design.md` section 1.4 for the
full design rationale and `gatekey.db.models.team_membership.TeamMembership`
for the ORM side. This migration is the source of truth for actual DDL.

One row per (team, user) pair - `UNIQUE (team_id, user_id)`. `budget_usd` is
the per-(user, team) spend cutoff (A6: the counter every new personal key and
team-attributed service-account key charges against); NULL = unmetered for
that pair. Removal is a hard row delete (history lives in `audit_entries`),
and `ON DELETE CASCADE` on both FKs: deleting the team or the underlying
user removes their memberships.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Created by 0007 - referenced here with create_type=False so this migration
# never attempts a second CREATE TYPE.
team_role_enum = postgresql.ENUM("team_lead", "member", name="team_role", create_type=False)


def upgrade() -> None:
    op.create_table(
        "team_memberships",
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
        sa.Column("role", team_role_enum, nullable=False, server_default=sa.text("'member'")),
        # NULL = unmetered for this (user, team) pair. NUMERIC(20, 10) per
        # Phase 1.4's ADR-1 precision convention (see `db/models/user.py`).
        sa.Column("budget_usd", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column(
            "current_spend_usd",
            sa.Numeric(precision=20, scale=10),
            nullable=False,
            server_default=sa.text("0"),
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
        sa.UniqueConstraint("team_id", "user_id", name="uq_team_memberships_team_id_user_id"),
    )
    op.create_index("ix_team_memberships_user_id", "team_memberships", ["user_id"])
    op.create_index("ix_team_memberships_team_id", "team_memberships", ["team_id"])


def downgrade() -> None:
    op.drop_index("ix_team_memberships_team_id", table_name="team_memberships")
    op.drop_index("ix_team_memberships_user_id", table_name="team_memberships")
    op.drop_table("team_memberships")
