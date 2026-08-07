"""create users table and attribute service_account_keys to a budget-owning user

Phase 1.4 (Budget - Basic). See `gatekey.db.models.user.User` for the ORM
side and `docs/design/phase-1.4-budget-basic-design.md` section 1 for the
full rationale (monetary column precision ADR-1, default-legacy-user
backfill ADR-7). This migration is the source of truth for actual DDL.

Backfill strategy (product spec section 1, "Migration of pre-existing
service-account keys"): every pre-1.4 `service_account_keys` row gets
attributed to one auto-created, unmetered (`budget_usd = NULL`) default
user per org, so existing pilot traffic keeps working with zero required
admin action. Uses a FIXED, well-known UUID for that default user
(`00000000-0000-0000-0000-000000000002`), mirroring `0001`'s own fixed
`DEFAULT_ORG_ID` seed convention - deterministic and idempotent
(`ON CONFLICT (id) DO NOTHING`), not `gen_random_uuid()`/`uuid-ossp` (no
such extension is otherwise required by this codebase).

Scoped to the single default org (`00000000-0000-0000-0000-000000000001`)
seeded by `0001` - Phase 1 has no multi-org signup flow yet
(`gatekey.constants.DEFAULT_ORG_ID`).

`service_account_keys.user_id` is added nullable, backfilled to the
default legacy user, then altered `NOT NULL`, then given its `FOREIGN KEY
... ON DELETE RESTRICT` constraint and index - in that order, so the
`NOT NULL`/FK additions never fail against pre-existing rows.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-17

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"
# Fixed UUID for the single default/legacy user seeded by this migration -
# see module docstring (ADR-7). Referenced by literal value (not
# re-derived) so it is stable across every environment/deployment this
# migration runs against.
DEFAULT_LEGACY_USER_ID = "00000000-0000-0000-0000-000000000002"
DEFAULT_LEGACY_USER_NAME = "Unassigned (pre-1.4 legacy keys)"


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        # NULL = unmetered (no spend cutoff). See ADR-1 in the design doc
        # for why this and `current_spend_usd` are NUMERIC(20, 10), not a
        # smaller/currency-typical scale.
        sa.Column("budget_usd", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column(
            "current_spend_usd",
            sa.Numeric(precision=20, scale=10),
            nullable=False,
            server_default=sa.text("0"),
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
    op.create_index("ix_users_org_id", "users", ["org_id"])

    # Idempotent seed of the single default/legacy user - see module
    # docstring. Safe to re-run; safe if a row with this id already exists
    # for any reason.
    op.execute(
        sa.text(
            """
            INSERT INTO users (id, org_id, name, budget_usd, current_spend_usd, created_at, updated_at)
            VALUES (CAST(:id AS uuid), CAST(:org_id AS uuid), :name, NULL, 0, now(), now())
            ON CONFLICT (id) DO NOTHING
            """
        ).bindparams(id=DEFAULT_LEGACY_USER_ID, org_id=DEFAULT_ORG_ID, name=DEFAULT_LEGACY_USER_NAME)
    )

    # Added nullable first so the column can exist alongside pre-existing
    # rows, backfilled to the default legacy user, then tightened to
    # NOT NULL - see module docstring.
    op.add_column(
        "service_account_keys",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE service_account_keys SET user_id = CAST(:default_user_id AS uuid) "
            "WHERE user_id IS NULL"
        ).bindparams(default_user_id=DEFAULT_LEGACY_USER_ID)
    )
    op.alter_column("service_account_keys", "user_id", nullable=False)
    op.create_foreign_key(
        "fk_service_account_keys_user_id",
        "service_account_keys",
        "users",
        ["user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_service_account_keys_user_id", "service_account_keys", ["user_id"])


def downgrade() -> None:
    # `service_account_keys.user_id` (index, FK, column) before `users`
    # (the FK depends on `users` existing).
    op.drop_index("ix_service_account_keys_user_id", table_name="service_account_keys")
    op.drop_constraint(
        "fk_service_account_keys_user_id", "service_account_keys", type_="foreignkey"
    )
    op.drop_column("service_account_keys", "user_id")

    op.drop_index("ix_users_org_id", table_name="users")
    op.drop_table("users")
