"""create join_requests table

Phase 2 (Multi-Tenant Governance), DB-4. See
`docs/design/phase-2-multi-tenant-governance-design.md` section 1.5 for the
full design rationale and `gatekey.db.models.join_request.JoinRequest` for
the ORM side. This migration is the source of truth for actual DDL.

AC6.4 ("one pending request per user at a time") is enforced as a
schema-level invariant via the partial unique index
`uq_join_requests_one_pending_per_user` (`WHERE status = 'pending'`) - the
service layer maps the resulting `IntegrityError` to a 409, it never
pre-checks-then-inserts.

`team_id` is `ON DELETE RESTRICT`: a team cannot be deleted while any join
request (pending *or* historical) references it - request history stays
attached to a real team row, never silently orphaned. `requester_name` and
`routed_to` are snapshots taken at submit time, independent of later edits.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Created by 0007 - referenced here with create_type=False so this migration
# never attempts a second CREATE TYPE.
join_request_status_enum = postgresql.ENUM(
    "pending", "approved", "rejected", name="join_request_status", create_type=False
)
join_request_routed_to_enum = postgresql.ENUM(
    "team_lead", "org_admin", name="join_request_routed_to", create_type=False
)


def upgrade() -> None:
    op.create_table(
        "join_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requester_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Snapshot at submit time (AC6.2's editable IdP claim), independent
        # of `users.name`.
        sa.Column("requester_name", sa.String(), nullable=False),
        # RESTRICT: request history blocks team deletion (see module
        # docstring).
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "status",
            join_request_status_enum,
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        # Snapshot at submit time - live queue visibility is NOT solely
        # derived from this column (design doc section 4.3).
        sa.Column("routed_to", join_request_routed_to_enum, nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        # SET NULL: the resolution record survives deletion of the resolving
        # admin/lead's user row.
        sa.Column(
            "resolved_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # Set only when status = 'approved'.
        sa.Column("approved_budget_usd", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("rejection_reason", sa.String(), nullable=True),
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
    op.create_index("ix_join_requests_team_id_status", "join_requests", ["team_id", "status"])
    op.create_index("ix_join_requests_requester_user_id", "join_requests", ["requester_user_id"])
    # AC6.4 as a schema-level invariant - see module docstring.
    op.create_index(
        "uq_join_requests_one_pending_per_user",
        "join_requests",
        ["requester_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("uq_join_requests_one_pending_per_user", table_name="join_requests")
    op.drop_index("ix_join_requests_requester_user_id", table_name="join_requests")
    op.drop_index("ix_join_requests_team_id_status", table_name="join_requests")
    op.drop_table("join_requests")
