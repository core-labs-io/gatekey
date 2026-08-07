"""`CustomModel` - one admin-registered, DB-backed "custom model" per org
(Custom Model Registry / Admin-Managed BYOK Models).

See `gatekey/custom-model-registry-technical-design.md` sections 2/4 for
the full design rationale and `gatekey/custom-model-registry-product-spec.md`
section 2 for field-level rationale; this docstring summarizes the two
decisions that most affect how this model must be used from the service
layer.

No new credential type
------------------------
Unlike `SelfHostedProvider`, this table stores no secret of any kind - a
custom model routes through the **existing**, already-encrypted
`provider_keys` row for its `provider` (fetched via
`services.proxy_keys.get_decrypted_provider_credential()`, the identical
function every gateway request already calls). There is no
`ciphertext`/`nonce`/`auth_tag` envelope on this model at all - a genuine
simplification versus the self-hosted precedent, not an oversight.

`provider` / `capability` are plain strings, not Postgres enums
-------------------------------------------------------------------
`provider` is constrained at the DB level to the four-value BYOK subset
(`openai`/`anthropic`/`vertex_ai`/`openrouter`) via a `CHECK` constraint,
deliberately **not** `provider_key.provider_name_enum` - that Postgres enum
type includes `'ollama'`, which this table must exclude (`ollama` has its
own mechanism via `self_hosted_providers`, Phase 5.5 - see design doc
section 4.1/7). `capability` is likewise a plain, `CHECK`-constrained
string column rather than a new Postgres enum type, mapped here to the
existing `providers.model_registry.ModelCapability` Python enum at the
application layer only (`sa.Enum(..., native_enum=False)` - no `CREATE
TYPE`, no new type for Alembic to own).

`pricing_as_of` is server-set, not admin-entered
---------------------------------------------------
`services/custom_models.py` always sets this explicitly to `date.today()`
on every create *and* every pricing edit (not just at row creation) -
there is no DB-level default to lean on here, unlike `created_at`/
`updated_at`.

`verified` gates routing eligibility
---------------------------------------
Identical mechanism to `SelfHostedProvider.verified`: `resolve_route()`'s
custom-model fallback (`services/custom_models.py::CustomModelRouteCache`)
only ever serves entries from rows where `verified = true` - an unverified
custom model is treated as an unknown model, same 404 shape as any other
unknown model (design doc section 2.2/2.3).

No FK from `usage_logs`
--------------------------
Unlike `SelfHostedProvider` (which added `usage_logs.
self_hosted_provider_id`), this design deliberately does not add a
`custom_models` FK to `usage_logs` - `usage_logs.provider`/`.model` already
fully capture what happened for a custom-model request. See design doc
section 2.6 for the full rationale and its one known, low-severity
limitation.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0044_create_custom_models.py` - that migration, not
`Base.metadata.create_all()`, is the source of truth for actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Enum as SAEnum

from gatekey.db.base import Base
from gatekey.providers.model_registry import ModelCapability

if TYPE_CHECKING:
    from gatekey.db.models.org import Org

# String-backed, not a Postgres enum - see module docstring. `create_
# constraint=False` because the equivalent CHECK
# (`chk_custom_models_capability`) is owned exclusively by the Alembic
# migration, mirroring `provider_name_enum`'s `create_type=False`
# rationale in `provider_key.py` (avoid two competing DDL owners).
custom_model_capability_type = SAEnum(
    ModelCapability,
    name="custom_model_capability",
    native_enum=False,
    create_constraint=False,
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    validate_strings=True,
    length=32,
)


class CustomModel(Base):
    __tablename__ = "custom_models"
    __table_args__ = (
        UniqueConstraint("org_id", "name", name="uq_custom_models_org_id_name"),
        CheckConstraint(
            "provider IN ('openai', 'anthropic', 'vertex_ai', 'openrouter')",
            name="chk_custom_models_provider",
        ),
        CheckConstraint(
            "capability IN ('chat', 'embeddings')",
            name="chk_custom_models_capability",
        ),
        CheckConstraint(
            "input_price_per_million_usd > 0",
            name="chk_custom_models_input_price_positive",
        ),
        CheckConstraint(
            "output_price_per_million_usd IS NULL OR output_price_per_million_usd > 0",
            name="chk_custom_models_output_price_positive",
        ),
        CheckConstraint(
            "(capability = 'chat' AND output_price_per_million_usd IS NOT NULL) OR "
            "(capability = 'embeddings' AND output_price_per_million_usd IS NULL)",
            name="chk_custom_models_capability_output_price",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)

    # Plain TEXT + CHECK, not `provider_key.provider_name_enum` - see
    # module docstring. Strict subset of `providers.registry.
    # SUPPORTED_PROVIDERS`; deliberately excludes `"ollama"`.
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    native_model_id: Mapped[str] = mapped_column(Text, nullable=False)

    # String-backed enum, not a Postgres enum type - see module docstring
    # and `custom_model_capability_type` above.
    capability: Mapped[ModelCapability] = mapped_column(
        custom_model_capability_type, nullable=False
    )

    input_price_per_million_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    output_price_per_million_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 6), nullable=True
    )
    pricing_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Server-set (`date.today()`) by `services/custom_models.py` on every
    # create and pricing edit - never a DB-level default. See module
    # docstring.
    pricing_as_of: Mapped[date] = mapped_column(Date, nullable=False)

    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))

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
