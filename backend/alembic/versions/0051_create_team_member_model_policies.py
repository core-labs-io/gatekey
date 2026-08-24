"""create team_member_model_policies table

Per-team-member model-access overlay - a third layer below org model policy
(`model_policies`) and team model policy (`team_model_policies`): org admin
enables N models org-wide, a team lead narrows that further for their whole
team (existing `team_model_policies`), and now a team lead can narrow it
once more per INDIVIDUAL member - "of the models my team can use, this
specific person gets these." See `gatekey.db.models.team_member_model_policy.
TeamMemberModelPolicy` for the full ORM-side design rationale (mirrors
`team_memberships`' surrogate-PK + `UNIQUE(team_id, user_id)` shape, not
`team_model_policies`' team-id-as-PK shape, since this is genuinely one row
per (team, member) pair). This migration is the source of truth for actual
DDL.

`models` (JSONB, default `[]`) is validated only at the service layer
(`services.model_policy.set_member_model_policy`) against the team's own
live effective model set - no FK target exists for an in-memory list, same
convention `team_model_policies.models` already uses. Absence of a row (the
default/common case) means "no further restriction beyond the team's own
effective set" - not a third state needing a column, same ADR-2 convention
`team_model_policies`/`model_policies` already establish.

`ON DELETE CASCADE` on both FKs: this overlay has no independent meaning
once the team or user row is gone.

Revision ID: 0051
Revises: 0050
Create Date: 2026-08-22

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0051"
down_revision: Union[str, None] = "0050"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "team_member_model_policies",
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
        sa.Column(
            "models", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
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
        sa.UniqueConstraint(
            "team_id", "user_id", name="uq_team_member_model_policies_team_id_user_id"
        ),
    )
    op.create_index(
        "ix_team_member_model_policies_team_id", "team_member_model_policies", ["team_id"]
    )
    op.create_index(
        "ix_team_member_model_policies_user_id", "team_member_model_policies", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_team_member_model_policies_user_id", table_name="team_member_model_policies"
    )
    op.drop_index(
        "ix_team_member_model_policies_team_id", table_name="team_member_model_policies"
    )
    op.drop_table("team_member_model_policies")
