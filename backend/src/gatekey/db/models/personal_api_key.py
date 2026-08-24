"""`PersonalApiKey` - a self-service (or delegated) per-human gateway
credential (Phase 2 - Multi-Tenant Governance).

See `docs/design/phase-2-multi-tenant-governance-design.md` section 1.6 for
the full design rationale. Deliberately a separate table, not a repurposed
`ServiceAccountKey` row (locked architecture decision), but every
column-level convention is copied verbatim from `service_account_key.py` -
see that module's docstring for the shared rationale on:

- `secret_hash`: raw SHA-256 digest (32 bytes) of the full plaintext
  secret; deliberately NOT a slow KDF (256-bit random token, not a
  guessable password). No plaintext secret column exists, ever.
- `key_prefix`: first chars of the plaintext secret after the `gk_pk_`
  prefix (distinct from service accounts' `gk_sk_`, so the unified gateway
  auth dependency routes by prefix with a single lookup) - list-view
  identification only, never used for auth lookup.
- `revoked_at`: NULL = active; no redundant `is_active` boolean.
- `ON DELETE RESTRICT` on the user FKs: a live credential row must never be
  silently orphan-deleted via its owner's (or creator's) deletion.

Differences from `ServiceAccountKey`:

- `expires_at` (NULL = no expiration): personal keys support self-serve
  expiry; the auth path checks it in addition to `revoked_at`.
- `team_id` is `NOT NULL` at the schema level (unlike
  `service_account_keys.team_id`): every personal key is created fresh
  under Phase 2, so there is no legacy-row population problem forcing
  nullability. Budget always resolves against the owner's
  `TeamMembership(team_id, owner_user_id)` counter (A6), guaranteed to
  exist by service-layer construction.
- `created_by_user_id`: self-serve keys have creator == owner; delegated
  keys (team lead/admin minting for a member) record the actual creator for
  the audit trail.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0011_create_personal_api_keys_and_add_team_to_service_
account_keys.py` - that migration, not `Base.metadata.create_all()`, is the
source of truth for actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.team import Team
    from gatekey.db.models.user import User


class PersonalApiKey(Base):
    __tablename__ = "personal_api_keys"
    # See module docstring "Migration ownership" - must match `0011` exactly.
    __table_args__ = (
        Index("ix_personal_api_keys_secret_hash", "secret_hash", unique=True),
        Index("ix_personal_api_keys_owner_user_id", "owner_user_id"),
        Index("ix_personal_api_keys_org_id", "org_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)

    # See module docstring - identification only, never auth lookup.
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    # SHA-256 digest (32 bytes) - uniqueness enforced by the named unique
    # index in `__table_args__`, same shape as `service_account_keys`.
    secret_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)

    # NULL = no expiration.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # NULL = active, non-NULL = revoked as of that timestamp.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Self-reported by the CLI-sync tool at `POST /v1/auth/device/start`
    # time (added by `0047`) - the machine being paired knows its own
    # hostname; the approving browser doesn't. NULL for every key not
    # minted through the device-code flow (self-service-portal personal
    # keys, all service-account keys). See `services.cli_refresh_
    # credentials.DeviceAuthStore` for where this is captured.
    device_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    owner: Mapped["User"] = relationship("User", foreign_keys=[owner_user_id])
    created_by: Mapped["User"] = relationship("User", foreign_keys=[created_by_user_id])
    team: Mapped["Team"] = relationship("Team")
