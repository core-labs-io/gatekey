"""`ShadowAiIngestConfig` - an org's Shadow AI Discovery ingestion
credential + detection/enforcement/retention configuration (Phase 5 -
Differentiators, 5.1 Shadow AI Discovery).

See `gatekey/phase-5-technical-design.md` sections 2.5/4.2 for the full
design rationale. Follows the identical one-row-per-org,
hash-only-secret-if-present shape as `ScimConfig` (mirrors that table's own
`org_id`-as-PK, absence-of-row-means-not-set-up-yet posture).

Ingest token storage (deliberate, security-reviewed choice)
--------------------------------------------------------------
`ingest_token_hash` is the raw SHA-256 digest of the shadow-AI ingest
bearer token (`gk_sai_...`) - the exact same lookup-hash discipline (fast
hash, not a slow KDF) as `ScimConfig.bearer_token_hash`/`ServiceAccountKey.
secret_hash`/`PersonalApiKey.secret_hash`/`UserSession.token_hash`, for the
identical "256-bit random token, not a guessable password" rationale.
`NULL` = ingestion has not been set up yet - `require_shadow_ai_ingest_
token` (backend-developer task, `api/deps.py`) rejects ALL traffic to the
ingestion endpoint while this is `NULL` (AC5.1.4's fail-closed-until-setup
requirement).

Deliberately NOT the AES-256-GCM envelope `provider_keys`/
`self_hosted_providers` use: that envelope exists specifically for secrets
Gatekey must later decrypt and use outbound (a provider API key, a
self-hosted bearer token Gatekey sends to a self-hosted server). A
shadow-AI ingest token is inbound-only - Gatekey only ever needs to verify
a presented token equals what it issued, never reverse it. Hash-only
storage is strictly MORE secure than a reversible envelope for this shape
of credential (a hash cannot be reversed even if the master key were later
compromised) - see design doc section 2.5, "This is a deliberate revision
of the orchestrator's brief."

Config fields
--------------
`detection_source` ('sase_log' | 'proxy_log'), `enforcement_mode`
('detect_only' | 'notification' | 'webhook'), `webhook_ciphertext`/
`webhook_nonce`/`webhook_auth_tag` (only meaningful when `enforcement_mode =
'webhook'` - not enforced at the DB level, an app-layer concern),
`shadow_ai_retention_days` (default 90, never NULL unlike `compliance_
settings.audit_retention_days` - this table's purge job, `run_shadow_ai_
purge_if_due`, always fires against a finite cutoff, no interval-gating, per
design doc section 5.5 row 5).

`shadow_ai_retention_days` lives here, not on `compliance_settings` - see
the owning migration's module docstring for the full rationale (keeping
Shadow AI's differential retention/backup posture decoupled from the
Phase 3 audit/DLP retention table).

Webhook URL at rest (security-reviewer/QA Fix 3)
----------------------------------------------------
`webhook_ciphertext`/`webhook_nonce`/`webhook_auth_tag` are the byte-for-byte
identical AES-256-GCM envelope shape `Team.webhook_ciphertext`/`webhook_
nonce`/`webhook_auth_tag` use (`services.teams`'s established convention,
now mirrored here rather than deviated from) - a webhook/SOAR integration
URL can embed a bearer token or signing secret directly in its query
string, so this is never stored as plaintext. All three `NULL` = no webhook
configured; always written together (`services.shadow_ai.set_shadow_ai_
config`). Associated data is bound via `services.shadow_ai.
shadow_ai_webhook_aad()` - deliberately distinct from `services.teams.
team_webhook_aad()`'s `team_id`-bound AAD, same "no cross-table ciphertext
reuse" rationale `services.self_hosted_providers`' module docstring
documents for its own distinct AAD binding. The plaintext URL is never
returned by any read path (`schemas.shadow_ai.ShadowAiConfigResponse`
exposes only `webhook_configured: bool`).

`0042_create_shadow_ai_tables.py` originally created a plain `webhook_url
Text` column here (a self-disclosed deviation from the design doc's stated
requirement - see `alembic/versions/0043_encrypt_shadow_ai_webhook_url.py`'s
own docstring for the full history) - `0043` replaced it with this envelope.

Migration ownership
--------------------
Names/definitions here must stay in lockstep with the explicit DDL in
`alembic/versions/0042_create_shadow_ai_tables.py` and `alembic/versions/
0043_encrypt_shadow_ai_webhook_url.py` - those migrations, not `Base.
metadata.create_all()`, are the source of truth for actual DDL.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, LargeBinary, Text, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from gatekey.db.base import Base

if TYPE_CHECKING:
    from gatekey.db.models.org import Org


class ShadowAiIngestConfig(Base):
    __tablename__ = "shadow_ai_ingest_config"
    __table_args__ = (
        CheckConstraint(
            "detection_source IN ('sase_log', 'proxy_log')",
            name="chk_shadow_ai_ingest_config_detection_source",
        ),
        CheckConstraint(
            "enforcement_mode IN ('detect_only', 'notification', 'webhook')",
            name="chk_shadow_ai_ingest_config_enforcement_mode",
        ),
        CheckConstraint(
            "shadow_ai_retention_days > 0",
            name="chk_shadow_ai_ingest_config_retention_days_positive",
        ),
    )

    # Primary key is `org_id` itself, not a surrogate `id` - see module
    # docstring. At most one row per org, by construction.
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orgs.id", ondelete="CASCADE"),
        primary_key=True,
    )
    # SHA-256 digest (32 bytes) of the ingest bearer token - see module
    # docstring "Ingest token storage". NULL = not yet set up.
    ingest_token_hash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    token_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    detection_source: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'sase_log'")
    )
    enforcement_mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'detect_only'")
    )
    # AES-256-GCM envelope for the webhook URL - all three NULL, or all
    # three set, always written together by the app layer. See module
    # docstring "Webhook URL at rest".
    webhook_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    webhook_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    webhook_auth_tag: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    shadow_ai_retention_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("90")
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
