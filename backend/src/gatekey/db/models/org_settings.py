"""`OrgSettings` - org-wide governance settings (Phase 2 - Multi-Tenant
Governance).

See `docs/design/phase-2-multi-tenant-governance-design.md` section 1.1 for
the full design rationale. Mirrors `ModelPolicy`'s ADR-1 exactly (`org_id`
as PK, not a surrogate id - "exactly one settings row per org" is a
schema-level invariant) and its ADR-2 (absence of a row = the default
state: no org ceiling, `USD`, no max key expiration, soft cap 10,
auto-provision off - no signup-seed dependency needed; nothing seeds a row
per org, and callers must treat "no row" as those defaults via the service
layer, never assume a row exists).

`budget_ceiling_usd` is an *allocation* constraint (checked when team
ceilings are written, under a `SELECT ... FOR UPDATE` on this row - design
doc ADR-5/A3), never re-checked or decremented on the spend hot path.

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
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org


class OrgSettings(Base):
    __tablename__ = "org_settings"

    # Primary key is `org_id` itself, not a surrogate `id` - see module
    # docstring. At most one row per org, by construction.
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orgs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # NULL = no org-wide ceiling. NUMERIC(20, 10) per ADR-1 in
    # `db/models/user.py`.
    budget_ceiling_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 10), nullable=True
    )
    # ISO 4217 code - Phase 2 only ever writes/reads 'USD' (design doc
    # ADR-9); the column exists so real FX support later is additive.
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default=text("'USD'")
    )
    # NULL = no max.
    max_self_serve_key_expiration_days: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    personal_key_soft_cap: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("10")
    )
    auto_provision_personal_key_on_approval: Mapped[bool] = mapped_column(
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
