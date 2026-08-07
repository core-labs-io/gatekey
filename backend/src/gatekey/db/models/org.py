"""`Org` - the tenant root entity for Phase 1.1 (Provider & Key Management).

Phase 1.1 ships as a single-admin, single-org deployment (no multi-org
signup flow, no team hierarchy yet - see `gatekey/phase-1-core-gateway.md`
1.1/1.6 and `gatekey/phase-2-multi-tenant-governance.md` for what's
deliberately deferred). `org_id` is still threaded through `ProviderKey`
from day one: retrofitting a tenant foreign key onto an existing table
after Phase 2 lands is far more disruptive than including it now, and the
initial migration seeds exactly one row here (see
`alembic/versions/0001_create_orgs_and_provider_keys.py`) so this slice
never has to deal with "no org exists yet" as a state.

Do not add team/user/budget columns or tables here - out of scope for this
slice (see phase-1 "Out of Scope").
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.backup_group import BackupGroup
    from gatekey.db.models.model_policy import ModelPolicy
    from gatekey.db.models.provider_key import ProviderKey
    from gatekey.db.models.service_account_key import ServiceAccountKey
    from gatekey.db.models.user import User


class Org(Base):
    __tablename__ = "orgs"

    # App-side UUID default (uuid.uuid4), not pgcrypto/gen_random_uuid() -
    # keeps deploy friction low (no Postgres extension required).
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    provider_keys: Mapped[list["ProviderKey"]] = relationship(
        "ProviderKey",
        back_populates="org",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    service_account_keys: Mapped[list["ServiceAccountKey"]] = relationship(
        "ServiceAccountKey",
        back_populates="org",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # Phase 1.4 (Budget - Basic) - `User` is the budget-owning cost-center
    # a `ServiceAccountKey` attributes its usage to (see
    # `db/models/user.py`). Added for symmetry with `provider_keys`/
    # `service_account_keys` above, required because `User.org` declares
    # `back_populates="users"`.
    users: Mapped[list["User"]] = relationship(
        "User",
        back_populates="org",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # At most one row (see `ModelPolicy`'s module docstring, ADR-1) - hence
    # `uselist=False` rather than a list, unlike the two relationships
    # above. Added for symmetry with `provider_keys`/`service_account_keys`
    # even though no code in this phase traverses `Org -> ModelPolicy` from
    # the ORM side (the service layer queries `ModelPolicy` directly by
    # `org_id`, see `services.model_policy`); the DB-level `ON DELETE
    # CASCADE` on `model_policies.org_id` is already sufficient for
    # cleanup, this relationship is purely a convenience accessor.
    model_policy: Mapped["ModelPolicy | None"] = relationship(
        "ModelPolicy",
        back_populates="org",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    # Phase 4: backup groups for multi-key failover orchestration.
    # Keys in the same backup group can serve as backups for each other.
    backup_groups: Mapped[list["BackupGroup"]] = relationship(
        "BackupGroup",
        back_populates="org",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
