"""soft-delete team_memberships (removed_at)

Product owner request: an accidentally-removed team member (or team -
though team deletion already refuses while members exist, so that risk is
smaller) should be restorable. `remove_team_member` now sets `removed_at`
instead of deleting the row; a new `POST /v1/teams/{id}/members/{user_id}/
restore` clears it. The unique constraint on `(team_id, user_id)` is
deliberately UNCHANGED - see `db/models/team_membership.py`'s module
docstring for why "re-adding" a removed user restores the same row rather
than needing a second row / a partial unique index.

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-21

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0049"
down_revision: Union[str, None] = "0048"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "team_memberships", sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True)
    )
    # Every existing query assumes "one row = one active membership" -
    # this index makes the now-common "active memberships for this team/
    # user" filter cheap without a full-table scan once removed rows
    # accumulate.
    op.create_index(
        "ix_team_memberships_active",
        "team_memberships",
        ["team_id", "user_id"],
        postgresql_where=sa.text("removed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_team_memberships_active", table_name="team_memberships")
    op.drop_column("team_memberships", "removed_at")
