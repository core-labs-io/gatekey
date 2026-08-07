"""create self_hosted_providers table, add usage_logs.self_hosted_provider_id

Phase 5 (Differentiators), 5.5 Unified BYOK+Self-Hosted Governance. See
`gatekey.db.models.self_hosted_provider.SelfHostedProvider` and
`gatekey.db.models.usage_log.UsageLog` for the ORM side and
`gatekey/phase-5-technical-design.md` sections 2.3/4.1/4.2 for the full
design rationale. This migration is the source of truth for actual DDL.

`self_hosted_providers` is a genuinely separate table from `provider_keys`,
not a new `provider_name_enum` member (product spec section 9 judgment call
#9) - it supports multiple named self-hosted endpoints per org (matching
the admin UI mock) without a Postgres enum-type migration, and its
`ciphertext`/`nonce`/`auth_tag` columns are the byte-for-byte identical
AES-256-GCM envelope shape `provider_keys` already uses (see that table's
own module docstring "Encryption fields") - encrypted at rest, no plaintext
`bearer_token` column, ever. The distinct associated-data binding
(`org_id:self_hosted:{self_hosted_provider_id}`, vs. `provider_keys`'
`org_id:provider`) is an application-layer concern
(`services/self_hosted_providers.py`, backend-developer task) - not
expressible as schema, but noted here so the two tables' otherwise-
identical envelope shape is never mistaken for interchangeable ciphertext.

`usage_logs.self_hosted_provider_id` is nullable + `ON DELETE SET NULL`,
following the exact same "a historical usage record must outlive the
entity that generated it" pattern every other nullable FK on this table
uses (`user_id`/`service_account_key_id`/`team_id`/`personal_api_key_id`/
`failover_key_id` - see `usage_log.py`'s module docstring). `usage_logs.
provider` (existing plain-string column, untouched here) takes the literal
value `"self_hosted"` for these rows at the application layer - no
`provider_name_enum` migration needed, since that Postgres enum type is
untouched this phase.

Creation order: `self_hosted_providers` before the `usage_logs` FK column
that references it (same "create referenced table before the referencing
column" ordering `0025`/`0030` already established). Downgrade reverses
that order.

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-06

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0040"
down_revision: Union[str, None] = "0039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "self_hosted_providers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        # AES-256-GCM envelope pieces - identical shape to
        # `provider_keys.ciphertext`/`.nonce`/`.auth_tag`. See module
        # docstring. Never nullable; no plaintext bearer_token column exists.
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("auth_tag", sa.LargeBinary(), nullable=False),
        sa.Column("cost_basis_per_gpu_hour", sa.Numeric(10, 4), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "models", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
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
            "cost_basis_per_gpu_hour > 0", name="chk_self_hosted_providers_cost_basis_positive"
        ),
        sa.UniqueConstraint("org_id", "name", name="uq_self_hosted_providers_org_id_name"),
    )

    op.add_column(
        "usage_logs",
        sa.Column(
            "self_hosted_provider_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("self_hosted_providers.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_usage_logs_self_hosted_provider_id", "usage_logs", ["self_hosted_provider_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_usage_logs_self_hosted_provider_id", table_name="usage_logs")
    op.drop_column("usage_logs", "self_hosted_provider_id")

    op.drop_table("self_hosted_providers")
