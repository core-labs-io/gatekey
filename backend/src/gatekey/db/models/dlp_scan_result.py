"""`DlpScanResult` - one persisted row per DLP scan of a gateway request/
response (Phase 3 - Security & Compliance Hardening).

See `docs/design/phase-3-security-compliance-design.md` section 1.9 for the
full design rationale.

Deliberately keyed by `request_id` (text), not a typed FK to `usage_logs` -
same rationale `audit_entries.target_id` already documents: the log-only
DLP path's scan completes asynchronously, after the response has been sent
and independent of exactly when/whether a `usage_logs` row exists yet, so
coupling this table's write to that row's lifecycle would be a real
ordering hazard for no benefit. `team_id`/`user_id` are plain nullable UUID
columns with no FK, for the same "display/filtering only, never a
referential-integrity boundary" reason.

`findings` is `[{detector_or_pattern_name, action}]` - never raw content
unless `dlp_policies.store_raw_flagged_content = true`, in which case
`raw_flagged_content` (`NULL` by default, ratified #3) is also populated.

Retention/purge (Phase 3, AC6.2)
--------------------------------
`services.scheduler.run_log_prompt_purge_if_due` hard-deletes rows older
than `compliance_settings.log_prompt_retention_days` (default 30, never
NULL). The more privacy-sensitive of the two tables that job purges from -
`raw_flagged_content` can hold actual flagged substrings when an org opted
into `dlp_policies.store_raw_flagged_content`.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0018_create_dlp_scan_results.py` - that migration, not
`Base.metadata.create_all()`, is the source of truth for actual DDL. Reuses
the `dlp_action` enum type created by `0014` (see `dlp_policy.py`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base
from gatekey.db.models.dlp_policy import DlpAction, dlp_action_enum

if TYPE_CHECKING:
    from gatekey.db.models.org import Org


class DlpScanResult(Base):
    __tablename__ = "dlp_scan_results"
    # See module docstring "Migration ownership" - must match `0018` exactly.
    __table_args__ = (
        Index("ix_dlp_scan_results_org_id_created_at", "org_id", "created_at"),
        Index("ix_dlp_scan_results_request_id", "request_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    # The same opaque correlation id `common.new_request_id()` already
    # generates - see module docstring for why this is text, not a typed FK.
    request_id: Mapped[str] = mapped_column(String, nullable=False)
    # Display/filtering only - no FK, see module docstring.
    team_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    model: Mapped[str] = mapped_column(String, nullable=False)
    ran_sync: Mapped[bool] = mapped_column(Boolean, nullable=False)
    action_taken: Mapped[DlpAction] = mapped_column(dlp_action_enum, nullable=False)
    # [{detector_or_pattern_name, action}] - never raw content unless
    # `dlp_policies.store_raw_flagged_content = true`.
    findings: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    # NULL by default (ratified #3) - populated only when
    # `dlp_policies.store_raw_flagged_content = true`.
    raw_flagged_content: Mapped[list[Any] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    org: Mapped["Org"] = relationship("Org")
