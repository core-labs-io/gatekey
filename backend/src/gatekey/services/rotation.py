"""Credential-rotation timing + notification orchestration (Phase 3, BD-12).

See `docs/design/phase-3-security-compliance-design.md` sections 4.1-4.5 and
the product spec's §7 (AC7.1-AC7.9). The actual dual-secret mutations
(`rotate_service_account_key`/`rotate_provider_key`) live on the modules
that already own each credential's CRUD (`services.service_accounts`/
`services.provider_keys`) - this module owns:

- `compute_next_rotation` (AC7.3's off-hours timing resolution), a pure,
  DB-free function - see its own docstring for the deliberate simplification
  it takes on access-schedule resolution.
- notification wiring: a `RotationEvent` payload fanned out through a small
  notifier pair that mirrors `services.notifiers`'s `Notifier`/
  `NotifierDispatcher` shape exactly (same Protocol contract, same
  try/log-per-channel dispatch loop) - kept as separate classes rather than
  literally reusing `NotifierDispatcher` because that class's `Notifier`
  Protocol is typed to `ThresholdAlertEvent` specifically (a different event
  shape); the *pattern* is reused unchanged, the concrete types aren't.

AC7.1 (locked): `service_account` scope is always fully automatic
(`rotate_service_account_key`, called from the scheduler loop with zero
admin action); `provider_key` scope is always guided/manual
(`rotate_provider_key`, called from an admin-initiated request after live
validation) - this module never auto-fires a provider-key rotation.

Known gap, flagged rather than silently built around (AC7.5's "deliver via
one-time-reveal, surfaced on next view"): this codebase's non-negotiable is
"no plaintext secret at rest, ever" (see `ServiceAccountKey`'s module
docstring), and the design doc does not specify a mechanism for surfacing a
freshly auto-rotated secret's plaintext to an admin who wasn't present at
rotation time (there is no admin-facing request/response to return it
through, unlike the manual "Rotate now" path). Building a transient,
process-memory plaintext cache for this would be a real, unreviewed security
decision (multi-worker cache coherency, TTL, exposure surface) - not
invented here. Automatic rotation therefore mints and notifies but does NOT
attempt to make the new plaintext retrievable after the fact; flagged back
to the architect (see this task's handoff notes) rather than shipped
silently short of the AC.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Protocol
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select

from gatekey.db.models.team import Team
from gatekey.db.models.user import User

if TYPE_CHECKING:
    from fastapi import FastAPI

    from gatekey.config import Settings

logger = logging.getLogger("gatekey")

# UI default (AC7.4) - never long/multi-day by default.
DEFAULT_OVERLAP_BUFFER_MINUTES = 5
# AC7.3's org-wide off-hours fallback.
DEFAULT_ORG_OFF_HOURS_LOCAL_TIME = time(2, 0)


# --- AC7.3 off-hours timing resolution (pure, DB-free) -----------------------


@dataclass(frozen=True)
class AccessScheduleWindow:
    """The minimal `AccessSchedule` fields `compute_next_rotation` needs -
    a plain dataclass (not the ORM row) so this stays a pure function,
    testable with zero DB. `allowed_days`: ISO weekday ints 1(Mon)-7(Sun),
    matching `AccessSchedule.allowed_days`'s convention."""

    enabled: bool
    allowed_days: tuple[int, ...]
    allowed_hours_start: time | None
    allowed_hours_end: time | None


def compute_next_rotation(
    *,
    now: datetime,
    interval_days: int,
    rotate_at_local_time: time | None,
    org_off_hours_default: time = DEFAULT_ORG_OFF_HOURS_LOCAL_TIME,
    timezone_name: str = "UTC",
    access_schedule: AccessScheduleWindow | None = None,
) -> datetime:
    """AC7.3: the next rotation instant, `interval_days` from `now`, anchored
    to an off-hours moment - never a blanket cron time shared by every key.

    (a) If `access_schedule` is given and enabled: anchors to the END of its
        allowed-hours window (`allowed_hours_end`, falling back to the org
        default if the schedule has no hour bound) - the first moment
        outside the key's own allowed window on the target calendar date.
    (b) Otherwise: anchors to `rotate_at_local_time` if the policy set one,
        else `org_off_hours_default` (spec default 02:00 org-local).

    Both branches resolve in `timezone_name` (`compliance_settings.
    access_schedule_timezone` - Phase 3's one shared timezone setting,
    reused here per design doc section 4.4) via stdlib `zoneinfo`, then
    convert to UTC.

    This function itself resolves only the ONE `access_schedule` window
    passed in - it does not itself walk the org->team->key precedence
    chain. `services.scheduler._resolve_access_schedule_window` (BD-16)
    now supplies the real, resolved effective schedule for the claimed key
    via `services.access_schedules.resolve_effective_schedule` before
    calling this function; a caller with no schedule concept at all can
    still pass `access_schedule=None`, which safely falls back to branch
    (b).
    """
    tz = ZoneInfo(timezone_name)
    local_now = now.astimezone(tz)
    target_date: date = (local_now + timedelta(days=interval_days)).date()

    if access_schedule is not None and access_schedule.enabled:
        anchor_time = access_schedule.allowed_hours_end or org_off_hours_default
    else:
        anchor_time = rotate_at_local_time or org_off_hours_default

    local_target = datetime.combine(target_date, anchor_time, tzinfo=tz)
    return local_target.astimezone(timezone.utc)


# --- AC7.5/AC7.9 notification wiring ------------------------------------------


@dataclass(frozen=True)
class RotationRecipient:
    name: str
    email: str | None


@dataclass(frozen=True)
class RotationEvent:
    """Design doc section 4.5's payload shape - deliberately carries NO
    secret material (never the new/old plaintext, never a hash) - see
    module docstring's AC7.5 gap note for why the plaintext isn't threaded
    anywhere near this event."""

    key_name: str
    rotated_at: datetime
    overlap_expires_at: datetime
    recipients: list[RotationRecipient]


class RotationNotifier(Protocol):
    async def send(self, event: RotationEvent) -> None: ...


def _rotation_human_summary(event: RotationEvent) -> str:
    return (
        f"Gatekey credential rotation: '{event.key_name}' was rotated at "
        f"{event.rotated_at.isoformat()}. The previous secret remains valid "
        f"until {event.overlap_expires_at.isoformat()}."
    )


class RotationWebhookNotifier:
    """Same shape as `services.notifiers.WebhookNotifier` - POSTs to a
    pre-resolved URL via the shared pooled `httpx.AsyncClient`, never logs
    the URL."""

    def __init__(self, url: str, http_client: httpx.AsyncClient) -> None:
        self._url = url
        self._http_client = http_client

    async def send(self, event: RotationEvent) -> None:
        payload = {
            "event": "credential_rotated",
            "key_name": event.key_name,
            "rotated_at": event.rotated_at.isoformat(),
            "overlap_expires_at": event.overlap_expires_at.isoformat(),
            "message": _rotation_human_summary(event),
        }
        response = await self._http_client.post(self._url, json=payload, timeout=10.0)
        response.raise_for_status()


class RotationEmailNotifier:
    """UNVERIFIED-LIVE (AC7.9, same caveat class as `services.notifiers.
    EmailNotifier`) - same SMTP delivery mechanics as that class, mirrored
    here (not called directly) since `EmailNotifier.send()` is typed to
    `ThresholdAlertEvent`'s fields, a different event shape (see module
    docstring)."""

    def __init__(self, settings: "Settings") -> None:
        self._settings = settings

    async def send(self, event: RotationEvent) -> None:
        import asyncio
        import smtplib
        from email.message import EmailMessage

        if not self._settings.smtp_enabled():
            logger.info("rotation_email_skipped_smtp_unconfigured")
            return
        to_addresses = [r.email for r in event.recipients if r.email]
        if not to_addresses:
            logger.info("rotation_email_skipped_no_recipients", extra={"key_name": event.key_name})
            return

        def _send_sync() -> None:
            settings = self._settings
            # Guaranteed by the `smtp_enabled()` check above - mypy can't
            # narrow across the `asyncio.to_thread` closure boundary into
            # this nested function (same as `shadow_ai.EmailNotifier.send`).
            assert settings.GATEKEY_SMTP_HOST is not None
            message = EmailMessage()
            message["Subject"] = f"Gatekey: '{event.key_name}' credential rotated"
            message["From"] = settings.GATEKEY_SMTP_FROM_ADDRESS
            message["To"] = ", ".join(to_addresses)
            message.set_content(_rotation_human_summary(event))
            with smtplib.SMTP(settings.GATEKEY_SMTP_HOST, settings.GATEKEY_SMTP_PORT) as smtp:
                if settings.GATEKEY_SMTP_USE_TLS:
                    smtp.starttls()
                if settings.GATEKEY_SMTP_USERNAME and settings.GATEKEY_SMTP_PASSWORD:
                    smtp.login(settings.GATEKEY_SMTP_USERNAME, settings.GATEKEY_SMTP_PASSWORD)
                smtp.send_message(message)

        await asyncio.to_thread(_send_sync)


class RotationNotifierDispatcher:
    """Structurally identical to `services.notifiers.NotifierDispatcher` -
    fans one event out to every enabled channel, isolates/logs each
    channel's failure independently, never raises."""

    def __init__(self, notifiers: list[RotationNotifier]) -> None:
        self._notifiers = notifiers

    async def dispatch(self, event: RotationEvent) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.send(event)
            except Exception:
                logger.error(
                    "rotation_notification_delivery_failed",
                    exc_info=True,
                    extra={"channel": type(notifier).__name__, "key_name": event.key_name},
                )


async def deliver_service_account_rotation_notification(
    app: "FastAPI",
    *,
    service_account_id: uuid.UUID,
    key_name: str,
    rotated_at: datetime,
    overlap_expires_at: datetime,
) -> None:
    """BackgroundTasks entry point (manual "Rotate now") / direct-await
    entry point (the scheduler loop's own async context - see
    `services.scheduler.run_due_rotations`'s docstring for why a literal
    `BackgroundTasks` object doesn't apply there, there is no HTTP response
    to run after). Recipients: the key's owning user (AC7.5's "As a key
    owner, I'm notified"). Webhook: reuses the key's team's existing
    threshold-alert webhook config if the key is team-attributed (no
    per-key or org-level webhook config exists this phase - see module-level
    ponytail note above `RotationWebhookNotifier` if a dedicated channel is
    ever needed). Never raises."""
    from gatekey.db.models.service_account_key import ServiceAccountKey
    from gatekey.services.encryption import EnvKeyProvider, decrypt_secret
    from gatekey.services.teams import team_webhook_aad

    try:
        settings = app.state.settings
        async with app.state.db_session_factory() as session:
            key = (
                await session.execute(
                    select(ServiceAccountKey).where(ServiceAccountKey.id == service_account_id)
                )
            ).scalar_one_or_none()
            if key is None:
                return
            owner = (await session.execute(select(User).where(User.id == key.user_id))).scalar_one_or_none()
            recipients = [RotationRecipient(name=owner.name, email=owner.sso_email)] if owner else []

            notifiers: list[RotationNotifier] = []
            team = None
            if key.team_id is not None:
                team = (
                    await session.execute(select(Team).where(Team.id == key.team_id))
                ).scalar_one_or_none()
            if (
                team is not None
                and team.webhook_alert_enabled
                and team.webhook_ciphertext is not None
            ):
                try:
                    url = decrypt_secret(
                        team.webhook_ciphertext,
                        nonce=team.webhook_nonce,
                        auth_tag=team.webhook_auth_tag,
                        aad=team_webhook_aad(team.id),
                        key_provider=EnvKeyProvider.from_settings(settings),
                    ).decode("utf-8")
                    notifiers.append(RotationWebhookNotifier(url, app.state.provider_http_client))
                except Exception:
                    logger.error(
                        "rotation_webhook_decrypt_failed", extra={"service_account_id": str(service_account_id)}
                    )
            if team is not None and team.email_alert_enabled and settings.smtp_enabled():
                notifiers.append(RotationEmailNotifier(settings))
            elif team is None and settings.smtp_enabled():
                # Legacy/unattributed key - still notify the owner by email
                # if SMTP is configured at all (no team-level toggle to gate on).
                notifiers.append(RotationEmailNotifier(settings))
            if not notifiers or not recipients:
                return

            event = RotationEvent(
                key_name=key_name,
                rotated_at=rotated_at,
                overlap_expires_at=overlap_expires_at,
                recipients=recipients,
            )
            await RotationNotifierDispatcher(notifiers).dispatch(event)
    except Exception:
        logger.error(
            "rotation_notification_dispatch_failed",
            exc_info=True,
            extra={"service_account_id": str(service_account_id)},
        )


async def deliver_provider_key_rotation_notification(
    app: "FastAPI",
    *,
    provider: str,
    rotated_at: datetime,
    overlap_expires_at: datetime,
) -> None:
    """Provider-key rotation is org-wide (no team scope), so recipients are
    every org_admin (same recipient pool `services.notifiers._load_
    recipients` uses for org_admins). No org-level webhook config exists
    this phase - email only (ponytail: add an org-wide webhook config if a
    real need surfaces; not built speculatively now)."""
    from gatekey.db.models.user import UserOrgRole

    try:
        settings = app.state.settings
        if not settings.smtp_enabled():
            return
        async with app.state.db_session_factory() as session:
            admins = (
                await session.execute(
                    select(User.name, User.sso_email).where(User.org_role == UserOrgRole.ORG_ADMIN)
                )
            ).all()
            recipients = [RotationRecipient(name=row.name, email=row.sso_email) for row in admins]
            if not recipients:
                return
            event = RotationEvent(
                key_name=f"provider key ({provider})",
                rotated_at=rotated_at,
                overlap_expires_at=overlap_expires_at,
                recipients=recipients,
            )
            await RotationNotifierDispatcher([RotationEmailNotifier(settings)]).dispatch(event)
    except Exception:
        logger.error(
            "provider_key_rotation_notification_dispatch_failed", exc_info=True, extra={"provider": provider}
        )
