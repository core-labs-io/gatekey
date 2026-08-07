"""`ComplianceSettings` - org-level retention/access-schedule-timezone
singleton settings (Phase 3 - Security & Compliance Hardening).

See `docs/design/phase-3-security-compliance-design.md` section 1.2 for the
full design rationale. Mirrors `OrgSettings`' ADR-2 exactly (absence of a
row = the default state: no audit purge, 30-day usage/prompt retention,
UTC). A separate table from `OrgSettings`, not new columns on it - AC6.1/
AC6.2 require the two retention windows to be genuinely separable at the
infra level, and a dedicated compliance table keeps Phase 3's purge-job
configuration from being interleaved with Phase 2's budget/currency settings
in the same row.

`audit_retention_days = NULL` means never auto-purged (ratified #1) - the
scheduled purge job (`services.scheduler.run_audit_purge_if_due`, Phase 3
backend track) must treat `NULL` as "do not fire", not as "purge
immediately" or "purge everything".

`access_schedule_timezone` lives here rather than on any `access_schedules`
row because AC9.4 is explicit that timezone is a single org-wide setting,
not per-scope.

`chain_enabled` (Phase 5 - Differentiators, 5.2)
--------------------------------------------------
Defaults `false` (off-by-default, same posture as every other compliance
toggle). Mutually exclusive with a non-null `audit_retention_days` - the DB
enforces this with `chk_chain_purge_mutually_exclusive`
(`NOT (chain_enabled AND audit_retention_days IS NOT NULL)`), backstopping
the app-layer validation `services/compliance_settings.py` must perform
(backend-developer task). See `gatekey/phase-5-technical-design.md` section
2.1/12 for the "chain and purge are mutually exclusive, not co-existing"
v1 design rationale, and `0038_add_chain_enabled_to_compliance_settings.py`
for the DDL.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0014_create_compliance_settings_dlp_policies_and_
overrides.py` and
`alembic/versions/0038_add_chain_enabled_to_compliance_settings.py` - those
migrations, not `Base.metadata.create_all()`, are the source of truth for
actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org


class ComplianceSettings(Base):
    __tablename__ = "compliance_settings"
    # See module docstring "chain_enabled" - must match `0038` exactly.
    __table_args__ = (
        CheckConstraint(
            "NOT (chain_enabled AND audit_retention_days IS NOT NULL)",
            name="chk_chain_purge_mutually_exclusive",
        ),
    )

    # Primary key is `org_id` itself, not a surrogate `id` - see module
    # docstring. At most one row per org, by construction.
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orgs.id", ondelete="CASCADE"),
        primary_key=True,
    )

    # NULL = never auto-purged (ratified #1) - see module docstring.
    audit_retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    log_prompt_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("30")
    )
    # AC9.4: one org-wide timezone, no per-scope override.
    access_schedule_timezone: Mapped[str] = mapped_column(
        String, nullable=False, server_default=text("'UTC'")
    )
    # Phase 5 - see module docstring "chain_enabled".
    chain_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
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
