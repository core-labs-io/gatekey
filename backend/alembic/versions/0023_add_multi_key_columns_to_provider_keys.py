"""add multi-key/failover columns to provider_keys, relax uniqueness to
(org_id, provider, label)

Phase 4 (Reliability & Cost Efficiency), DB-1. See
`docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.2 for
the full design rationale and `gatekey.db.models.provider_key.ProviderKey`
for the ORM side. This migration is the source of truth for actual DDL.

Every pre-existing row (at most one per (org, provider) today, per Phase 1.1's
`UNIQUE(org_id, provider)`) is backfilled to `label='Default'`,
`is_primary=true` in this same migration, so no org's existing single-key
configuration silently breaks once `label` becomes part of the unique key and
`is_primary` starts mattering for routing (design doc section 1.2, "no org's
existing configuration silently breaks"). `label` is added with a temporary
`server_default` so the `NOT NULL` constraint can attach to existing rows,
then the backfill UPDATE overwrites the placeholder, then the default is
dropped - `label` has no default going forward (AC1.1: required on every new
key going forward).

`is_primary`'s partial unique index (`WHERE is_primary`) is what makes
"exactly one primary key per (org, provider)" a schema-level invariant, not
an app-level pre-check-then-insert - same philosophy as `ResidencyRule`/
`ModelPolicy`'s one-row-per-scope indexes. `failover_target_id` is a
self-referential FK, `ON DELETE SET NULL` (not `CASCADE`) - deleting a
configured failover target should silently clear a dangling reference, not
cascade-delete the key that pointed to it.

Downgrade note: this is only reversible if no org has actually added a
second key for the same provider since upgrading (the reintroduced
`UNIQUE(org_id, provider)` constraint would otherwise fail) - the same
data-dependent-reversibility caveat any uniqueness-relaxation migration has.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-05

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: Union[str, None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LABEL_BACKFILL_PLACEHOLDER = "__pending__"


def upgrade() -> None:
    op.add_column(
        "provider_keys",
        sa.Column(
            "label",
            sa.Text(),
            nullable=False,
            server_default=sa.text(f"'{_LABEL_BACKFILL_PLACEHOLDER}'"),
        ),
    )
    op.add_column(
        "provider_keys",
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "provider_keys",
        sa.Column(
            "failover_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "provider_keys",
        sa.Column(
            "failover_target_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_keys.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # Backfill every pre-existing row (see module docstring) before the
    # unique constraint/index below can attach.
    op.execute(
        sa.text(
            "UPDATE provider_keys SET label = 'Default', is_primary = true "
            f"WHERE label = '{_LABEL_BACKFILL_PLACEHOLDER}'"
        )
    )
    op.alter_column("provider_keys", "label", server_default=None)

    op.drop_constraint("uq_provider_keys_org_id_provider", "provider_keys", type_="unique")
    op.create_unique_constraint(
        "uq_provider_keys_org_id_provider_label", "provider_keys", ["org_id", "provider", "label"]
    )
    op.create_index(
        "uq_provider_keys_one_primary_per_provider",
        "provider_keys",
        ["org_id", "provider"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )
    op.create_index(
        "ix_provider_keys_failover_target_id", "provider_keys", ["failover_target_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_provider_keys_failover_target_id", table_name="provider_keys")
    op.drop_index("uq_provider_keys_one_primary_per_provider", table_name="provider_keys")
    op.drop_constraint(
        "uq_provider_keys_org_id_provider_label", "provider_keys", type_="unique"
    )
    # See module docstring "Downgrade note": fails if any org now has more
    # than one key per provider.
    op.create_unique_constraint(
        "uq_provider_keys_org_id_provider", "provider_keys", ["org_id", "provider"]
    )

    op.drop_column("provider_keys", "failover_target_id")
    op.drop_column("provider_keys", "failover_enabled")
    op.drop_column("provider_keys", "is_primary")
    op.drop_column("provider_keys", "label")
