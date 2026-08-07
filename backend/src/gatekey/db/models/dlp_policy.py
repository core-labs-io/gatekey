"""`DlpPolicy` - an org's PII/DLP detector configuration (Phase 3 - Security
& Compliance Hardening).

See `docs/design/phase-3-security-compliance-design.md` section 1.3 for the
full design rationale. Mirrors `ModelPolicy`/`OrgSettings`' `org_id`-as-PK,
absence-of-row-means-default shape (ADR-1/ADR-2 in
`phase-1.3-model-governance.md`) - no seed row is inserted anywhere, absence
means every detector is off and `default_action` is `log`.

Detector toggles default `false` and `default_action` defaults `'log'` -
this phase's consistent off-by-default posture: an org must deliberately
turn PII scanning on, and turning on a detector without also picking an
action never surprises an org with silent blocking.

`dlp_action` enum
------------------
Defined here since `DlpPolicy` is its primary consumer; also imported by
`dlp_custom_pattern.py`, `team_dlp_action_override.py`, and
`dlp_scan_result.py`, all of which reuse the same Postgres enum type
(`create_type=False` - the type itself is created once by
`alembic/versions/0014_create_compliance_settings_dlp_policies_and_
overrides.py`).

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0014_create_compliance_settings_dlp_policies_and_
overrides.py` - that migration, not `Base.metadata.create_all()`, is the
source of truth for actual DDL.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, func, text
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org


class DlpAction(str, enum.Enum):
    LOG = "log"
    REDACT = "redact"
    BLOCK = "block"


# `create_type=False`: DDL for this Postgres enum type is owned exclusively
# by the Alembic migration (`0014_create_compliance_settings_dlp_policies_
# and_overrides.py`) - see `model_policy.py`'s `model_policy_mode_enum` for
# the identical rationale/pattern.
dlp_action_enum = PGEnum(
    DlpAction,
    name="dlp_action",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)


class DlpPolicy(Base):
    __tablename__ = "dlp_policies"

    # Primary key is `org_id` itself, not a surrogate `id` - see module
    # docstring. At most one row per org, by construction.
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orgs.id", ondelete="CASCADE"),
        primary_key=True,
    )

    ssn_detector_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    credit_card_detector_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    email_detector_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    phone_detector_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    default_action: Mapped[DlpAction] = mapped_column(
        dlp_action_enum, nullable=False, server_default=text("'log'")
    )
    # Ratified #3: default to NOT storing raw flagged substrings.
    store_raw_flagged_content: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    # Ratified #4.
    scan_inbound_responses: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
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
