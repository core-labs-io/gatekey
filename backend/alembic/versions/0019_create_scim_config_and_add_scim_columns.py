"""create scim_config table and add scim columns to users/teams

Phase 3 (Security & Compliance Hardening), DB-6. See
`docs/design/phase-3-security-compliance-design.md` section 1.10 for the full
design rationale; `gatekey.db.models.scim_config.ScimConfig` /
`gatekey.db.models.user.User` / `gatekey.db.models.team.Team` for the ORM
side. This migration is the source of truth for actual DDL.

`users.scim_external_id` / `teams.scim_external_id` are the IdP's durable
per-resource identifier (SCIM's own `externalId`), the correlation key for
`PUT`/`PATCH` idempotency, distinct from `users.sso_subject` (the OIDC `sub`
claim used for SSO login correlation - design doc section 6.3). Both are
nullable with a partial unique index (`WHERE ... IS NOT NULL`), same pattern
as `users.sso_subject`. `users.scim_deactivated_at` is a durable block flag
(ratified #8). `scim_config` follows the identical one-row-per-org,
hash-only-secret shape as every other org-scoped singleton config table in
this codebase (`bearer_token_hash` is SHA-256, same discipline as every
other credential lookup hash).

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "scim_config",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # SHA-256 digest of the bearer token - same hash-only-secret
        # discipline as every other credential in this codebase.
        sa.Column("bearer_token_hash", sa.LargeBinary(), nullable=True),
        sa.Column("token_created_at", sa.DateTime(timezone=True), nullable=True),
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

    op.add_column("users", sa.Column("scim_external_id", sa.String(), nullable=True))
    op.create_index(
        "ix_users_scim_external_id",
        "users",
        ["scim_external_id"],
        unique=True,
        postgresql_where=sa.text("scim_external_id IS NOT NULL"),
    )
    op.add_column("users", sa.Column("scim_deactivated_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column("teams", sa.Column("scim_external_id", sa.String(), nullable=True))
    op.create_index(
        "ix_teams_scim_external_id",
        "teams",
        ["scim_external_id"],
        unique=True,
        postgresql_where=sa.text("scim_external_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_teams_scim_external_id", table_name="teams")
    op.drop_column("teams", "scim_external_id")

    op.drop_column("users", "scim_deactivated_at")
    op.drop_index("ix_users_scim_external_id", table_name="users")
    op.drop_column("users", "scim_external_id")

    op.drop_table("scim_config")
