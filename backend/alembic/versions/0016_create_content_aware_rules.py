"""create content_aware_rules table

Phase 3 (Security & Compliance Hardening), DB-3. See
`docs/design/phase-3-security-compliance-design.md` section 1.7 for the full
design rationale and `gatekey.db.models.content_aware_rule.ContentAwareRule`
for the ORM side. This migration is the source of truth for actual DDL.

Composite `(org_id, category)` primary key - org-wide only (AC4.2, no
team-level override), at most one row per category per org. Per ratified #6,
all three category rows the UI mock shows are ship-able, but only
`category = 'pii'` is wired to a real signal this phase - `category` is
deliberately `text`, not an enum, so Phase 5 can add real classifier-backed
categories without a schema change (design doc section 12).

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "content_aware_rules",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # 'pii' (functional), 'source_code'/'financial_data' (inert, A6) -
        # see module docstring.
        sa.Column("category", sa.String(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "allowed_models", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
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
    )


def downgrade() -> None:
    op.drop_table("content_aware_rules")
