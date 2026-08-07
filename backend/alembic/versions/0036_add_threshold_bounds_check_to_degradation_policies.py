"""add threshold_pct_of_budget bounds CHECK to degradation_policies

Phase 4 (Reliability & Cost Efficiency) schema-gap fix. `degradation_
policies.threshold_pct_of_budget` had no DB-level bound - only
`api/v1/admin/degradation_policy.py`'s POST/PUT handlers reject values
outside (1.0, 99.0) exclusive-ish (`< 1.0 or > 99.0` is rejected, so the
app layer's effective accepted range is `[1.0, 99.0]`). This migration adds
a deliberately slightly wider DB `CHECK`, `(0, 100]`, rather than mirroring
the app layer's exact `[1, 99]` bounds - a pure sanity bound against the
column's actual meaning (a percentage), not a duplicate of the app's
business-rule range, so a future legitimate business-rule change (e.g.
allowing exactly 100%) doesn't also require a migration. Confirmed
non-conflicting: every existing write path (`api/v1/admin/degradation_
policy.py`, the `0028` migration's own `server_default '10.0'`) already
produces values inside `[1, 99]`, well within `(0, 100]`.

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-05

"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0036"
down_revision: Union[str, None] = "0035"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "chk_degradation_policies_threshold_bounds",
        "degradation_policies",
        "threshold_pct_of_budget > 0 AND threshold_pct_of_budget <= 100",
    )


def downgrade() -> None:
    op.drop_constraint(
        "chk_degradation_policies_threshold_bounds",
        "degradation_policies",
        type_="check",
    )
