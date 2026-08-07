"""create orgs and provider_keys tables

Phase 1.1 (Provider & Key Management) - the first two tables in the
project. See `gatekey.db.models.org.Org` / `gatekey.db.models.provider_key.
ProviderKey` for the ORM side; this migration is the source of truth for
actual DDL.

Also seeds a single default org (fixed id
`00000000-0000-0000-0000-000000000001`) since Phase 1.1 is a single-org
slice with no multi-org signup flow yet - the app layer can rely on this
row existing rather than special-casing "no org yet". The insert is
idempotent (`ON CONFLICT (id) DO NOTHING`) so this migration is a clean,
repeatable forward path.

No plaintext key material appears anywhere in this migration - only the
opaque `ciphertext`/`nonce`/`auth_tag` byte columns are created; no seed
data touches those columns.

Revision ID: 0001
Revises:
Create Date: 2026-07-11

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Fixed UUID for the single default org seeded by this migration. Referenced
# by literal value (not re-derived) so it is stable across every
# environment/deployment this migration runs against.
DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_ORG_NAME = "Default Org"

PROVIDER_ENUM_NAME = "provider_name"
PROVIDER_VALUES = ("openai", "anthropic", "vertex_ai")


def upgrade() -> None:
    bind = op.get_bind()

    # `create_type=False`: we create the enum type explicitly, once, right
    # below. Without this flag, `op.create_table()` would *also* try to
    # auto-create the same type while building the `provider_keys.provider`
    # column (Postgres ENUM columns default to create_type=True), which
    # fails with `DuplicateObjectError` since checkfirst doesn't reliably
    # short-circuit that second, implicit creation path under the async
    # dialect. Explicit create + create_type=False avoids the double-create
    # entirely rather than relying on checkfirst to paper over it.
    provider_enum = postgresql.ENUM(
        *PROVIDER_VALUES, name=PROVIDER_ENUM_NAME, create_type=False
    )
    provider_enum.create(bind, checkfirst=True)

    op.create_table(
        "orgs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "provider_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", provider_enum, nullable=False),
        # Envelope-encryption pieces (AES-256-GCM) - always written together
        # by the app layer, so always NOT NULL. No plaintext key column
        # exists on this table by design.
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("auth_tag", sa.LargeBinary(), nullable=False),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.UniqueConstraint(
            "org_id", "provider", name="uq_provider_keys_org_id_provider"
        ),
    )

    # Idempotent seed of the single default org. Safe to re-run; safe if a
    # row with this id already exists for any reason.
    op.execute(
        sa.text(
            """
            INSERT INTO orgs (id, name, created_at)
            VALUES (CAST(:id AS uuid), :name, now())
            ON CONFLICT (id) DO NOTHING
            """
        ).bindparams(id=DEFAULT_ORG_ID, name=DEFAULT_ORG_NAME)
    )


def downgrade() -> None:
    # Child table first (FK + NOT NULL org_id -> orgs.id), then parent.
    op.drop_table("provider_keys")
    op.drop_table("orgs")

    bind = op.get_bind()
    postgresql.ENUM(*PROVIDER_VALUES, name=PROVIDER_ENUM_NAME).drop(bind, checkfirst=True)
