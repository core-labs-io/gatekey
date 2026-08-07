"""seed content_aware_rules 'source_code'/'financial_data'/'legal' rows,
create sensitivity_label_mappings table

Phase 5 (Differentiators), 5.3 Content-Classification-Aware Routing. See
`gatekey.db.models.sensitivity_label_mapping.SensitivityLabelMapping` for
the ORM side and `gatekey/phase-5-technical-design.md` sections 2.4/4.2 for
the full design rationale. This migration is the source of truth for the
`sensitivity_label_mappings` DDL; the `content_aware_rules` half is a
data-only seed (no schema change - the existing `(org_id, category)`
composite PK from `0016_create_content_aware_rules.py` already supports
arbitrary category strings).

Seed-vs-on-demand-creation note (flagged for reviewer attention)
--------------------------------------------------------------------
`content_aware_rules` rows are *normally* created on demand in this
codebase - `services/model_policy.py::set_content_aware_rule` performs an
upsert (`INSERT ... ON CONFLICT (org_id, category) DO UPDATE`) the first
time an admin PUTs a category via `PUT /v1/admin/content-aware-rules/
{category}`, and no migration has ever pre-seeded a `'pii'` row (`0016`
only creates the empty table - confirmed by inspection). This migration
deliberately deviates from that absence-of-row-means-default precedent for
exactly these three new categories, per the design doc's explicit section
4.2 instruction (verbatim SQL, `ON CONFLICT (org_id, category) DO NOTHING`)
- the rationale given there is that pre-seeding lets the admin console list
all four categories (including `'source_code'`/`'financial_data'`/
`'legal'`) with a visible "disabled" state immediately, rather than only
after an admin has first touched each one. The `ON CONFLICT DO NOTHING`
makes this idempotent and defensive against an admin having already
created one of these rows manually before this migration runs (the
category string was never restricted at the schema level). Net effect: a
fresh org will have persisted (disabled) rows for `source_code`/
`financial_data`/`legal` but NOT for `pii`, an intentional asymmetry per
the design doc, not an oversight - flagged here for anyone auditing row
existence across categories.

`sensitivity_label_mappings` columns are taken verbatim from the design
doc's section 4.2 DDL (no `created_at`/`updated_at` - this table is a
small, admin-CRUD-only mapping list, not intended for its own historical
audit trail beyond the org-level `AuditEntry` writes the CRUD endpoints
themselves emit for every mutation, per the mandatory `write_audit_entry`
convention).

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-06

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0041"
down_revision: Union[str, None] = "0040"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Matches `gatekey.constants.DEFAULT_ORG_ID` - hardcoded literal, mirroring
# `0001`/`0004`'s own convention of not importing app code into a migration.
DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"

_NEW_CATEGORIES = ("source_code", "financial_data", "legal")


def upgrade() -> None:
    op.create_table(
        "sensitivity_label_mappings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("external_label", sa.Text(), nullable=False),
        sa.Column("gatekey_category", sa.Text(), nullable=False),
        sa.UniqueConstraint(
            "org_id", "external_label", name="uq_sensitivity_label_mappings_org_label"
        ),
    )

    # Idempotent seed - see module docstring "Seed-vs-on-demand-creation
    # note".
    insert_stmt = sa.text(
        """
        INSERT INTO content_aware_rules (org_id, category, enabled, allowed_models)
        VALUES (CAST(:org_id AS uuid), :category, false, '[]'::jsonb)
        ON CONFLICT (org_id, category) DO NOTHING
        """
    )
    for category in _NEW_CATEGORIES:
        op.execute(insert_stmt.bindparams(org_id=DEFAULT_ORG_ID, category=category))


def downgrade() -> None:
    # Only remove the exact rows this migration seeded, and only if they
    # were never enabled/customized by an admin - a blind DELETE here would
    # destroy real admin configuration if this migration is ever downgraded
    # after the feature has been used. Downgrading a data-seed migration
    # after real usage is inherently lossy for any *changed* row; this
    # scopes the loss to "seeded-and-never-touched" rows only.
    op.execute(
        sa.text(
            """
            DELETE FROM content_aware_rules
            WHERE org_id = CAST(:org_id AS uuid)
              AND category IN :categories
              AND enabled = false
              AND allowed_models = '[]'::jsonb
            """
        ).bindparams(
            sa.bindparam("categories", expanding=True),
            org_id=DEFAULT_ORG_ID,
            categories=list(_NEW_CATEGORIES),
        )
    )

    op.drop_table("sensitivity_label_mappings")
