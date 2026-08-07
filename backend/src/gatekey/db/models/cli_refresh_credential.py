"""`CliRefreshCredential` - a long-lived credential minted by the device-code
auth flow, whose only power is calling `GET /v1/me/current-key` (Phase 3 -
Security & Compliance Hardening, the CLI sync helper).

See `docs/design/phase-3-security-compliance-design.md` section 8.2 for the
full design rationale. Never usable directly against the gateway routes -
distinct from `ServiceAccountKey`/`PersonalApiKey`. Every fetch of
`/v1/me/current-key` rotates the bound `PersonalApiKey` (fork #3) rather
than caching a temporarily-readable plaintext anywhere.

`secret_hash` is the raw SHA-256 digest (32 bytes) of the full plaintext
secret (`gk_rf_` prefix) - same "fast hash, not a slow KDF" discipline as
every other high-entropy token in this codebase (see
`service_account_key.py` for the full rationale). No plaintext secret
column exists, ever.

`ON DELETE CASCADE` on `user_id`/`bound_personal_key_id`: unlike
`ServiceAccountKey`/`PersonalApiKey` (which `RESTRICT` to protect a live
gateway credential from silent orphan-deletion), this refresh token has no
independent value once its owner or bound key is gone - nothing is lost by
letting it cascade-delete, so there is no "credential blocks deletion"
tension to resolve here.

SCIM deactivation revokes active rows here too (design doc section 6.4).

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0022_create_cli_refresh_credentials.py` - that migration,
not `Base.metadata.create_all()`, is the source of truth for actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org
    from gatekey.db.models.personal_api_key import PersonalApiKey
    from gatekey.db.models.user import User


class CliRefreshCredential(Base):
    __tablename__ = "cli_refresh_credentials"
    # See module docstring "Migration ownership" - must match `0022` exactly.
    __table_args__ = (
        Index("ix_cli_refresh_credentials_secret_hash", "secret_hash", unique=True),
        Index("ix_cli_refresh_credentials_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    bound_personal_key_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("personal_api_keys.id", ondelete="CASCADE"),
        nullable=False,
    )
    # SHA-256 digest (32 bytes) of the full plaintext secret - see module
    # docstring.
    secret_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # NULL = active, non-NULL = revoked as of that timestamp.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    org: Mapped["Org"] = relationship("Org")
    user: Mapped["User"] = relationship("User")
    bound_personal_key: Mapped["PersonalApiKey"] = relationship("PersonalApiKey")
