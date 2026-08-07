"""`TeamFailoverOverride` - a team-scoped, narrowing-only override of the
org/key-level failover default (Phase 4 - Reliability & Cost Efficiency).

See `docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.3
for the full design rationale. `team_id` is the primary key - at most one
override row per team, same shape as `team_dlp_action_overrides`/
`team_model_policies`.

`failover_disabled` can only ever *disable* `provider_keys.failover_enabled`
for a team - there is structurally no "enable" value, so unlike
`residency_rules`/`team_model_policies`'s write-time subset-check pattern, no
write-time narrowing validation is needed at all: the schema itself makes
widening impossible to express. Read cumulatively at request time (both the
key's own `failover_enabled` AND `NOT team_override.failover_disabled` must
hold) by `services.provider_key_health.resolve_failover_opt_in` (backend-
developer task), not validated-narrower-at-write-then-innermost-only-at-read.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0024_create_team_failover_overrides.py` - that migration,
not `Base.metadata.create_all()`, is the source of truth for actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.team import Team


class TeamFailoverOverride(Base):
    __tablename__ = "team_failover_overrides"

    # Primary key is `team_id` itself, not a surrogate `id` - see module
    # docstring. At most one row per team, by construction.
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE"),
        primary_key=True,
    )
    failover_disabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
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

    team: Mapped["Team"] = relationship("Team")
