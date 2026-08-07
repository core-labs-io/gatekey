"""`CachingSettings` - org-level response-caching config singleton
(Phase 4 - Reliability & Cost Efficiency).

See `docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.6
for the full design rationale. Mirrors `ComplianceSettings`/`DlpPolicy`'s
"absence of row = default state" ADR exactly - an org that never touches
this config gets caching on (`enabled` defaults `true`, AC3.5) with a
1-hour TTL, not an inert feature.

`ttl_seconds` default: no number is given by either the phase doc or the
product spec; 3600 (1 hour) is chosen and documented here (design doc
section 1.6) - a small, undictated default this design supplies, not a
value ratified by name in either source doc.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0027_create_caching_settings_and_cache_lookup_events.py` -
that migration, not `Base.metadata.create_all()`, is the source of truth for
actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org


class CachingSettings(Base):
    __tablename__ = "caching_settings"

    # Primary key is `org_id` itself, not a surrogate `id` - see module
    # docstring. At most one row per org, by construction.
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orgs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    ttl_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3600"))

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
