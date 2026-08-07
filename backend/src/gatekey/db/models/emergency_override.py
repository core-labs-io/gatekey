"""`EmergencyOverride` - a time-boxed, human-granted bypass of a
service-account key's resolved access schedule (Phase 3 - Security &
Compliance Hardening).

See `docs/design/phase-3-security-compliance-design.md` sections 1.12 and
5.3 for the full design rationale. Checked only on the access-schedule
rejection path (zero extra I/O in the common allowed case) - an active,
non-revoked, non-expired row for a `service_account_id` covering the
current instant allows the request through regardless of the resolved
schedule.

`reason` has a server-side non-empty `CHECK` (AC9.7 - not just a UI hint).
`granted_by_user_id` is `ON DELETE RESTRICT` (a grant record must never be
silently orphan-deleted via the granter's own deletion, same rationale as
every other credential/grant-record FK in this codebase); `revoked_by_
user_id` is `SET NULL` (the revocation record outlives the revoker's user
row, same pattern as `AuditEntry.actor_user_id`). Rows are never deleted -
history is preserved via `revoked_at`, same append-and-mark-not-delete
discipline as every other `revoked_at`-bearing table here.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0021_create_access_schedules_holiday_dates_and_emergency_
overrides.py` - that migration, not `Base.metadata.create_all()`, is the
source of truth for actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org
    from gatekey.db.models.service_account_key import ServiceAccountKey
    from gatekey.db.models.user import User


class EmergencyOverride(Base):
    __tablename__ = "emergency_overrides"
    # See module docstring "Migration ownership" - must match `0021` exactly.
    __table_args__ = (
        Index("ix_emergency_overrides_service_account_id", "service_account_id"),
        # AC9.7: server-side non-empty, not just a UI hint.
        CheckConstraint("length(reason) > 0", name="ck_emergency_overrides_reason_not_empty"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    service_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_account_keys.id", ondelete="CASCADE"),
        nullable=False,
    )
    granted_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(String, nullable=False)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # NULL = active, non-NULL = revoked as of that timestamp.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    org: Mapped["Org"] = relationship("Org")
    service_account: Mapped["ServiceAccountKey"] = relationship(
        "ServiceAccountKey", foreign_keys=[service_account_id]
    )
    granted_by: Mapped["User"] = relationship("User", foreign_keys=[granted_by_user_id])
    revoked_by: Mapped["User | None"] = relationship("User", foreign_keys=[revoked_by_user_id])
