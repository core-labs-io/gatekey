"""Effective org-settings read helper (Phase 2, BD-19 + consumers).

`org_settings` follows ModelPolicy's ADR-2 exactly: absence of a row = the
default state (design doc section 1.1) - no signup seed, and no caller may
assume a row exists. This module centralizes the "row or defaults" read so
BD-16 (personal-key soft cap / max expiration), BD-18 (currency on alert
events), and BD-19 (the admin org-settings endpoints) all resolve defaults
identically. Writes go through `services.team_budget.set_org_budget_ceiling`
(the ADR-5 locked upsert) - never here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.org_settings import OrgSettings
from gatekey.errors import GatekeyError
from gatekey.services.encryption import encrypt_secret

if TYPE_CHECKING:
    from gatekey.services.encryption import KeyProvider

# Defaults mirror the column server defaults in `db/models/org_settings.py` /
# migration 0007 exactly - keep in lockstep.
DEFAULT_CURRENCY = "USD"
DEFAULT_PERSONAL_KEY_SOFT_CAP = 10


def org_webhook_aad(org_id: uuid.UUID) -> bytes:
    """AAD binding the org alert-webhook ciphertext to `org_id` (added
    alongside migration `0045`) - mirrors `services.teams.team_webhook_aad`
    exactly, one level up. Shared with the org-level notifier's decrypt
    path - both sides must build AAD through this one function."""
    return f"org:{org_id}".encode("utf-8")


def webhook_configured(row: OrgSettings) -> bool:
    return row.webhook_ciphertext is not None


@dataclass(frozen=True)
class EffectiveOrgSettings:
    """The org's settings with absence-of-row defaults already applied."""

    budget_ceiling_usd: Decimal | None
    currency: str
    max_self_serve_key_expiration_days: int | None
    personal_key_soft_cap: int
    auto_provision_personal_key_on_approval: bool
    # Org-wide budget safeguard alert config (added alongside `0045`/`0046`)
    # - see those migrations' docstrings. `current_spend_usd` is
    # deliberately NOT here - it's live per-request mutable state, not a
    # "settings" value; see `services.budget.get_org_budget_state` for that.
    # 50/75/100% (NOT team's 80/100 - `0046`'s docstring).
    alert_threshold_50_enabled: bool
    alert_threshold_75_enabled: bool
    alert_threshold_100_enabled: bool
    webhook_alert_enabled: bool
    webhook_configured: bool
    email_alert_enabled: bool
    # Dedicated, self-registered recipient (added by `0048`) - see that
    # migration's docstring. `None` = the first-SSO-login org_admin
    # onboarding gate has not been satisfied yet.
    alert_recipient_email: str | None


_DEFAULTS = EffectiveOrgSettings(
    budget_ceiling_usd=None,
    currency=DEFAULT_CURRENCY,
    max_self_serve_key_expiration_days=None,
    personal_key_soft_cap=DEFAULT_PERSONAL_KEY_SOFT_CAP,
    auto_provision_personal_key_on_approval=False,
    alert_threshold_50_enabled=True,
    alert_threshold_75_enabled=True,
    alert_threshold_100_enabled=True,
    webhook_alert_enabled=False,
    webhook_configured=False,
    email_alert_enabled=False,
    alert_recipient_email=None,
)


async def get_effective_org_settings(session: AsyncSession) -> EffectiveOrgSettings:
    """Return the org's settings row, or the ADR-2 defaults if none exists."""
    row = (
        await session.execute(
            select(OrgSettings).where(OrgSettings.org_id == DEFAULT_ORG_ID)
        )
    ).scalar_one_or_none()
    if row is None:
        return _DEFAULTS
    return EffectiveOrgSettings(
        budget_ceiling_usd=row.budget_ceiling_usd,
        currency=row.currency,
        max_self_serve_key_expiration_days=row.max_self_serve_key_expiration_days,
        personal_key_soft_cap=row.personal_key_soft_cap,
        auto_provision_personal_key_on_approval=row.auto_provision_personal_key_on_approval,
        alert_threshold_50_enabled=row.alert_threshold_50_enabled,
        alert_threshold_75_enabled=row.alert_threshold_75_enabled,
        alert_threshold_100_enabled=row.alert_threshold_100_enabled,
        webhook_alert_enabled=row.webhook_alert_enabled,
        webhook_configured=webhook_configured(row),
        email_alert_enabled=row.email_alert_enabled,
        alert_recipient_email=row.alert_recipient_email,
    )


async def set_org_alert_recipient_email(session: AsyncSession, *, email: str) -> OrgSettings:
    """Set the org's dedicated alert-recipient email (added by `0048`) -
    the first-SSO-login org_admin onboarding action
    (`POST /v1/admin/org-settings/alert-email`). Upserts the row first
    (same pattern every other org-settings writer in this module uses) so
    there is always a row to update. Flushes, does not commit."""
    await session.execute(
        postgresql.insert(OrgSettings)
        .values(org_id=DEFAULT_ORG_ID)
        .on_conflict_do_nothing(index_elements=[OrgSettings.org_id])
    )
    row = (
        await session.execute(
            select(OrgSettings).where(OrgSettings.org_id == DEFAULT_ORG_ID).with_for_update()
        )
    ).scalar_one()
    row.alert_recipient_email = email
    await session.flush()
    return row


async def set_org_alert_config(
    session: AsyncSession,
    *,
    threshold_50_enabled: bool,
    threshold_75_enabled: bool,
    threshold_100_enabled: bool,
    webhook_enabled: bool,
    email_enabled: bool,
    webhook_url: str | None,
    webhook_url_provided: bool,
    key_provider: KeyProvider,
) -> OrgSettings:
    """Apply the full org alert-config write (added alongside `0045`) -
    mirrors `services.teams.set_team_alert_config` exactly, one level up.
    Upserts the row first (same pattern `services.team_budget.
    set_org_budget_ceiling` uses) so there is always a row to update, even
    for an org that has never touched org settings. The webhook URL is
    encrypted at rest with AAD bound to the org id and never echoed back.
    `webhook_url_provided=False` keeps the stored envelope; `True` with a
    string replaces it; `True` with `None` clears it. Flushes, does not
    commit."""
    await session.execute(
        postgresql.insert(OrgSettings)
        .values(org_id=DEFAULT_ORG_ID)
        .on_conflict_do_nothing(index_elements=[OrgSettings.org_id])
    )
    row = (
        await session.execute(
            select(OrgSettings).where(OrgSettings.org_id == DEFAULT_ORG_ID).with_for_update()
        )
    ).scalar_one()

    if webhook_url_provided:
        if webhook_url is None:
            row.webhook_ciphertext = None
            row.webhook_nonce = None
            row.webhook_auth_tag = None
        else:
            envelope = encrypt_secret(
                webhook_url.encode("utf-8"),
                aad=org_webhook_aad(row.org_id),
                key_provider=key_provider,
            )
            row.webhook_ciphertext = envelope.ciphertext
            row.webhook_nonce = envelope.nonce
            row.webhook_auth_tag = envelope.auth_tag
    if webhook_enabled and row.webhook_ciphertext is None:
        raise GatekeyError(
            "Cannot enable webhook alerts without a webhook URL configured.",
            code="webhook_url_required",
            status_code=422,
        )
    row.alert_threshold_50_enabled = threshold_50_enabled
    row.alert_threshold_75_enabled = threshold_75_enabled
    row.alert_threshold_100_enabled = threshold_100_enabled
    row.webhook_alert_enabled = webhook_enabled
    row.email_alert_enabled = email_enabled
    await session.flush()
    return row
