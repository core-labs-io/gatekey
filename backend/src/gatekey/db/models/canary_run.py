"""`CanaryRun` - one synthetic-prompt call result against one model, for the
drift detector (Phase 5 - Differentiators, 5.4 Provider Drift Detector).

See `gatekey/phase-5-technical-design.md` sections 2.2/4.2 for the full
design rationale. Written by `services/drift_detector.py::run_canary_suite_
for_org` (backend-developer task) once per `(model, prompt)` pair, per
scheduler tick that the model is due for testing.

**Cost isolation (NFR, design doc section 1.2)**: `cost_usd` is the ONLY
spend-tracking column any canary call ever touches - canary traffic never
writes a `usage_logs` row and never calls `record_usage_charge()`/mutates
`users.current_spend_usd`. This is what makes "canary cost never touches
user-attributable budget" a verifiable invariant (`SELECT count(*) FROM
usage_logs` referencing canary traffic is always zero) rather than a
convention that could silently drift.

`output_text` holds real (synthetic, non-user) model output - explicitly
not user traffic (AC5.4.3), stored so `establish_baseline_if_ready()` can
compute `canary_baselines.baseline_output_text` and so
`similarity_score_vs_baseline` can be recomputed/audited later if needed.

`similarity_score_vs_baseline` is `NULL` until a baseline exists for this
`(model, prompt_id)` pair (fewer than 7 days of runs) - not a proxy for
"no drift detected", genuinely "not yet computable".

`is_canary` is always `true` for every row this table will ever hold - kept
as an explicit column (rather than implied by "this row exists in this
table") so any future query/export code that might join or union canary
data with real usage data has an unambiguous discriminator column to filter
on, mirroring `usage_logs.provider = "self_hosted"`'s own discriminator-
column precedent (design doc section 1.2).

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0039_create_drift_detector_tables.py` - that migration,
not `Base.metadata.create_all()`, is the source of truth for actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from gatekey.db.base import Base


class CanaryRun(Base):
    __tablename__ = "canary_runs"
    __table_args__ = (Index("ix_canary_runs_model_run_at", "model", text("run_at DESC")),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("canary_prompts.id", ondelete="CASCADE"), nullable=False
    )

    run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Synthetic content - not user traffic. See module docstring.
    output_text: Mapped[str] = mapped_column(Text, nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    refusal_detected: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # NULL until a baseline exists for this (model, prompt) pair.
    similarity_score_vs_baseline: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4), nullable=True
    )
    # THE ONLY spend column canary traffic ever touches - see module
    # docstring "Cost isolation".
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    is_canary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
