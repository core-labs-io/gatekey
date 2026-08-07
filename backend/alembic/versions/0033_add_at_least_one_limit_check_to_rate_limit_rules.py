"""add chk_at_least_one_limit CHECK constraint to rate_limit_rules

Phase 4 (Reliability & Cost Efficiency), AC4.2.1 schema-gap fix. AC4.2.1
requires "at least one [of requests_per_min/tokens_per_min] must be
configured for the feature to be active" - previously this was only
enforced in application code (`api/v1/admin/rate_limits.py`'s POST/PUT
handlers), not as a schema-level invariant, so a row violating it could
still reach the table via any future write path (a script, a different
admin surface, a bug) that skips that specific app-layer check. This
migration promotes it to a DB `CHECK`, the same "schema-level invariant, not
an app-level pre-check-then-insert" philosophy `0023`'s `is_primary` partial
unique index and this table's own existing `scope_type`/`scope_team_id`
CHECK already establish.

Not data-dependent in practice (the app layer has enforced this since the
table's own creation in `0026` - every existing row should already satisfy
it), but if this migration ever fails against a real database it means a
row violating AC4.2.1 slipped in through some path outside the app-layer
check and needs a manual data fix before this constraint can be added -
flagged here rather than silently adding `NOT VALID`.

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-05

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0033"
down_revision: Union[str, None] = "0032"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "chk_at_least_one_limit",
        "rate_limit_rules",
        "requests_per_min IS NOT NULL OR tokens_per_min IS NOT NULL",
    )


def downgrade() -> None:
    op.drop_constraint("chk_at_least_one_limit", "rate_limit_rules", type_="check")
