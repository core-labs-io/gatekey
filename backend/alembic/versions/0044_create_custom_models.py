"""create custom_models table

Custom Model Registry (Admin-Managed BYOK Models). See
`gatekey.db.models.custom_model.CustomModel` for the ORM side and
`gatekey/custom-model-registry-technical-design.md` sections 4.1/4.2 for
the full design rationale. This migration is the source of truth for
actual DDL.

`custom_models` lets an Org Admin register a gateway-facing name mapped to
a native model id at one of four existing BYOK providers (`openai`/
`anthropic`/`vertex_ai`/`openrouter` - deliberately **not** `ollama`, which
has its own mechanism via `self_hosted_providers`, Phase 5.5), with
admin-entered real per-token pricing, gated behind a one-time live
verification call (`verified`) before it becomes routable - see design doc
section 2.3/5.

No new credential type is introduced by this table - a custom model rides
the existing, already-encrypted `provider_keys` row for its `provider`
(fetched via `services.proxy_keys.get_decrypted_provider_credential()` at
verify/request time), so there is no `ciphertext`/`nonce`/`auth_tag`
envelope here at all, unlike `self_hosted_providers` (design doc section
2 "Explicitly not asked for at registration").

`provider` is a plain `TEXT` + `CHECK`, deliberately **not**
`provider_name_enum` (design doc section 4.1): that Postgres enum type
includes `'ollama'`, which this table must exclude, and reusing it would
require either a partial-values migration or an app-layer-only exclusion
of a value the column itself still permits. A fresh `CHECK` is simpler and
matches the product spec's own explicit guidance (section 8).

`capability` is plain `TEXT` + `CHECK` rather than a Postgres enum, mirroring
this table's own "no dependency on a type owned by a different bounded
module" posture (design doc section 4.1) - the ORM model maps it to the
existing `providers.model_registry.ModelCapability` Python enum at the
application layer only (`sa.Enum(..., native_enum=False)`), never a `CREATE
TYPE`.

`input_price_per_million_usd` is hard-blocked at `CHECK > 0` - no $0
pricing permitted in v1 (product spec section 12's finalized decision,
superseding an earlier draft that would have allowed it) - matching
`self_hosted_providers.cost_basis_per_gpu_hour`'s identical `> 0`
constraint. `output_price_per_million_usd` is nullable but `CHECK > 0` when
present, and its nullability is cross-checked against `capability` by
`chk_custom_models_capability_output_price` - the DB-level defense-in-depth
backstop for the same completeness invariant `services/custom_models.py`
enforces at every create/edit (design doc section 4.1 guard #4); this
mirrors the "pair a business rule with a DB-level sanity bound" convention
Phase 5's `chk_chain_purge_mutually_exclusive` established.

`pricing_as_of DATE NOT NULL` has no DB-level default - `services/
custom_models.py` always sets it explicitly to `date.today()` server-side,
re-set on every pricing edit, not just at row creation (design doc section
4.1).

No FK from `usage_logs` to `custom_models` is added by this or any future
migration in this feature - design doc section 2.6 explains why (`usage_
logs.provider`/`.model` already fully capture what happened for a
custom-model request; no per-custom-model usage-breakdown endpoint is in
scope for v1).

Revision ID: 0044
Revises: 0043
Create Date: 2026-08-06

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0044"
down_revision: Union[str, None] = "0043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "custom_models",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "org_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orgs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        # Plain TEXT + CHECK, not `provider_name_enum` - that Postgres enum
        # includes 'ollama', which this table must exclude. See module
        # docstring.
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("native_model_id", sa.Text(), nullable=False),
        # Plain TEXT + CHECK, not a Postgres enum - see module docstring.
        # Maps to `providers.model_registry.ModelCapability` at the ORM
        # layer only.
        sa.Column("capability", sa.Text(), nullable=False),
        sa.Column("input_price_per_million_usd", sa.Numeric(12, 6), nullable=False),
        sa.Column("output_price_per_million_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("pricing_source", sa.Text(), nullable=True),
        # Server-set by the app layer (`date.today()`), never a DB default -
        # must be re-set on every pricing edit, not just at row creation.
        # See module docstring.
        sa.Column("pricing_as_of", sa.Date(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "provider IN ('openai', 'anthropic', 'vertex_ai', 'openrouter')",
            name="chk_custom_models_provider",
        ),
        sa.CheckConstraint(
            "capability IN ('chat', 'embeddings')",
            name="chk_custom_models_capability",
        ),
        sa.CheckConstraint(
            "input_price_per_million_usd > 0",
            name="chk_custom_models_input_price_positive",
        ),
        sa.CheckConstraint(
            "output_price_per_million_usd IS NULL OR output_price_per_million_usd > 0",
            name="chk_custom_models_output_price_positive",
        ),
        # Defense-in-depth backstop mirroring the app-layer completeness
        # guard (design doc section 4.1 guard #4) - same "pair a business
        # rule with a DB-level sanity bound" convention Phase 5 established
        # (`chk_chain_purge_mutually_exclusive`).
        sa.CheckConstraint(
            "(capability = 'chat' AND output_price_per_million_usd IS NOT NULL) OR "
            "(capability = 'embeddings' AND output_price_per_million_usd IS NULL)",
            name="chk_custom_models_capability_output_price",
        ),
        sa.UniqueConstraint("org_id", "name", name="uq_custom_models_org_id_name"),
    )

    op.create_index("ix_custom_models_org_id", "custom_models", ["org_id"])


def downgrade() -> None:
    op.drop_index("ix_custom_models_org_id", table_name="custom_models")
    op.drop_table("custom_models")
