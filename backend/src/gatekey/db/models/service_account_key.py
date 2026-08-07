"""`ServiceAccountKey` - a per-app credential used to authenticate gateway
requests (`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`).

Phase 1.2 constraint: like `ProviderKey`, this table is scoped to the
single default org (`gatekey.constants.DEFAULT_ORG_ID`) seeded by
`alembic/versions/0001_create_orgs_and_provider_keys.py` - there is no
multi-org signup flow yet (see `db/models/org.py`). `org_id` is still
threaded through from day one for the same reason it is on `ProviderKey`:
retrofitting a tenant foreign key after Phase 2 lands is far more
disruptive than including it now.

`user_id` (Phase 1.4 - Budget Basic)
-------------------------------------
Every key is attributed to exactly one `User` - the cost-center whose
`budget_usd`/`current_spend_usd` the gateway checks/charges against on
every authenticated request (see `gatekey.db.models.user.User` and
`docs/design/phase-1.4-budget-basic-design.md` section 1). The column is
`NOT NULL`: `alembic/versions/0004_create_users_and_attribute_service_account_keys.py`
backfills every pre-1.4 row to one auto-created, unmetered default/legacy
user per org before tightening the column to `NOT NULL`, so there is never
a key without a `user_id` in practice, including rows created before this
column existed.

The FK is deliberately `ON DELETE RESTRICT`, not `CASCADE`: a `User` must
never be silently able to orphan-delete a live app credential's row via
its own deletion. This is also the mechanism `services.users.delete_user()`
relies on to block deleting a user still referenced by any
`ServiceAccountKey` row - active *or* revoked (revoking a key sets
`revoked_at`, it never deletes the row, so a `RESTRICT` FK blocks on both).
See the design doc section 1.4 (ADR-2) for the full resolution of this
tension against the product spec's "active-only" phrasing.

Secret storage
--------------
The plaintext secret (`gk_sk_...`) is shown to the caller exactly once, at
creation time, and is never persisted anywhere - there is deliberately no
plaintext secret column on this model, the same "no plaintext secrets at
rest" non-negotiable that already governs `ProviderKey`'s
ciphertext/nonce/auth_tag columns (see `provider_key.py` and
`00-overview.md`).

What *is* persisted is `secret_hash`: the raw SHA-256 digest (32 bytes) of
the full plaintext secret. This is intentionally **not** bcrypt/argon2/
scrypt. Those slow, salted KDFs exist to defend low-entropy human passwords
against offline brute force; a service-account secret is a 256-bit
cryptographically random token, not a guessable password, so that threat
model does not apply, and a deliberately slow hash would consume the
gateway's entire ~150ms request-latency budget on auth alone. SHA-256 is
fast, has no meaningful collision risk at this token size, and lets the
auth path do a single indexed equality lookup on `secret_hash`. See design
doc `phase-1.2-gateway-core.md` section 4 (Q2) for the full rationale.

`key_prefix` is the first characters of the plaintext secret after the
`gk_sk_` prefix. It exists purely so list/admin views can show callers
"which key is which" without revealing the secret - it is never used for
auth lookup (that is always by `secret_hash`), and by itself it carries
nowhere near enough entropy to be a credential.

`revoked_at`
------------
NULL means the key is active; a non-NULL timestamp means revoked as of
that time. There is deliberately no separate `is_active` boolean - two
columns encoding the same fact invite drift (e.g. `is_active=True` with a
non-NULL `revoked_at`), and the auth path only ever needs to ask
"`revoked_at IS NULL`?".

`team_id` (Phase 2 - Multi-Tenant Governance)
----------------------------------------------
Nullable, with NO backfill (see
`docs/design/phase-2-multi-tenant-governance-design.md` section 1.7):
`NULL` = legacy row (created before Phase 2, or never team-attributed) -
resolves budget against the owning `User.budget_usd`, byte-for-byte the
Phase 1.4 code path. Non-NULL = charges the owner's
`TeamMembership(team_id, user_id)` counter instead (A6). "New keys require
`team_id`" is enforced at the API-schema layer
(`ServiceAccountKeyCreateRequest.team_id`), not by a column constraint - a
`NOT NULL`/`CHECK` cannot distinguish "legacy row" from "new row created
without a team", the same tension `user_id` already resolved once (minus
the safe backfill default that column had). `ON DELETE RESTRICT`: a
team-attributed credential row blocks its team's deletion, same rationale
as `user_id`.

Deliberately absent columns
----------------------------
- No `last_used_at`. Writing to this table on every authenticated request
  would add a write to the hot request path for a "nice to have" usage
  signal that is out of scope for this slice.
- No plaintext secret column, ever (see above).

`previous_secret_hash` / `previous_secret_valid_until` (Phase 3 - Security &
Compliance Hardening)
------------------------------------------------------------------------------
The dual-secret overlap mechanism automatic rotation relies on (design doc
`phase-3-security-compliance-design.md` sections 1.11/4.3) - no separate
`RotationEvent` table exists. On rotation, the current `secret_hash` moves
into `previous_secret_hash` with `previous_secret_valid_until = now() +
overlap_buffer_minutes`, and a freshly minted secret becomes the new
`secret_hash`. The gateway auth lookup matches either column:
`secret_hash = :hash OR (previous_secret_hash = :hash AND previous_
secret_valid_until > now())` - both are indexed (the unique partial index
below), so this stays a single indexed-equality-shaped lookup. A stale
`previous_secret_hash` past its `valid_until` is simply never matched
again; no separate cleanup job is needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org
    from gatekey.db.models.team import Team
    from gatekey.db.models.user import User


class ServiceAccountKey(Base):
    __tablename__ = "service_account_keys"
    # Names/definitions here must stay in lockstep with the explicit
    # `op.create_index()`/`op.create_foreign_key()` calls in
    # `alembic/versions/0002_create_service_account_keys.py` and
    # `alembic/versions/0004_create_users_and_attribute_service_account_keys.py`
    # - those migrations, not `Base.metadata.create_all()`, are the source
    # of truth for DDL, but keeping them identical avoids spurious
    # autogenerate diffs against `Base.metadata` (see `alembic/env.py`).
    __table_args__ = (
        Index("ix_service_account_keys_org_id", "org_id"),
        Index("ix_service_account_keys_secret_hash", "secret_hash", unique=True),
        Index("ix_service_account_keys_user_id", "user_id"),
        Index("ix_service_account_keys_team_id", "team_id"),
        # Phase 3 - see module docstring "previous_secret_hash /
        # previous_secret_valid_until". Added by
        # `alembic/versions/0020_create_rotation_policies_and_add_overlap_
        # columns.py`.
        Index(
            "ix_service_account_keys_previous_secret_hash",
            "previous_secret_hash",
            unique=True,
            postgresql_where=text("previous_secret_hash IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)

    # The budget-owning cost-center this key charges against - see module
    # docstring ("user_id (Phase 1.4 - Budget Basic)") for the
    # `ON DELETE RESTRICT` rationale.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    # Phase 2 - NULL = legacy flat-budget row; see module docstring
    # ("team_id (Phase 2 - Multi-Tenant Governance)").
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=True
    )

    # First chars of the plaintext secret after the `gk_sk_` prefix, for
    # identification in list views only - never used for auth lookup. See
    # module docstring.
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)

    # SHA-256 digest (32 bytes) of the full plaintext secret. Uniqueness is
    # enforced by the named unique index in `__table_args__` above (so a
    # (theoretical) hash collision across two distinct secrets cannot both
    # resolve to active credentials), not by a column-level `unique=True`
    # flag - see the `__table_args__` note on why. See module docstring for
    # why this is SHA-256 and not a slow password KDF.
    secret_hash: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # NULL = active, non-NULL = revoked as of that timestamp. See module
    # docstring for why there is no separate `is_active` boolean.
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Phase 3 - dual-secret rotation overlap. See module docstring
    # "previous_secret_hash / previous_secret_valid_until".
    previous_secret_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    previous_secret_valid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    org: Mapped["Org"] = relationship("Org", back_populates="service_account_keys")
    user: Mapped["User"] = relationship("User", back_populates="service_account_keys")
    team: Mapped["Team | None"] = relationship("Team")
