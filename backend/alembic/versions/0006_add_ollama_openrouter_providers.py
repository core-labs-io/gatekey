"""add ollama and openrouter to the provider_name enum

Phase 1.1/1.2/1.4 addition (Ollama & OpenRouter providers). See
`gatekey.db.models.provider_key.ProviderName` for the ORM side and
`docs/design/phase-1.1-1.2-1.4-ollama-openrouter-providers-design.md`
section 1 for the full rationale (transactional-DDL safety, downgrade
limitation).

No table DDL, no data backfill - no existing row references either new
value. `ALTER TYPE ... ADD VALUE IF NOT EXISTS` is used (not a plain
ADD VALUE) so this migration is safe to re-run against a database where it
was already partially/fully applied out-of-band.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-28

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PROVIDER_ENUM_NAME = "provider_name"
NEW_VALUES = ("ollama", "openrouter")


def upgrade() -> None:
    for value in NEW_VALUES:
        # Postgres 12+ permits ALTER TYPE ... ADD VALUE inside a transaction
        # block, as long as the new value is not *used* (compared, cast, or
        # inserted) within that same transaction - this migration only adds
        # the values, it never uses them, so it is safe under Alembic's
        # default transactional-DDL wrapping. See design doc section 1.2 for
        # the concrete verification database-admin must run before this is
        # considered done, not merely assumed safe.
        op.execute(f"ALTER TYPE {PROVIDER_ENUM_NAME} ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    # Postgres has no native "DROP VALUE FROM enum" primitive - removing an
    # enum value requires rebuilding the type (CREATE new type, ALTER every
    # column using it, DROP old type), which is destructive if any row still
    # references the value and out of proportion for this addition. This is
    # an honest, documented hard limitation, not a silent no-op: downgrading
    # past this revision is NOT SUPPORTED. If this must ever be reversed,
    # do it as a hand-written, reviewed one-off migration at that time, not
    # by trusting this function.
    raise NotImplementedError(
        "0006 cannot be downgraded: Postgres has no DROP VALUE for enum "
        "types. See this migration's module docstring / design doc section "
        "1.1 - a real reversal requires a hand-written type-rebuild "
        "migration, not implemented here."
    )
