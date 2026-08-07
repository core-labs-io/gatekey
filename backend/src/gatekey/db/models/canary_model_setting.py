"""`CanaryModelSetting` - per-model canary enable/disable (Phase 5 -
Differentiators, 5.4 Provider Drift Detector).

See `gatekey/phase-5-technical-design.md` section 2.2 for the full design
rationale. A judgment-call addition beyond the product spec's own section 8
data-model checklist: AC5.4.11 grants Org Admin the ability to "configure
per-model canary enable/disable and thresholds," but AC5.4.6 hardcodes the
drift thresholds as fixed constants. This table resolves that tension by
building only the enable/disable half - drift thresholds stay global/fixed
constants in application code (`services/drift_detector.py`, backend-
developer task), not a per-model column here.

Absence of a row for a given `model` means "enabled" - the same
absence-of-row-means-default convention every other config table in this
codebase uses (`ComplianceSettings`/`DlpPolicy`/`ModelPolicy`, etc.) - so an
Org Admin only ever needs to write a row to *disable* canary testing for a
specific model, never to opt every model in.

`model` is the primary key (not a surrogate `id`) - at most one row per
model string, and the row's only meaningful payload is `enabled` itself.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0039_create_drift_detector_tables.py` - that migration,
not `Base.metadata.create_all()`, is the source of truth for actual DDL.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from gatekey.db.base import Base


class CanaryModelSetting(Base):
    __tablename__ = "canary_model_settings"

    model: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
