"""org-level alert thresholds: 50/75/100%, not 80/100

Product owner request: org-wide safeguard alerts should fire at 50%, 75%,
and 100% of the ceiling - a wider, earlier-warning ladder than team-level
alerts (which stay at 80/100 unchanged), since an org-wide crossing is a
bigger deal and admins want more runway to react. All three enabled by
default.

`org_settings.alert_threshold_80_enabled` (added by `0045`, in the same
session, never shipped/relied on by any real deployment) is dropped rather
than left as dead, confusingly-named vestigial state - `services.
notifiers.crossed_thresholds` never reads it for the org path from this
point on. `alert_threshold_100_enabled` (still part of the org set) is
left untouched.

Revision ID: 0046
Revises: 0045
Create Date: 2026-08-21

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0046"
down_revision: Union[str, None] = "0045"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("org_settings", "alert_threshold_80_enabled")
    op.add_column(
        "org_settings",
        sa.Column(
            "alert_threshold_50_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "org_settings",
        sa.Column(
            "alert_threshold_75_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )


def downgrade() -> None:
    op.drop_column("org_settings", "alert_threshold_75_enabled")
    op.drop_column("org_settings", "alert_threshold_50_enabled")
    op.add_column(
        "org_settings",
        sa.Column(
            "alert_threshold_80_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
