"""`SelfHostedProvider` - one admin-registered self-hosted model-serving
endpoint (e.g. Ollama/vLLM) per org (Phase 5 - Differentiators, 5.5 Unified
BYOK+Self-Hosted Governance).

See `gatekey/phase-5-technical-design.md` sections 2.3/4.1/4.2 for the full
design rationale, and product spec section 9 judgment call #9 for why this
is a genuinely separate table from `provider_keys` rather than a new
`provider_name_enum` member: it supports multiple named self-hosted
endpoints per org (matching the admin UI mock) without a Postgres enum-type
migration, and `provider_name_enum` doesn't even have a `self_hosted`
member.

Encryption fields
------------------
`ciphertext`/`nonce`/`auth_tag` are the byte-for-byte identical AES-256-GCM
envelope shape `provider_keys` uses (same three pieces produced by
`services.encryption.encrypt_secret()`) - see that table's own module
docstring "Encryption fields" for the full rationale. Always written
together, atomically, by the app layer; all three `NOT NULL`. There is no
plaintext `bearer_token` column anywhere on this model, by design.

The associated-data (AAD) binding used to encrypt/decrypt this row's
`bearer_token` is `f"{org_id}:self_hosted:{self_hosted_provider_id}"` - a
distinct binding from `provider_keys`' `f"{org_id}:{provider}"` (see
`services.encryption.build_aad`), so a ciphertext can never be decrypted
successfully if copied between a `provider_keys` row and a
`self_hosted_providers` row, even within the same org. This binding is
applied by `services/self_hosted_providers.py` (backend-developer task) at
encrypt/decrypt time - not expressible as schema.

`cost_basis_per_gpu_hour` feeds `services.pricing.compute_self_hosted_
cost()`'s estimation formula (`cost_basis_per_gpu_hour *
wall_clock_latency_seconds / 3600`) - a rough proxy the design doc flags
prominently as "must be visibly labeled 'estimated' in the UI, never
presented as invoice-grade" (product spec section 9 judgment call #10).

`verified` gates routing eligibility: `resolve_route()`'s self-hosted
fallback (`services/self_hosted_providers.py::SelfHostedModelRouteCache`)
only serves entries where `verified = true` - an unverified endpoint is
treated as an unknown model, same 404 shape as any other unknown model
(design doc section 7.3).

`models` (JSONB list of model-id strings) is the admin-typed list of model
ids this endpoint serves - the string an admin types is both the gateway-
facing model key and the literal `model` field value sent to the
self-hosted endpoint (design doc section 2.3(b)); no separate native-id
mapping table exists in v1.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0040_create_self_hosted_providers.py` - that migration,
not `Base.metadata.create_all()`, is the source of truth for actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    LargeBinary,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org


class SelfHostedProvider(Base):
    __tablename__ = "self_hosted_providers"
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_self_hosted_providers_org_id_name"),
        CheckConstraint(
            "cost_basis_per_gpu_hour > 0", name="chk_self_hosted_providers_cost_basis_positive"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)

    # AES-256-GCM envelope pieces - see module docstring. Never nullable.
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    auth_tag: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    cost_basis_per_gpu_hour: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    models: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
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
