"""create compliance_settings, dlp_policies, dlp_custom_patterns, and
team_dlp_action_overrides tables plus the dlp_action enum

Phase 3 (Security & Compliance Hardening), DB-1. See
`docs/design/phase-3-security-compliance-design.md` sections 1.2-1.5 for the
full design rationale; `gatekey.db.models.compliance_settings.ComplianceSettings`
/ `gatekey.db.models.dlp_policy.DlpPolicy` /
`gatekey.db.models.dlp_custom_pattern.DlpCustomPattern` /
`gatekey.db.models.team_dlp_action_override.TeamDlpActionOverride` for the ORM
side. This migration is the source of truth for actual DDL.

`compliance_settings`/`dlp_policies` are both `org_id`-as-PK,
absence-of-row-means-default tables (same `ModelPolicy`/`OrgSettings` ADR-1/
ADR-2 shape) - no seed row is inserted anywhere. `team_dlp_action_overrides`
mirrors `team_model_policies`' `team_id`-as-PK shape exactly (at most one
override row per team). Detector toggles on `dlp_policies` default `false`
and `default_action` defaults `'log'` - this phase's off-by-default,
least-disruptive posture (design doc section 1.3).

The `dlp_action` enum (`log`/`redact`/`block`) is created here since
`dlp_policies`/`dlp_custom_patterns`/`team_dlp_action_overrides` all need it
immediately; `dlp_scan_results` (migration 0018) reuses the same type without
recreating it.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DLP_ACTION_VALUES = ("log", "redact", "block")


def _dlp_action_enum() -> postgresql.ENUM:
    # `create_type=False`: the type is created explicitly in upgrade(), once
    # - see `0001_create_orgs_and_provider_keys.py` for the full rationale.
    return postgresql.ENUM(*DLP_ACTION_VALUES, name="dlp_action", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    postgresql.ENUM(*DLP_ACTION_VALUES, name="dlp_action", create_type=False).create(
        bind, checkfirst=True
    )

    op.create_table(
        "compliance_settings",
        # `org_id` as PK - exactly one settings row per org (mirrors
        # `org_settings`'/`model_policies`' ADR-1/ADR-2).
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # NULL = never auto-purged (ratified #1).
        sa.Column("audit_retention_days", sa.Integer(), nullable=True),
        sa.Column(
            "log_prompt_retention_days", sa.Integer(), nullable=False, server_default=sa.text("30")
        ),
        # AC9.4: one org-wide timezone, no per-scope override.
        sa.Column(
            "access_schedule_timezone", sa.String(), nullable=False, server_default=sa.text("'UTC'")
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

    op.create_table(
        "dlp_policies",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ssn_detector_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "credit_card_detector_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("email_detector_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("phone_detector_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "default_action", _dlp_action_enum(), nullable=False, server_default=sa.text("'log'")
        ),
        # Ratified #3: default to NOT storing raw flagged substrings.
        sa.Column(
            "store_raw_flagged_content", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        # Ratified #4.
        sa.Column(
            "scan_inbound_responses", sa.Boolean(), nullable=False, server_default=sa.text("false")
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

    op.create_table(
        "dlp_custom_patterns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        # Regex source - validated compilable at write time (service layer,
        # no DB constraint can express this).
        sa.Column("pattern", sa.String(), nullable=False),
        sa.Column("action", _dlp_action_enum(), nullable=False),
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
        sa.UniqueConstraint("org_id", "name", name="uq_dlp_custom_patterns_org_id_name"),
    )
    op.create_index("ix_dlp_custom_patterns_org_id", "dlp_custom_patterns", ["org_id"])

    op.create_table(
        "team_dlp_action_overrides",
        # `team_id` as PK - at most one override row per team, mirrors
        # `team_model_policies`' shape exactly. No `created_at`/`updated_at`
        # - not part of the design doc's column list for this table.
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("action", _dlp_action_enum(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("team_dlp_action_overrides")
    op.drop_index("ix_dlp_custom_patterns_org_id", table_name="dlp_custom_patterns")
    op.drop_table("dlp_custom_patterns")
    op.drop_table("dlp_policies")
    op.drop_table("compliance_settings")

    bind = op.get_bind()
    postgresql.ENUM(*DLP_ACTION_VALUES, name="dlp_action").drop(bind, checkfirst=True)
