"""`DegradationEvent` - records model downgrades for cost savings calculation (Phase 4).

See `docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.7
for the full design rationale. This table tracks when graceful degradation
substitutes an expensive model with a cheaper fallback, enabling cost savings
calculations in the dashboard.

Each degradation event records:
- team_id/user_id: for multi-tenant isolation
- request_id: link to the original request log
- original_model: the model that was requested
- degraded_model: the model that was substituted (cheaper fallback)
- original_cost: estimated cost of the original model
- degraded_cost: actual cost incurred with the degraded model
- created_at: when degradation occurred

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0032_create_degradation_events.py` - that migration, not
`Base.metadata.create_all()`, is the source of truth for actual DDL. (Prior
to that migration this table was never actually created in Postgres, and
this model was not registered in `db/models/__init__.py` / `Base.metadata` -
see the Phase 4 schema/code-drift audit fix that added both.)

`request_id` FK target correction
----------------------------------
This originally pointed at `request_logs.id`, a table that has never existed
in this codebase - Phase 1's actual persisted per-request record is
`usage_logs` (see `gatekey.db.models.usage_log.UsageLog`). Corrected to
`ForeignKey("usage_logs.id", ondelete="SET NULL")` - `SET NULL` (not
`CASCADE`/`RESTRICT`) so this cost-savings history record survives deletion
of its originating usage-log row, same "never lose history" posture
`usage_logs.failover_key_id` already establishes for its own FK.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.usage_log import UsageLog


class DegradationEvent(Base):
    """A record of model downgrading via graceful degradation.

    When a user/team is approaching their budget limit (within the configured
    threshold), Gatekey can automatically substitute a cheaper model for the
    requested one. This table records each such downgrade for cost savings
    tracking and dashboard reporting.
    """

    __tablename__ = "degradation_events"
    # See module docstring "Migration ownership" - must match `0032` exactly.
    __table_args__ = (
        # AC4.5's dashboard cost-savings-over-time queries filter by team and
        # order/range by created_at - same shape as
        # `usage_logs.ix_usage_logs_org_id_created_at`.
        Index("ix_degradation_events_team_id_created_at", "team_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # See module docstring "request_id FK target correction".
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("usage_logs.id", ondelete="SET NULL"), nullable=True
    )
    original_model: Mapped[str] = mapped_column(Text, nullable=False)
    degraded_model: Mapped[str] = mapped_column(Text, nullable=False)
    original_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False
    )
    degraded_cost: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # Relationships
    request: Mapped["UsageLog | None"] = relationship("UsageLog")
