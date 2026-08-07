"""`RotationPolicy` - an org/service-account-key/provider-key credential
rotation policy (Phase 3 - Security & Compliance Hardening).

See `docs/design/phase-3-security-compliance-design.md` sections 1.11 and
4 for the full design rationale. Same one-row-per-scope
partial-unique-index pattern as `ResidencyRule`, extended to three scope
levels (`org`, `service_account`, `provider_key`). The `CHECK` constraint
enforces that exactly one of `scope_service_account_id`/
`scope_provider_key_id` is set, matching `scope_type`.

`mode` is `CHECK`-implied by `scope_type` at the app layer only
(`service_account` scope is always `automatic`, `provider_key` scope is
always `manual_guided` - AC7.1) - not encoded as a DB `CHECK` across
`scope_type`/`mode`, to avoid over-constraining a column pair the service
layer already fully controls at every write path (no ad hoc writer exists
elsewhere).

`next_rotation_at` is computed once per rotation
(`services.rotation.compute_next_rotation`, a backend-developer task) and
read by the scheduler loop's due-work query via the partial index on
`next_rotation_at WHERE enabled`.

Dual-secret overlap columns (`service_account_keys.previous_secret_hash`/
`previous_secret_valid_until`, `provider_keys.previous_ciphertext`/
`previous_nonce`/`previous_auth_tag`/`previous_valid_until`) live on those
tables directly, not here - see `service_account_key.py`/`provider_key.py`
and design doc section 4.3 for why no `RotationEvent` table exists.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0020_create_rotation_policies_and_add_overlap_columns.py`
- that migration, not `Base.metadata.create_all()`, is the source of truth
for actual DDL.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, time
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Time,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org
    from gatekey.db.models.provider_key import ProviderKey
    from gatekey.db.models.service_account_key import ServiceAccountKey


class RotationScopeType(str, enum.Enum):
    ORG = "org"
    SERVICE_ACCOUNT = "service_account"
    PROVIDER_KEY = "provider_key"


class RotationMode(str, enum.Enum):
    AUTOMATIC = "automatic"
    MANUAL_GUIDED = "manual_guided"


# `create_type=False`: DDL for these Postgres enum types is owned
# exclusively by the Alembic migration
# (`0020_create_rotation_policies_and_add_overlap_columns.py`) - see
# `model_policy.py`'s `model_policy_mode_enum` for the identical
# rationale/pattern.
rotation_scope_type_enum = PGEnum(
    RotationScopeType,
    name="rotation_scope_type",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)
rotation_mode_enum = PGEnum(
    RotationMode,
    name="rotation_mode",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)


class RotationPolicy(Base):
    __tablename__ = "rotation_policies"
    # See module docstring "Migration ownership" - must match `0020` exactly.
    __table_args__ = (
        Index(
            "uq_rotation_policies_org_wide",
            "org_id",
            unique=True,
            postgresql_where=text("scope_type = 'org'"),
        ),
        Index(
            "uq_rotation_policies_sa_scoped",
            "scope_service_account_id",
            unique=True,
            postgresql_where=text("scope_service_account_id IS NOT NULL"),
        ),
        Index(
            "uq_rotation_policies_pk_scoped",
            "scope_provider_key_id",
            unique=True,
            postgresql_where=text("scope_provider_key_id IS NOT NULL"),
        ),
        Index(
            "ix_rotation_policies_next_rotation_at",
            "next_rotation_at",
            postgresql_where=text("enabled"),
        ),
        CheckConstraint(
            "(scope_type = 'org' AND scope_service_account_id IS NULL "
            "AND scope_provider_key_id IS NULL) OR "
            "(scope_type = 'service_account' AND scope_service_account_id IS NOT NULL "
            "AND scope_provider_key_id IS NULL) OR "
            "(scope_type = 'provider_key' AND scope_provider_key_id IS NOT NULL "
            "AND scope_service_account_id IS NULL)",
            name="ck_rotation_policies_scope_type_matches_scope_id",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    scope_type: Mapped[RotationScopeType] = mapped_column(
        rotation_scope_type_enum, nullable=False
    )
    scope_service_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("service_account_keys.id", ondelete="CASCADE"),
        nullable=True,
    )
    scope_provider_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_keys.id", ondelete="CASCADE"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    interval_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # e.g. '02:00' org-local; NULL falls back to the org off-hours default.
    rotate_at_local_time: Mapped[time | None] = mapped_column(Time, nullable=True)
    overlap_buffer_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("5")
    )
    next_rotation_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    mode: Mapped[RotationMode] = mapped_column(rotation_mode_enum, nullable=False)

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
    scope_service_account: Mapped["ServiceAccountKey | None"] = relationship(
        "ServiceAccountKey", foreign_keys=[scope_service_account_id]
    )
    scope_provider_key: Mapped["ProviderKey | None"] = relationship(
        "ProviderKey", foreign_keys=[scope_provider_key_id]
    )
