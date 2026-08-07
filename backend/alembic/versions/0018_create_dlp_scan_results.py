"""create dlp_scan_results table

Phase 3 (Security & Compliance Hardening), DB-5. See
`docs/design/phase-3-security-compliance-design.md` section 1.9 for the full
design rationale and `gatekey.db.models.dlp_scan_result.DlpScanResult` for the
ORM side. This migration is the source of truth for actual DDL. Reuses the
`dlp_action` enum type created by `0014` (not recreated here).

Deliberately keyed by `request_id` (text), not a typed FK to `usage_logs` -
same rationale `audit_entries.target_id` already documents: the log-only
scan path completes asynchronously, independent of exactly when/whether a
`usage_logs` row exists yet, so coupling this table's write to that row's
lifecycle would be a real ordering hazard for no benefit. `team_id`/
`user_id` are plain nullable UUID columns (no FK) for the same reason -
display/filtering only, never a referential-integrity boundary this table
should be blocked by. `raw_flagged_content` is `NULL` by default (ratified
#3: default to NOT storing raw flagged substrings).

Sequenced after `0017` (audit_entries.source_ip) per the design doc's own
`[D: DB-4]` task ordering, though this table has no direct dependency on
that column - see design doc section 11.

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: Union[str, None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "dlp_scan_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The same opaque correlation id `common.new_request_id()` already
        # generates - see module docstring for why this is text, not a
        # typed FK to `usage_logs`.
        sa.Column("request_id", sa.String(), nullable=False),
        # Display/filtering only - no FK, see module docstring.
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("ran_sync", sa.Boolean(), nullable=False),
        sa.Column(
            "action_taken",
            postgresql.ENUM("log", "redact", "block", name="dlp_action", create_type=False),
            nullable=False,
        ),
        # [{detector_or_pattern_name, action}] - never raw content unless
        # `dlp_policies.store_raw_flagged_content = true`.
        sa.Column(
            "findings", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        # NULL by default (ratified #3) - populated only when
        # `dlp_policies.store_raw_flagged_content = true`.
        sa.Column("raw_flagged_content", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_dlp_scan_results_org_id_created_at", "dlp_scan_results", ["org_id", "created_at"]
    )
    op.create_index("ix_dlp_scan_results_request_id", "dlp_scan_results", ["request_id"])


def downgrade() -> None:
    op.drop_index("ix_dlp_scan_results_request_id", table_name="dlp_scan_results")
    op.drop_index("ix_dlp_scan_results_org_id_created_at", table_name="dlp_scan_results")
    op.drop_table("dlp_scan_results")
