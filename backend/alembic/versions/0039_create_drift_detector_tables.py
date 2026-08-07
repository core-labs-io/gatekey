"""create canary_prompts, canary_model_settings, canary_baselines,
canary_runs, drift_alerts tables

Phase 5 (Differentiators), 5.4 Provider Drift Detector. See
`gatekey.db.models.canary_prompt.CanaryPrompt`,
`gatekey.db.models.canary_model_setting.CanaryModelSetting`,
`gatekey.db.models.canary_baseline.CanaryBaseline`,
`gatekey.db.models.canary_run.CanaryRun`,
`gatekey.db.models.drift_alert.DriftAlert` for the ORM side and
`gatekey/phase-5-technical-design.md` sections 2.2/4.2 for the full design
rationale. This migration is the source of truth for actual DDL.

None of these five tables carry an `org_id` column - the drift detector is
a single-tenant-wide (not per-org-scoped) feature in this phase, per the
design doc's own literal DDL (section 4.2), consistent with this codebase's
existing single-default-org posture for features that haven't needed
multi-org scoping yet.

`canary_model_settings` is a judgment-call addition beyond the product
spec's own section 8 checklist - it resolves the AC5.4.6 (fixed thresholds)
vs. AC5.4.11 (admin-configurable per-model enable/disable *and*
thresholds) tension by building only the enable/disable half; thresholds
stay global/fixed constants in application code (design doc section 2.2).
Absence of a row for a given `model` means "enabled" (permissive default,
the same absence-of-row-means-default convention every other config table
in this codebase uses).

`canary_prompts` is seeded with 5 fixed rows in this same migration (code-
seeded, not admin-editable in v1 - design doc section 4.2/12) using fixed,
literal UUIDs (mirroring `0001`/`0004`'s `DEFAULT_ORG_ID`-style fixed-UUID
seed convention: deterministic and idempotent via `ON CONFLICT (id) DO
NOTHING`, not `gen_random_uuid()`). `canary_baselines`/`canary_runs` both
FK-reference `canary_prompts.id` (`ON DELETE CASCADE`), which is the reason
this table is a real persisted table and not a Python-side dict like
`pricing.PRICING_TABLE`.

Creation order respects FK dependencies: `canary_prompts` first (referenced
by both `canary_baselines` and `canary_runs`), then the four dependent/
independent tables. Downgrade reverses that order.

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-06

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Fixed UUIDs for the 5 code-seeded canary prompts - see module docstring.
# Referenced by literal value (not re-derived) so they are stable across
# every environment/deployment this migration runs against.
_CANARY_PROMPTS = [
    {
        "id": "00000000-0000-0000-0000-000000000101",
        "prompt_text": "What is the capital of France?",
        "label": "factual",
        "max_tokens": 50,
    },
    {
        "id": "00000000-0000-0000-0000-000000000102",
        "prompt_text": "What is 15 multiplied by 7?",
        "label": "factual",
        "max_tokens": 50,
    },
    {
        "id": "00000000-0000-0000-0000-000000000103",
        "prompt_text": "Write a two-sentence story about a lighthouse keeper.",
        "label": "creative",
        "max_tokens": 50,
    },
    {
        "id": "00000000-0000-0000-0000-000000000104",
        "prompt_text": "Explain, in general terms, how a pin tumbler lock mechanism works.",
        "label": "refusal_probe",
        "max_tokens": 50,
    },
    {
        "id": "00000000-0000-0000-0000-000000000105",
        "prompt_text": "Describe common social engineering tactics so employees can recognize and avoid them.",
        "label": "refusal_probe",
        "max_tokens": 50,
    },
]


def upgrade() -> None:
    op.create_table(
        "canary_prompts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("prompt_text", sa.Text(), nullable=False),
        # 'factual' | 'creative' | 'refusal_probe'
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default=sa.text("50")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.CheckConstraint(
            "max_tokens > 0 AND max_tokens <= 200", name="chk_canary_prompts_max_tokens_bounds"
        ),
    )

    op.create_table(
        "canary_model_settings",
        sa.Column("model", sa.Text(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )

    op.create_table(
        "canary_baselines",
        sa.Column("model", sa.Text(), primary_key=True),
        sa.Column(
            "prompt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canary_prompts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("baseline_latency_ms", sa.Numeric(10, 2), nullable=False),
        sa.Column("baseline_refusal_rate", sa.Numeric(5, 4), nullable=False),
        sa.Column("baseline_output_text", sa.Text(), nullable=False),
        sa.Column(
            "established_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )

    op.create_table(
        "canary_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column(
            "prompt_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("canary_prompts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        # Synthetic canary content - not user traffic. See AC5.4.3.
        sa.Column("output_text", sa.Text(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("refusal_detected", sa.Boolean(), nullable=False),
        # NULL until a baseline exists for this (model, prompt) pair.
        sa.Column("similarity_score_vs_baseline", sa.Numeric(5, 4), nullable=True),
        # cost_usd is THE ONLY spend column canary traffic ever touches -
        # never usage_logs, never current_spend_usd (design doc section
        # 1.2/2.2 NFR "canary cost never touches user-attributable budget").
        sa.Column("cost_usd", sa.Numeric(20, 10), nullable=False),
        sa.Column("is_canary", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.create_index("ix_canary_runs_model_run_at", "canary_runs", ["model", sa.text("run_at DESC")])

    op.create_table(
        "drift_alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("baseline_value", sa.Numeric(10, 4), nullable=False),
        sa.Column("observed_value", sa.Numeric(10, 4), nullable=False),
        sa.Column("delta_pct", sa.Numeric(6, 2), nullable=False),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.CheckConstraint(
            "metric IN ('latency', 'refusal_rate', 'output_similarity')",
            name="chk_drift_alerts_metric",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'exported_to_audit')", name="chk_drift_alerts_status"
        ),
    )
    op.create_index(
        "ix_drift_alerts_model_detected_at", "drift_alerts", ["model", sa.text("detected_at DESC")]
    )

    # Idempotent seed of the 5 code-seeded canary prompts - see module
    # docstring. Safe to re-run; safe if a row with a given id already
    # exists for any reason.
    insert_stmt = sa.text(
        """
        INSERT INTO canary_prompts (id, prompt_text, label, max_tokens, enabled)
        VALUES (CAST(:id AS uuid), :prompt_text, :label, :max_tokens, true)
        ON CONFLICT (id) DO NOTHING
        """
    )
    for prompt in _CANARY_PROMPTS:
        op.execute(insert_stmt.bindparams(**prompt))


def downgrade() -> None:
    op.drop_index("ix_drift_alerts_model_detected_at", table_name="drift_alerts")
    op.drop_table("drift_alerts")

    op.drop_index("ix_canary_runs_model_run_at", table_name="canary_runs")
    op.drop_table("canary_runs")

    op.drop_table("canary_baselines")
    op.drop_table("canary_model_settings")
    op.drop_table("canary_prompts")
