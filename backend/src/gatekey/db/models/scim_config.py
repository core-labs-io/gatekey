"""`ScimConfig` - an org's SCIM 2.0 provisioning configuration (Phase 3 -
Security & Compliance Hardening).

See `docs/design/phase-3-security-compliance-design.md` section 1.10 for the
full design rationale. Follows the identical one-row-per-org,
hash-only-secret shape as every other org-scoped singleton config table in
this codebase (mirrors `OrgSettings`/`DlpPolicy`/`ComplianceSettings`'
`org_id`-as-PK, absence-of-row-means-default shape).

`bearer_token_hash` is the raw SHA-256 digest of the SCIM bearer token -
same lookup-hash discipline (fast hash, not a slow KDF) as
`ServiceAccountKey.secret_hash`/`PersonalApiKey.secret_hash`/
`UserSession.token_hash`, for the identical "256-bit random token, not a
guessable password" rationale. `NULL` = no token has been generated yet.
Token rotation (`POST /v1/admin/scim-config/rotate-token`) overwrites this
column in place - no overlap window, unlike credential rotation (design doc
section 6.2): this is an inbound credential the IdP holds, not a scheduled
outbound rotation.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0019_create_scim_config_and_add_scim_columns.py` - that
migration, not `Base.metadata.create_all()`, is the source of truth for
actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, LargeBinary, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org


class ScimConfig(Base):
    __tablename__ = "scim_config"

    # Primary key is `org_id` itself, not a surrogate `id` - see module
    # docstring. At most one row per org, by construction.
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orgs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # SHA-256 digest (32 bytes) of the bearer token - see module docstring.
    # NULL = no token generated yet.
    bearer_token_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_created_at: Mapped[datetime | None] = mapped_column(
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

    org: Mapped["Org"] = relationship("Org")
