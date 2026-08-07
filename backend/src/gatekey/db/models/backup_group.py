"""`BackupGroup` - groups provider keys for failover routing (Phase 4 - Reliability & Cost Efficiency).

See `docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.2
for the full design rationale. Backup groups allow multiple keys for the same
provider to be grouped together so that when failover is enabled and the
primary key fails, Gatekey can automatically retry against backup keys in
the same group.

Groups are org-scoped to prevent cross-org key mixing (multi-tenant isolation).
Keys in the same group can serve as backups for each other.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0025_create_backup_groups.py` - that migration, not
`Base.metadata.create_all()`, is the source of truth for actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org
    from gatekey.db.models.provider_key import ProviderKey


class BackupGroup(Base):
    """A group of provider keys that can serve as backups for each other.

    Backup groups are org-scoped to prevent cross-org key mixing. Keys in the
    same group share the same `backup_group_id` and can be used for automatic
    failover when `failover_enabled` is set on the primary key.

    Design doc section 1.2: a backup group is a collection of provider keys
    that can serve as backups for each other. Keys in the same group are
    checked in order (primary first, then backups by availability_24h desc)
    when failover is triggered.
    """

    __tablename__ = "backup_groups"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    org: Mapped["Org"] = relationship("Org", back_populates="backup_groups")
    provider_keys: Mapped[list["ProviderKey"]] = relationship(
        "ProviderKey",
        back_populates="backup_group",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
