"""`HolidayDate` - a flat, org-wide holiday date entry consulted by
scheduled access windows (Phase 3 - Security & Compliance Hardening).

See `docs/design/phase-3-security-compliance-design.md` sections 1.12 and
5.2 for the full design rationale. No calendar-ref indirection (ratified
#10) - just a flat `(org_id, holiday_date)` list, evaluated against the
org-local date derived from `compliance_settings.access_schedule_timezone`
(never a UTC-date comparison, since a UTC-date vs. org-local-date mismatch
near midnight would make the wrong day's holiday status apply).

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0021_create_access_schedules_holiday_dates_and_emergency_
overrides.py` - that migration, not `Base.metadata.create_all()`, is the
source of truth for actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org


class HolidayDate(Base):
    __tablename__ = "holiday_dates"
    # See module docstring "Migration ownership" - must match `0021` exactly.
    __table_args__ = (
        UniqueConstraint("org_id", "holiday_date", name="uq_holiday_dates_org_id_holiday_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False)
    label: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    org: Mapped["Org"] = relationship("Org")
