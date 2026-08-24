"""`TeamMembership` - one (team, user) pair with its per-pair role and
budget (Phase 2 - Multi-Tenant Governance).

See `docs/design/phase-2-multi-tenant-governance-design.md` section 1.4 for
the full design rationale.

This row is the budget counter A6 designates: once a user has at least one
membership, every NEW personal key and any team-attributed
`ServiceAccountKey` resolves budget against `budget_usd`/
`current_spend_usd` *here* - looked up by `(team_id, user_id)` - never the
legacy flat `User.budget_usd`. Same semantics as that flat counter:
`budget_usd = NULL` = unmetered for this pair, `current_spend_usd >=
budget_usd` = exhausted, charged via single `UPDATE ... RETURNING`
statements (see `services.budget`).

`role` is per-team (a user can be `team_lead` on one team and `member` on
another - AC1.2); org-wide roles live on `users.org_role` instead.

Removal (`removed_at`, added by `0049`)
----------------------------------------
Was a hard row delete; is now a soft marker - product owner request: an
accidental removal (or team deletion cascade... no, teams still block
delete while members exist) should be undoable. `removed_at IS NULL` =
active; NOT NULL = removed. The unique constraint on `(team_id, user_id)`
is DELIBERATELY UNCHANGED (still exactly one row, ever, ever per pair) -
"re-adding" a previously-removed user is `services.team_budget.
create_team_membership` transparently restoring + updating that same row,
not a second INSERT; see that function's docstring. Every query that
means "does this user currently have this membership" (budget checks,
gateway auth, RBAC, key-creation eligibility gates, alert recipients,
period rollover) MUST filter `removed_at IS NULL` - a query that doesn't
is very likely a real bug (a removed member silently retaining access/
budget), not a stylistic choice. `ON DELETE CASCADE` on both FKs is now
purely a safety net for the user/team row itself being hard-deleted
(unusual - `users`/`teams` are themselves never hard-deleted by normal
product flows either), not the primary removal path anymore.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0008_create_team_memberships.py` and `alembic/versions/
0049_soft_delete_team_memberships.py` - those migrations, not
`Base.metadata.create_all()`, are the source of truth for actual DDL
(including the `team_role` enum type, created by `0007` - hence
`create_type=False` below).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.team import Team
    from gatekey.db.models.user import User


class TeamRole(str, enum.Enum):
    TEAM_LEAD = "team_lead"
    MEMBER = "member"


# `create_type=False`: DDL for this Postgres enum type is owned exclusively
# by the Alembic migration (`0007_create_org_settings_teams_and_team_model_
# policies.py`) - see `model_policy.py`'s `model_policy_mode_enum` for the
# identical rationale/pattern.
team_role_enum = PGEnum(
    TeamRole,
    name="team_role",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)


class TeamMembership(Base):
    __tablename__ = "team_memberships"
    # See module docstring "Migration ownership" - must match `0008` exactly.
    __table_args__ = (
        UniqueConstraint("team_id", "user_id", name="uq_team_memberships_team_id_user_id"),
        Index("ix_team_memberships_user_id", "user_id"),
        Index("ix_team_memberships_team_id", "team_id"),
        # Added by `0049` - see module docstring "Removal".
        Index(
            "ix_team_memberships_active",
            "team_id",
            "user_id",
            postgresql_where=text("removed_at IS NULL"),
        ),
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
    role: Mapped[TeamRole] = mapped_column(
        team_role_enum, nullable=False, server_default=text("'member'")
    )

    # NULL = unmetered for this (user, team) pair. NUMERIC(20, 10) per
    # ADR-1 in `db/models/user.py`.
    budget_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)
    current_spend_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 10), nullable=False, server_default=text("0")
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
    # Soft-delete marker (added by `0049`) - see module docstring "Removal".
    # NULL = active (the overwhelming majority of rows, and of queries -
    # matches this column's own server default so existing rows are
    # unaffected by the migration).
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    team: Mapped["Team"] = relationship("Team")
    user: Mapped["User"] = relationship("User")
