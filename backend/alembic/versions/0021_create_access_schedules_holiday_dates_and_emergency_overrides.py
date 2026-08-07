"""create access_schedules, holiday_dates, and emergency_overrides tables
plus the access_schedule_scope_type enum

Phase 3 (Security & Compliance Hardening), DB-8. See
`docs/design/phase-3-security-compliance-design.md` section 1.12 for the full
design rationale; `gatekey.db.models.access_schedule.AccessSchedule` /
`gatekey.db.models.holiday_date.HolidayDate` /
`gatekey.db.models.emergency_override.EmergencyOverride` for the ORM side.
This migration is the source of truth for actual DDL.

`access_schedules` mirrors `residency_rules`'/`rotation_policies`' one-row-
per-scope partial-unique-index pattern across all three scope levels (org,
team, service-account key). No `timezone`/`holiday_calendar_ref` columns -
timezone lives once on `compliance_settings` (AC9.4), and holidays are a
flat org-wide date list (`holiday_dates`), per ratified #10 - a deviation
from the product spec's tentative calendar-ref indirection, made explicit
here. `emergency_overrides.reason` has a server-side non-empty `CHECK`
(AC9.7 - not just a UI hint).

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: Union[str, None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ACCESS_SCHEDULE_SCOPE_TYPE_VALUES = ("org", "team", "service_account")


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(
        *ACCESS_SCHEDULE_SCOPE_TYPE_VALUES, name="access_schedule_scope_type", create_type=False
    ).create(bind, checkfirst=True)

    op.create_table(
        "access_schedules",
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
                *ACCESS_SCHEDULE_SCOPE_TYPE_VALUES,
                name="access_schedule_scope_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "scope_team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "scope_service_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_account_keys.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # ISO weekday ints 1(Mon)-7(Sun).
        sa.Column(
            "allowed_days", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("allowed_hours_start", sa.Time(), nullable=True),
        sa.Column("allowed_hours_end", sa.Time(), nullable=True),
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
    op.create_index(
        "uq_access_schedules_org_wide",
        "access_schedules",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("scope_type = 'org'"),
    )
    op.create_index(
        "uq_access_schedules_team_scoped",
        "access_schedules",
        ["scope_team_id"],
        unique=True,
        postgresql_where=sa.text("scope_team_id IS NOT NULL"),
    )
    op.create_index(
        "uq_access_schedules_sa_scoped",
        "access_schedules",
        ["scope_service_account_id"],
        unique=True,
        postgresql_where=sa.text("scope_service_account_id IS NOT NULL"),
    )

    op.create_table(
        "holiday_dates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("holiday_date", sa.Date(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("org_id", "holiday_date", name="uq_holiday_dates_org_id_holiday_date"),
    )

    op.create_table(
        "emergency_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "service_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_account_keys.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "granted_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "revoked_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # AC9.7: server-side non-empty, not just a UI hint.
        sa.CheckConstraint("length(reason) > 0", name="ck_emergency_overrides_reason_not_empty"),
    )
    op.create_index(
        "ix_emergency_overrides_service_account_id", "emergency_overrides", ["service_account_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_emergency_overrides_service_account_id", table_name="emergency_overrides"
    )
    op.drop_table("emergency_overrides")

    op.drop_table("holiday_dates")

    op.drop_index("uq_access_schedules_sa_scoped", table_name="access_schedules")
    op.drop_index("uq_access_schedules_team_scoped", table_name="access_schedules")
    op.drop_index("uq_access_schedules_org_wide", table_name="access_schedules")
    op.drop_table("access_schedules")

    bind = op.get_bind()
    postgresql.ENUM(
        *ACCESS_SCHEDULE_SCOPE_TYPE_VALUES, name="access_schedule_scope_type"
    ).drop(bind, checkfirst=True)
