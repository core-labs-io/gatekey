"""Org-settings admin endpoints (Phase 2, BD-19) - design doc section 5.5.

`org_settings` follows the ADR-1/ADR-2 pattern: single row keyed by
`org_id`, absence of a row = defaults (`services.org_settings`). `GET`
therefore never writes; `PUT` upserts. Ceiling edits go through
`services.team_budget.set_org_budget_ceiling`'s `SELECT ... FOR UPDATE`
check (A3: never below the current sum of team ceilings) - never a bare
column write. The `org_settings.update` audit entry rides the same
transaction (design doc section 7).

Org-wide budget safeguard (added alongside migration `0045`) - "one minor
typo in team budget can cost an org millions." `current_spend_usd` in
`OrgSettingsResponse` is read-only informational state (live, checked and
incremented on every gateway request - see `services.budget.
get_org_budget_state`/`record_org_usage_charge`); the alert-config
(threshold %, webhook, email) lives on its own `alert-config` sub-resource,
mirroring `api/v1/teams.py`'s identical team-level split, one level up.
`reset-spend` is a deliberately separate, explicit admin action (never
automatic - see migration `0045`'s docstring for why).
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import get_key_provider, get_source_ip, require_role
from gatekey.db.session import get_db_session
from gatekey.services.audit import write_audit_entry
from gatekey.services.budget import get_org_budget_state, reset_org_spend
from gatekey.services.encryption import KeyProvider
from gatekey.services.org_settings import (
    DEFAULT_PERSONAL_KEY_SOFT_CAP,
    EffectiveOrgSettings,
    get_effective_org_settings,
    set_org_alert_config,
    set_org_alert_recipient_email,
)
from gatekey.services.sessions import SessionContext
from gatekey.services.team_budget import set_org_budget_ceiling

# Lightweight shape check only (same "not full RFC 5322 parsing, just catch
# obvious mistakes" posture as this file's own `webhook_url` validator) -
# no new third-party dependency (`email-validator`) for one field.
_EMAIL_SHAPE_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

router = APIRouter(prefix="/v1/admin/org-settings", tags=["admin", "org-settings"])


class OrgSettingsResponse(BaseModel):
    budget_ceiling_usd: Decimal | None
    currency: str
    max_self_serve_key_expiration_days: int | None
    personal_key_soft_cap: int
    auto_provision_personal_key_on_approval: bool
    # Org-wide budget safeguard (added alongside `0045`) - live spend state,
    # read-only here (see module docstring).
    current_spend_usd: Decimal
    # Dedicated alert-recipient email (added by `0048`) - read-only here,
    # written only through `POST .../alert-email`. `None` = the
    # first-SSO-login org_admin onboarding prompt hasn't been satisfied.
    alert_recipient_email: str | None


class OrgSettingsPutRequest(BaseModel):
    """Full-replace PUT. `currency` is structurally pinned to 'USD' this
    phase (ADR-9's identity normalization - the field exists so real FX
    support later is additive, but no other value is writable yet)."""

    model_config = ConfigDict(extra="forbid")

    budget_ceiling_usd: Decimal | None = Field(default=None, ge=0)
    currency: Literal["USD"] = "USD"
    max_self_serve_key_expiration_days: int | None = Field(default=None, ge=1)
    personal_key_soft_cap: int = Field(default=DEFAULT_PERSONAL_KEY_SOFT_CAP, ge=1)
    auto_provision_personal_key_on_approval: bool = False


async def _response(session: AsyncSession, settings: EffectiveOrgSettings) -> OrgSettingsResponse:
    org_state = await get_org_budget_state(session)
    return OrgSettingsResponse(
        budget_ceiling_usd=settings.budget_ceiling_usd,
        currency=settings.currency,
        max_self_serve_key_expiration_days=settings.max_self_serve_key_expiration_days,
        personal_key_soft_cap=settings.personal_key_soft_cap,
        auto_provision_personal_key_on_approval=settings.auto_provision_personal_key_on_approval,
        current_spend_usd=org_state.current_spend_usd,
        alert_recipient_email=settings.alert_recipient_email,
    )


@router.get("", response_model=OrgSettingsResponse)
async def get_org_settings_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> OrgSettingsResponse:
    return await _response(session, await get_effective_org_settings(session))


@router.put("", response_model=OrgSettingsResponse)
async def put_org_settings_endpoint(
    payload: OrgSettingsPutRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> OrgSettingsResponse:
    old = await get_effective_org_settings(session)
    # Lock-ordering fix (CMR-14 security review): write the audit entry
    # BEFORE taking the `org_settings` row lock. `write_audit_entry`, when
    # the org's hash chain is enabled, takes `SELECT ... FOR UPDATE` on
    # `compliance_settings`; `set_org_budget_ceiling` below takes
    # `SELECT ... FOR UPDATE` on `org_settings`. `api/v1/admin/
    # custom_models.py`'s and `self_hosted_providers.py`'s POST/PUT
    # handlers already acquire compliance_settings before org_settings (the
    # audit write happens first there too, since their service calls commit
    # internally) - this handler previously acquired the two locks in the
    # OPPOSITE order (org_settings first), which is a real, reproduced
    # Postgres deadlock under concurrent admin usage with chaining enabled.
    # Ordering both endpoints identically (compliance_settings, then
    # org_settings) eliminates the cycle. `new_value` is built from the
    # request payload (not the post-write row) since the row lock/upsert
    # hasn't happened yet - identical to every value the upsert below will
    # actually persist on success; if the ceiling check below rejects the
    # write, the whole transaction (including this queued-but-uncommitted
    # audit entry) rolls back with it - same "audit discarded on failure"
    # discipline `custom_models.py`'s docstring describes.
    new_value = payload.model_dump()
    await write_audit_entry(
        session,
        actor=ctx,
        action="org_settings.update",
        target_type="org_settings",
        target_id=str(ctx.org_id),
        old_value={
            "budget_ceiling_usd": old.budget_ceiling_usd,
            "currency": old.currency,
            "max_self_serve_key_expiration_days": old.max_self_serve_key_expiration_days,
            "personal_key_soft_cap": old.personal_key_soft_cap,
            "auto_provision_personal_key_on_approval": old.auto_provision_personal_key_on_approval,
        },
        new_value=new_value,
        source_ip=source_ip,
    )
    # Upserts + locks the row, and runs the A3 sum-of-team-ceilings check
    # (422 budget_ceiling_below_current_allocation passes through). Flushes,
    # does not commit - the remaining field writes ride the same
    # transaction/lock, and the audit entry above already rode it too.
    row = await set_org_budget_ceiling(session, budget_ceiling_usd=payload.budget_ceiling_usd)
    row.currency = payload.currency
    row.max_self_serve_key_expiration_days = payload.max_self_serve_key_expiration_days
    row.personal_key_soft_cap = payload.personal_key_soft_cap
    row.auto_provision_personal_key_on_approval = (
        payload.auto_provision_personal_key_on_approval
    )
    await session.flush()
    await session.commit()
    return await _response(session, await get_effective_org_settings(session))


# --- Alert config (added alongside migration `0045`) --------------------------


class OrgAlertConfigResponse(BaseModel):
    """Never carries the webhook URL in any form - `webhook_configured` is
    the only signal, same rationale as `TeamAlertConfigResponse`.

    50/75/100% (added by `0046`) - deliberately NOT the same threshold set
    `TeamAlertConfigResponse` uses (80/100) - see that migration's
    docstring."""

    threshold_50_enabled: bool
    threshold_75_enabled: bool
    threshold_100_enabled: bool
    webhook_enabled: bool
    webhook_configured: bool
    email_enabled: bool


class OrgAlertConfigPutRequest(BaseModel):
    """`PUT /v1/admin/org-settings/alert-config` body. `webhook_url`
    semantics (via `model_fields_set`) mirror `TeamAlertConfigPutRequest`
    exactly: omitted = keep the stored URL; a string = replace (re-encrypted
    at rest); explicit `null` = clear."""

    model_config = ConfigDict(extra="forbid")

    threshold_50_enabled: bool
    threshold_75_enabled: bool
    threshold_100_enabled: bool
    webhook_enabled: bool
    webhook_url: str | None = None
    email_enabled: bool

    @field_validator("webhook_url")
    @classmethod
    def _url_shape(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("https://", "http://")):
            raise ValueError("webhook_url must be an http(s) URL.")
        return value


def _alert_config_response(settings: EffectiveOrgSettings) -> OrgAlertConfigResponse:
    return OrgAlertConfigResponse(
        threshold_50_enabled=settings.alert_threshold_50_enabled,
        threshold_75_enabled=settings.alert_threshold_75_enabled,
        threshold_100_enabled=settings.alert_threshold_100_enabled,
        webhook_enabled=settings.webhook_alert_enabled,
        webhook_configured=settings.webhook_configured,
        email_enabled=settings.email_alert_enabled,
    )


@router.get("/alert-config", response_model=OrgAlertConfigResponse)
async def get_org_alert_config_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> OrgAlertConfigResponse:
    return _alert_config_response(await get_effective_org_settings(session))


@router.put("/alert-config", response_model=OrgAlertConfigResponse)
async def put_org_alert_config_endpoint(
    payload: OrgAlertConfigPutRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
    key_provider: KeyProvider = Depends(get_key_provider),
) -> OrgAlertConfigResponse:
    old = await get_effective_org_settings(session)
    old_value = {
        "threshold_50_enabled": old.alert_threshold_50_enabled,
        "threshold_75_enabled": old.alert_threshold_75_enabled,
        "threshold_100_enabled": old.alert_threshold_100_enabled,
        "webhook_enabled": old.webhook_alert_enabled,
        "webhook_configured": old.webhook_configured,
        "email_enabled": old.email_alert_enabled,
    }
    row = await set_org_alert_config(
        session,
        threshold_50_enabled=payload.threshold_50_enabled,
        threshold_75_enabled=payload.threshold_75_enabled,
        threshold_100_enabled=payload.threshold_100_enabled,
        webhook_enabled=payload.webhook_enabled,
        email_enabled=payload.email_enabled,
        webhook_url=payload.webhook_url,
        webhook_url_provided="webhook_url" in payload.model_fields_set,
        key_provider=key_provider,
    )
    # The audit record carries configured-state booleans only - never the
    # webhook URL in any form (design doc section 7 secret hygiene, same
    # discipline `teams.py`'s identical endpoint uses).
    await write_audit_entry(
        session,
        actor=ctx,
        action="org_settings.alert_config.update",
        target_type="org_settings",
        target_id=str(ctx.org_id),
        old_value=old_value,
        new_value={
            "threshold_50_enabled": row.alert_threshold_50_enabled,
            "threshold_75_enabled": row.alert_threshold_75_enabled,
            "threshold_100_enabled": row.alert_threshold_100_enabled,
            "webhook_enabled": row.webhook_alert_enabled,
            "webhook_configured": row.webhook_ciphertext is not None,
            "email_enabled": row.email_alert_enabled,
        },
        source_ip=source_ip,
    )
    await session.commit()
    return _alert_config_response(await get_effective_org_settings(session))


# --- Reset spend (added alongside migration `0045`) ----------------------------


class OrgResetSpendResponse(BaseModel):
    current_spend_usd: Decimal


@router.post("/reset-spend", response_model=OrgResetSpendResponse)
async def reset_org_spend_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> OrgResetSpendResponse:
    """Explicit, audited clear of the org-wide spend counter - the only way
    it ever goes back to zero (migration `0045`'s docstring: deliberately
    never automatic, so a still-ongoing leak can't quietly reset itself
    away). Typically used after raising the ceiling or resolving whatever
    triggered the org-wide block."""
    org_state = await get_org_budget_state(session)
    await write_audit_entry(
        session,
        actor=ctx,
        action="org_settings.spend.reset",
        target_type="org_settings",
        target_id=str(ctx.org_id),
        old_value={"current_spend_usd": str(org_state.current_spend_usd)},
        new_value={"current_spend_usd": "0"},
        source_ip=source_ip,
    )
    row = await reset_org_spend(session)
    await session.commit()
    return OrgResetSpendResponse(current_spend_usd=row.current_spend_usd)


# --- Alert-recipient email / first-SSO-login org_admin onboarding (`0048`) ----


class OrgAlertEmailPutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def _shape(cls, value: str) -> str:
        if not _EMAIL_SHAPE_RE.match(value):
            raise ValueError("email must look like a valid email address.")
        return value


class OrgAlertEmailResponse(BaseModel):
    alert_recipient_email: str


@router.post("/alert-email", response_model=OrgAlertEmailResponse)
async def set_org_alert_email_endpoint(
    payload: OrgAlertEmailPutRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> OrgAlertEmailResponse:
    """The first-SSO-login org_admin onboarding action (`api/v1/auth.py`'s
    `_resolve_post_login_redirect` routes here until this is set) - also
    freely re-editable afterward like any other org setting. No guidance
    toward a group address is enforced server-side (there's no reliable
    way to tell a group inbox from a personal one) - the prompt's copy is
    where that steering lives, not validation here."""
    old = await get_effective_org_settings(session)
    row = await set_org_alert_recipient_email(session, email=payload.email)
    await write_audit_entry(
        session,
        actor=ctx,
        action="org_settings.alert_email.update",
        target_type="org_settings",
        target_id=str(ctx.org_id),
        old_value={"alert_recipient_email": old.alert_recipient_email},
        new_value={"alert_recipient_email": row.alert_recipient_email},
        source_ip=source_ip,
    )
    await session.commit()
    assert row.alert_recipient_email is not None
    return OrgAlertEmailResponse(alert_recipient_email=row.alert_recipient_email)
