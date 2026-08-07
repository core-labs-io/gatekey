"""`ContentAwareRule` - an org-wide content-classification model-routing
rule (Phase 3 - Security & Compliance Hardening).

See `docs/design/phase-3-security-compliance-design.md` section 1.7 for the
full design rationale. Org-wide only (AC4.2 - no team-level override exists
in the UI spec). Composite `(org_id, category)` primary key - at most one
row per category per org.

`category` is deliberately `text`, not an enum. Per ratified #6 (Phase 3),
all three category rows the UI mock shows (`'pii'`, `'source_code'`,
`'financial_data'`) were ship-able (cheap, forward-compatible), but only
`category = 'pii'` was wired to a real signal that phase (`services/dlp.py`'s
findings) - `source_code`/`financial_data` rows persisted and rendered but
never received a triggered finding, since no classifier produced one yet.

Phase 5 (5.3, AC5.3.1/AC5.3.4) fulfills that forward-compat promise: a
FOURTH category, `'legal'`, was added (seed migration `0041`, no schema
change - this table's free-text `category` column already supported it),
and ALL FOUR categories (`'pii'`, `'source_code'`, `'financial_data'`,
`'legal'`) are now wired to a real classifier signal
(`services/dlp.py`'s `category_findings`, generalized from the Phase-3-only
`pii_detected: bool` - see `services/content_classifiers.py` for the two
new non-Presidio heuristics and `services/dlp.py`'s module-level comments
for the two new Presidio-engine-based ones) and functionally equivalent -
no category is inert anymore. Keeping `category` free-text (not a Postgres
enum) is exactly what made this possible without a schema change, per the
original design intent (design doc section 12).

`ModelAccessDecision.blocking_layer` gains the literal
`"content_classification"` at the Python type level only (no schema change).

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0016_create_content_aware_rules.py` - that migration, not
`Base.metadata.create_all()`, is the source of truth for actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org


class ContentAwareRule(Base):
    __tablename__ = "content_aware_rules"

    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orgs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # 'pii' (functional), 'source_code'/'financial_data' (inert) - see
    # module docstring.
    category: Mapped[str] = mapped_column(String, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    allowed_models: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    org: Mapped["Org"] = relationship("Org")
