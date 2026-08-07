"""create rotation_policies table plus its enums, and add dual-secret
overlap columns to service_account_keys/provider_keys

Phase 3 (Security & Compliance Hardening), DB-7. See
`docs/design/phase-3-security-compliance-design.md` sections 1.11 and 4.3 for
the full design rationale; `gatekey.db.models.rotation_policy.RotationPolicy`
/ `gatekey.db.models.service_account_key.ServiceAccountKey` /
`gatekey.db.models.provider_key.ProviderKey` for the ORM side. This migration
is the source of truth for actual DDL.

`rotation_policies` follows the same one-row-per-scope partial-unique-index
pattern as `residency_rules`. The `CHECK` constraint enforces that exactly
one of `scope_service_account_id`/`scope_provider_key_id` is set, matching
`scope_type` - the `mode`/`scope_type` pairing itself (`service_account`
always `automatic`, `provider_key` always `manual_guided`, AC7.1) is left to
the service layer, not encoded as a DB `CHECK` (design doc section 1.11 -
no ad hoc writer exists elsewhere that could drift from it). The partial
index on `next_rotation_at WHERE enabled` is what the scheduler loop polls.

The `previous_secret_hash`/`previous_secret_valid_until` columns on
`service_account_keys` are the actual dual-secret overlap mechanism (no new
`RotationEvent` table - design doc section 4.3); the parallel
`previous_ciphertext`/`previous_nonce`/`previous_auth_tag`/
`previous_valid_until` columns on `provider_keys` exist purely for admin-
console display during the overlap window (not functionally load-bearing for
any live lookup - Gatekey is the only reader of a provider credential). No
plaintext secret material appears anywhere in this migration.

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: Union[str, None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROTATION_SCOPE_TYPE_VALUES = ("org", "service_account", "provider_key")
ROTATION_MODE_VALUES = ("automatic", "manual_guided")


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(
        *ROTATION_SCOPE_TYPE_VALUES, name="rotation_scope_type", create_type=False
    ).create(bind, checkfirst=True)
    postgresql.ENUM(*ROTATION_MODE_VALUES, name="rotation_mode", create_type=False).create(
        bind, checkfirst=True
    )

    op.create_table(
        "rotation_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "scope_type",
            postgresql.ENUM(
                *ROTATION_SCOPE_TYPE_VALUES, name="rotation_scope_type", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "scope_service_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_account_keys.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "scope_provider_key_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("provider_keys.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("interval_days", sa.Integer(), nullable=True),
        # e.g. '02:00' org-local; NULL falls back to the org off-hours
        # default.
        sa.Column("rotate_at_local_time", sa.Time(), nullable=True),
        sa.Column(
            "overlap_buffer_minutes", sa.Integer(), nullable=False, server_default=sa.text("5")
        ),
        sa.Column("next_rotation_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "mode",
            postgresql.ENUM(*ROTATION_MODE_VALUES, name="rotation_mode", create_type=False),
            nullable=False,
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
        sa.CheckConstraint(
            "(scope_type = 'org' AND scope_service_account_id IS NULL "
            "AND scope_provider_key_id IS NULL) OR "
            "(scope_type = 'service_account' AND scope_service_account_id IS NOT NULL "
            "AND scope_provider_key_id IS NULL) OR "
            "(scope_type = 'provider_key' AND scope_provider_key_id IS NOT NULL "
            "AND scope_service_account_id IS NULL)",
            name="ck_rotation_policies_scope_type_matches_scope_id",
        ),
    )
    op.create_index(
        "uq_rotation_policies_org_wide",
        "rotation_policies",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'org'"),
    )
    op.create_index(
        "uq_rotation_policies_sa_scoped",
        "rotation_policies",
        ["scope_service_account_id"],
        unique=True,
        postgresql_where=sa.text("scope_service_account_id IS NOT NULL"),
    )
    op.create_index(
        "uq_rotation_policies_pk_scoped",
        "rotation_policies",
        ["scope_provider_key_id"],
        unique=True,
        postgresql_where=sa.text("scope_provider_key_id IS NOT NULL"),
    )
    op.create_index(
        "ix_rotation_policies_next_rotation_at",
        "rotation_policies",
        ["next_rotation_at"],
        postgresql_where=sa.text("enabled"),
    )

    op.add_column(
        "service_account_keys", sa.Column("previous_secret_hash", sa.LargeBinary(), nullable=True)
    )
    op.add_column(
        "service_account_keys",
        sa.Column("previous_secret_valid_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_service_account_keys_previous_secret_hash",
        "service_account_keys",
        ["previous_secret_hash"],
        unique=True,
        postgresql_where=sa.text("previous_secret_hash IS NOT NULL"),
    )

    op.add_column("provider_keys", sa.Column("previous_ciphertext", sa.LargeBinary(), nullable=True))
    op.add_column("provider_keys", sa.Column("previous_nonce", sa.LargeBinary(), nullable=True))
    op.add_column("provider_keys", sa.Column("previous_auth_tag", sa.LargeBinary(), nullable=True))
    op.add_column(
        "provider_keys", sa.Column("previous_valid_until", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("provider_keys", "previous_valid_until")
    op.drop_column("provider_keys", "previous_auth_tag")
    op.drop_column("provider_keys", "previous_nonce")
    op.drop_column("provider_keys", "previous_ciphertext")

    op.drop_index(
        "ix_service_account_keys_previous_secret_hash", table_name="service_account_keys"
    )
    op.drop_column("service_account_keys", "previous_secret_valid_until")
    op.drop_column("service_account_keys", "previous_secret_hash")

    op.drop_index("ix_rotation_policies_next_rotation_at", table_name="rotation_policies")
    op.drop_index("uq_rotation_policies_pk_scoped", table_name="rotation_policies")
    op.drop_index("uq_rotation_policies_sa_scoped", table_name="rotation_policies")
    op.drop_index("uq_rotation_policies_org_wide", table_name="rotation_policies")
    op.drop_table("rotation_policies")

    bind = op.get_bind()
    postgresql.ENUM(*ROTATION_MODE_VALUES, name="rotation_mode").drop(bind, checkfirst=True)
    postgresql.ENUM(*ROTATION_SCOPE_TYPE_VALUES, name="rotation_scope_type").drop(
        bind, checkfirst=True
    )
