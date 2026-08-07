"""`ProviderKey` - one encrypted provider API key per (org, provider, label).

Phase 1.1 constraint (superseded): one key per provider per org, enforced by
`UNIQUE(org_id, provider)`. Phase 4 (Reliability & Cost Efficiency) relaxes
this to `UNIQUE(org_id, provider, label)` to allow multiple keys per
provider for same-provider automatic failover - see
`docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.2 and
`alembic/versions/0023_add_multi_key_columns_to_provider_keys.py`.

Multi-key/failover columns (Phase 4)
-------------------------------------
`label` is required going forward (AC1.1); every pre-existing single-key row
was backfilled to `label='Default'`, `is_primary=true` by `0023` so no org's
existing configuration silently broke. Exactly one key per `(org, provider)`
is flagged `is_primary` (DB-enforced via the partial unique index below) and
serves all normal, non-failover routing - the first key ever added for a
provider becomes primary automatically, so an org that never adds a second
key sees byte-for-byte today's one-key-per-provider behavior. Genuine
traffic-spreading/load-balancing across multiple keys for one provider is
out of scope this phase (design doc section 10, fork #1) - `is_primary` only
picks which single key serves fresh traffic.

`failover_enabled`/`failover_target_id` are the org/key-level failover
default, only meaningfully read off the *primary* key at request time (see
`services.provider_key_health.resolve_failover_opt_in`, a backend-developer
task) - `team_failover_overrides` (see that module) is the narrowing-only,
team-scoped override. `failover_target_id` is same-provider-constrained at
the app layer only, not the schema layer (cross-provider failover is out of
scope this phase, design doc section 12).

Encryption fields
------------------
`ciphertext` / `nonce` / `auth_tag` are the three pieces produced by
`services.encryption.encrypt_secret()` (AES-256-GCM; see that module for the
envelope format and associated-data binding to `org_id:provider`). They are
always written together, atomically, by the app layer - a row missing any
one of the three is not decryptable and must never exist, so all three are
`NOT NULL` rather than left nullable-and-forgotten. There is no plaintext
key column anywhere on this model, by design (see `00-overview.md`
"No plaintext provider keys at rest or in logs, from Phase 1 onward").

`metadata` column note
-----------------------
The spec'd column name is `metadata`, but `metadata` is reserved on
SQLAlchemy declarative classes (it collides with `Base.metadata`). The
Python attribute here is `key_metadata`; the actual database column name is
still exactly `metadata` via `mapped_column("metadata", ...)`.

`previous_ciphertext`/`previous_nonce`/`previous_auth_tag`/
`previous_valid_until` (Phase 3 - Security & Compliance Hardening)
------------------------------------------------------------------------------
A parallel dual-secret overlap shape to `service_account_keys.previous_
secret_hash`, added by
`alembic/versions/0020_create_rotation_policies_and_add_overlap_columns.py`.
Unlike the service-account case, this is *not* functionally load-bearing
for any live lookup - Gatekey is both the only writer and the only reader
of a provider credential, and starts using the new key for every outbound
call the moment it's validated. These columns exist purely so the admin
console can display "previous key, retiring in N minutes" during the
overlap window, and so a human operator has a visible grace window to
manually deactivate the old key at the provider's own console. See design
doc section 4.3.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org
    from gatekey.db.models.usage_log import UsageLog


class ProviderName(str, enum.Enum):
    """Matches `providers.registry.SUPPORTED_PROVIDERS` exactly."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    VERTEX_AI = "vertex_ai"
    OLLAMA = "ollama"
    OPENROUTER = "openrouter"


# `create_type=False`: DDL for this Postgres enum type is owned exclusively
# by the Alembic migration (see
# `alembic/versions/0001_create_orgs_and_provider_keys.py`), not by
# SQLAlchemy's metadata.create_all()/drop_all(). This avoids two competing
# owners of the same `CREATE TYPE` / `DROP TYPE` and keeps autogenerate from
# proposing spurious enum diffs.
provider_name_enum = PGEnum(
    ProviderName,
    name="provider_name",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)


class ProviderKey(Base):
    __tablename__ = "provider_keys"
    # See module docstring "Multi-key/failover columns" - must match `0023`
    # exactly.
    __table_args__ = (
        UniqueConstraint(
            "org_id", "provider", "label", name="uq_provider_keys_org_id_provider_label"
        ),
        Index(
            "uq_provider_keys_one_primary_per_provider",
            "org_id",
            "provider",
            unique=True,
            postgresql_where=text("is_primary"),
        ),
        Index("ix_provider_keys_failover_target_id", "failover_target_id"),
        Index("ix_provider_keys_backup_group", "backup_group_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[ProviderName] = mapped_column(provider_name_enum, nullable=False)

    # AES-256-GCM envelope pieces - see module docstring. Never nullable.
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    auth_tag: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    # DB column name is `metadata`; Python attribute is `key_metadata`
    # (see module docstring). Non-secret only (e.g. key label, last-4 of a
    # provider-issued key id if the provider exposes one) - never plaintext
    # key material.
    key_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Phase 4 - multi-key/failover columns. See module docstring
    # "Multi-key/failover columns".
    label: Mapped[str] = mapped_column(Text, nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    failover_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    failover_target_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_keys.id", ondelete="SET NULL"), nullable=True
    )

    # Phase 4 backup group for multi-key failover orchestration.
    # A backup group is a collection of provider keys that can serve as
    # backups for each other. Keys in the same group are checked in order
    # when failover is triggered.
    backup_group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backup_groups.id", ondelete="SET NULL"), nullable=True
    )

    # Health tracking for proactive failover decisions.
    # health_status: current health status from scheduled checks
    # last_health_check: timestamp of last health check attempt
    # last_error: last error message from health check (if any)
    # availability_24h: 24-hour rolling availability percentage (0.0000 to 1.0000)
    # last_degraded_at: when key was last marked degraded
    health_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'unknown'")
    )
    last_health_check: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    availability_24h: Mapped[float | None] = mapped_column(
        "availability_24h", nullable=True
    )
    last_degraded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Phase 3 - dual-secret rotation overlap (admin-console display only,
    # not load-bearing for any live lookup). See module docstring.
    previous_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    previous_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    previous_auth_tag: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    previous_valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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

    org: Mapped["Org"] = relationship("Org", back_populates="provider_keys")
    # Self-referential - see module docstring "Multi-key/failover columns".
    failover_target: Mapped["ProviderKey | None"] = relationship(
        "ProviderKey", remote_side=[id], foreign_keys=[failover_target_id]
    )
    # Backup group relationship
    backup_group: Mapped["BackupGroup | None"] = relationship(
        "BackupGroup", back_populates="provider_keys"
    )
    # Usage logs that used this key (for failover tracking)
    usage_logs: Mapped[list["UsageLog"]] = relationship(
        "UsageLog", back_populates="failover_key"
    )
