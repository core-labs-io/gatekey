"""Threshold-alert notifiers (Phase 2, BD-18) - design doc section 6.

Detection is synchronous, pure arithmetic on values the charge `UPDATE`'s
own RETURNING clause already produced (`crossed_thresholds`, section 3.4's
zero-extra-queries contract); delivery (outbound HTTP / SMTP) is scheduled
via FastAPI `BackgroundTasks` and runs AFTER the gateway response has been
sent - a slow or failing notifier target must never add latency to, or risk
failing, the gateway request that triggered it.

Failure posture: everything in the delivery path is caught and logged, never
raised. Log lines never contain the webhook URL (a Slack-style webhook URL
embeds a bearer-equivalent secret in its path) or SMTP credentials.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import uuid
from dataclasses import dataclass
from decimal import Decimal
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlparse

import httpx
from sqlalchemy import or_, select

from gatekey.config import Settings
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.org_settings import OrgSettings
from gatekey.db.models.team import Team
from gatekey.db.models.team_membership import TeamMembership, TeamRole
from gatekey.db.models.user import User, UserOrgRole
from gatekey.services.encryption import EnvKeyProvider, decrypt_secret
from gatekey.services.org_settings import get_effective_org_settings, org_webhook_aad
from gatekey.services.teams import team_webhook_aad

if TYPE_CHECKING:
    from fastapi import BackgroundTasks, FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

    from gatekey.services.budget import ChargeResult, OrgChargeResult

logger = logging.getLogger("gatekey")

# The thresholds team-level alerts evaluate (design doc section 3.4) - kept
# as its own tuple (not the org one below) since `crossed_thresholds`'s
# `pct in enabled` filter means a threshold with no corresponding `enabled`
# key raises KeyError, so the two levels' threshold SETS must stay
# independently correct even though they share the one detection function.
_TEAM_THRESHOLD_PCTS: tuple[int, ...] = (80, 100)
# Org-level safeguard (added alongside migration `0046`): 50/75/100, all
# enabled by default - a wider, earlier-warning ladder than team alerts,
# since an org-wide crossing is a bigger deal.
_ORG_THRESHOLD_PCTS: tuple[int, ...] = (50, 75, 100)


@dataclass(frozen=True)
class NotifyRecipient:
    """A human alert recipient - every team_lead of the team + every
    org_admin (design doc section 6). `email` is None for users with no SSO
    email on record (pre-Phase-2 flat users); the email channel skips them.
    """

    name: str
    email: str | None


@dataclass(frozen=True)
class ThresholdAlertEvent:
    team_id: uuid.UUID
    team_name: str
    threshold_pct: Literal[80, 100]
    current_spend_usd: Decimal
    budget_ceiling_usd: Decimal
    currency: str
    recipients: list[NotifyRecipient]


class Notifier(Protocol):
    async def send(self, event: ThresholdAlertEvent) -> None:
        """Deliver one event. Implementations may raise - the dispatcher
        isolates/logs per-channel failures."""
        ...


# --- detection (pure, zero I/O) ----------------------------------------------


def crossed_thresholds(
    *,
    old_total: Decimal,
    new_total: Decimal,
    ceiling: Decimal | None,
    thresholds: tuple[int, ...] = _TEAM_THRESHOLD_PCTS,
    alert_80_enabled: bool = False,
    alert_100_enabled: bool = False,
    alert_50_enabled: bool = False,
    alert_75_enabled: bool = False,
) -> list[int]:
    """The thresholds this charge JUST crossed (false -> true transition):
    `old_total < pct% of ceiling <= new_total`. Repeated over-threshold
    charges therefore never re-fire (design doc section 3.4). Unmetered
    (`ceiling` None) or nonpositive ceilings never fire.

    `thresholds` selects WHICH pct values to evaluate - defaults to the
    original team-level set (80/100) so every pre-existing call site is
    unchanged. Added alongside migration `0046` (org-level 50/75/100%
    alerts): rather than a generic `dict[int, bool]` (which would let a
    caller silently omit a threshold's enabled-flag and have it default to
    "off" with no type-level signal), each level's four possible pcts are
    named, explicit keyword params - only the ones present in `thresholds`
    are ever consulted, so team-level callers simply never pass `alert_50_
    enabled`/`alert_75_enabled` at all."""
    if ceiling is None or ceiling <= 0:
        return []
    enabled = {
        50: alert_50_enabled,
        75: alert_75_enabled,
        80: alert_80_enabled,
        100: alert_100_enabled,
    }
    return [
        pct
        for pct in thresholds
        if enabled[pct] and old_total < ceiling * pct / 100 <= new_total
    ]


# --- webhook payloads (pure - unit-testable without HTTP) --------------------


def is_slack_webhook(url: str) -> bool:
    """Slack detection by URL shape (design doc section 6's default-generic
    variant selection - no per-team `webhook_format` setting this phase)."""
    return urlparse(url).hostname == "hooks.slack.com"


def _human_summary(event: ThresholdAlertEvent) -> str:
    return (
        f"Gatekey budget alert: team '{event.team_name}' has crossed "
        f"{event.threshold_pct}% of its budget ceiling "
        f"({event.current_spend_usd:.2f} of {event.budget_ceiling_usd:.2f} "
        f"{event.currency} spent)."
    )


def build_webhook_payload(event: ThresholdAlertEvent, url: str) -> dict[str, Any]:
    """Slack-compatible `{"text": ...}` for hooks.slack.com targets, generic
    structured JSON otherwise. Decimals serialize as strings (no float
    precision loss), matching the audit trail's convention. Recipients are
    deliberately not included in the webhook body (channel-level broadcast,
    not a per-person notification)."""
    if is_slack_webhook(url):
        return {"text": _human_summary(event)}
    return {
        "event": "budget_threshold_crossed",
        "team_id": str(event.team_id),
        "team_name": event.team_name,
        "threshold_pct": event.threshold_pct,
        "current_spend_usd": str(event.current_spend_usd),
        "budget_ceiling_usd": str(event.budget_ceiling_usd),
        "currency": event.currency,
        "message": _human_summary(event),
    }


class WebhookNotifier:
    """POSTs the event to the team's (decrypted) webhook URL via the shared
    pooled `httpx.AsyncClient` on `app.state` - no per-call client setup.
    The URL is held in memory only for the lifetime of one delivery and is
    never logged, on success or failure."""

    def __init__(self, url: str, http_client: httpx.AsyncClient) -> None:
        self._url = url
        self._http_client = http_client

    async def send(self, event: ThresholdAlertEvent) -> None:
        response = await self._http_client.post(
            self._url, json=build_webhook_payload(event, self._url), timeout=10.0
        )
        response.raise_for_status()


class EmailNotifier:
    """SMTP delivery via stdlib `smtplib`, run in a worker thread
    (`asyncio.to_thread` - smtplib is blocking). Config from
    `GATEKEY_SMTP_*` settings; a no-op unless `settings.smtp_enabled()`,
    regardless of any team's `email_alert_enabled` toggle. Recipients
    without an email address are skipped.

    UNVERIFIED-LIVE (A8-equivalent): implemented and spec-compliant, but no
    real SMTP credentials exist in this build environment - QA must not mark
    this verified without a real mailbox test.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _send_sync(self, event: ThresholdAlertEvent, to_addresses: list[str]) -> None:
        settings = self._settings
        message = EmailMessage()
        message["Subject"] = (
            f"Gatekey budget alert: '{event.team_name}' crossed "
            f"{event.threshold_pct}% of its budget"
        )
        message["From"] = settings.GATEKEY_SMTP_FROM_ADDRESS
        message["To"] = ", ".join(to_addresses)
        message.set_content(_human_summary(event))
        assert settings.GATEKEY_SMTP_HOST is not None  # smtp_enabled() checked
        with smtplib.SMTP(settings.GATEKEY_SMTP_HOST, settings.GATEKEY_SMTP_PORT) as smtp:
            if settings.GATEKEY_SMTP_USE_TLS:
                smtp.starttls()
            if settings.GATEKEY_SMTP_USERNAME and settings.GATEKEY_SMTP_PASSWORD:
                smtp.login(settings.GATEKEY_SMTP_USERNAME, settings.GATEKEY_SMTP_PASSWORD)
            smtp.send_message(message)

    async def send(self, event: ThresholdAlertEvent) -> None:
        if not self._settings.smtp_enabled():
            logger.info("threshold_alert_email_skipped_smtp_unconfigured")
            return
        to_addresses = [r.email for r in event.recipients if r.email]
        if not to_addresses:
            logger.info(
                "threshold_alert_email_skipped_no_recipients",
                extra={"team_id": str(event.team_id)},
            )
            return
        await asyncio.to_thread(self._send_sync, event, to_addresses)


class NotifierDispatcher:
    """Fans one event out to every enabled channel. Each channel's failure
    is caught and logged independently - one channel failing never blocks or
    masks another (design doc section 6)."""

    def __init__(self, notifiers: list[Notifier]) -> None:
        self._notifiers = notifiers

    async def dispatch(self, event: ThresholdAlertEvent) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.send(event)
            except Exception:
                # Never re-raised, and never logs the webhook URL/SMTP
                # credentials - channel + team id only.
                logger.error(
                    "threshold_alert_delivery_failed",
                    exc_info=True,
                    extra={
                        "channel": type(notifier).__name__,
                        "team_id": str(event.team_id),
                        "threshold_pct": event.threshold_pct,
                    },
                )


# --- charge-path wiring ------------------------------------------------------


def schedule_threshold_alerts(
    background_tasks: "BackgroundTasks",
    app: "FastAPI",
    *,
    team_id: uuid.UUID,
    charge: "ChargeResult",
) -> None:
    """The gateway charge path's single hook (called from
    `api.v1.gateway.common.record_usage_charge`): pure-arithmetic crossing
    check on the RETURNING values, then schedules delivery only when a
    threshold was actually crossed AND at least one channel is enabled -
    the common case adds zero work beyond this function call."""
    if charge.team_old_total is None or charge.team_new_total is None:
        return
    crossed = crossed_thresholds(
        old_total=charge.team_old_total,
        new_total=charge.team_new_total,
        ceiling=charge.team_ceiling_usd,
        alert_80_enabled=charge.team_alert_80_enabled,
        alert_100_enabled=charge.team_alert_100_enabled,
    )
    if not crossed:
        return
    if not (charge.team_webhook_alert_enabled or charge.team_email_alert_enabled):
        return
    background_tasks.add_task(
        deliver_threshold_alerts,
        app,
        team_id=team_id,
        threshold_pcts=crossed,
        current_spend_usd=charge.team_new_total,
    )


async def _load_recipients(session: "AsyncSession", team_id: uuid.UUID) -> list[NotifyRecipient]:
    """Name+email of every ACTIVE team_lead of the team plus every
    org_admin. `removed_at IS NULL` (added by `0049`) - a removed
    team_lead must stop receiving this team's budget alerts."""
    stmt = (
        select(User.name, User.sso_email)
        .where(
            or_(
                User.org_role == UserOrgRole.ORG_ADMIN,
                User.id.in_(
                    select(TeamMembership.user_id).where(
                        TeamMembership.team_id == team_id,
                        TeamMembership.role == TeamRole.TEAM_LEAD,
                        TeamMembership.removed_at.is_(None),
                    )
                ),
            )
        )
        .distinct()
    )
    return [
        NotifyRecipient(name=row.name, email=row.sso_email)
        for row in (await session.execute(stmt)).all()
    ]


async def deliver_threshold_alerts(
    app: "FastAPI",
    *,
    team_id: uuid.UUID,
    threshold_pcts: list[int],
    current_spend_usd: Decimal,
) -> None:
    """BackgroundTasks entry point - runs after the gateway response is on
    the wire, on a fresh DB session (the request's session is closed by
    then). Loads the team's live alert config/webhook envelope + recipients,
    builds one event per crossed threshold, dispatches. Never raises."""
    try:
        settings: Settings = app.state.settings
        async with app.state.db_session_factory() as session:
            team = (
                await session.execute(select(Team).where(Team.id == team_id))
            ).scalar_one_or_none()
            if team is None or team.budget_ceiling_usd is None:
                return
            org = await get_effective_org_settings(session)
            recipients = await _load_recipients(session, team_id)

            notifiers: list[Notifier] = []
            if team.webhook_alert_enabled and team.webhook_ciphertext is not None:
                try:
                    url = decrypt_secret(
                        team.webhook_ciphertext,
                        nonce=team.webhook_nonce,
                        auth_tag=team.webhook_auth_tag,
                        aad=team_webhook_aad(team.id),
                        key_provider=EnvKeyProvider.from_settings(settings),
                    ).decode("utf-8")
                    notifiers.append(WebhookNotifier(url, app.state.provider_http_client))
                except Exception:
                    logger.error(
                        "threshold_alert_webhook_decrypt_failed",
                        extra={"team_id": str(team_id)},
                    )
            if team.email_alert_enabled and settings.smtp_enabled():
                notifiers.append(EmailNotifier(settings))
            if not notifiers:
                return

            dispatcher = NotifierDispatcher(notifiers)
            for pct in threshold_pcts:
                await dispatcher.dispatch(
                    ThresholdAlertEvent(
                        team_id=team_id,
                        team_name=team.name,
                        threshold_pct=pct,  # type: ignore[arg-type]
                        current_spend_usd=current_spend_usd,
                        budget_ceiling_usd=team.budget_ceiling_usd,
                        currency=org.currency,
                        recipients=recipients,
                    )
                )
    except Exception:
        # Absolute backstop - a notifier bug must never surface anywhere
        # near the request lifecycle.
        logger.error(
            "threshold_alert_dispatch_failed", exc_info=True, extra={"team_id": str(team_id)}
        )


# ---------------------------------------------------------------------------
# Org-wide safeguard alerts (added alongside migration `0045`) - "one minor
# typo in team budget can cost an org millions." Mirrors every structure
# above ONE LEVEL UP (org instead of team); kept as separate classes/
# functions rather than generalizing the team ones over a shared type
# parameter - same rationale `services.rotation`'s module docstring gives
# for its own parallel notifier pair: the *pattern* is reused unchanged,
# the concrete event shape isn't (no team_id/team_name here).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrgThresholdAlertEvent:
    threshold_pct: Literal[50, 75, 100]
    current_spend_usd: Decimal
    budget_ceiling_usd: Decimal
    currency: str
    recipients: list[NotifyRecipient]


class OrgNotifier(Protocol):
    async def send(self, event: OrgThresholdAlertEvent) -> None: ...


def _org_human_summary(event: OrgThresholdAlertEvent) -> str:
    return (
        f"Gatekey org-wide budget alert: the organization has crossed "
        f"{event.threshold_pct}% of its budget ceiling "
        f"({event.current_spend_usd:.2f} of {event.budget_ceiling_usd:.2f} "
        f"{event.currency} spent)."
    )


def build_org_webhook_payload(event: OrgThresholdAlertEvent, url: str) -> dict[str, Any]:
    if is_slack_webhook(url):
        return {"text": _org_human_summary(event)}
    return {
        "event": "org_budget_threshold_crossed",
        "threshold_pct": event.threshold_pct,
        "current_spend_usd": str(event.current_spend_usd),
        "budget_ceiling_usd": str(event.budget_ceiling_usd),
        "currency": event.currency,
        "message": _org_human_summary(event),
    }


class OrgWebhookNotifier:
    """Same shape as `WebhookNotifier` above, org-scoped."""

    def __init__(self, url: str, http_client: httpx.AsyncClient) -> None:
        self._url = url
        self._http_client = http_client

    async def send(self, event: OrgThresholdAlertEvent) -> None:
        response = await self._http_client.post(
            self._url, json=build_org_webhook_payload(event, self._url), timeout=10.0
        )
        response.raise_for_status()


class OrgEmailNotifier:
    """Same shape as `EmailNotifier` above, org-scoped. Same UNVERIFIED-LIVE
    caveat - no real SMTP credentials in this build environment."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _send_sync(self, event: OrgThresholdAlertEvent, to_addresses: list[str]) -> None:
        settings = self._settings
        message = EmailMessage()
        message["Subject"] = (
            f"Gatekey org-wide budget alert: crossed {event.threshold_pct}% of budget"
        )
        message["From"] = settings.GATEKEY_SMTP_FROM_ADDRESS
        message["To"] = ", ".join(to_addresses)
        message.set_content(_org_human_summary(event))
        assert settings.GATEKEY_SMTP_HOST is not None  # smtp_enabled() checked
        with smtplib.SMTP(settings.GATEKEY_SMTP_HOST, settings.GATEKEY_SMTP_PORT) as smtp:
            if settings.GATEKEY_SMTP_USE_TLS:
                smtp.starttls()
            if settings.GATEKEY_SMTP_USERNAME and settings.GATEKEY_SMTP_PASSWORD:
                smtp.login(settings.GATEKEY_SMTP_USERNAME, settings.GATEKEY_SMTP_PASSWORD)
            smtp.send_message(message)

    async def send(self, event: OrgThresholdAlertEvent) -> None:
        if not self._settings.smtp_enabled():
            logger.info("org_threshold_alert_email_skipped_smtp_unconfigured")
            return
        to_addresses = [r.email for r in event.recipients if r.email]
        if not to_addresses:
            logger.info("org_threshold_alert_email_skipped_no_recipients")
            return
        await asyncio.to_thread(self._send_sync, event, to_addresses)


class OrgNotifierDispatcher:
    """Same shape as `NotifierDispatcher` above, org-scoped."""

    def __init__(self, notifiers: list[OrgNotifier]) -> None:
        self._notifiers = notifiers

    async def dispatch(self, event: OrgThresholdAlertEvent) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.send(event)
            except Exception:
                logger.error(
                    "org_threshold_alert_delivery_failed",
                    exc_info=True,
                    extra={
                        "channel": type(notifier).__name__,
                        "threshold_pct": event.threshold_pct,
                    },
                )


async def _load_org_recipients(session: "AsyncSession") -> list[NotifyRecipient]:
    """Name+email of every org_admin (no team_lead concept at org level),
    PLUS the dedicated `alert_recipient_email` (added by `0048`) if one is
    registered - deliberately a SEPARATE, additive recipient, not a
    replacement: the registered address is meant to survive any single
    admin's SSO identity changing. Deduplicated by email so a registered
    address that happens to match an org_admin's own SSO email doesn't
    receive two copies of the same alert."""
    stmt = select(User.name, User.sso_email).where(User.org_role == UserOrgRole.ORG_ADMIN)
    recipients = [
        NotifyRecipient(name=row.name, email=row.sso_email)
        for row in (await session.execute(stmt)).all()
    ]
    org = await get_effective_org_settings(session)
    if org.alert_recipient_email is not None and not any(
        r.email == org.alert_recipient_email for r in recipients
    ):
        recipients.append(
            NotifyRecipient(name="Org alert recipient", email=org.alert_recipient_email)
        )
    return recipients


def schedule_org_threshold_alerts(
    background_tasks: "BackgroundTasks",
    app: "FastAPI",
    *,
    charge: "OrgChargeResult",
) -> None:
    """The gateway charge path's org-level hook (called from
    `api.v1.gateway.common.record_usage_charge`, unconditionally, after
    the per-user/per-team charge above it) - same "pure arithmetic first,
    schedule delivery only if something actually crossed" shape as
    `schedule_threshold_alerts`."""
    crossed = crossed_thresholds(
        old_total=charge.old_total,
        new_total=charge.new_total,
        ceiling=charge.ceiling_usd,
        thresholds=_ORG_THRESHOLD_PCTS,
        alert_50_enabled=charge.alert_50_enabled,
        alert_75_enabled=charge.alert_75_enabled,
        alert_100_enabled=charge.alert_100_enabled,
    )
    if not crossed:
        return
    if not (charge.webhook_alert_enabled or charge.email_alert_enabled):
        return
    background_tasks.add_task(
        deliver_org_threshold_alerts,
        app,
        threshold_pcts=crossed,
        current_spend_usd=charge.new_total,
    )


async def deliver_org_threshold_alerts(
    app: "FastAPI",
    *,
    threshold_pcts: list[int],
    current_spend_usd: Decimal,
) -> None:
    """Org-scoped counterpart of `deliver_threshold_alerts` - BackgroundTasks
    entry point, runs after the gateway response is on the wire, on a fresh
    DB session. Never raises."""
    try:
        settings: Settings = app.state.settings
        async with app.state.db_session_factory() as session:
            row = (
                await session.execute(
                    select(OrgSettings).where(OrgSettings.org_id == DEFAULT_ORG_ID)
                )
            ).scalar_one_or_none()
            if row is None or row.budget_ceiling_usd is None:
                return
            org = await get_effective_org_settings(session)
            recipients = await _load_org_recipients(session)

            org_notifiers: list[OrgNotifier] = []
            if row.webhook_alert_enabled and row.webhook_ciphertext is not None:
                try:
                    url = decrypt_secret(
                        row.webhook_ciphertext,
                        nonce=row.webhook_nonce,
                        auth_tag=row.webhook_auth_tag,
                        aad=org_webhook_aad(row.org_id),
                        key_provider=EnvKeyProvider.from_settings(settings),
                    ).decode("utf-8")
                    org_notifiers.append(OrgWebhookNotifier(url, app.state.provider_http_client))
                except Exception:
                    logger.error("org_threshold_alert_webhook_decrypt_failed", exc_info=True)
            if row.email_alert_enabled and settings.smtp_enabled():
                org_notifiers.append(OrgEmailNotifier(settings))
            if not org_notifiers:
                return

            dispatcher = OrgNotifierDispatcher(org_notifiers)
            for pct in threshold_pcts:
                await dispatcher.dispatch(
                    OrgThresholdAlertEvent(
                        threshold_pct=pct,  # type: ignore[arg-type]
                        current_spend_usd=current_spend_usd,
                        budget_ceiling_usd=row.budget_ceiling_usd,
                        currency=org.currency,
                        recipients=recipients,
                    )
                )
    except Exception:
        logger.error("org_threshold_alert_dispatch_failed", exc_info=True)
