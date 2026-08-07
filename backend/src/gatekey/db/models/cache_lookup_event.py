"""`CacheLookupEvent` - one persisted row per response-cache lookup, hit or
miss (Phase 4 - Reliability & Cost Efficiency).

See `docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.6
for the full design rationale. Written via `BackgroundTasks` (deferred,
after the response is already on the wire), the same async-recording
mechanism Phase 3's log-only DLP path already established
(`_deliver_async_dlp_scan`) - so recording never adds to the synchronous
cache-lookup critical path (AC3.9's ~10ms budget) on either a hit or a miss.

No prompt/response content stored here at all - less sensitive than
`dlp_scan_results` (which at least stores redacted findings) - purely a
hit/miss/token-count event log for dashboard aggregation (design doc
section 7.1). `team_id` is a plain nullable UUID column with no FK -
display/filtering only, same reasoning as `dlp_scan_results.team_id`.
`prompt_tokens`/`completion_tokens` are populated on a hit only, copied from
the cache entry (a miss has no tokens yet at lookup time).

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

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org


class CacheLookupEvent(Base):
    __tablename__ = "cache_lookup_events"
    # See module docstring "Migration ownership" - must match `0027` exactly.
    __table_args__ = (
        Index("ix_cache_lookup_events_org_id_occurred_at", "org_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    # Display/filtering only - no FK, see module docstring.
    team_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    hit: Mapped[bool] = mapped_column(Boolean, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    # native_model_id actually looked up (post-resolve_route).
    model: Mapped[str] = mapped_column(Text, nullable=False)
    # Populated on a hit only, copied from the cache entry.
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    org: Mapped["Org"] = relationship("Org")
