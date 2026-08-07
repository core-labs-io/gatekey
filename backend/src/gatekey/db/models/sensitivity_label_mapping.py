"""`SensitivityLabelMapping` - maps an external pre-set sensitivity label
(e.g. from Microsoft Purview/Google DLP) to a Gatekey content-classification
category (Phase 5 - Differentiators, 5.3 Content-Classification-Aware
Routing).

See `gatekey/phase-5-technical-design.md` sections 2.4/4.2 for the full
design rationale. A request carrying the optional `X-Gatekey-Sensitivity-
Label` header is looked up against this org's mappings; a match
pre-trusts (short-circuits Gatekey's own classifier for) exactly the ONE
mapped `gatekey_category` - every other category still runs Gatekey's own
classifier. An unrecognized label value falls through to Gatekey's own
classifiers for every category - never a hard error (design doc section
2.4/7.4).

This table gets no dedicated `*Cache` class - it is read on the
already-non-zero-I/O DLP-scan code path (which already pays several other
per-request config reads when scanning is required), not the zero-I/O
`ModelPolicyCache` tier reserved for checks that run on every gateway
request regardless of DLP config (design doc section 2.4, "Why
`sensitivity_label_mappings` gets no dedicated `*Cache` class").

Composite-unique `(org_id, external_label)` - at most one Gatekey category
per external label string, per org. No `created_at`/`updated_at` columns
(see the owning migration's docstring) - this is a small, admin-CRUD-only
mapping list.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0041_seed_content_aware_rules_and_create_sensitivity_
label_mappings.py` - that migration, not `Base.metadata.create_all()`, is
the source of truth for actual DDL.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org


class SensitivityLabelMapping(Base):
    __tablename__ = "sensitivity_label_mappings"
    __table_args__ = (
        UniqueConstraint(
            "org_id", "external_label", name="uq_sensitivity_label_mappings_org_label"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    external_label: Mapped[str] = mapped_column(Text, nullable=False)
    gatekey_category: Mapped[str] = mapped_column(Text, nullable=False)

    org: Mapped["Org"] = relationship("Org")
