"""create org_settings, teams, and team_model_policies tables plus all Phase 2 enums

Phase 2 (Multi-Tenant Governance), DB-1. See
`docs/design/phase-2-multi-tenant-governance-design.md` sections 1.1-1.3 for
the full design rationale; `gatekey.db.models.org_settings.OrgSettings` /
`gatekey.db.models.team.Team` /
`gatekey.db.models.team_model_policy.TeamModelPolicy` for the ORM side. This
migration is the source of truth for actual DDL.

All six Phase 2 enums are created here, even though `team_role` /
`join_request_status` / `join_request_routed_to` / `user_org_role` are only
consumed by later migrations (0008/0009/0010) - matching `0001`'s precedent
of creating an enum ahead of the table that needs it, and keeping every
`CREATE TYPE` in one reviewable place.

`org_settings` and `team_model_policies` both use the entity-id-as-PK /
absence-of-row-means-default pattern established by `model_policies`
(ADR-1/ADR-2 in `phase-1.3-model-governance.md`): no seed row is inserted
anywhere - absence is the correct initial state.

`teams.webhook_ciphertext`/`webhook_nonce`/`webhook_auth_tag`: a Slack-style
incoming-webhook URL embeds a bearer-equivalent secret, so it is stored as
an AES-256-GCM envelope (same three-piece discipline as `provider_keys`),
never as plaintext. No plaintext secret material appears anywhere in this
migration.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-04

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# All six Phase 2 enums, created here (see module docstring). Values must
# stay in lockstep with the enum classes in `gatekey.db.models`.
ENUMS: dict[str, tuple[str, ...]] = {
    "user_org_role": ("org_admin", "auditor"),
    "team_role": ("team_lead", "member"),
    "team_period_type": ("monthly", "quarterly"),
    "team_period_end": ("rollover", "reset"),
    "join_request_status": ("pending", "approved", "rejected"),
    "join_request_routed_to": ("team_lead", "org_admin"),
}


def _enum(name: str) -> postgresql.ENUM:
    # `create_type=False`: the type is created explicitly in upgrade(), once
    # - see `0001_create_orgs_and_provider_keys.py` for the full rationale
    # (avoids the implicit double-create `op.create_table()` would attempt).
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name, create_type=False).create(bind, checkfirst=True)

    op.create_table(
        "org_settings",
        # `org_id` as PK, not a surrogate id - "exactly one settings row per
        # org" is a schema-level invariant (mirrors `model_policies`' ADR-1).
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # NULL = no org-wide ceiling. NUMERIC(20, 10) per Phase 1.4's ADR-1
        # precision convention (see `db/models/user.py`).
        sa.Column("budget_ceiling_usd", sa.Numeric(precision=20, scale=10), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default=sa.text("'USD'")),
        # NULL = no max.
        sa.Column("max_self_serve_key_expiration_days", sa.Integer(), nullable=True),
        sa.Column("personal_key_soft_cap", sa.Integer(), nullable=False, server_default=sa.text("10")),
        sa.Column(
            "auto_provision_personal_key_on_approval",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
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
        "teams",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        # NULL = unmetered team ceiling.
        sa.Column("budget_ceiling_usd", sa.Numeric(precision=20, scale=10), nullable=True),
        # Denormalized aggregate of the team's memberships' spend,
        # transactionally maintained by the service layer (design doc ADR-7).
        sa.Column(
            "current_spend_usd",
            sa.Numeric(precision=20, scale=10),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "period_type",
            _enum("team_period_type"),
            nullable=False,
            server_default=sa.text("'monthly'"),
        ),
        sa.Column(
            "on_period_end",
            _enum("team_period_end"),
            nullable=False,
            server_default=sa.text("'reset'"),
        ),
        sa.Column(
            "current_period_started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("alert_threshold_80_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("alert_threshold_100_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("webhook_alert_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        # AES-256-GCM envelope for the webhook URL - always written together
        # by the app layer (all three NULL, or all three set). No plaintext
        # URL column exists on this table by design (see module docstring).
        sa.Column("webhook_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("webhook_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("webhook_auth_tag", sa.LargeBinary(), nullable=True),
        sa.Column("email_alert_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")),
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
        sa.UniqueConstraint("org_id", "name", name="uq_teams_org_id_name"),
    )
    op.create_index("ix_teams_org_id", "teams", ["org_id"])

    op.create_table(
        "team_model_policies",
        # `team_id` as PK - at most one restriction row per team; absence of
        # a row = "no further restriction beyond the org baseline". No
        # `mode` column: a team overlay is always a narrowing allowlist.
        sa.Column(
            "team_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("teams.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "models",
            postgresql.JSONB(),
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
    # Child table first (FK to teams), then teams, then org_settings.
    op.drop_table("team_model_policies")
    op.drop_index("ix_teams_org_id", table_name="teams")
    op.drop_table("teams")
    op.drop_table("org_settings")

    bind = op.get_bind()
    # Safe to drop all six here: every later migration that consumes one of
    # these types (0008/0009/0010) has already been downgraded by the time
    # Alembic reaches this revision's downgrade.
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).drop(bind, checkfirst=True)
