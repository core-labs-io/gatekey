"""`User` - a per-app-caller budget/cost-center entity.

Phase 1.4 (Budget - Basic). See `docs/design/phase-1.4-budget-basic-design.md`
section 1 for the full design rationale (ADR-1: monetary column precision;
ADR-7: fixed-UUID default/legacy-user backfill; ADR-2 in section 1.4: the
`ON DELETE RESTRICT` delete-blocking semantics on
`ServiceAccountKey.user_id`).

Like `ProviderKey`/`ServiceAccountKey`/`ModelPolicy`, this table is scoped
to the single default org (`gatekey.constants.DEFAULT_ORG_ID`) seeded by
`alembic/versions/0001_create_orgs_and_provider_keys.py` - there is no
multi-org signup flow yet.

`budget_usd` / `current_spend_usd` precision (ADR-1)
------------------------------------------------------
Both columns are `NUMERIC(20, 10)`, not a smaller/currency-typical scale
like `NUMERIC(12, 4)`. A naive 4-decimal-place column would silently round
many individual per-token charges to `$0.0000` before they ever get a
chance to accumulate (e.g. a 10-token prompt at `gpt-4o-mini`'s ~$0.15/
million-token input rate costs $0.0000015, already below 4-decimal-place
resolution). `NUMERIC(20, 10)` stores that same charge exactly. See the
design doc for the full worked example. Python-side this maps to `Decimal`
- never `float`, anywhere on the charge path.

`budget_usd = NULL` means unmetered (no spend cutoff) - see
`services.budget.is_budget_exhausted`. Phase 2 (A6): once a user has at
least one `TeamMembership` row, `budget_usd`/`current_spend_usd` become
read-only legacy state relevant only to pre-existing `team_id = NULL`
`ServiceAccountKey` rows - all team-attributed spend resolves against
`TeamMembership.budget_usd` instead (see `db/models/team_membership.py`).

Phase 2 SSO/RBAC columns
-------------------------
`org_role = NULL` is the common case (ordinary member/team_lead - those
roles live on `TeamMembership.role`, per the locked RBAC data-model
decision); `org_admin`/`auditor` are org-wide and independent of any team.
`sso_subject` (the OIDC `sub` claim, the IdP's durable per-user identifier)
is the auth-lookup key - not `sso_email`, which is display-only and can
change or be reassigned. The unique index on `sso_subject` is partial
(`WHERE sso_subject IS NOT NULL`) so every pre-Phase-2, admin-created flat
user (`sso_subject IS NULL`) is exempt.

Phase 3 SCIM columns
---------------------
`scim_external_id` is the IdP's durable per-resource identifier (SCIM's own
`externalId`), the correlation key for `PUT`/`PATCH` idempotency - distinct
from `sso_subject` (the OIDC `sub` claim used for SSO login correlation).
NULL for every user never touched by SCIM; the partial unique index mirrors
`sso_subject`'s own pattern. `scim_deactivated_at` is a durable block flag
(ratified #8): non-NULL blocks both existing-session validation and new SSO
login (see `docs/design/phase-3-security-compliance-design.md` sections
1.10 and 6.4 for the full rationale).

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0004_create_users_and_attribute_service_account_keys.py`,
`alembic/versions/0009_add_sso_columns_to_users_and_create_sessions.py`, and
`alembic/versions/0019_create_scim_config_and_add_scim_columns.py` - those
migrations, not `Base.metadata.create_all()`, are the source of truth for
actual DDL (including the `user_org_role` enum type, created by
`0007` - hence `create_type=False` below), but keeping them identical
avoids spurious autogenerate diffs against `Base.metadata` (see
`alembic/env.py`).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, func, text
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org
    from gatekey.db.models.service_account_key import ServiceAccountKey


class UserOrgRole(str, enum.Enum):
    """The two org-wide roles. Deliberately does not include member/
    team_lead members - those are per-team roles on `TeamMembership.role`
    (see module docstring), and "no org-wide role" is `org_role IS NULL`,
    never an enum value.
    """

    ORG_ADMIN = "org_admin"
    AUDITOR = "auditor"


# `create_type=False`: DDL for this Postgres enum type is owned exclusively
# by the Alembic migration (`0007_create_org_settings_teams_and_team_model_
# policies.py`) - see `model_policy.py`'s `model_policy_mode_enum` for the
# identical rationale/pattern.
user_org_role_enum = PGEnum(
    UserOrgRole,
    name="user_org_role",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)


class User(Base):
    __tablename__ = "users"
    # See module docstring "Migration ownership" - must match
    # `alembic/versions/0004_create_users_and_attribute_service_account_keys.py`
    # and `alembic/versions/0009_add_sso_columns_to_users_and_create_sessions.py`
    # exactly.
    __table_args__ = (
        Index("ix_users_org_id", "org_id"),
        Index(
            "ix_users_sso_subject",
            "sso_subject",
            unique=True,
            postgresql_where=text("sso_subject IS NOT NULL"),
        ),
        # Phase 3 - see module docstring "Phase 3 SCIM columns".
        Index(
            "ix_users_scim_external_id",
            "scim_external_id",
            unique=True,
            postgresql_where=text("scim_external_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)

    # NULL = unmetered. See module docstring (ADR-1) for the
    # NUMERIC(20, 10) precision rationale.
    budget_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 10), nullable=True
    )
    current_spend_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 10), nullable=False, server_default=text("0")
    )

    # Phase 2 - see module docstring "Phase 2 SSO/RBAC columns". NULL = no
    # org-wide role.
    org_role: Mapped[UserOrgRole | None] = mapped_column(
        user_org_role_enum, nullable=True
    )
    # OIDC `sub` claim - the durable auth-lookup key. NULL for pre-Phase-2
    # admin-created flat users.
    sso_subject: Mapped[str | None] = mapped_column(String, nullable=True)
    # IdP-asserted email, display only - never used for auth lookup.
    sso_email: Mapped[str | None] = mapped_column(String, nullable=True)

    # Phase 3 - see module docstring "Phase 3 SCIM columns".
    scim_external_id: Mapped[str | None] = mapped_column(String, nullable=True)
    scim_deactivated_at: Mapped[datetime | None] = mapped_column(
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

    org: Mapped["Org"] = relationship("Org", back_populates="users")
    service_account_keys: Mapped[list["ServiceAccountKey"]] = relationship(
        "ServiceAccountKey", back_populates="user"
    )
