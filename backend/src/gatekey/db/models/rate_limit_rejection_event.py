"""`RateLimitRejectionEvent` - one persisted row per request rejected (or
queue-timed-out) by `check_rate_limit` (Phase 4 - Reliability & Cost
Efficiency).

See `docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.5
for the full design rationale. Written synchronously, before the
`RateLimitExceededError` is raised - mirroring Phase 3's established
residency/DLP/schedule-block convention exactly (a raised exception has no
live response for `BackgroundTasks` to run after). Feeds the per-rule
`rejection_count` dashboard column (design doc section 7.1).

`scope_team_id`/`user_id` are plain nullable UUID columns with no FK -
display/filtering only on an event log, same "no referential-integrity
boundary" reason `dlp_scan_results.team_id`/`user_id` already establish.
`rule_id` is nullable + `SET NULL` so this historical log survives a later
rule deletion (same "never lose history" posture as `failover_events`'s key
references).

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0026_create_rate_limit_rules_and_rejection_events.py` -
that migration, not `Base.metadata.create_all()`, is the source of truth for
actual DDL. Reuses the `rate_limit_scope_type` enum type created by `0026`
(see `rate_limit_rule.py`).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base
from gatekey.db.models.rate_limit_rule import RateLimitScopeType, rate_limit_scope_type_enum

if TYPE_CHECKING:
    from gatekey.db.models.org import Org
    from gatekey.db.models.rate_limit_rule import RateLimitRule


class RateLimitRejectionOutcome(str, enum.Enum):
    REJECT = "reject"
    QUEUE_TIMEOUT = "queue_timeout"


# `create_type=False`: DDL for this Postgres enum type is owned exclusively
# by the Alembic migration
# (`0026_create_rate_limit_rules_and_rejection_events.py`) - see
# `model_policy.py`'s `model_policy_mode_enum` for the identical
# rationale/pattern.
rate_limit_rejection_outcome_enum = PGEnum(
    RateLimitRejectionOutcome,
    name="rate_limit_rejection_outcome",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)


class RateLimitRejectionEvent(Base):
    __tablename__ = "rate_limit_rejection_events"
    # See module docstring "Migration ownership" - must match `0026` exactly.
    __table_args__ = (
        Index(
            "ix_rate_limit_rejection_events_org_id_occurred_at", "org_id", "occurred_at"
        ),
        Index(
            "ix_rate_limit_rejection_events_rule_id_occurred_at", "rule_id", "occurred_at"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    rule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rate_limit_rules.id", ondelete="SET NULL"), nullable=True
    )
    scope_type: Mapped[RateLimitScopeType] = mapped_column(
        rate_limit_scope_type_enum, nullable=False
    )
    # Display/filtering only - no FK, see module docstring.
    scope_team_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    outcome: Mapped[RateLimitRejectionOutcome] = mapped_column(
        rate_limit_rejection_outcome_enum, nullable=False
    )

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    org: Mapped["Org"] = relationship("Org")
    rule: Mapped["RateLimitRule | None"] = relationship("RateLimitRule")
