"""`ShadowAiIngestEvent` - one persisted, matched-hostname-only shadow-AI
detection event (Phase 5 - Differentiators, 5.1 Shadow AI Discovery).

See `gatekey/phase-5-technical-design.md` sections 2.5/4.2 for the full
design rationale. Written by `services.shadow_ai.ingest_events` (backend-
developer task) - only for events whose `destination_host` matches an
enabled `known_ai_tool_hostnames` row; every non-matching event in a
submitted batch is dropped, never persisted (AC5.1.1's privacy-by-
minimization gate - design doc section 2.5).

**No body/URL/query-string column exists on this table, by design** - the
NFR "Shadow-AI collects connection metadata only" (design doc section 1.2)
is enforced structurally, not just by convention: there is no column this
table could even accidentally be given that content in. `raw_metadata`
(JSONB, nullable) is whatever bounded, non-content connection metadata the
ingesting SASE/proxy tool supplies beyond the named columns.

`matched_user_id` is nullable + `ON DELETE SET NULL` (best-effort match of
`user_identifier` against `User.email`, AC5.1.5) - a row persists with
`matched_user_id = NULL` when no match is found, surfaced in the report as
"not linked to a Gatekey user" rather than being dropped.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0042_create_shadow_ai_tables.py` - that migration, not
`Base.metadata.create_all()`, is the source of truth for actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org
    from gatekey.db.models.user import User


class ShadowAiIngestEvent(Base):
    __tablename__ = "shadow_ai_ingest_events"
    __table_args__ = (
        Index("ix_shadow_ai_ingest_events_org_created", "org_id", "created_at"),
        Index("ix_shadow_ai_ingest_events_matched_user", "matched_user_id"),
        CheckConstraint(
            "source IN ('sase_log', 'proxy_log')", name="chk_shadow_ai_ingest_events_source"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    # Raw identifier as reported by the ingesting tool (e.g. an email or
    # username string) - see module docstring "matched_user_id".
    user_identifier: Mapped[str] = mapped_column(Text, nullable=False)
    matched_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    destination_host: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 'sase_log' | 'proxy_log'
    source: Mapped[str] = mapped_column(Text, nullable=False)
    # Connection metadata only - never body/URL/query-string content. See
    # module docstring.
    raw_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    org: Mapped["Org"] = relationship("Org")
    matched_user: Mapped["User | None"] = relationship("User")
