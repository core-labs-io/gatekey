"""add failover/cache/degradation tracking columns to usage_logs

Phase 4 (Reliability & Cost Efficiency) schema/code-drift fix. See
`gatekey.db.models.usage_log.UsageLog` (module docstring's "Phase 4:" column
comments) for the ORM side - `cache_hit`, `failover_attempt`,
`failover_key_id`, `degraded_from_model`, `degraded_to_model` and their four
indexes were already declared on the model and are the columns
`services/response_cache.py`, `api.v1.gateway.common.call_provider_with_
failover`, and `services/degradation.py` write into today, with no migration
ever adding them - an `UndefinedColumn` crash risk against a real Postgres
the moment those code paths run. This migration is the source of truth for
that DDL going forward, alongside `0029` (`original_model`) which already
covers the degradation "what model was originally requested" half of this
same feature set.

All five columns are additive and either nullable or carry a safe default
(`cache_hit` defaults `false`, `failover_attempt` defaults `0`,
`failover_key_id`/`degraded_from_model`/`degraded_to_model` all nullable) -
no pre-existing row's meaning changes, so no backfill is needed.

`failover_key_id` is `ON DELETE SET NULL` against `provider_keys.id` - same
"a usage record must outlive the credential that generated it" posture the
module docstring already establishes for `user_id`/`service_account_key_id`/
`team_id`/`personal_api_key_id`.

Downgrade is fully reversible and non-data-dependent: drop the four indexes,
then the five columns, in reverse of creation order.

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-05

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0031"
down_revision: Union[str, None] = "0030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "usage_logs",
        sa.Column("cache_hit", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "usage_logs",
        sa.Column(
            "failover_attempt", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "usage_logs",
        sa.Column(
            "failover_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_keys.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "usage_logs",
        sa.Column("degraded_from_model", sa.Text(), nullable=True),
    )
    op.add_column(
        "usage_logs",
        sa.Column("degraded_to_model", sa.Text(), nullable=True),
    )

    op.create_index(
        "ix_usage_logs_failover", "usage_logs", ["failover_attempt", "failover_key_id"]
    )
    op.create_index("ix_usage_logs_cache_hit", "usage_logs", ["cache_hit"])
    op.create_index("ix_usage_logs_degraded_from", "usage_logs", ["degraded_from_model"])
    op.create_index("ix_usage_logs_degraded_to", "usage_logs", ["degraded_to_model"])


def downgrade() -> None:
    op.drop_index("ix_usage_logs_degraded_to", table_name="usage_logs")
    op.drop_index("ix_usage_logs_degraded_from", table_name="usage_logs")
    op.drop_index("ix_usage_logs_cache_hit", table_name="usage_logs")
    op.drop_index("ix_usage_logs_failover", table_name="usage_logs")

    op.drop_column("usage_logs", "degraded_to_model")
    op.drop_column("usage_logs", "degraded_from_model")
    op.drop_column("usage_logs", "failover_key_id")
    op.drop_column("usage_logs", "failover_attempt")
    op.drop_column("usage_logs", "cache_hit")
