"""`TeamModelPolicy` - a team's narrowing-only model-restriction overlay
(Phase 2 - Multi-Tenant Governance).

See `docs/design/phase-2-multi-tenant-governance-design.md` section 1.3 for
the full design rationale. Directly executes
`phase-1.3-model-governance.md` section 8's own forward-looking rework flag:
a *new* table alongside `model_policies`, so Phase 1 org-baseline policy
data and behavior are preserved unchanged.

Mirrors `ModelPolicy`'s ADR-1 (`team_id`-as-PK, at most one row per team)
and ADR-2 (absence of a row = "no further restriction beyond the org
baseline" - not a third state needing a column). There is deliberately no
`mode` column: a team can only ever narrow (AC3.1/3.2), so `models` is
always an allowlist intersected with the org baseline, never a denylist -
"team re-enables an org-banned model" is structurally impossible to express
as anything other than a no-op.

`models` stores gateway-facing `MODEL_REGISTRY` keys, validated only at the
service-layer write path (`services.model_policy.set_team_model_policy`) -
no FK target exists for an in-memory dict.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0007_create_org_settings_teams_and_team_model_policies.py`
- that migration, not `Base.metadata.create_all()`, is the source of truth
for actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.team import Team


class TeamModelPolicy(Base):
    __tablename__ = "team_model_policies"

    # Primary key is `team_id` itself, not a surrogate `id` - see module
    # docstring. At most one row per team, by construction.
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # Allowed subset of the org baseline - see module docstring.
    models: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
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
