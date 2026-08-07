"""`ResidencyRule` - an org/team data-residency rule (Phase 3 - Security &
Compliance Hardening).

See `docs/design/phase-3-security-compliance-design.md` sections 1.6 and 3
for the full design rationale. At most one rule per scope (org, or per
team) is a schema-level invariant via the two partial unique indexes below -
same "let the schema guarantee the one-row-per-scope invariant" philosophy
as `ModelPolicy`/`TeamModelPolicy`, via partial unique indexes rather than
app-level pre-check-then-insert.

`scope_team_id = NULL` means an org-wide rule. `allowed_regions` is a
JSON string array drawn from `services.residency.SUPPORTED_REGIONS`
(validated only at the service-layer write path - an in-process frozenset,
not a DB table, so no FK target exists). `violation_behavior` defaults to
`hard_block` at the column level (AC3.2 - the create path cannot silently
default to `warn`).

AC3.2 defense-in-depth (narrowing-only, ratified #12): a team rule's
`allowed_regions` must be a subset of the org rule's - enforced at the
service-layer write path (`services.residency.set_team_residency_rule`),
not by a DB constraint (the org rule it must narrow against is itself
mutable, so this can only be checked transactionally at write time, not
expressed as a static schema constraint).

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0015_create_residency_rules.py` - that migration, not
`Base.metadata.create_all()`, is the source of truth for actual DDL.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, func, text
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org
    from gatekey.db.models.team import Team


class ResidencyViolationBehavior(str, enum.Enum):
    HARD_BLOCK = "hard_block"
    WARN = "warn"


# `create_type=False`: DDL for this Postgres enum type is owned exclusively
# by the Alembic migration (`0015_create_residency_rules.py`) - see
# `model_policy.py`'s `model_policy_mode_enum` for the identical
# rationale/pattern.
residency_violation_behavior_enum = PGEnum(
    ResidencyViolationBehavior,
    name="residency_violation_behavior",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)


class ResidencyRule(Base):
    __tablename__ = "residency_rules"
    # See module docstring "Migration ownership" - must match `0015` exactly.
    __table_args__ = (
        Index(
            "uq_residency_rules_org_wide",
            "org_id",
            unique=True,
            postgresql_where=text("scope_team_id IS NULL"),
        ),
        Index(
            "uq_residency_rules_team_scoped",
            "scope_team_id",
            unique=True,
            postgresql_where=text("scope_team_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    # NULL = org-wide rule - see module docstring.
    scope_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=True
    )
    allowed_regions: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    violation_behavior: Mapped[ResidencyViolationBehavior] = mapped_column(
        residency_violation_behavior_enum, nullable=False, server_default=text("'hard_block'")
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
