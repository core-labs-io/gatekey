"""`TeamDlpActionOverride` - a team's override of the *action* applied to
built-in-detector DLP findings (Phase 3 - Security & Compliance Hardening).

See `docs/design/phase-3-security-compliance-design.md` section 1.5 for the
full design rationale. One row per team, mirrors `TeamModelPolicy`'s
`team_id`-as-PK shape exactly (at most one row per team, absence = "use the
org default"). Overrides only the *action* applied to built-in-detector
findings - AC2.4's explicit two-layer (not three-layer) system: no per-key
DLP override table exists, and there is deliberately no
`TeamDlpPatternOverride` - custom patterns (`dlp_custom_pattern.py`) stay
org-authored-only, each carrying its own independent `action` never touched
by this table.

Deliberately no `created_at`/`updated_at` columns - not part of the design
doc's column list for this table (unlike every other new Phase 3 table).

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0014_create_compliance_settings_dlp_policies_and_
overrides.py` - that migration, not `Base.metadata.create_all()`, is the
source of truth for actual DDL.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base
from gatekey.db.models.dlp_policy import DlpAction, dlp_action_enum

if TYPE_CHECKING:
    from gatekey.db.models.team import Team


class TeamDlpActionOverride(Base):
    __tablename__ = "team_dlp_action_overrides"

    # Primary key is `team_id` itself, not a surrogate `id` - see module
    # docstring. At most one row per team, by construction.
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="CASCADE"),
        primary_key=True,
    )
    action: Mapped[DlpAction] = mapped_column(dlp_action_enum, nullable=False)

    team: Mapped["Team"] = relationship("Team")
