"""`FailoverEvent` - one persisted row per successful reactive failover
switch from a primary provider key to its configured backup (Phase 4 -
Reliability & Cost Efficiency).

See `docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.4
for the full design rationale. Written by the provider-call wrapper
(`services.provider_key_health`, backend-developer task) only when the
backup call actually succeeds after the primary's call failed - never for a
primary failure with no configured/eligible backup, and never for a primary
failure whose backup also fails (that path re-raises the primary's original
error unchanged, AC1.7).

`ON DELETE SET NULL` (not `CASCADE`) on both `from_provider_key_id`/
`to_provider_key_id` - a failover event is history that must survive a later
key deletion, same "never lose history" posture as `audit_entries.target_id`.

`detected_at`/`switched_at` are stored as two timestamps, not a precomputed
`duration_ms` column - the admin API (`GET /v1/admin/failover-events`)
computes the detection-to-switch duration at read time, avoiding a derived
value that could drift from its source columns.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0025_create_failover_events.py` - that migration, not
`Base.metadata.create_all()`, is the source of truth for actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org
    from gatekey.db.models.provider_key import ProviderKey


class FailoverEvent(Base):
    __tablename__ = "failover_events"
    # See module docstring "Migration ownership" - must match `0025` exactly.
    __table_args__ = (
        Index("ix_failover_events_org_id_created_at", "org_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    # Nullable + SET NULL - see module docstring.
    from_provider_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_keys.id", ondelete="SET NULL"), nullable=True
    )
    to_provider_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_keys.id", ondelete="SET NULL"), nullable=True
    )
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    # When the primary's failing call returned.
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # When the backup call succeeded.
    switched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    org: Mapped["Org"] = relationship("Org")
    from_provider_key: Mapped["ProviderKey | None"] = relationship(
        "ProviderKey", foreign_keys=[from_provider_key_id]
    )
    to_provider_key: Mapped["ProviderKey | None"] = relationship(
        "ProviderKey", foreign_keys=[to_provider_key_id]
    )
