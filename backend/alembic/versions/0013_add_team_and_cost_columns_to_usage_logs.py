"""add team/personal-key attribution and cost-normalization columns to usage_logs

Phase 2 (Multi-Tenant Governance), DB-7. See
`docs/design/phase-2-multi-tenant-governance-design.md` section 1.11 (and
ADR-9) for the full design rationale and
`gatekey.db.models.usage_log.UsageLog` for the ORM side. This migration is
the source of truth for actual DDL.

`team_id`/`personal_api_key_id` follow the exact nullable + `SET NULL`
pattern already used for `user_id`/`service_account_key_id` on this table -
a usage record must outlive the team/credential that generated it. No
backfill: every pre-Phase-2 row legitimately has no team/personal-key
attribution.

Cost normalization (ADR-9): the existing `cost_usd` column continues to
mean "the normalized cost charged against the org's budget currency";
`raw_provider_cost_usd` keeps the provider-native pre-normalization figure
and `fx_rate_applied` the rate used, so normalization stays auditable. In
Phase 2 both are trivially `raw == cost` / `rate == 1` - the columns exist
now so later real FX conversion is additive, not a rewrite.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "usage_logs",
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_usage_logs_team_id",
        "usage_logs",
        "teams",
        ["team_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "usage_logs",
        sa.Column("personal_api_key_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_usage_logs_personal_api_key_id",
        "usage_logs",
        "personal_api_keys",
        ["personal_api_key_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # NULL for uncharged rows (same semantics as `cost_usd`) and for every
    # pre-Phase-2 row - no backfill, historic rows predate normalization.
    op.add_column(
        "usage_logs",
        sa.Column("raw_provider_cost_usd", sa.Numeric(precision=20, scale=10), nullable=True),
    )
    op.add_column(
        "usage_logs",
        sa.Column(
            "fx_rate_applied",
            sa.Numeric(precision=20, scale=10),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_index("ix_usage_logs_team_id", "usage_logs", ["team_id"])


def downgrade() -> None:
    op.drop_index("ix_usage_logs_team_id", table_name="usage_logs")
    op.drop_column("usage_logs", "fx_rate_applied")
    op.drop_column("usage_logs", "raw_provider_cost_usd")
    op.drop_constraint(
        "fk_usage_logs_personal_api_key_id", "usage_logs", type_="foreignkey"
    )
    op.drop_column("usage_logs", "personal_api_key_id")
    op.drop_constraint("fk_usage_logs_team_id", "usage_logs", type_="foreignkey")
    op.drop_column("usage_logs", "team_id")
