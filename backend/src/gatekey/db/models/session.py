"""`UserSession` - a server-side browser session for SSO console login
(Phase 2 - Multi-Tenant Governance).

See `docs/design/phase-2-multi-tenant-governance-design.md` sections 1.9 and
2.1-2.2 for the full design rationale.

The class is named `UserSession` (table name stays `"sessions"`) to avoid
clashing with SQLAlchemy's own `Session`/`AsyncSession` names, which this
codebase imports pervasively - `from gatekey.db.models import UserSession`
must never shadow or be confused with a DB-session object.

`token_hash` is the raw SHA-256 digest (32 bytes) of the opaque cookie
value (`secrets.token_urlsafe(32)`), same lookup-hash pattern as
`ServiceAccountKey.secret_hash`/`PersonalApiKey.secret_hash` - the raw
token lives only in the httpOnly cookie, never in the DB, so a leaked dump
cannot be replayed as a live session. Same "not a slow KDF" rationale too:
a 256-bit random token, not a guessable password.

A session is active iff `revoked_at IS NULL AND expires_at > now()`.
`last_seen_at` is updated best-effort only (never load-bearing for auth).
`ON DELETE CASCADE` from `users`: a deleted user's sessions die with them -
a session is not a credential row that must block user deletion (contrast
with `service_account_keys.user_id`'s RESTRICT).

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0009_add_sso_columns_to_users_and_create_sessions.py` -
that migration, not `Base.metadata.create_all()`, is the source of truth
for actual DDL.
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
    from gatekey.db.models.user import User


class UserSession(Base):
    __tablename__ = "sessions"
    # See module docstring "Migration ownership" - must match `0009` exactly.
    __table_args__ = (
        Index("ix_sessions_token_hash", "token_hash", unique=True),
        Index("ix_sessions_user_id", "user_id"),
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
    # SHA-256 digest (32 bytes) of the opaque cookie value - see module
    # docstring.
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Best-effort operational visibility only - never load-bearing for auth.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # NULL = active, non-NULL = revoked as of that timestamp.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship("User")
