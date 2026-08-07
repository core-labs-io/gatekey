"""`CanaryBaseline` - the established "normal" latency/refusal/output
reference point for one `(model, prompt)` pair (Phase 5 - Differentiators,
5.4 Provider Drift Detector).

See `gatekey/phase-5-technical-design.md` sections 2.2/4.2 for the full
design rationale. Once 7 days of `canary_runs` exist for a `(model,
prompt_id)` pair with no baseline row yet, `services/drift_detector.py`'s
`establish_baseline_if_ready()` (backend-developer task) computes and
inserts exactly one row here - this table is never updated in place after
that (a v1 simplicity choice; re-baselining is a fast-follow concern, not
built this phase).

Composite `(model, prompt_id)` primary key - at most one baseline per model
per canary prompt. `prompt_id` cascades on `canary_prompts` deletion, but
in practice `canary_prompts` rows are never deleted by application code
(only the 5 migration-seeded rows exist) - the cascade exists for schema
correctness, not because deletion is an expected operation.

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

from sqlalchemy import DateTime, ForeignKey, Numeric, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from gatekey.db.base import Base


class CanaryBaseline(Base):
    __tablename__ = "canary_baselines"

    model: Mapped[str] = mapped_column(Text, primary_key=True)
    prompt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("canary_prompts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Decimal (not float) for every Numeric column - see e.g. `usage_log.py`/
    # `user.py`'s identical convention for `cost_usd`/`budget_usd`.
    baseline_latency_ms: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    baseline_refusal_rate: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    baseline_output_text: Mapped[str] = mapped_column(Text, nullable=False)

    established_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
