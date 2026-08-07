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

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.org_settings import OrgSettings

# Defaults mirror the column server defaults in `db/models/org_settings.py` /
# migration 0007 exactly - keep in lockstep.
DEFAULT_CURRENCY = "USD"
DEFAULT_PERSONAL_KEY_SOFT_CAP = 10


@dataclass(frozen=True)
class EffectiveOrgSettings:
    """The org's settings with absence-of-row defaults already applied."""

    budget_ceiling_usd: Decimal | None
    currency: str
    max_self_serve_key_expiration_days: int | None
    personal_key_soft_cap: int
    auto_provision_personal_key_on_approval: bool


_DEFAULTS = EffectiveOrgSettings(
    budget_ceiling_usd=None,
    currency=DEFAULT_CURRENCY,
    max_self_serve_key_expiration_days=None,
    personal_key_soft_cap=DEFAULT_PERSONAL_KEY_SOFT_CAP,
    auto_provision_personal_key_on_approval=False,
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
    )
