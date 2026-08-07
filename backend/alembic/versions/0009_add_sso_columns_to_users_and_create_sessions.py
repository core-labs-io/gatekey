"""add org_role/sso columns to users and create sessions table

Phase 2 (Multi-Tenant Governance), DB-3. See
`docs/design/phase-2-multi-tenant-governance-design.md` sections 1.8-1.9 for
the full design rationale; `gatekey.db.models.user.User` /
`gatekey.db.models.session.UserSession` for the ORM side. This migration is
the source of truth for actual DDL.

`users` additions - all nullable, no backfill needed: `org_role = NULL` is
the common case (member/team_lead roles live on `team_memberships.role`);
`sso_subject`/`sso_email` stay NULL for every pre-Phase-2, admin-created
flat user. The unique index on `sso_subject` is partial
(`WHERE sso_subject IS NOT NULL`) precisely so those legacy rows never
conflict with each other.

`sessions.token_hash` is the SHA-256 digest of the opaque httpOnly cookie
value - the raw token is never persisted (same lookup-hash pattern as
`service_account_keys.secret_hash`). No plaintext secret material appears
anywhere in this migration.

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Created by 0007 - referenced here with create_type=False so this migration
# never attempts a second CREATE TYPE.
user_org_role_enum = postgresql.ENUM("org_admin", "auditor", name="user_org_role", create_type=False)


def upgrade() -> None:
    # NULL = no org-wide role (ordinary member/team_lead - that role lives
    # on `team_memberships.role` instead).
    op.add_column("users", sa.Column("org_role", user_org_role_enum, nullable=True))
    # OIDC `sub` claim - the durable auth-lookup key (not email, which can
    # change or be reassigned).
    op.add_column("users", sa.Column("sso_subject", sa.String(), nullable=True))
    # IdP-asserted email, display only - never used for auth lookup.
    op.add_column("users", sa.Column("sso_email", sa.String(), nullable=True))
    op.create_index(
        "ix_users_sso_subject",
        "users",
        ["sso_subject"],
        unique=True,
        postgresql_where=sa.text("sso_subject IS NOT NULL"),
    )

    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # CASCADE (not RESTRICT): a deleted user's sessions die with them -
        # a session is not a credential row that must block user deletion.
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # SHA-256 digest (32 bytes) of the opaque cookie value.
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        # NULL = active, non-NULL = revoked as of that timestamp (same
        # convention as `service_account_keys.revoked_at`).
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_sessions_token_hash", "sessions", ["token_hash"], unique=True)
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_index("ix_sessions_token_hash", table_name="sessions")
    op.drop_table("sessions")

    op.drop_index("ix_users_sso_subject", table_name="users")
    op.drop_column("users", "sso_email")
    op.drop_column("users", "sso_subject")
    op.drop_column("users", "org_role")
