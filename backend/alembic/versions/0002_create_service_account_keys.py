"""create service_account_keys table

Phase 1.2 (Unified API / Gateway Core) - per-app service-account
credentials used to authenticate gateway requests
(`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`). See
`gatekey.db.models.service_account_key.ServiceAccountKey` for the ORM
side and its module docstring for full rationale; this migration is the
source of truth for actual DDL.

Like `provider_keys`, this table is scoped to the single default org
(`00000000-0000-0000-0000-000000000001`) seeded by
`0001_create_orgs_and_provider_keys.py` - no multi-org signup flow exists
yet.

No plaintext secret material appears anywhere in this migration or on
this table - only the opaque `secret_hash` (SHA-256 digest) and
`key_prefix` (non-secret identification label) columns are created. See
the `ServiceAccountKey` model docstring for why `secret_hash` is a plain
SHA-256 digest rather than a slow password KDF (bcrypt/argon2/scrypt):
this is a high-entropy random token, not a human password, and the
gateway's request-latency budget cannot absorb a deliberately slow hash
on every authenticated request.

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-14

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "service_account_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        # First chars of the plaintext secret after the `gk_sk_` prefix -
        # identification-only, never used for auth lookup.
        sa.Column("key_prefix", sa.String(length=12), nullable=False),
        # SHA-256 digest (32 bytes) of the full plaintext secret. No
        # plaintext secret column exists on this table, by design.
        sa.Column("secret_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # NULL = active, non-NULL = revoked as of that timestamp. No
        # separate `is_active` boolean - see model docstring.
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "ix_service_account_keys_org_id",
        "service_account_keys",
        ["org_id"],
    )
    op.create_index(
        "ix_service_account_keys_secret_hash",
        "service_account_keys",
        ["secret_hash"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_service_account_keys_secret_hash", table_name="service_account_keys"
    )
    op.drop_index("ix_service_account_keys_org_id", table_name="service_account_keys")
    op.drop_table("service_account_keys")
