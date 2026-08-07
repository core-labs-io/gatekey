"""`Team` - the org's team layer (Phase 2 - Multi-Tenant Governance).

See `docs/design/phase-2-multi-tenant-governance-design.md` section 1.2 for
the full design rationale. Like every Phase 1 table, scoped to the single
default org (`gatekey.constants.DEFAULT_ORG_ID`) - Phase 2 adds the team
layer beneath the existing single org, not multi-org support.

`current_spend_usd` (ADR-7)
----------------------------
A denormalized, transactionally-maintained running total of
`SUM(team_memberships.current_spend_usd)` - kept in lockstep at every
mutation site (the usage-charge path and period rollover/reset, both
funneled through shared service functions) rather than computed via a live
aggregate on the gateway hot path. Threshold-alert (80%/100%) detection
reads this column's before/after values from the charge `UPDATE`'s
RETURNING clause.

Webhook alert config
---------------------
`webhook_ciphertext`/`webhook_nonce`/`webhook_auth_tag` are the AES-256-GCM
envelope for the team's alert-webhook URL (a Slack-style incoming-webhook
URL embeds a bearer-equivalent secret, so it is never stored as plaintext -
same three-piece always-written-together discipline as `ProviderKey`, with
associated data bound to `team_id`). All three NULL = no webhook configured.

Period columns
---------------
`period_type`/`on_period_end`/`current_period_started_at` drive the lazy,
touch-based rollover/reset (design doc ADR-6/ADR-10) - there is no
scheduler; `services.team_periods.ensure_current_period` applies boundary
crossings on next touch.

`scim_external_id` (Phase 3 - Security & Compliance Hardening)
------------------------------------------------------------------
The IdP's durable per-Group identifier (SCIM's own `externalId`), the
correlation key for `PUT`/`PATCH` idempotency on `/scim/v2/Groups`. NULL
for every team never touched by SCIM; mirrors `users.scim_external_id`'s
own partial-unique-index pattern. See
`docs/design/phase-3-security-compliance-design.md` section 1.10.

`cache_opt_out` (Phase 4 - Reliability & Cost Efficiency)
------------------------------------------------------------
Same per-team-toggle-column style as `alert_threshold_80_enabled`/
`webhook_alert_enabled` - a team that opts out never gets a response-cache
hit/write, even when `caching_settings.enabled` is org-wide on. See
`docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.6.

`cache_enabled` / `cache_ttl_minutes` (Phase 4 schema/code-drift fix,
AC4.3.2/AC4.3.3)
------------------------------------------------------------------------------
AC4.3.2 requires caching to default to *disabled*, opt-IN per team -
`cache_opt_out` above defaults `false`, meaning every team was cached BY
DEFAULT, the opposite direction. `cache_enabled` (defaults `false`) is the
AC-correct opt-in column going forward; `cache_opt_out` is left in place,
un-migrated-away, purely to avoid a destructive rename/drop while any
lingering references are confirmed clear - see
`alembic/versions/0035_add_cache_enabled_and_ttl_to_teams.py`'s docstring
for the full rationale and the flagged (not yet decided) cleanup. AC4.3.3
requires a per-team TTL (default 5 minutes, bounds 1-1440); `cache_ttl_
minutes` (`CHECK` bounded, defaults 5) is that column - previously only the
org-wide `caching_settings.ttl_seconds` existed. Org-level `caching_
settings.enabled` remains the kill switch that wins over a team's own
`cache_enabled=true` (resolution logic lives in `services/response_
cache.py`, not this model).

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0007_create_org_settings_teams_and_team_model_policies.py`,
`alembic/versions/0019_create_scim_config_and_add_scim_columns.py`,
`alembic/versions/0027_create_caching_settings_and_cache_lookup_events.py`,
and `alembic/versions/0035_add_cache_enabled_and_ttl_to_teams.py` - those
migrations, not `Base.metadata.create_all()`, are the source of truth for
actual DDL (including the `team_period_type`/`team_period_end` enum types -
hence `create_type=False` below).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org


class TeamPeriodType(str, enum.Enum):
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"


class TeamPeriodEnd(str, enum.Enum):
    ROLLOVER = "rollover"
    RESET = "reset"


# `create_type=False`: DDL for these Postgres enum types is owned
# exclusively by the Alembic migration (`0007_create_org_settings_teams_and_
# team_model_policies.py`) - see `model_policy.py`'s
# `model_policy_mode_enum` for the identical rationale/pattern.
team_period_type_enum = PGEnum(
    TeamPeriodType,
    name="team_period_type",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)
team_period_end_enum = PGEnum(
    TeamPeriodEnd,
    name="team_period_end",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)


class Team(Base):
    __tablename__ = "teams"
    # See module docstring "Migration ownership" - must match `0007` exactly.
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_teams_org_id_name"),
        Index("ix_teams_org_id", "org_id"),
        # Phase 3 - see module docstring "scim_external_id".
        Index(
            "ix_teams_scim_external_id",
            "scim_external_id",
            unique=True,
            postgresql_where=text("scim_external_id IS NOT NULL"),
        ),
        # AC4.3.3 - added by `0035`. See module docstring "cache_enabled /
        # cache_ttl_minutes".
        CheckConstraint(
            "cache_ttl_minutes >= 1 AND cache_ttl_minutes <= 1440",
            name="chk_teams_cache_ttl_minutes_bounds",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String, nullable=False)

    # NULL = unmetered team ceiling. NUMERIC(20, 10) per ADR-1 in
    # `db/models/user.py`.
    budget_ceiling_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 10), nullable=True
    )
    # Denormalized membership-spend aggregate - see module docstring (ADR-7).
    current_spend_usd: Mapped[Decimal] = mapped_column(
        Numeric(20, 10), nullable=False, server_default=text("0")
    )

    period_type: Mapped[TeamPeriodType] = mapped_column(
        team_period_type_enum, nullable=False, server_default=text("'monthly'")
    )
    on_period_end: Mapped[TeamPeriodEnd] = mapped_column(
        team_period_end_enum, nullable=False, server_default=text("'reset'")
    )
    current_period_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    alert_threshold_80_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    alert_threshold_100_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    webhook_alert_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # AES-256-GCM envelope for the webhook URL - all three NULL, or all
    # three set, always written together by the app layer. See module
    # docstring.
    webhook_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    webhook_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    webhook_auth_tag: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    email_alert_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    # Phase 3 - see module docstring "scim_external_id".
    scim_external_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # Phase 4 - see module docstring "cache_opt_out".
    cache_opt_out: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Phase 4 schema/code-drift fix - see module docstring "cache_enabled /
    # cache_ttl_minutes".
    cache_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    cache_ttl_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("5")
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
