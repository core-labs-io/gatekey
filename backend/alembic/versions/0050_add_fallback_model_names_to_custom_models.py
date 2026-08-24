"""add fallback_model_names to custom_models, model_fallback_* to usage_logs

Model Catalog + Cross-Provider Fallback Chains, Part B. See
`gatekey/model-catalog-fallback-chains-technical-design.md` sections 2.1/2.7
for the full design rationale. This migration is the source of truth for
actual DDL; ORM-side definitions (`db/models/custom_model.py`,
`db/models/usage_log.py`) must stay in lockstep with it.

`custom_models.fallback_model_names` is `JSONB NOT NULL DEFAULT '[]'::jsonb`
- a small, admin-authored, ordered list of model-id strings that belongs to
one parent row, not a normalized child table. Identical convention to
`self_hosted_providers.models` (see `0040_create_self_hosted_providers.py`),
which is the direct structural precedent. An empty list (the default) means
"no fallback chain configured" - byte-for-byte pre-this-feature behavior for
every existing custom model; no backfill needed. No CHECK constraint is
added for chain length/self-reference/duplicates/resolvability - none of
that is expressible as a static SQL CHECK (resolvability depends on the live
contents of two other tables plus `MODEL_REGISTRY`), so it is app-layer-only
validation (`services/custom_models.py`), matching how
`self_hosted_providers.models`' own collision guards are also app-layer-only
with no DB CHECK equivalent.

`usage_logs.model_fallback_attempt` (Integer, `NOT NULL DEFAULT 0`) and
`usage_logs.model_fallback_from_model` (Text, nullable) directly mirror
`failover_attempt`/`failover_key_id`'s existing shape and naming convention
(an int count + the "from" identifier, `0`/`NULL` on the overwhelming
majority of rows - see `0031_add_failover_cache_degradation_columns_to_
usage_logs.py`), scoped to models instead of keys. These are deliberately
NOT a reuse of the existing `original_model`/`degraded_from_model`/
`degraded_to_model` columns (migrations `0029`/`0031`), which already belong
to graceful degradation's own, distinct, budget-proximity-triggered
substitution decided BEFORE dispatch - model-fallback is a dispatch-
failure-triggered substitution decided AFTER a call already failed, and both
can legitimately apply to the same request (a degraded model can itself then
fail over), so they need independent columns.
`model_fallback_from_model` is a plain `Text` column with **no FK** -
consistent with `degraded_from_model`/`degraded_to_model`'s identical
no-FK-to-a-model-names-table choice (there is no models table to reference;
`custom_models.name` isn't even unique across time the way an id would be).
New composite index `ix_usage_logs_model_fallback` on
`(model_fallback_attempt, model_fallback_from_model)`, mirroring `ix_usage_
logs_failover`'s composite-index shape exactly, for "how often did fallback
actually fire, and from which model" dashboard queries.

Both new usage_logs columns are additive with a safe default/nullable, so no
pre-existing row's meaning changes - no backfill needed.

Downgrade is fully reversible and non-data-dependent: drop the new index,
then the two usage_logs columns, then the custom_models column, in reverse
of creation order.

Revision ID: 0050
Revises: 0049
Create Date: 2026-08-21

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0050"
down_revision: Union[str, None] = "0049"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "custom_models",
        sa.Column(
            "fallback_model_names",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    op.add_column(
        "usage_logs",
        sa.Column(
            "model_fallback_attempt", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "usage_logs",
        sa.Column("model_fallback_from_model", sa.Text(), nullable=True),
    )

    op.create_index(
        "ix_usage_logs_model_fallback",
        "usage_logs",
        ["model_fallback_attempt", "model_fallback_from_model"],
    )


def downgrade() -> None:
    op.drop_index("ix_usage_logs_model_fallback", table_name="usage_logs")

    op.drop_column("usage_logs", "model_fallback_from_model")
    op.drop_column("usage_logs", "model_fallback_attempt")

    op.drop_column("custom_models", "fallback_model_names")
