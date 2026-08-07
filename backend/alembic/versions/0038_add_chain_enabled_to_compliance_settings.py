"""add chain_enabled to compliance_settings, with a mutual-exclusivity CHECK

Phase 5 (Differentiators), 5.2 Hash-Chained Audit Ledger. See
`gatekey.db.models.compliance_settings.ComplianceSettings` for the ORM side
and `gatekey/phase-5-technical-design.md` section 4.1 for the full design
rationale. This migration is the source of truth for actual DDL.

`chain_enabled` defaults `false` - an org must deliberately opt in, same
off-by-default posture every other compliance/DLP toggle in this codebase
uses (see `db/models/dlp_policy.py`'s module docstring).

The `chk_chain_purge_mutually_exclusive` CHECK is a DB-level backstop on top
of the app-layer validation `services/compliance_settings.py::set_
compliance_settings`/`set_chain_enabled` must perform (backend-developer
task) - mirrors this codebase's existing convention of pairing an app-layer
business rule with a DB-level sanity bound (see `0036`'s degradation-
threshold CHECK). The expression is taken verbatim from the design doc:
`NOT (chain_enabled AND audit_retention_days IS NOT NULL)` - an org can have
a finite purge policy (`audit_retention_days` non-null) OR the hash chain
enabled, never both simultaneously (design doc section 12, "Known
Limitations": "Chain and purge are mutually exclusive, not co-existing -
simpler/safer v1 choice over purge-aware re-genesis bookkeeping").

Depends on `0037` (same feature, ordered for readability per the design
doc's migration-sequencing table) but has no actual DDL dependency on it -
`compliance_settings` and `audit_entries` are separate tables.

No data backfill needed: `chain_enabled` defaults `false` for every
pre-existing row, so no existing org's configuration changes meaning. The
new CHECK is satisfiable by every existing row for the same reason
(`chain_enabled = false` makes the CHECK's `AND` clause vacuously true
regardless of `audit_retention_days`).

Downgrade is fully reversible and non-data-dependent: nothing else
references this column by FK, so dropping the CHECK then the column is safe
regardless of row count.

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-06

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0038"
down_revision: Union[str, None] = "0037"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "compliance_settings",
        sa.Column("chain_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.create_check_constraint(
        "chk_chain_purge_mutually_exclusive",
        "compliance_settings",
        "NOT (chain_enabled AND audit_retention_days IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "chk_chain_purge_mutually_exclusive", "compliance_settings", type_="check"
    )
    op.drop_column("compliance_settings", "chain_enabled")
