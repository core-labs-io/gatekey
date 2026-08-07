"""`DriftAlert` - a flagged, statistically-fixed-threshold drift event for
one `(model, metric)` pair (Phase 5 - Differentiators, 5.4 Provider Drift
Detector).

See `gatekey/phase-5-technical-design.md` sections 2.2/4.2 for the full
design rationale. Written by `services/drift_detector.py::flag_drift`
(backend-developer task) - a rolling 7-run window vs. the established
`canary_baselines` row, compared against fixed global thresholds (AC5.4.6:
50%/20pp/0.7, not admin-configurable - see `CanaryModelSetting`'s module
docstring for the AC5.4.6/AC5.4.11 tension this resolves).

`metric` is constrained to exactly the three drift dimensions the design
computes: `'latency'`, `'refusal_rate'`, `'output_similarity'`.

`status` starts `'open'` and transitions to `'exported_to_audit'` via
`POST /v1/admin/drift-detector/alerts/{id}/export` (backend-developer task),
which also writes a real `AuditEntry` (`action="drift.alert_exported"`) -
this table itself is never the audit record, only the operational alert
queue; the audit trail is the durable compliance-facing record once
exported.

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

from sqlalchemy import CheckConstraint, DateTime, Index, Numeric, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from gatekey.db.base import Base


class DriftAlert(Base):
    __tablename__ = "drift_alerts"
    __table_args__ = (
        Index("ix_drift_alerts_model_detected_at", "model", text("detected_at DESC")),
        CheckConstraint(
            "metric IN ('latency', 'refusal_rate', 'output_similarity')",
            name="chk_drift_alerts_metric",
        ),
        CheckConstraint("status IN ('open', 'exported_to_audit')", name="chk_drift_alerts_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    # 'latency' | 'refusal_rate' | 'output_similarity' - see module docstring.
    metric: Mapped[str] = mapped_column(Text, nullable=False)
    # Decimal (not float) for every Numeric column - see e.g. `usage_log.py`/
    # `user.py`'s identical convention for `cost_usd`/`budget_usd`.
    baseline_value: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    observed_value: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    delta_pct: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)

    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # 'open' | 'exported_to_audit' - see module docstring.
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'open'"))
