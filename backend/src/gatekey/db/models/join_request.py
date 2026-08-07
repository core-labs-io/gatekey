"""`JoinRequest` - the onboarding/approval workflow row (Phase 2 -
Multi-Tenant Governance).

See `docs/design/phase-2-multi-tenant-governance-design.md` section 1.5 for
the full design rationale.

AC6.4 ("one pending request per user at a time") is a schema-level
invariant: the partial unique index `uq_join_requests_one_pending_per_user`
(`ON (requester_user_id) WHERE status = 'pending'`) makes a second pending
INSERT fail with an `IntegrityError`, which the service layer maps to a
clean 409 - never pre-check-then-insert.

Snapshots vs. live state: `requester_name` (AC6.2's editable IdP claim) and
`routed_to` are captured at submit time and never rewritten. Approval
(AC6.7) sets `status`/`resolved_at`/`resolved_by_user_id`/
`approved_budget_usd` in the same locked transaction that creates the
`TeamMembership` - no intermediate approved-but-unbudgeted state.

FK semantics: `team_id` is `ON DELETE RESTRICT` (a team's request history -
pending *or* historical - blocks its deletion, never silently orphaned);
`resolved_by_user_id` is `SET NULL` (the resolution record outlives the
resolver's user row); `requester_user_id` is `CASCADE` (a deleted
requester's requests go with them).

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0010_create_join_requests.py` - that migration, not
`Base.metadata.create_all()`, is the source of truth for actual DDL
(including the `join_request_status`/`join_request_routed_to` enum types,
created by `0007` - hence `create_type=False` below).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.team import Team


class JoinRequestStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class JoinRequestRoutedTo(str, enum.Enum):
    TEAM_LEAD = "team_lead"
    ORG_ADMIN = "org_admin"


# `create_type=False`: DDL for these Postgres enum types is owned
# exclusively by the Alembic migration (`0007_create_org_settings_teams_and_
# team_model_policies.py`) - see `model_policy.py`'s
# `model_policy_mode_enum` for the identical rationale/pattern.
join_request_status_enum = PGEnum(
    JoinRequestStatus,
    name="join_request_status",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)
join_request_routed_to_enum = PGEnum(
    JoinRequestRoutedTo,
    name="join_request_routed_to",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)


class JoinRequest(Base):
    __tablename__ = "join_requests"
    # See module docstring "Migration ownership" - must match `0010` exactly.
    __table_args__ = (
        Index("ix_join_requests_team_id_status", "team_id", "status"),
        Index("ix_join_requests_requester_user_id", "requester_user_id"),
        # AC6.4 as a schema-level invariant - see module docstring.
        Index(
            "uq_join_requests_one_pending_per_user",
            "requester_user_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    requester_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Snapshot at submit time - independent of `users.name`.
    requester_name: Mapped[str] = mapped_column(String, nullable=False)
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[JoinRequestStatus] = mapped_column(
        join_request_status_enum, nullable=False, server_default=text("'pending'")
    )
    # Snapshot at submit time - live queue visibility is NOT solely derived
    # from this column (design doc section 4.3).
    routed_to: Mapped[JoinRequestRoutedTo] = mapped_column(
        join_request_routed_to_enum, nullable=False
    )
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    # Set only when status = 'approved'. NUMERIC(20, 10) per ADR-1 in
    # `db/models/user.py`.
    approved_budget_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 10), nullable=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    team: Mapped["Team"] = relationship("Team")
