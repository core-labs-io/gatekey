"""create model_policies table

Phase 1.3 (Model Access Governance - Basic). See
`gatekey.db.models.model_policy.ModelPolicy` for the ORM side and its
module docstring for the "absence-of-row = unconfigured" rationale (ADR-2
in `docs/design/phase-1.3-model-governance.md`); this migration is the
source of truth for actual DDL.

Like `provider_keys`/`service_account_keys`, this table is scoped to the
single default org (`00000000-0000-0000-0000-000000000001`) seeded by
`0001_create_orgs_and_provider_keys.py` - no multi-org signup flow exists
yet. `org_id` is the primary key here (not a surrogate `id`), per ADR-1:
by product design there is never more than one policy row per org, so
"exactly one policy per org" is a schema-level invariant rather than an
app-enforced one.

The `model_policy_mode` Postgres enum intentionally has exactly two
values, `allowlist`/`denylist` - never `unconfigured`. Per ADR-2, the
product-level "unconfigured" state is represented by the *absence* of a
row for the org, not a third enum value; this keeps the write path
(`PUT /v1/admin/model-policy`) structurally unable to persist
"unconfigured" as a stored mode.

No seed data - unlike `0001`, there is nothing to seed here (absence of a
row is the correct initial state for every org).

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-15

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MODEL_POLICY_MODE_ENUM_NAME = "model_policy_mode"
MODEL_POLICY_MODE_VALUES = ("allowlist", "denylist")


def upgrade() -> None:
    bind = op.get_bind()

    # `create_type=False`: we create the enum type explicitly, once, right
    # below. Without this flag, `op.create_table()` would *also* try to
    # auto-create the same type while building the `model_policies.mode`
    # column (Postgres ENUM columns default to create_type=True), which
    # fails with `DuplicateObjectError` since checkfirst doesn't reliably
    # short-circuit that second, implicit creation path under the async
    # dialect. Explicit create + create_type=False avoids the double-create
    # entirely rather than relying on checkfirst to paper over it. See
    # `0001_create_orgs_and_provider_keys.py`'s identical comment for the
    # first use of this pattern.
    mode_enum = postgresql.ENUM(
        *MODEL_POLICY_MODE_VALUES, name=MODEL_POLICY_MODE_ENUM_NAME, create_type=False
    )
    mode_enum.create(bind, checkfirst=True)

    op.create_table(
        "model_policies",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("mode", mode_enum, nullable=False),
        sa.Column(
            "models",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
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


def downgrade() -> None:
    # Table before enum type (the column depends on the type existing).
    op.drop_table("model_policies")

    bind = op.get_bind()
    postgresql.ENUM(*MODEL_POLICY_MODE_VALUES, name=MODEL_POLICY_MODE_ENUM_NAME).drop(
        bind, checkfirst=True
    )
