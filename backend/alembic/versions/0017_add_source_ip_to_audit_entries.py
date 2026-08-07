"""add source_ip column to audit_entries

Phase 3 (Security & Compliance Hardening), DB-4. See
`docs/design/phase-3-security-compliance-design.md` section 1.8 for the full
design rationale and `gatekey.db.models.audit_entry.AuditEntry` for the ORM
side. This migration is the source of truth for actual DDL.

Native Postgres `INET` type, nullable - AC1.2's best-effort contract: an
audit write must never fail because a source IP genuinely isn't available
(e.g. an internal service call with no request context).

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "audit_entries",
        sa.Column("source_ip", postgresql.INET(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("audit_entries", "source_ip")
