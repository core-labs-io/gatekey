"""create rate_limit_rules and rate_limit_rejection_events tables plus the
rate_limit_scope_type/rate_limit_on_limit/rate_limit_rejection_outcome enums

Phase 4 (Reliability & Cost Efficiency), DB-4. See
`docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.5 for
the full design rationale;
`gatekey.db.models.rate_limit_rule.RateLimitRule`/
`gatekey.db.models.rate_limit_rejection_event.RateLimitRejectionEvent` for the
ORM side. This migration is the source of truth for actual DDL.

`rate_limit_rules` is a Postgres config table, not the hot-path counter
store (the actual per-minute counters live in the shared-state store,
`services/shared_state.py`, backend-developer track). Same one-row-per-scope
partial-unique-index pattern as `residency_rules`/`rotation_policies`/
`access_schedules` - the fourth application of this exact pattern in this
codebase. The `CHECK` constraint enforces `scope_team_id` is set iff
`scope_type = 'team'`.

`rate_limit_rejection_events.scope_team_id`/`user_id` are plain nullable UUID
columns with no FK - display/filtering-only on an event log, same "no
referential-integrity boundary" reason `dlp_scan_results.team_id`/`user_id`
already establish.

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-05

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: Union[str, None] = "0025"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RATE_LIMIT_SCOPE_TYPE_VALUES = ("org_default_per_user", "team")
RATE_LIMIT_ON_LIMIT_VALUES = ("reject", "queue_retry")
RATE_LIMIT_REJECTION_OUTCOME_VALUES = ("reject", "queue_timeout")


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(
        *RATE_LIMIT_SCOPE_TYPE_VALUES, name="rate_limit_scope_type", create_type=False
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        *RATE_LIMIT_ON_LIMIT_VALUES, name="rate_limit_on_limit", create_type=False
    ).create(bind, checkfirst=True)
    postgresql.ENUM(
        *RATE_LIMIT_REJECTION_OUTCOME_VALUES,
        name="rate_limit_rejection_outcome",
        create_type=False,
    ).create(bind, checkfirst=True)

    op.create_table(
        "rate_limit_rules",
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
                *RATE_LIMIT_SCOPE_TYPE_VALUES, name="rate_limit_scope_type", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "scope_team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("requests_per_min", sa.Integer(), nullable=True),
        sa.Column("tokens_per_min", sa.Integer(), nullable=True),
        sa.Column(
            "on_limit",
            postgresql.ENUM(
                *RATE_LIMIT_ON_LIMIT_VALUES, name="rate_limit_on_limit", create_type=False
            ),
            nullable=False,
            server_default=sa.text("'reject'"),
        ),
        # Ratified #8's default, admin-configurable.
        sa.Column(
            "max_queue_wait_seconds", sa.Integer(), nullable=False, server_default=sa.text("30")
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
        sa.CheckConstraint(
            "(scope_type = 'org_default_per_user' AND scope_team_id IS NULL) OR "
            "(scope_type = 'team' AND scope_team_id IS NOT NULL)",
            name="ck_rate_limit_rules_scope_type_matches_scope_team_id",
        ),
    )
    op.create_index(
        "uq_rate_limit_rules_org_default",
        "rate_limit_rules",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'org_default_per_user'"),
    )
    op.create_index(
        "uq_rate_limit_rules_team_scoped",
        "rate_limit_rules",
        ["scope_team_id"],
        unique=True,
        postgresql_where=sa.text("scope_team_id IS NOT NULL"),
    )

    op.create_table(
        "rate_limit_rejection_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "rule_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("rate_limit_rules.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "scope_type",
            postgresql.ENUM(
                *RATE_LIMIT_SCOPE_TYPE_VALUES, name="rate_limit_scope_type", create_type=False
            ),
            nullable=False,
        ),
        # Display/filtering only - no FK, see module docstring.
        sa.Column("scope_team_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "outcome",
            postgresql.ENUM(
                *RATE_LIMIT_REJECTION_OUTCOME_VALUES,
                name="rate_limit_rejection_outcome",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_rate_limit_rejection_events_org_id_occurred_at",
        "rate_limit_rejection_events",
        ["org_id", "occurred_at"],
    )
    op.create_index(
        "ix_rate_limit_rejection_events_rule_id_occurred_at",
        "rate_limit_rejection_events",
        ["rule_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rate_limit_rejection_events_rule_id_occurred_at",
        table_name="rate_limit_rejection_events",
    )
    op.drop_index(
        "ix_rate_limit_rejection_events_org_id_occurred_at",
        table_name="rate_limit_rejection_events",
    )
    op.drop_table("rate_limit_rejection_events")

    op.drop_index("uq_rate_limit_rules_team_scoped", table_name="rate_limit_rules")
    op.drop_index("uq_rate_limit_rules_org_default", table_name="rate_limit_rules")
    op.drop_table("rate_limit_rules")

    bind = op.get_bind()
    postgresql.ENUM(
        *RATE_LIMIT_REJECTION_OUTCOME_VALUES, name="rate_limit_rejection_outcome"
    ).drop(bind, checkfirst=True)
    postgresql.ENUM(*RATE_LIMIT_ON_LIMIT_VALUES, name="rate_limit_on_limit").drop(
        bind, checkfirst=True
    )
    postgresql.ENUM(*RATE_LIMIT_SCOPE_TYPE_VALUES, name="rate_limit_scope_type").drop(
        bind, checkfirst=True
    )
