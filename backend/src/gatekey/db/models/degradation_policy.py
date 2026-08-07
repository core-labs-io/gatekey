"""`DegradationPolicy` - an org/team graceful cost-degradation config row
(Phase 4 - Reliability & Cost Efficiency).

See `docs/design/phase-4-reliability-cost-efficiency-design.md` sections 1.7
and 6.2 for the full design rationale. Fifth application of the same
one-row-per-scope partial-unique-index pattern as `residency_rules`/
`rotation_policies`/`access_schedules`/`rate_limit_rules`. The `CHECK`
constraint enforces `scope_team_id` is set iff `scope_type = 'team'`.

Org/team resolution is cumulative on the `enabled` flag only - a team's
policy only ever applies if BOTH the team's own `enabled` AND the org's
`enabled` are true (see `services.degradation.resolve_degradation_policy`,
a backend-developer task). `threshold_pct_of_budget`/`downgrade_target_model`
themselves are NOT merged/compared across layers - whichever layer is
effectively enabled supplies its own values wholesale, since there is no
meaningful "narrower" ordering between two distinct (threshold, model)
configurations to check cumulatively (design doc section 6.2, a deliberate,
explicit exception to this codebase's cumulative-every-enabled-layer
pattern, flagged there in full).

`downgrade_target_model` is validated as a `MODEL_REGISTRY` key at the
service-layer write time (mirrors `ModelPolicy.models`) - not a DB
constraint, since `MODEL_REGISTRY` is an in-process registry, not a DB
table.

No `created_at`/`updated_at` columns - not part of the design doc's column
list for this table (same precedent `TeamDlpActionOverride` already
establishes for a table the design doc lists without them).

`threshold_pct_of_budget` bounds (schema/code-drift fix)
------------------------------------------------------------
`CHECK (threshold_pct_of_budget > 0 AND threshold_pct_of_budget <= 100)`,
added by
`alembic/versions/0036_add_threshold_bounds_check_to_degradation_policies.py`
- a sanity bound on the column's meaning (a percentage), deliberately wider
than `api/v1/admin/degradation_policy.py`'s app-layer `[1, 99]` business
rule (see that migration's docstring).

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0028_create_degradation_policies.py` and
`alembic/versions/0036_add_threshold_bounds_check_to_degradation_policies.py`
- those migrations, not `Base.metadata.create_all()`, are the source of
truth for actual DDL.
"""

from __future__ import annotations

import enum
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Index, Numeric, Text, text
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org
    from gatekey.db.models.team import Team


class DegradationScopeType(str, enum.Enum):
    ORG = "org"
    TEAM = "team"


# `create_type=False`: DDL for this Postgres enum type is owned exclusively
# by the Alembic migration (`0028_create_degradation_policies.py`) - see
# `model_policy.py`'s `model_policy_mode_enum` for the identical
# rationale/pattern.
degradation_scope_type_enum = PGEnum(
    DegradationScopeType,
    name="degradation_scope_type",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)


class DegradationPolicy(Base):
    __tablename__ = "degradation_policies"
    # See module docstring "Migration ownership" - must match `0028` exactly.
    __table_args__ = (
        Index(
            "uq_degradation_policies_org",
            "org_id",
            unique=True,
            postgresql_where=text("scope_type = 'org'"),
        ),
        Index(
            "uq_degradation_policies_team",
            "scope_team_id",
            unique=True,
            postgresql_where=text("scope_team_id IS NOT NULL"),
        ),
        CheckConstraint(
            "(scope_type = 'org' AND scope_team_id IS NULL) OR "
            "(scope_type = 'team' AND scope_team_id IS NOT NULL)",
            name="ck_degradation_policies_scope_type_matches_scope_team_id",
        ),
        # See module docstring "threshold_pct_of_budget bounds" - added by
        # `0036`.
        CheckConstraint(
            "threshold_pct_of_budget > 0 AND threshold_pct_of_budget <= 100",
            name="chk_degradation_policies_threshold_bounds",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    scope_type: Mapped[DegradationScopeType] = mapped_column(
        degradation_scope_type_enum, nullable=False
    )
    scope_team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="CASCADE"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    threshold_pct_of_budget: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, server_default=text("10.0")
    )
    # Validated as a MODEL_REGISTRY key at write time - see module docstring.
    downgrade_target_model: Mapped[str] = mapped_column(Text, nullable=False)

    org: Mapped["Org"] = relationship("Org")
    team: Mapped["Team | None"] = relationship("Team")
