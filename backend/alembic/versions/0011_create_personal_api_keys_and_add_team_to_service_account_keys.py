"""create personal_api_keys table and add team_id to service_account_keys

Phase 2 (Multi-Tenant Governance), DB-5. See
`docs/design/phase-2-multi-tenant-governance-design.md` sections 1.6-1.7 for
the full design rationale;
`gatekey.db.models.personal_api_key.PersonalApiKey` /
`gatekey.db.models.service_account_key.ServiceAccountKey` for the ORM side.
This migration is the source of truth for actual DDL.

`personal_api_keys` copies `service_account_keys`' column conventions
verbatim: SHA-256 `secret_hash` (32 bytes, not a slow KDF), `key_prefix`
for list-view identification only, `revoked_at`-only (no `is_active`
boolean), no plaintext secret column ever. `ON DELETE RESTRICT` on
`owner_user_id`/`created_by_user_id`/`team_id`: a live credential row must
never be silently orphan-deleted by its owner's or team's deletion.
`team_id` here is `NOT NULL` - every personal key is created fresh under
Phase 2, so there is no legacy-row population problem.

`service_account_keys.team_id` is added nullable with NO backfill: NULL =
legacy row resolving against the flat `users.budget_usd` path (byte-for-byte
the Phase 1.4 behavior). "New keys require team_id" is enforced at the
API-schema layer, not by a column constraint - see design doc section 1.7
for why (same tension `0004` resolved for `user_id`, minus the safe
backfill default that column had).

No plaintext secret material appears anywhere in this migration.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "personal_api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The human whose (team, user) membership budget this key charges
        # against - RESTRICT for the same "credential blocks user deletion"
        # rationale as `service_account_keys.user_id`.
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # Who minted it (self-serve: same as owner; delegated: the team
        # lead/admin).
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # NOT NULL (unlike `service_account_keys.team_id`) - no legacy rows
        # exist for this table, see module docstring.
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        # First chars of the plaintext secret after the `gk_pk_` prefix, for
        # identification in list views only - never used for auth lookup.
        sa.Column("key_prefix", sa.String(12), nullable=False),
        # SHA-256 digest (32 bytes) of the full plaintext secret.
        sa.Column("secret_hash", sa.LargeBinary(), nullable=False),
        # NULL = no expiration.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        # NULL = active, non-NULL = revoked as of that timestamp.
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_personal_api_keys_secret_hash", "personal_api_keys", ["secret_hash"], unique=True)
    op.create_index("ix_personal_api_keys_owner_user_id", "personal_api_keys", ["owner_user_id"])
    op.create_index("ix_personal_api_keys_org_id", "personal_api_keys", ["org_id"])

    # Nullable, no backfill - NULL means "legacy flat-budget row" forever
    # (see module docstring).
    op.add_column(
        "service_account_keys",
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_service_account_keys_team_id",
        "service_account_keys",
        "teams",
        ["team_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_service_account_keys_team_id", "service_account_keys", ["team_id"])


def downgrade() -> None:
    op.drop_index("ix_service_account_keys_team_id", table_name="service_account_keys")
    op.drop_constraint(
        "fk_service_account_keys_team_id", "service_account_keys", type_="foreignkey"
    )
    op.drop_column("service_account_keys", "team_id")

    op.drop_index("ix_personal_api_keys_org_id", table_name="personal_api_keys")
    op.drop_index("ix_personal_api_keys_owner_user_id", table_name="personal_api_keys")
    op.drop_index("ix_personal_api_keys_secret_hash", table_name="personal_api_keys")
    op.drop_table("personal_api_keys")
