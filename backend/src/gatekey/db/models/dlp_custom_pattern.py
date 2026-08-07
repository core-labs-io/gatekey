"""`DlpCustomPattern` - an org-authored regex-based DLP detector (Phase 3 -
Security & Compliance Hardening).

See `docs/design/phase-3-security-compliance-design.md` section 1.4 for the
full design rationale. Org-level authoring only (AC2.3 - no team-level
pattern authoring exists in the UI spec). Each pattern carries its own
independent `action` (see `dlp_policy.DlpAction`), never overridden by
`team_dlp_action_overrides` (AC2.4 - that table only overrides the action
applied to built-in-detector findings).

`pattern` is the regex source, validated compilable only at the
service-layer write path - no DB constraint can express "is this a valid
regex".

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0014_create_compliance_settings_dlp_policies_and_
overrides.py` - that migration, not `Base.metadata.create_all()`, is the
source of truth for actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base
from gatekey.db.models.dlp_policy import DlpAction, dlp_action_enum

if TYPE_CHECKING:
    from gatekey.db.models.org import Org


class DlpCustomPattern(Base):
    __tablename__ = "dlp_custom_patterns"
    # See module docstring "Migration ownership" - must match `0014` exactly.
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_dlp_custom_patterns_org_id_name"),
        Index("ix_dlp_custom_patterns_org_id", "org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    # Regex source - see module docstring.
    pattern: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[DlpAction] = mapped_column(dlp_action_enum, nullable=False)

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
