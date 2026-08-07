"""`CanaryPrompt` - one of the 5 fixed, code-seeded test prompts the drift
detector runs against every actively-used model (Phase 5 - Differentiators,
5.4 Provider Drift Detector).

See `gatekey/phase-5-technical-design.md` sections 2.2/4.2 for the full
design rationale. Persisted (not a Python dict like `providers.pricing.
PRICING_TABLE`) because `canary_baselines`/`canary_runs` both FK-reference
`id`. No admin-editable prompt authoring in v1 (design doc section 12) -
the 5 rows are seeded once by
`alembic/versions/0039_create_drift_detector_tables.py` using fixed,
literal UUIDs; nothing in the application ever inserts a new row into this
table at runtime.

`label` is one of `'factual'` / `'creative'` / `'refusal_probe'` -
deliberately free-text (not a Postgres enum), matching this codebase's
existing `content_aware_rules.category`/`usage_logs.status`-style
convention for small, code-controlled vocabularies that don't need DB-level
enforcement since only migration-seeded rows and read-only application code
ever populate this column.

`max_tokens` is bounded `(0, 200]` at the DB level - a defense-in-depth
backstop keeping the drift detector's own cost floor claim (AC5.4.10: 5
prompts x `max_tokens=50` x N models) from silently drifting upward even if
a future migration or manual `UPDATE` tried to raise it.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0039_create_drift_detector_tables.py` - that migration,
not `Base.metadata.create_all()`, is the source of truth for actual DDL.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, CheckConstraint, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from gatekey.db.base import Base


class CanaryPrompt(Base):
    __tablename__ = "canary_prompts"
    __table_args__ = (
        CheckConstraint(
            "max_tokens > 0 AND max_tokens <= 200", name="chk_canary_prompts_max_tokens_bounds"
        ),
    )

    # App-side UUID default (uuid.uuid4) for symmetry with every other
    # surrogate-PK table in this codebase - not exercised at runtime for
    # this table specifically, since the only rows that ever exist are the
    # 5 migration-seeded ones with their own fixed literal ids.
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 'factual' | 'creative' | 'refusal_probe' - see module docstring.
    label: Mapped[str] = mapped_column(Text, nullable=False)
    max_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("50"))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
