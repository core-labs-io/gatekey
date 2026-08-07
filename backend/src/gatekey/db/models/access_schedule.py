"""`AccessSchedule` - an org/team/service-account-key scheduled access
window (Phase 3 - Security & Compliance Hardening).

See `docs/design/phase-3-security-compliance-design.md` sections 1.12 and 5
for the full design rationale. Same one-row-per-scope
partial-unique-index pattern as `ResidencyRule`/`RotationPolicy`, across all
three scope levels (org, team, service-account key).

No `timezone`/`holiday_calendar_ref` columns on this table - timezone lives
once on `compliance_settings.access_schedule_timezone` (AC9.4, a single
org-wide setting), and per ratified #10 there is no calendar-ref
indirection at all: holidays are the flat org-wide `holiday_dates` list
below, a deviation from the product spec's tentative calendar-ref shape.

Write-time narrowing validation (a child schedule's `allowed_days`/
`allowed_hours` must be a subset of its resolved parent's) is enforced only
at the service layer (`services.access_schedules.
validate_schedule_narrows_parent`), identical defense-in-depth shape to
`ResidencyRule`'s AC3.2 check - not expressible as a static DB constraint
since the parent schedule it must narrow against is itself mutable.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0021_create_access_schedules_holiday_dates_and_emergency_
overrides.py` - that migration, not `Base.metadata.create_all()`, is the
source of truth for actual DDL.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Time, func, text
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org
    from gatekey.db.models.service_account_key import ServiceAccountKey
    from gatekey.db.models.team import Team


class AccessScheduleScopeType(str, enum.Enum):
    ORG = "org"
    TEAM = "team"
    SERVICE_ACCOUNT = "service_account"


# `create_type=False`: DDL for this Postgres enum type is owned exclusively
# by the Alembic migration (`0021_create_access_schedules_holiday_dates_and_
# emergency_overrides.py`) - see `model_policy.py`'s
# `model_policy_mode_enum` for the identical rationale/pattern.
access_schedule_scope_type_enum = PGEnum(
    AccessScheduleScopeType,
    name="access_schedule_scope_type",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)


class AccessSchedule(Base):
    __tablename__ = "access_schedules"
    # See module docstring "Migration ownership" - must match `0021` exactly.
    __table_args__ = (
        Index(
            "uq_access_schedules_org_wide",
            "org_id",
            unique=True,
            postgresql_where=text("scope_type = 'org'"),
        ),
        Index(
            "uq_access_schedules_team_scoped",
            "scope_team_id",
            unique=True,
            postgresql_where=text("scope_team_id IS NOT NULL"),
        ),
        Index(
            "uq_access_schedules_sa_scoped",
            "scope_service_account_id",
            unique=True,
            postgresql_where=text("scope_service_account_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    scope_type: Mapped[AccessScheduleScopeType] = mapped_column(
        access_schedule_scope_type_enum, nullable=False
    )
    scope_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=True
    )
    scope_service_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_account_keys.id", ondelete="CASCADE"),
        nullable=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # ISO weekday ints 1(Mon)-7(Sun).
    allowed_days: Mapped[list[int]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    allowed_hours_start: Mapped[time | None] = mapped_column(Time, nullable=True)
    allowed_hours_end: Mapped[time | None] = mapped_column(Time, nullable=True)

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
    team: Mapped["Team | None"] = relationship("Team")
    service_account: Mapped["ServiceAccountKey | None"] = relationship("ServiceAccountKey")
