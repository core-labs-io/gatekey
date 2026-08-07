"""create cli_refresh_credentials table

Phase 3 (Security & Compliance Hardening), DB-9. See
`docs/design/phase-3-security-compliance-design.md` section 8.2 for the full
design rationale and
`gatekey.db.models.cli_refresh_credential.CliRefreshCredential` for the ORM
side. This migration is the source of truth for actual DDL.

A long-lived refresh credential whose only power is calling
`GET /v1/me/current-key` - never usable directly against the gateway routes.
`secret_hash` is SHA-256 (`gk_rf_` prefix), same lookup-hash discipline as
every other credential in this codebase (no plaintext secret column, ever).
`ON DELETE CASCADE` on `user_id`/`bound_personal_key_id`: unlike
`service_account_keys`/`personal_api_keys` (which `RESTRICT` to protect a
live gateway credential from silent orphaning), this is a convenience
refresh token with no independent value once its owner or bound key is
gone - nothing is lost by letting it cascade-delete.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: Union[str, None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cli_refresh_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "bound_personal_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("personal_api_keys.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # SHA-256 digest of the full plaintext secret (`gk_rf_` prefix).
        sa.Column("secret_hash", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # NULL = active, non-NULL = revoked as of that timestamp.
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_cli_refresh_credentials_secret_hash",
        "cli_refresh_credentials",
        ["secret_hash"],
        unique=True,
    )
    op.create_index(
        "ix_cli_refresh_credentials_user_id", "cli_refresh_credentials", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_cli_refresh_credentials_user_id", table_name="cli_refresh_credentials")
    op.drop_index(
        "ix_cli_refresh_credentials_secret_hash", table_name="cli_refresh_credentials"
    )
    op.drop_table("cli_refresh_credentials")
