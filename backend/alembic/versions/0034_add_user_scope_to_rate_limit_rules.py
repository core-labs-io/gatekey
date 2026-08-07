"""add 'user' value to rate_limit_scope_type, add rate_limit_rules.scope_user_id

Phase 4 (Reliability & Cost Efficiency), AC4.2.9 schema-gap fix. AC4.2.9
requires a genuine, admin-configured, individual per-user rate limit that is
additive to the team's shared pool ("a team with limit 100 rpm and a user
with limit 50 rpm can send up to 150 rpm total"). Previously `scope_type`
only supported `org_default_per_user` (one uniform value applied to every
user - not a per-user override) and `team` (one shared team pool) - there
was no schema-level way to store an individual user's own limit at all.
This migration adds a third `scope_type` value, `'user'`, plus a
`scope_user_id` column, mirroring the existing `scope_team_id` shape/pattern
exactly (nullable FK, `ON DELETE CASCADE`, one-row-per-scope partial unique
index). This is schema-only - `services/rate_limit.py`'s enforcement/lookup
logic and the admin API's read/write surface for this new scope are
explicitly a separate, follow-up (backend-developer) task, not touched here.

Enum value addition via type rebuild, not `ALTER TYPE ... ADD VALUE`
--------------------------------------------------------------------------
Unlike `0006`'s `provider_name` enum addition (a genuinely one-way,
NOT-implemented-downgrade change), this migration rebuilds the
`rate_limit_scope_type` type (`RENAME` old -> `CREATE` new with the extra
value -> `ALTER COLUMN ... USING` cast -> `DROP` old) so that `downgrade()`
is a real, working reversal rather than a documented dead end - consistent
with this task's requirement that every new migration's `downgrade()`
actually works, not just be defined. This is the same rebuild technique
Postgres itself recommends for reversible enum changes when `ALTER TYPE ...
DROP VALUE` isn't available.

Every object that references `scope_type` by value in its definition (the
two existing partial unique indexes and the existing two-way `CHECK`) must
be dropped before `ALTER COLUMN ... TYPE` and recreated after - Postgres
resolves an index/constraint's `'literal'` operands against the column's
type *at definition time*, so leaving them in place across the type swap
produces "operator does not exist: rate_limit_scope_type =
rate_limit_scope_type_old"-style errors (verified against a real Postgres
16 instance while writing this migration).

`rate_limit_rejection_events.scope_type` (added by `0026` alongside
`rate_limit_rules.scope_type`, same enum type, no default value/index/CHECK
referencing it by value) is cast to the rebuilt type in the same step, for
the same reason: the old enum type cannot be dropped while any column in
the database - not just `rate_limit_rules.scope_type` - still depends on it.

`CHECK` constraint is extended to a three-way mutual-exclusion check
(`org_default_per_user` <-> neither `scope_team_id` nor `scope_user_id` set;
`team` <-> only `scope_team_id` set; `user` <-> only `scope_user_id` set) -
same one-row-per-scope discipline `ck_rate_limit_rules_scope_type_matches_
scope_team_id` already enforced for the two-way case.

`uq_rate_limit_rules_user_scoped` is the same partial-unique-index shape as
`uq_rate_limit_rules_team_scoped` - at most one rule row per
`scope_user_id`, i.e. one rule per user (a user only ever belongs to one
org in this codebase's current single-org-per-user model, so `(org_id,
user_id)` and `(user_id)` are equivalent uniqueness scopes here, same
reasoning `uq_rate_limit_rules_team_scoped` already relies on for teams).

Downgrade note: the enum-rebuild step in `downgrade()` fails if any row has
`scope_type = 'user'` at the time of downgrade (the cast to the narrower,
two-value type has no target for that row) - the same data-dependent-
reversibility caveat `0023`'s uniqueness-relaxation downgrade already
carries. Delete or re-scope any `'user'`-scoped rows before downgrading past
this revision.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-05

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0034"
down_revision: Union[str, None] = "0033"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ENUM_NAME = "rate_limit_scope_type"
OLD_VALUES = ("org_default_per_user", "team")
NEW_VALUES = ("org_default_per_user", "team", "user")

OLD_CHECK_NAME = "ck_rate_limit_rules_scope_type_matches_scope_team_id"
NEW_CHECK_NAME = "ck_rate_limit_rules_scope_type_matches_scope_id"

ORG_DEFAULT_INDEX = "uq_rate_limit_rules_org_default"
TEAM_SCOPED_INDEX = "uq_rate_limit_rules_team_scoped"
USER_SCOPED_INDEX = "uq_rate_limit_rules_user_scoped"


def _recreate_org_and_team_indexes() -> None:
    op.create_index(
        ORG_DEFAULT_INDEX,
        "rate_limit_rules",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'org_default_per_user'"),
    )
    op.create_index(
        TEAM_SCOPED_INDEX,
        "rate_limit_rules",
        ["scope_team_id"],
        unique=True,
        postgresql_where=sa.text("scope_team_id IS NOT NULL"),
    )


def upgrade() -> None:
    # Every existing index/CHECK that references `scope_type` by value must
    # be dropped before the column's type changes underneath it, and
    # recreated after - see module docstring.
    op.drop_index(ORG_DEFAULT_INDEX, table_name="rate_limit_rules")
    op.drop_index(TEAM_SCOPED_INDEX, table_name="rate_limit_rules")
    op.drop_constraint(OLD_CHECK_NAME, "rate_limit_rules", type_="check")

    # --- Rebuild the enum type with the new 'user' value (see module
    # docstring "Enum value addition via type rebuild") ---
    op.execute(f"ALTER TYPE {ENUM_NAME} RENAME TO {ENUM_NAME}_old")
    new_enum = postgresql.ENUM(*NEW_VALUES, name=ENUM_NAME)
    new_enum.create(op.get_bind(), checkfirst=False)
    op.execute(
        f"ALTER TABLE rate_limit_rules "
        f"ALTER COLUMN scope_type TYPE {ENUM_NAME} "
        f"USING scope_type::text::{ENUM_NAME}"
    )
    # See module docstring "rate_limit_rejection_events.scope_type" - the
    # only other column in the database using this enum type.
    op.execute(
        f"ALTER TABLE rate_limit_rejection_events "
        f"ALTER COLUMN scope_type TYPE {ENUM_NAME} "
        f"USING scope_type::text::{ENUM_NAME}"
    )
    op.execute(f"DROP TYPE {ENUM_NAME}_old")

    _recreate_org_and_team_indexes()

    # --- scope_user_id column, mirroring scope_team_id's shape ---
    op.add_column(
        "rate_limit_rules",
        sa.Column(
            "scope_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )

    # --- CHECK: three-way mutual exclusion, replacing the two-way check ---
    op.create_check_constraint(
        NEW_CHECK_NAME,
        "rate_limit_rules",
        "(scope_type = 'org_default_per_user' AND scope_team_id IS NULL "
        "AND scope_user_id IS NULL) OR "
        "(scope_type = 'team' AND scope_team_id IS NOT NULL "
        "AND scope_user_id IS NULL) OR "
        "(scope_type = 'user' AND scope_team_id IS NULL "
        "AND scope_user_id IS NOT NULL)",
    )

    # --- one rule per user, same partial-unique-index pattern as teams ---
    op.create_index(
        USER_SCOPED_INDEX,
        "rate_limit_rules",
        ["scope_user_id"],
        unique=True,
        postgresql_where=sa.text("scope_user_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(USER_SCOPED_INDEX, table_name="rate_limit_rules")
    op.drop_constraint(NEW_CHECK_NAME, "rate_limit_rules", type_="check")
    op.drop_column("rate_limit_rules", "scope_user_id")

    # Same "drop before type change, recreate after" requirement as upgrade()
    # - see module docstring.
    op.drop_index(ORG_DEFAULT_INDEX, table_name="rate_limit_rules")
    op.drop_index(TEAM_SCOPED_INDEX, table_name="rate_limit_rules")

    # See module docstring "Downgrade note": fails if any row still has
    # scope_type = 'user'.
    op.execute(f"ALTER TYPE {ENUM_NAME} RENAME TO {ENUM_NAME}_new")
    old_enum = postgresql.ENUM(*OLD_VALUES, name=ENUM_NAME)
    old_enum.create(op.get_bind(), checkfirst=False)
    op.execute(
        f"ALTER TABLE rate_limit_rules "
        f"ALTER COLUMN scope_type TYPE {ENUM_NAME} "
        f"USING scope_type::text::{ENUM_NAME}"
    )
    op.execute(
        f"ALTER TABLE rate_limit_rejection_events "
        f"ALTER COLUMN scope_type TYPE {ENUM_NAME} "
        f"USING scope_type::text::{ENUM_NAME}"
    )
    op.execute(f"DROP TYPE {ENUM_NAME}_new")

    _recreate_org_and_team_indexes()

    op.create_check_constraint(
        OLD_CHECK_NAME,
        "rate_limit_rules",
        "(scope_type = 'org_default_per_user' AND scope_team_id IS NULL) OR "
        "(scope_type = 'team' AND scope_team_id IS NOT NULL)",
    )
