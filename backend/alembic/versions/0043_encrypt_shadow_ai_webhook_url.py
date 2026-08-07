"""replace shadow_ai_ingest_config.webhook_url with an AES-256-GCM envelope

Phase 5 (Differentiators), 5.1 Shadow AI Discovery - security-reviewer/QA
Fix 3 (self-disclosed gap, now closed). See
`gatekey.db.models.shadow_ai_ingest_config.ShadowAiIngestConfig` for the ORM
side, `gatekey/phase-5-technical-design.md` section 4.2 for the design
doc's original (unmet, until now) requirement, and
`docs/policy/shadow-ai-data-handling.md` section 9 for the self-disclosed
gap this closes. This migration is the source of truth for actual DDL.

`0042_create_shadow_ai_tables.py` created `shadow_ai_ingest_config.
webhook_url` as a plain `Text` column - a deviation from the design doc's
stated requirement that it follow `teams.webhook_ciphertext`/`webhook_nonce`/
`webhook_auth_tag`'s existing encrypted-at-rest convention (AES-256-GCM
envelope). A webhook/SOAR integration URL can embed a bearer token or
signing secret directly in its query string, so plaintext-at-rest is a real
gap, not just a defense-in-depth nicety - `shadow_ai_ingest_config.
webhook_url` was never returned by any read path (`schemas.shadow_ai.
ShadowAiConfigResponse` only ever exposed `webhook_configured: bool`), but
that is an application-layer mitigation, not at-rest encryption.

This is a destructive column swap (`webhook_url` dropped, replaced by the
three-column envelope) - `shadow_ai_ingest_config` has no pre-existing
production data to migrate/preserve in any deployed sense (Phase 5 is not
yet GA at the time of this fix), so a clean drop-and-recreate is acceptable
here rather than an in-place re-encrypt-in-migration step. Any org that had
already configured a webhook URL under the old plaintext column must
reconfigure it once after this migration applies (the same "opt-in
enforcement mode requires re-confirming `confirm=true`" flow already gates
this write - see `services.shadow_ai.set_shadow_ai_config`'s docstring).

`webhook_ciphertext`/`webhook_nonce`/`webhook_auth_tag` are the byte-for-byte
identical envelope shape `teams.webhook_ciphertext`/`webhook_nonce`/
`webhook_auth_tag` use (`LargeBinary`, all three nullable, always written
together) - see `services.shadow_ai.shadow_ai_webhook_aad()` for the
associated-data binding (deliberately distinct from `services.teams.
team_webhook_aad()`'s `team_id`-bound AAD, same "no cross-table ciphertext
reuse" rationale `services.self_hosted_providers`' module docstring already
documents for its own distinct AAD binding).

Downgrade recreates the plain `webhook_url` `Text` column (`NULL` for every
row - the encrypted envelope cannot be reversed back to plaintext without
the application-layer decrypt key, so downgrade is schema-only, not
data-preserving, consistent with this migration's own destructive-swap
rationale above).

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-06

"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0043"
down_revision: Union[str, None] = "0042"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("shadow_ai_ingest_config", "webhook_url")
    op.add_column(
        "shadow_ai_ingest_config",
        sa.Column("webhook_ciphertext", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "shadow_ai_ingest_config",
        sa.Column("webhook_nonce", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "shadow_ai_ingest_config",
        sa.Column("webhook_auth_tag", sa.LargeBinary(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("shadow_ai_ingest_config", "webhook_auth_tag")
    op.drop_column("shadow_ai_ingest_config", "webhook_nonce")
    op.drop_column("shadow_ai_ingest_config", "webhook_ciphertext")
    op.add_column(
        "shadow_ai_ingest_config",
        sa.Column("webhook_url", sa.Text(), nullable=True),
    )
