"""`TeamMemberModelPolicy` - one team member's narrowing-only model-access
overlay, one layer below `TeamModelPolicy` (per-team-member model
assignment).

A team lead already narrows their team's model access below the org
baseline (`TeamModelPolicy` - see that module's docstring for the org/team
layering rationale). This table adds a THIRD layer: a team lead can further
narrow which of the TEAM's own effective models (org baseline intersected
with the team's own restriction, if any) a SPECIFIC member may use -
"org admin enabled 5 models, I decide which of those 5 each of my people
gets." Resolution order (org -> team -> member) mirrors `services.
model_policy.resolve_model_access()` exactly; see that function for the
full three-layer decision.

Shape mirrors `TeamMembership` (`db/models/team_membership.py`), not
`TeamModelPolicy`: a surrogate `id` primary key plus a `UniqueConstraint
(team_id, user_id)`, because this is genuinely a per-(team, member) row, not
a per-team row the way `TeamModelPolicy.team_id`-as-PK is. There is
deliberately no `mode` column, identical rationale to `TeamModelPolicy`: a
member overlay can only narrow (never re-enable a model the team itself
doesn't allow), so `models` is always an allowlist intersected with the
team's own effective set, never a denylist.

Absence of a row = "no further restriction beyond the team's own effective
set" - the same ADR-2 "no third state needing a column" convention
`TeamModelPolicy`/`ModelPolicy` already establish. `models` stores
gateway-facing `MODEL_REGISTRY`/custom-model/self-hosted-model names,
validated only at the service-layer write path (`services.model_policy.
set_member_model_policy`) - no FK target exists for an in-memory list, same
as `TeamModelPolicy.models`.

Removal: `ON DELETE CASCADE` on both `team_id`/`user_id` FKs - this overlay
has no independent meaning once the team or the underlying user row is gone.
Unlike `TeamMembership`, there is no soft-delete/`removed_at` concept here:
a member's OWN removal from the team (`TeamMembership.removed_at`) is what
actually revokes their access; this table only ever narrows access for a
CURRENTLY active membership, so an orphaned overlay row for a since-removed
member is harmless (never consulted - `resolve_model_access()`'s member
layer is only reached for a caller who currently holds team-scoped gateway
credentials, which requires an active membership by construction) and is
cleaned up automatically as a courtesy the next time `set_member_model_
policy()` is called for that same (team, user) pair after `TeamMembership.
create_team_membership`'s restore path re-adds them - not treated as a
correctness requirement to hard-delete it proactively on removal.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0051_create_team_member_model_policies.py` - that
migration, not `Base.metadata.create_all()`, is the source of truth for
actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.team import Team
    from gatekey.db.models.user import User


class TeamMemberModelPolicy(Base):
    __tablename__ = "team_member_model_policies"
    __table_args__ = (
        UniqueConstraint("team_id", "user_id", name="uq_team_member_model_policies_team_id_user_id"),
        Index("ix_team_member_model_policies_team_id", "team_id"),
        Index("ix_team_member_model_policies_user_id", "user_id"),
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
    # Allowed subset of the TEAM's own effective set - see module docstring.
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
    user: Mapped["User"] = relationship("User")
