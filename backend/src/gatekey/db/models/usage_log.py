"""`UsageLog` - one persisted row per gateway request (Phase 1.5 - Logging &
Observability Basic).

Prior to this phase, `api.v1.gateway.common.log_gateway_request()` only
emitted a structured log line - explicitly documented as NOT a persisted
usage-accounting record. This table is that persisted record: it is what
backs the admin usage-summary endpoint (`api/v1/admin/usage.py`) and the
dashboard's "totals by user, by model, over a selectable time range"
requirement (`gatekey/phase-1-core-gateway.md` 1.5/1.6).

Scope note: like every other Phase 1 table, this is scoped to the single
default org (`constants.DEFAULT_ORG_ID`) - no multi-org signup flow yet.

No prompt/response body logging (per 1.5's explicit scope boundary - body
logging with redaction is Phase 3). `model` stores the raw model string the
caller requested (even if it turned out to be invalid/denied/unpriced) -
useful for observability of rejected traffic, not just successful traffic.
`user_id`/`service_account_key_id` are both nullable+`SET NULL` (not
`RESTRICT`) specifically so this historical log table never blocks or is
blocked by admin CRUD on those entities - a usage record should outlive the
credential/user that generated it. Phase 2's `team_id`/
`personal_api_key_id` follow that exact same pattern.

Retention/purge (Phase 3, AC6.2)
--------------------------------
`services.scheduler.run_log_prompt_purge_if_due` hard-deletes rows older
than `compliance_settings.log_prompt_retention_days` (default 30, never
NULL - see `db/models/compliance_settings.py`). This table stores no raw
prompt/response text (see above), so purging here means deleting the row
entirely, not redacting content within it.

Cost normalization (Phase 2, design doc ADR-9)
-----------------------------------------------
`cost_usd` (existing) means "the normalized cost charged against the org's
budget currency". `raw_provider_cost_usd` keeps the provider-native
pre-normalization figure and `fx_rate_applied` the rate used, so the
normalization step stays auditable per row. In Phase 2 the org currency is
always USD, so `raw_provider_cost_usd == cost_usd` and
`fx_rate_applied == 1` always - the columns exist now so real FX conversion
later is additive, not a rewrite.

`original_model` (Phase 4 - Reliability & Cost Efficiency)
------------------------------------------------------------
`NULL` on every non-degraded request (the overwhelming majority); populated
with the originally-requested model only when `check_degradation`
substituted a cheaper `downgrade_target_model` (AC4.7). `model` (above)
always holds the model actually used/charged - never overwritten by
degradation. This is what the dashboard's "cost saved via degradation"
aggregation queries against
(`pricing.compute_cost(original_model, tokens) - cost_usd`, computed at
query time, never stored redundantly - same "compute, don't store" instinct
`failover_events`' duration follows). See
`docs/design/phase-4-reliability-cost-efficiency-design.md` section 1.8.

`self_hosted_provider_id` (Phase 5 - Differentiators, 5.5)
--------------------------------------------------------------
Nullable + `ON DELETE SET NULL`, following the exact same "a historical
usage record must outlive the entity that generated it" pattern every other
nullable FK on this table uses. `NULL` for every non-self-hosted request
(the overwhelming majority); populated with the `SelfHostedProvider.id`
that served the request when `route.provider == "self_hosted"`. `provider`
(above) takes the literal string value `"self_hosted"` for these rows - the
query discriminator the admin UI/export use to visibly label self-hosted
cost figures "estimated" (AC5.5.7). See
`gatekey/phase-5-technical-design.md` sections 2.3/4.1.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, Numeric, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org
    from gatekey.db.models.provider_key import ProviderKey


class UsageLog(Base):
    __tablename__ = "usage_logs"
    __table_args__ = (
        Index("ix_usage_logs_org_id_created_at", "org_id", "created_at"),
        Index("ix_usage_logs_user_id", "user_id"),
        Index("ix_usage_logs_created_at", "created_at"),
        Index("ix_usage_logs_team_id", "team_id"),
        # Phase 4: failover event queries
        Index("ix_usage_logs_failover", "failover_attempt", "failover_key_id"),
        # Phase 4: cache hit queries
        Index("ix_usage_logs_cache_hit", "cache_hit"),
        # Phase 4: degradation queries
        Index("ix_usage_logs_degraded_from", "degraded_from_model"),
        Index("ix_usage_logs_degraded_to", "degraded_to_model"),
        # Phase 5: self-hosted provider queries
        Index("ix_usage_logs_self_hosted_provider_id", "self_hosted_provider_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False
    )

    # Nullable + SET NULL: a usage row must survive deletion of the user or
    # service-account key that generated it (this table is a historical
    # record, not a live reference) - see module docstring.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    service_account_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("service_account_keys.id", ondelete="SET NULL"), nullable=True
    )
    # Phase 2 - same nullable + SET NULL pattern as the two columns above
    # (see module docstring).
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    personal_api_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("personal_api_keys.id", ondelete="SET NULL"), nullable=True
    )

    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Raw caller-requested model string - not necessarily a valid
    # MODEL_REGISTRY key (see module docstring).
    model: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # Phase 4 - see module docstring "original_model". NULL unless this
    # request was degraded.
    original_model: Mapped[str | None] = mapped_column(Text, nullable=True)

    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # NULL when the request was never charged (denied/errored before a
    # provider response) - distinct from a legitimate $0 charge. NUMERIC(20,10)
    # to match `users.current_spend_usd`'s precision (see db/models/user.py).
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(20, 10), nullable=True)

    # Phase 2 cost-normalization audit trail - see module docstring ("Cost
    # normalization"). NULL when the request was never charged, and for
    # every pre-Phase-2 historical row.
    raw_provider_cost_usd: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 10), nullable=True
    )
    fx_rate_applied: Mapped[Decimal] = mapped_column(
        Numeric(20, 10), nullable=False, server_default=text("1")
    )

    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stream: Mapped[bool] = mapped_column(nullable=False, default=False)

    # Mirrors api.v1.gateway.common.log_gateway_request's `status` values
    # ("ok", "model_denied", "budget_exhausted", "provider_not_configured",
    # "provider_error", "unsupported_request", "client_disconnected",
    # "usage_unavailable", "charge_failed", "internal_error", ...).
    status: Mapped[str] = mapped_column(String(64), nullable=False)
    # Redundant with `status == "ok"` but kept as its own indexable boolean
    # column so the usage-summary query's error-rate aggregation doesn't
    # need a string comparison per row.
    success: Mapped[bool] = mapped_column(nullable=False)

    # Phase 4: cache hit tracking - True if response was served from cache
    cache_hit: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    # Phase 4: failover tracking
    # failover_attempt: 0 = primary key, >0 = retry count
    # failover_key_id: backup key used (if any)
    failover_attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    failover_key_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("provider_keys.id", ondelete="SET NULL"), nullable=True
    )

    # Phase 4: graceful degradation tracking
    # degraded_from_model: original model (when degradation occurred)
    # degraded_to_model: substituted model (when degradation occurred)
    degraded_from_model: Mapped[str | None] = mapped_column(Text, nullable=True)
    degraded_to_model: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Phase 5: self-hosted routing tracking - see module docstring
    # "self_hosted_provider_id".
    self_hosted_provider_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("self_hosted_providers.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    org: Mapped["Org"] = relationship("Org")
    failover_key: Mapped["ProviderKey | None"] = relationship(
        "ProviderKey", back_populates="usage_logs"
    )
