"""add teams.cache_enabled and teams.cache_ttl_minutes

Phase 4 (Reliability & Cost Efficiency), AC4.3.2/AC4.3.3 schema-gap fix.

AC4.3.2 requires caching to default to disabled, opt-IN per team ("A team's
`cache_enabled` boolean defaults to `false`. When disabled, no cache
read/write occurs for that team's requests."). The schema as of `0027` only
had `caching_settings.enabled` (org-level, defaults `true`) and
`teams.cache_opt_out` (defaults `false`) - meaning every team's prompts were
cached BY DEFAULT unless an admin explicitly opted out, the opposite
direction from AC4.3.2. AC4.3.3 requires a per-team configurable TTL
(default 5 minutes, bounds 1-1440 minutes, i.e. 1 minute to 24 hours) -
no per-team TTL column existed at all, only the org-wide
`caching_settings.ttl_seconds`.

This migration adds `teams.cache_enabled` (opt-in, defaults `false`, per
AC4.3.2) and `teams.cache_ttl_minutes` (defaults 5, `CHECK` bounded to
[1, 1440], per AC4.3.3). The existing org-level `caching_settings.enabled`
kill switch is left untouched and still wins over a team's own
`cache_enabled=true` (org disabled always wins - the same
most-restrictive-layer-wins precedent Phase 3 already established for
DLP/residency) - that resolution logic itself is `services/response_
cache.py`'s job, not touched here.

`teams.cache_opt_out` is deliberately NOT dropped or renamed here, to avoid
a destructive change that could orphan existing data/references while two
overlapping columns briefly coexist. It is now superseded/redundant given
`cache_enabled` inverts and replaces its intent - flagged here as a
candidate for a follow-up cleanup migration (drop `cache_opt_out` once
`services/response_cache.py` and the admin API are confirmed to read
`cache_enabled` exclusively), but that decision and any data backfill
between the two columns is a human/product-owner call, not made
unilaterally by this migration.

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-05

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0035"
down_revision: Union[str, None] = "0034"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "teams",
        sa.Column(
            "cache_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "teams",
        sa.Column(
            "cache_ttl_minutes", sa.Integer(), nullable=False, server_default=sa.text("5")
        ),
    )
    op.create_check_constraint(
        "chk_teams_cache_ttl_minutes_bounds",
        "teams",
        "cache_ttl_minutes >= 1 AND cache_ttl_minutes <= 1440",
    )


def downgrade() -> None:
    op.drop_constraint("chk_teams_cache_ttl_minutes_bounds", "teams", type_="check")
    op.drop_column("teams", "cache_ttl_minutes")
    op.drop_column("teams", "cache_enabled")
