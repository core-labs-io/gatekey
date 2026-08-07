"""`ModelPolicy` - an org's static model access allow/denylist.

Phase 1.3 (Model Access Governance - Basic). See
`docs/design/phase-1.3-model-governance.md` §1 for the full design
rationale (ADR-1, ADR-2); this docstring summarizes the two decisions that
most affect how this model must be used from the service layer.

`org_id` as primary key (ADR-1)
--------------------------------
Unlike `ProviderKey` (surrogate `id` PK + `UNIQUE(org_id, provider)`),
this table is keyed directly on `org_id`. By product design there is never
more than one policy row per org in Phase 1 - `org_id`-as-PK makes "exactly
one policy per org" a schema-level invariant rather than an app-enforced
one, and gives the admin `PUT` (full-replace upsert) a natural, single
-column `ON CONFLICT (org_id) DO UPDATE` target. This is Phase-1-specific:
Phase 2's team-level nested policy is expected to add a *new* table
alongside this one rather than reshape this PK - see the design doc §8.

Absence of a row means "unconfigured" (ADR-2)
-----------------------------------------------
The Postgres enum `model_policy_mode` backing `mode` has exactly two
values, `allowlist` and `denylist` - deliberately never `unconfigured`.
The product-level "unconfigured" state (no policy has ever been set for
this org) is represented by *no row existing* for that `org_id`, not by a
third enum value. This means:

- A row existing at all implies an explicit choice was made between
  `allowlist` and `denylist` - "unconfigured" is not a state a row can be
  in, it's the state of there being no row.
- The admin `PUT` handler's request schema can only ever express
  `mode="allowlist"` or `mode="denylist"` (never `"unconfigured"`), so
  rejecting a client-supplied `mode="unconfigured"` falls out of ordinary
  Pydantic/FastAPI validation rather than needing app-level defensive code
  that could drift from what the DB actually allows.
- `services.model_policy` is the only place that maps "no row for this
  org_id" to the caller-facing `unconfigured` snapshot
  (`ModelPolicySnapshot(mode="unconfigured", ...)`) - callers must query
  through that service layer (`load_policy_snapshot`/`get_policy`), not
  assume a row always exists.
- Nothing in this migration or model seeds a default row per org (contrast
  with `orgs`, which `0001_create_orgs_and_provider_keys.py` does seed) -
  absence is the correct initial state for every org.

`models` column
----------------
Stores gateway-facing model identifiers - `MODEL_REGISTRY` keys (see
`providers/model_registry.py`), never a provider's `native_model_id`. This
is enforced only at the write path (`services.model_policy.set_policy()`)
since `MODEL_REGISTRY` is an in-memory Python dict, not a DB table - there
is no FK to lean on for this column.

DDL ownership
-------------
As with `ProviderKey.provider` / `provider_name_enum`, the
`model_policy_mode` Postgres enum type and the `model_policies` table are
owned exclusively by the Alembic migration
(`alembic/versions/0003_create_model_policies.py`), not by
`Base.metadata.create_all()`/`drop_all()` - hence `create_type=False`
below. This avoids two competing owners of the same `CREATE TYPE`/
`DROP TYPE` and keeps autogenerate from proposing spurious enum diffs.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, func, text
from sqlalchemy.dialects.postgresql import ENUM as PGEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org


class ModelPolicyMode(str, enum.Enum):
    """The two persistable policy modes. Deliberately does not include an
    `UNCONFIGURED` member - see module docstring (ADR-2): "unconfigured" is
    represented by the absence of a `ModelPolicy` row, never by a value of
    this column.
    """

    ALLOWLIST = "allowlist"
    DENYLIST = "denylist"


# `create_type=False`: DDL for this Postgres enum type is owned exclusively
# by the Alembic migration (see
# `alembic/versions/0003_create_model_policies.py`), not by SQLAlchemy's
# metadata.create_all()/drop_all(). See `provider_key.py`'s
# `provider_name_enum` for the identical rationale/pattern.
model_policy_mode_enum = PGEnum(
    ModelPolicyMode,
    name="model_policy_mode",
    values_callable=lambda enum_cls: [member.value for member in enum_cls],
    create_type=False,
)


class ModelPolicy(Base):
    __tablename__ = "model_policies"

    # Primary key is `org_id` itself, not a surrogate `id` - see module
    # docstring (ADR-1). At most one row per org, by construction.
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orgs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    mode: Mapped[ModelPolicyMode] = mapped_column(model_policy_mode_enum, nullable=False)

    # Gateway-facing `MODEL_REGISTRY` keys - see module docstring. Validated
    # against the registry only at the service-layer write path, not by any
    # DB constraint (no FK target exists for an in-memory dict).
    models: Mapped[list[str]] = mapped_column(
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

    org: Mapped["Org"] = relationship("Org", back_populates="model_policy")
