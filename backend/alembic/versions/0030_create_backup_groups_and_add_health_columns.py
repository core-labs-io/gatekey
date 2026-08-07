"""create backup_groups table, add backup-group/health columns to provider_keys

Phase 4 (Reliability & Cost Efficiency) schema/code-drift fix. See
`gatekey.db.models.backup_group.BackupGroup` and
`gatekey.db.models.provider_key.ProviderKey` (module docstring "Phase 4
backup group for multi-key failover orchestration" / "Health tracking") for
the ORM side - both were already declared and actively read/written by
`services/provider_keys.py` and `services/provider_key_health.py`
(`create_backup_group`, `get_backup_group_for_provider`,
`refresh_provider_key_health`, ...) with no migration ever creating the
table or columns, an `UndefinedTable`/`UndefinedColumn` crash risk against a
real Postgres the moment those code paths run. This migration is the source
of truth for that DDL going forward; the ORM model files' own "Migration
ownership" docstrings should be read as pointing here, not at `0023`/`0025`
placeholders that predate this fix.

`backup_groups` is created first (org-scoped, `ON DELETE CASCADE` from
`orgs`) so `provider_keys.backup_group_id`'s FK target exists before the
column is added - same "create referenced table before the referencing
column" ordering `0025` already used for `failover_events`' two
`provider_keys` FKs.

`health_status` is added as a plain `Text` column with `server_default
'unknown'`, matching `gatekey.db.models.provider_key.ProviderKey.health_
status` exactly - the ORM model does not declare a `CheckConstraint`/Postgres
enum for this column (unlike `provider`/`scope_type`-style columns
elsewhere in this codebase), so none is invented here; the value set
(`"unknown"`/`"healthy"`/`"degraded"`/`"down"`/`"unavailable"`, see
`services.provider_key_health.HealthStatus`) is enforced at the application
layer only, exactly as today.

`availability_24h` has no explicit SQLAlchemy column type in the ORM model
(`mapped_column("availability_24h", nullable=True)` on a `Mapped[float |
None]` attribute) - SQLAlchemy's default python-type mapping for `float` is
`Float`, so that is what this migration creates, to stay byte-for-byte in
sync with what `Base.metadata` actually reflects for this column.

No data backfill needed: every column is nullable or has a safe default
(`backup_group_id`/`last_health_check`/`last_error`/`availability_24h`/
`last_degraded_at` all NULL, `health_status` defaults `'unknown'`) - no
pre-existing `provider_keys` row's meaning changes.

Downgrade is fully reversible and non-data-dependent: nothing added here is
referenced by any other table's FK, so column/table drop order is simply the
reverse of creation (columns off `provider_keys` first, then the now-
unreferenced `backup_groups` table).

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-05

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0030"
down_revision: Union[str, None] = "0029"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "backup_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.add_column(
        "provider_keys",
        sa.Column(
            "backup_group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("backup_groups.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "provider_keys",
        sa.Column(
            "health_status", sa.Text(), nullable=False, server_default=sa.text("'unknown'")
        ),
    )
    op.add_column(
        "provider_keys",
        sa.Column("last_health_check", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "provider_keys",
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "provider_keys",
        sa.Column("availability_24h", sa.Float(), nullable=True),
    )
    op.add_column(
        "provider_keys",
        sa.Column("last_degraded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_provider_keys_backup_group", "provider_keys", ["backup_group_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_provider_keys_backup_group", table_name="provider_keys")
    op.drop_column("provider_keys", "last_degraded_at")
    op.drop_column("provider_keys", "availability_24h")
    op.drop_column("provider_keys", "last_error")
    op.drop_column("provider_keys", "last_health_check")
    op.drop_column("provider_keys", "health_status")
    op.drop_column("provider_keys", "backup_group_id")

    op.drop_table("backup_groups")
