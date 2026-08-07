"""create caching_settings and cache_lookup_events tables, add
teams.cache_opt_out

Phase 4 (Reliability & Cost Efficiency), DB-5. See
`docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.6 for
the full design rationale; `gatekey.db.models.caching_settings.CachingSettings`
/ `gatekey.db.models.cache_lookup_event.CacheLookupEvent` for the ORM side.
This migration is the source of truth for actual DDL.

`caching_settings` mirrors `compliance_settings`/`dlp_policies`' "absence of
row = default state" ADR exactly - an org that never touches this config gets
caching on (`enabled` defaults `true`, AC3.5) with a 1-hour TTL, not an inert
feature. No number is given by either source doc for the default TTL; 1 hour
(3600s) is chosen and documented here (design doc section 1.6).

`cache_opt_out` on `teams` follows the same per-team-toggle-column style as
`alert_threshold_80_enabled`/`webhook_alert_enabled`.

`cache_lookup_events.team_id` is a plain nullable UUID column with no FK -
display/filtering only, same reasoning as `dlp_scan_results.team_id`/
`rate_limit_rejection_events.scope_team_id`. No prompt/response content
stored here at all - purely a hit/miss/token-count event log.

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-05

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0027"
down_revision: Union[str, None] = "0026"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "caching_settings",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # Org-wide-on default (AC3.5) - see module docstring.
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        # No number given by either source doc; 1h chosen and documented in
        # the module docstring.
        sa.Column("ttl_seconds", sa.Integer(), nullable=False, server_default=sa.text("3600")),
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
    )

    op.add_column(
        "teams",
        sa.Column("cache_opt_out", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )

    op.create_table(
        "cache_lookup_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Display/filtering only - no FK, see module docstring.
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("hit", sa.Boolean(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        # native_model_id actually looked up (post-resolve_route).
        sa.Column("model", sa.Text(), nullable=False),
        # Populated on a hit only, copied from the cache entry.
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_cache_lookup_events_org_id_occurred_at",
        "cache_lookup_events",
        ["org_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cache_lookup_events_org_id_occurred_at", table_name="cache_lookup_events"
    )
    op.drop_table("cache_lookup_events")
    op.drop_column("teams", "cache_opt_out")
    op.drop_table("caching_settings")
