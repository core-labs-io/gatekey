"""`RateLimitRule` - an org-default-per-user or team-aggregate rate-limit
config row (Phase 4 - Reliability & Cost Efficiency).

See `docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.5
for the full design rationale. Same one-row-per-scope partial-unique-index
pattern as `residency_rules`/`rotation_policies`/`access_schedules` - the
fourth application of this exact pattern in this codebase. The `CHECK`
constraint enforces `scope_team_id` is set iff `scope_type = 'team'`.

This is a Postgres config table, not the hot-path counter store - the actual
per-minute counters live in the shared-state store
(`services.shared_state.SharedStateStore`, backend-developer task), never in
this table. `requests_per_min`/`tokens_per_min` are both independently
nullable (NULL = that axis unlimited); `on_limit` controls whether a trip on
this rule rejects immediately or queues-and-polls up to
`max_queue_wait_seconds`.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0026_create_rate_limit_rules_and_rejection_events.py` -
that migration, not `Base.metadata.create_all()`, is the source of truth for
actual DDL.

`scope_type = 'user'` / `scope_user_id` (schema/code-drift fix)
------------------------------------------------------------------
AC4.2.9 requires a genuine, admin-configured, individual per-user rate limit
additive to the team's shared pool - not representable by the original two
scope values (`org_default_per_user` is one uniform value applied to every
user, not a per-user override; `team` is one shared team pool). Added by
`alembic/versions/0034_add_user_scope_to_rate_limit_rules.py`, which is now
the additional source-of-truth migration for this column/enum value/
constraint set alongside `0026`. `scope_user_id` mirrors `scope_team_id`'s
shape exactly (nullable FK, `ON DELETE CASCADE`, one-row-per-scope partial
unique index). Schema-only: `services/rate_limit.py`'s enforcement/lookup
logic and the admin API's read/write surface for this scope are a separate,
not-yet-built follow-up.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, func, text
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org
    from gatekey.db.models.team import Team
    from gatekey.db.models.user import User


class RateLimitScopeType(str, enum.Enum):
    ORG_DEFAULT_PER_USER = "org_default_per_user"
    TEAM = "team"
    # See module docstring "scope_type = 'user' / scope_user_id".
    USER = "user"


class RateLimitOnLimit(str, enum.Enum):
    REJECT = "reject"
    QUEUE_RETRY = "queue_retry"


# `create_type=False`: DDL for these Postgres enum types is owned
# exclusively by the Alembic migration
# (`0026_create_rate_limit_rules_and_rejection_events.py`) - see
# `model_policy.py`'s `model_policy_mode_enum` for the identical
# rationale/pattern.
rate_limit_scope_type_enum = PGEnum(
    RateLimitScopeType,
    name="rate_limit_scope_type",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)
rate_limit_on_limit_enum = PGEnum(
    RateLimitOnLimit,
    name="rate_limit_on_limit",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)


class RateLimitRule(Base):
    __tablename__ = "rate_limit_rules"
    # See module docstring "Migration ownership" - must match `0026` +
    # `0033`/`0034` exactly.
    __table_args__ = (
        Index(
            "uq_rate_limit_rules_org_default",
            "org_id",
            unique=True,
            postgresql_where=text("scope_type = 'org_default_per_user'"),
        ),
        Index(
            "uq_rate_limit_rules_team_scoped",
            "scope_team_id",
            unique=True,
            postgresql_where=text("scope_team_id IS NOT NULL"),
        ),
        # See module docstring "scope_type = 'user' / scope_user_id" - added
        # by `0034`, same partial-unique-index shape as the team scope above.
        Index(
            "uq_rate_limit_rules_user_scoped",
            "scope_user_id",
            unique=True,
            postgresql_where=text("scope_user_id IS NOT NULL"),
        ),
        # Three-way mutual exclusion (added by `0034`, replacing `0026`'s
        # original two-way `ck_rate_limit_rules_scope_type_matches_scope_
        # team_id`).
        CheckConstraint(
            "(scope_type = 'org_default_per_user' AND scope_team_id IS NULL "
            "AND scope_user_id IS NULL) OR "
            "(scope_type = 'team' AND scope_team_id IS NOT NULL "
            "AND scope_user_id IS NULL) OR "
            "(scope_type = 'user' AND scope_team_id IS NULL "
            "AND scope_user_id IS NOT NULL)",
            name="ck_rate_limit_rules_scope_type_matches_scope_id",
        ),
        # AC4.2.1 - added by `0033`.
        CheckConstraint(
            "requests_per_min IS NOT NULL OR tokens_per_min IS NOT NULL",
            name="chk_at_least_one_limit",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    scope_type: Mapped[RateLimitScopeType] = mapped_column(
        rate_limit_scope_type_enum, nullable=False
    )
    scope_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=True
    )
    # See module docstring "scope_type = 'user' / scope_user_id" - added by
    # `0034`, same shape as `scope_team_id` above.
    scope_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    requests_per_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_per_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    on_limit: Mapped[RateLimitOnLimit] = mapped_column(
        rate_limit_on_limit_enum, nullable=False, server_default=text("'reject'")
    )
    # Ratified #8's default, admin-configurable.
    max_queue_wait_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("30")
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
    team: Mapped["Team | None"] = relationship("Team")
    user: Mapped["User | None"] = relationship("User")
