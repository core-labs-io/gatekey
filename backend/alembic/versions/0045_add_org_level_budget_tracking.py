"""add org-level budget tracking and alerting

Org-wide spend safeguard, requested directly by the product owner: "one
minor typo in team budget can cost an org millions" - team ceilings only
ALERT (never block) once crossed, and the pre-existing `org_settings.
budget_ceiling_usd` (see `0007`) is a pure allocation-time constraint
(checked only when an admin writes a team ceiling, never against real
spend). Neither one is a live, org-wide circuit breaker. This migration
adds the columns needed for one: `current_spend_usd` (a denormalized
running total, same ADR-7 shape as `teams.current_spend_usd`) plus the
same alert-config column set `teams` already carries
(`alert_threshold_80/100_enabled`, `webhook_alert_enabled` + its
AES-256-GCM envelope, `email_alert_enabled`) so org-level threshold
alerts (80%/100%) reuse the exact same detection/delivery machinery
(`services.notifiers.crossed_thresholds`) one level up.

Deliberate scope decision: unlike `teams.current_spend_usd`, this does
NOT get period/rollover columns (`period_type`/`on_period_end`/
`current_period_started_at`) - no automatic reset. A catastrophic-mistake
circuit breaker that silently resets itself every month could mask an
ongoing leak; clearing it is therefore always an explicit, audited admin
action (`services.budget.reset_org_spend`), not a lazy touch-based
rollover. Flagged here rather than silently matching every nuance of the
team-period system.

Revision ID: 0045
Revises: 0044
Create Date: 2026-08-21

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0045"
down_revision: Union[str, None] = "0044"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "org_settings",
        sa.Column(
            "current_spend_usd",
            sa.Numeric(20, 10),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "org_settings",
        sa.Column(
            "alert_threshold_80_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "org_settings",
        sa.Column(
            "alert_threshold_100_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "org_settings",
        sa.Column(
            "webhook_alert_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    # AES-256-GCM envelope for the org alert webhook URL - all three NULL,
    # or all three set, always written together by the app layer (same
    # discipline as teams.webhook_ciphertext/nonce/auth_tag).
    op.add_column("org_settings", sa.Column("webhook_ciphertext", sa.LargeBinary(), nullable=True))
    op.add_column("org_settings", sa.Column("webhook_nonce", sa.LargeBinary(), nullable=True))
    op.add_column("org_settings", sa.Column("webhook_auth_tag", sa.LargeBinary(), nullable=True))
    op.add_column(
        "org_settings",
        sa.Column(
            "email_alert_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("org_settings", "email_alert_enabled")
    op.drop_column("org_settings", "webhook_auth_tag")
    op.drop_column("org_settings", "webhook_nonce")
    op.drop_column("org_settings", "webhook_ciphertext")
    op.drop_column("org_settings", "webhook_alert_enabled")
    op.drop_column("org_settings", "alert_threshold_100_enabled")
    op.drop_column("org_settings", "alert_threshold_80_enabled")
    op.drop_column("org_settings", "current_spend_usd")
