"""create shadow_ai_ingest_events, known_ai_tool_hostnames, shadow_ai_ingest_config

Phase 5 (Differentiators), 5.1 Shadow AI Discovery. See
`gatekey.db.models.shadow_ai_ingest_event.ShadowAiIngestEvent`,
`gatekey.db.models.known_ai_tool_hostname.KnownAiToolHostname`,
`gatekey.db.models.shadow_ai_ingest_config.ShadowAiIngestConfig` for the ORM
side and `gatekey/phase-5-technical-design.md` sections 2.5/4.2 for the full
design rationale. This migration is the source of truth for actual DDL.

`shadow_ai_ingest_events` has no body/URL column by design (NFR: "Shadow-AI
collects connection metadata only - never full URLs, query strings, or
bodies", design doc section 1.2) - `raw_metadata` (JSONB, nullable) is
whatever bounded, non-content metadata the ingesting SASE/proxy tool
supplies beyond the named columns, never request/response content.

`known_ai_tool_hostnames` is seeded with a curated starter allowlist in this
same migration (idempotent, `ON CONFLICT (hostname) DO NOTHING`) - an admin
can add/remove hostnames afterward via the CRUD endpoint, this seed is just
a sane v1 starting point, not an exhaustive or admin-un-editable list.

`shadow_ai_ingest_config` is a singleton-per-org config+credential table -
`ingest_token_hash` is a SHA-256 digest (`LargeBinary`, nullable = "not yet
set up"), the exact same hash-only storage shape
`ScimConfig.bearer_token_hash` already establishes (`services.service_
accounts.hash_secret`), deliberately NOT the AES-256-GCM envelope
`provider_keys`/`self_hosted_providers` use - this is an inbound-only,
verify-a-presented-token credential (never decrypted/used outbound), so
hash-only storage is strictly more secure than a reversible envelope for
this shape of credential (design doc section 2.5, "This is a deliberate
revision of the orchestrator's brief"). `shadow_ai_retention_days` (default
90) lives on this table, not `compliance_settings` - the architect's
resolution per the design doc's section 4.2 literal DDL (the product
spec's own section 8 checklist said "`compliance_settings` (or org config
equivalent)"; this design puts it on the dedicated Shadow AI config
singleton instead, keeping Shadow AI's differential retention/backup
posture decoupled from the Phase 3 audit/DLP retention table, consistent
with this codebase's established "avoid coupling differential-retention
concerns into one shared settings row" precedent - see `compliance_
settings.py`'s own module docstring for why `audit_retention_days` lives on
its own table rather than folded into `org_settings` for the identical
reason).

Creation order: `shadow_ai_ingest_events.matched_user_id` FKs `users`
(already exists); no cross-dependency among the three new tables in this
migration, so order is immaterial for FK purposes - created in the order
listed above for readability. Downgrade reverses that order.

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-06

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0042"
down_revision: Union[str, None] = "0041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Matches `gatekey.constants.DEFAULT_ORG_ID` - hardcoded literal, mirroring
# `0001`/`0004`'s own convention of not importing app code into a migration.
DEFAULT_ORG_ID = "00000000-0000-0000-0000-000000000001"

# Curated starter allowlist - see module docstring. (hostname, tool_label)
_KNOWN_AI_TOOL_HOSTNAMES = [
    ("api.openai.com", "OpenAI API"),
    ("chat.openai.com", "ChatGPT"),
    ("chatgpt.com", "ChatGPT"),
    ("claude.ai", "Claude"),
    ("chat.deepseek.com", "DeepSeek"),
    ("gemini.google.com", "Gemini"),
    ("api.anthropic.com", "Anthropic API"),
]


def upgrade() -> None:
    op.create_table(
        "shadow_ai_ingest_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_identifier", sa.Text(), nullable=False),
        sa.Column(
            "matched_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("destination_host", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        # Connection metadata only - never body/URL/query-string content.
        # See module docstring.
        sa.Column("raw_metadata", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "source IN ('sase_log', 'proxy_log')", name="chk_shadow_ai_ingest_events_source"
        ),
    )
    op.create_index(
        "ix_shadow_ai_ingest_events_org_created",
        "shadow_ai_ingest_events",
        ["org_id", "created_at"],
    )
    op.create_index(
        "ix_shadow_ai_ingest_events_matched_user",
        "shadow_ai_ingest_events",
        ["matched_user_id"],
    )

    op.create_table(
        "known_ai_tool_hostnames",
        sa.Column("hostname", sa.Text(), primary_key=True),
        sa.Column("tool_label", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )

    op.create_table(
        "shadow_ai_ingest_config",
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # NULL = ingestion not yet set up (AC5.1.4) - fail-closed until an
        # admin generates a token. SHA-256 digest, same hash-only shape as
        # `scim_config.bearer_token_hash`. See module docstring.
        sa.Column("ingest_token_hash", sa.LargeBinary(), nullable=True),
        sa.Column("token_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "detection_source", sa.Text(), nullable=False, server_default=sa.text("'sase_log'")
        ),
        sa.Column(
            "enforcement_mode", sa.Text(), nullable=False, server_default=sa.text("'detect_only'")
        ),
        sa.Column("webhook_url", sa.Text(), nullable=True),
        sa.Column(
            "shadow_ai_retention_days",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("90"),
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
            "detection_source IN ('sase_log', 'proxy_log')",
            name="chk_shadow_ai_ingest_config_detection_source",
        ),
        sa.CheckConstraint(
            "enforcement_mode IN ('detect_only', 'notification', 'webhook')",
            name="chk_shadow_ai_ingest_config_enforcement_mode",
        ),
        sa.CheckConstraint(
            "shadow_ai_retention_days > 0",
            name="chk_shadow_ai_ingest_config_retention_days_positive",
        ),
    )

    # Idempotent seed of the curated starter allowlist - see module
    # docstring.
    insert_stmt = sa.text(
        """
        INSERT INTO known_ai_tool_hostnames (hostname, tool_label, enabled)
        VALUES (:hostname, :tool_label, true)
        ON CONFLICT (hostname) DO NOTHING
        """
    )
    for hostname, tool_label in _KNOWN_AI_TOOL_HOSTNAMES:
        op.execute(insert_stmt.bindparams(hostname=hostname, tool_label=tool_label))


def downgrade() -> None:
    op.drop_table("shadow_ai_ingest_config")
    op.drop_table("known_ai_tool_hostnames")

    op.drop_index("ix_shadow_ai_ingest_events_matched_user", table_name="shadow_ai_ingest_events")
    op.drop_index("ix_shadow_ai_ingest_events_org_created", table_name="shadow_ai_ingest_events")
    op.drop_table("shadow_ai_ingest_events")
