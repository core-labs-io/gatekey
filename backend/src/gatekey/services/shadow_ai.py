"""Shadow AI Discovery: ingestion, curated-hostname allowlist CRUD,
config/token issuance, the aggregated report, the "repeat violator" derived
flag, retention purge, and opt-in notification/webhook enforcement delivery
(Phase 5 - Differentiators, 5.1 Shadow AI Discovery).

See `gatekey/phase-5-product-spec.md` section 5 (AC5.1.x) and
`gatekey/phase-5-technical-design.md` sections 2.5/4.2/5's wiring checklist
("5.5 (Shadow AI, 5.1)") for the full design rationale this module
implements. The three ORM models this module owns
(`db.models.shadow_ai_ingest_event.ShadowAiIngestEvent`,
`db.models.known_ai_tool_hostname.KnownAiToolHostname`,
`db.models.shadow_ai_ingest_config.ShadowAiIngestConfig`) already exist -
see each model's own docstring for the schema-level rationale (migration
`0042`).

Data-minimization gate (AC5.1.1, judgment call #17)
-----------------------------------------------------
`ingest_events()` below is the ONLY write path into `shadow_ai_ingest_events`.
An event whose `destination_host` does not match an `enabled = true` row in
`known_ai_tool_hostnames` is dropped in-memory and never persisted, never
logged with any identifying content (not even at DEBUG level) - see that
function's docstring for the exact mechanics. This bounds the table's
privacy/retention exposure by construction, not by a downstream filter.

Ingest-token trust boundary (AC5.1.3, design doc section 2.5 "Key Decision")
-------------------------------------------------------------------------------
`ingest_token_hash` is a SHA-256 digest (`services.service_accounts.
hash_secret` - the same fast-hash-not-a-slow-KDF discipline
`ScimConfig.bearer_token_hash` already establishes, see that function's
docstring for the rationale), NEVER the AES-256-GCM reversible envelope
`provider_keys`/`self_hosted_providers` use - this is an inbound-only,
verify-a-presented-token credential, never decrypted/used outbound.
`shadow_ai_ingest_token_matches()` mirrors `services.scim.
scim_token_matches()`'s constant-time-compare shape exactly, including its
"NULL/absent config never matches" fail-closed default (AC5.1.4). See
`api.deps.require_shadow_ai_ingest_token` for the FastAPI dependency that
consumes this, and that dependency's own docstring for the explicit
non-overlap proof with every other auth boundary in this codebase.

Enforcement (AC5.1.7)
-----------------------
`shadow_ai_ingest_config.enforcement_mode` is a single three-way column
(`'detect_only' | 'notification' | 'webhook'`, migration `0042`'s
CHECK constraint) - NOT two independently-toggleable booleans. An org picks
one mode at a time: detection-only (default), email notification, or an
outbound webhook callback. `ShadowAiEmailNotifier`/`ShadowAiWebhookNotifier`
below mirror `services.rotation.RotationEmailNotifier`/
`RotationWebhookNotifier`'s SMTP/HTTP delivery mechanics exactly (same
"mirrored, not shared, because the event shape differs" precedent that
module's own docstring documents for reusing `services.notifiers.
EmailNotifier`'s mechanics) - reusing this codebase's ONE existing
email-sending mechanism (stdlib `smtplib` via `asyncio.to_thread`, configured
by `Settings.GATEKEY_SMTP_*`), not building a second one.

`webhook_url` at rest (security-reviewer/QA Fix 3 - closed)
-------------------------------------------------------------------------------
`ShadowAiIngestConfig.webhook_ciphertext`/`webhook_nonce`/`webhook_auth_tag`
(migration `0043`, replacing `0042`'s original plain `webhook_url Text`
column - a self-disclosed deviation from the design doc's stated
requirement) are the byte-for-byte identical AES-256-GCM envelope shape
`Team.webhook_ciphertext`/`webhook_nonce`/`webhook_auth_tag` use, via the
SAME `services.encryption.encrypt_secret`/`decrypt_secret` helpers `services.
teams.set_team_alert_config`/`services.notifiers.deliver_threshold_alerts`
already use - no new crypto code. `shadow_ai_webhook_aad()` below mirrors
`services.teams.team_webhook_aad()`'s shape exactly, with a deliberately
distinct AAD binding (`f"shadow_ai_ingest_config:{org_id}"` vs. `team.
team_webhook_aad`'s `f"team:{team_id}"`) - same "no cross-table ciphertext
reuse" rationale `services.self_hosted_providers`' module docstring
documents for its own distinct AAD binding. The plaintext URL is never
returned by any read path - `api/v1/admin/shadow_ai.py`'s `GET`/`PUT`
responses expose only `webhook_configured: bool` (`schemas.shadow_ai.
ShadowAiConfigResponse`), matching `Team.webhook_configured`'s identical
"never echo the secret-equivalent URL back" discipline.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.known_ai_tool_hostname import KnownAiToolHostname
from gatekey.db.models.shadow_ai_ingest_config import ShadowAiIngestConfig
from gatekey.db.models.shadow_ai_ingest_event import ShadowAiIngestEvent
from gatekey.db.models.team_membership import TeamMembership, TeamRole
from gatekey.db.models.user import User
from gatekey.errors import (
    GatekeyError,
    NotFoundError,
    ShadowAiEnforcementConfirmationRequiredError,
    ShadowAiWebhookUrlRequiredError,
)
from gatekey.services.encryption import EnvKeyProvider, KeyProvider, decrypt_secret, encrypt_secret
from gatekey.services.service_accounts import hash_secret

if TYPE_CHECKING:
    import httpx
    from fastapi import BackgroundTasks, FastAPI

    from gatekey.config import Settings

logger = logging.getLogger("gatekey")

# ---------------------------------------------------------------------------
# Ingest-token issuance/verification (AC5.1.3/AC5.1.4).
# ---------------------------------------------------------------------------

SHADOW_AI_INGEST_TOKEN_PREFIX = "gk_sai_"
_TOKEN_ENTROPY_BYTES = 32

# Trailing window AC5.1.8's "repeat violator" flag evaluates over - fixed,
# not admin-configurable (the phase spec states "trailing 7 days" as a plain
# constant, not a config knob).
_REPEAT_VIOLATOR_WINDOW_DAYS = 7
_REPEAT_VIOLATOR_THRESHOLD = 3

# Default report window when the caller supplies neither `since` nor `until`
# (AC5.1.5 doesn't specify a default) - chosen to match the retention
# default (§ AC5.1.10) so an unfiltered report call shows "everything
# Gatekey currently retains" rather than an arbitrary shorter slice.
_DEFAULT_REPORT_WINDOW_DAYS = 90


async def get_shadow_ai_ingest_config(
    session: AsyncSession, org_id: uuid.UUID = DEFAULT_ORG_ID
) -> ShadowAiIngestConfig | None:
    return (
        await session.execute(
            select(ShadowAiIngestConfig).where(ShadowAiIngestConfig.org_id == org_id)
        )
    ).scalar_one_or_none()


def shadow_ai_webhook_aad(org_id: uuid.UUID) -> bytes:
    """AAD binding this org's Shadow AI webhook-URL ciphertext to its
    `org_id` - mirrors `services.teams.team_webhook_aad()`'s shape exactly,
    with a deliberately distinct binding (see module docstring "webhook_url
    at rest"). Shared between `set_shadow_ai_config`'s encrypt path and
    `deliver_shadow_ai_enforcement`'s decrypt path - both sides must build
    AAD through this one function."""
    return f"shadow_ai_ingest_config:{org_id}".encode("utf-8")


def shadow_ai_webhook_configured(config: ShadowAiIngestConfig | None) -> bool:
    return config is not None and config.webhook_ciphertext is not None


def shadow_ai_ingest_token_matches(
    config: ShadowAiIngestConfig | None, submitted_token: str
) -> bool:
    """Constant-time bearer-token check - mirrors `services.scim.
    scim_token_matches()` exactly (see that function's docstring for the
    full rationale, including why a single per-org config row is fetched by
    the caller rather than a `WHERE ingest_token_hash = :hash` lookup).

    A missing config row, OR one with `ingest_token_hash IS NULL` (no token
    ever generated), never matches - this is the mechanism that enforces
    AC5.1.4's "the ingestion endpoint rejects all requests until an Org
    Admin completes setup" fail-closed requirement. Same generic `False`
    either way, so a probing caller cannot distinguish "not set up" from
    "wrong token".
    """
    if config is None or config.ingest_token_hash is None:
        return False
    return hmac.compare_digest(hash_secret(submitted_token), config.ingest_token_hash)


async def rotate_shadow_ai_ingest_token(
    session: AsyncSession, org_id: uuid.UUID = DEFAULT_ORG_ID
) -> tuple[ShadowAiIngestConfig, str]:
    """Mint a fresh ingest bearer token, overwriting any prior one in place -
    same "no overlap window" shape as `services.scim.rotate_scim_token`
    (an inbound credential the ingesting SASE/proxy tool holds, not one of
    the scheduled outbound rotations `services.rotation` manages). Returns
    `(row, plaintext_token)` - the plaintext exists only in this return
    value, never persisted (one-time-reveal, same discipline as
    `ScimTokenRotateResponse`/`ServiceAccountKeyCreateResponse`). Commits.
    """
    token = SHADOW_AI_INGEST_TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
    token_hash = hash_secret(token)
    insert_stmt = postgresql.insert(ShadowAiIngestConfig).values(
        org_id=org_id, ingest_token_hash=token_hash, token_created_at=func.now()
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[ShadowAiIngestConfig.org_id],
        set_={
            "ingest_token_hash": insert_stmt.excluded.ingest_token_hash,
            "token_created_at": insert_stmt.excluded.token_created_at,
            "updated_at": func.now(),
        },
    ).returning(ShadowAiIngestConfig)
    row = (await session.execute(upsert_stmt)).scalar_one()
    await session.commit()
    # See `set_shadow_ai_config`'s identical `refresh()` for why this is
    # needed whenever the row may already be identity-mapped from an
    # earlier read in this same session.
    await session.refresh(row)
    return row, token


async def set_shadow_ai_config(
    session: AsyncSession,
    *,
    detection_source: str,
    enforcement_mode: str,
    webhook_url: str | None,
    shadow_ai_retention_days: int,
    confirm: bool,
    key_provider: KeyProvider,
    org_id: uuid.UUID = DEFAULT_ORG_ID,
) -> ShadowAiIngestConfig:
    """Full-replace write for the detection-source/enforcement/retention
    config (`ingest_token_hash`/`token_created_at` are untouched by this
    function - they are owned exclusively by `rotate_shadow_ai_ingest_token`,
    so calling this never clobbers an already-issued token, and calling that
    never clobbers this config - the two admin actions AC5.1.4 requires
    together for opt-in are independent writes to the same row).

    AC5.1.7's confirm-required gate: raises
    `ShadowAiEnforcementConfirmationRequiredError` (422) if the caller is
    asking to TRANSITION into an intrusive mode (`enforcement_mode` in
    `("notification", "webhook")` AND it differs from the row's current
    value, or no row exists yet) without `confirm=True`. Resubmitting the
    SAME already-active intrusive mode (e.g. changing only
    `shadow_ai_retention_days` while `enforcement_mode` stays `"webhook"`)
    does NOT require `confirm` again - only the transition itself is the
    "intrusive action" the confirm dialog guards.

    Raises `ShadowAiWebhookUrlRequiredError` (422) if `enforcement_mode ==
    "webhook"` and `webhook_url` is falsy - mirrors `services.teams.
    set_team_alert_config`'s identical guard.

    `webhook_url` is encrypted at rest (AES-256-GCM, AAD bound via
    `shadow_ai_webhook_aad`) before this full-replace write - `None` clears
    the stored envelope (all three columns `NULL`), a string always
    re-encrypts (this endpoint's contract is full-replace-every-PUT, unlike
    `services.teams.set_team_alert_config`'s partial-update `webhook_url_
    provided` flag - see `schemas.shadow_ai.ShadowAiConfigPutRequest`'s
    docstring). The plaintext is never persisted anywhere else, never
    logged, never returned.
    """
    existing = await get_shadow_ai_ingest_config(session, org_id)
    is_intrusive = enforcement_mode != "detect_only"
    is_transition = existing is None or existing.enforcement_mode != enforcement_mode
    if is_intrusive and is_transition and not confirm:
        raise ShadowAiEnforcementConfirmationRequiredError(enforcement_mode)
    if enforcement_mode == "webhook" and not webhook_url:
        raise ShadowAiWebhookUrlRequiredError()

    if webhook_url is None:
        webhook_ciphertext = webhook_nonce = webhook_auth_tag = None
    else:
        envelope = encrypt_secret(
            webhook_url.encode("utf-8"),
            aad=shadow_ai_webhook_aad(org_id),
            key_provider=key_provider,
        )
        webhook_ciphertext = envelope.ciphertext
        webhook_nonce = envelope.nonce
        webhook_auth_tag = envelope.auth_tag

    insert_stmt = postgresql.insert(ShadowAiIngestConfig).values(
        org_id=org_id,
        detection_source=detection_source,
        enforcement_mode=enforcement_mode,
        webhook_ciphertext=webhook_ciphertext,
        webhook_nonce=webhook_nonce,
        webhook_auth_tag=webhook_auth_tag,
        shadow_ai_retention_days=shadow_ai_retention_days,
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[ShadowAiIngestConfig.org_id],
        set_={
            "detection_source": insert_stmt.excluded.detection_source,
            "enforcement_mode": insert_stmt.excluded.enforcement_mode,
            "webhook_ciphertext": insert_stmt.excluded.webhook_ciphertext,
            "webhook_nonce": insert_stmt.excluded.webhook_nonce,
            "webhook_auth_tag": insert_stmt.excluded.webhook_auth_tag,
            "shadow_ai_retention_days": insert_stmt.excluded.shadow_ai_retention_days,
            "updated_at": func.now(),
        },
    ).returning(ShadowAiIngestConfig)
    row = (await session.execute(upsert_stmt)).scalar_one()
    await session.commit()
    # Defensive `refresh()` (mirrors `services.self_hosted_providers`'
    # register/edit functions' own post-commit `session.refresh(row)`): if
    # this row was already loaded into this SAME session's identity map by
    # an earlier plain SELECT in this request (e.g. the route handler's own
    # "load old_row for the audit entry" read), the ORM does not
    # automatically overwrite that already-loaded object's attributes from
    # this statement's own RETURNING clause - `refresh()` forces a fresh
    # read so the returned object's fields are never stale.
    await session.refresh(row)
    return row


# ---------------------------------------------------------------------------
# Curated hostname allowlist CRUD (AC5.1.2).
# ---------------------------------------------------------------------------


class KnownAiToolHostnameNotFoundError(NotFoundError):
    def __init__(self, hostname: str) -> None:
        super().__init__(f"No known AI tool hostname '{hostname}' is configured.")


class KnownAiToolHostnameAlreadyExistsError(GatekeyError):
    status_code = 409
    code = "known_ai_tool_hostname_already_exists"

    def __init__(self, hostname: str) -> None:
        super().__init__(f"Hostname '{hostname}' is already in the known AI tool list.")


async def list_known_ai_tool_hostnames(session: AsyncSession) -> list[KnownAiToolHostname]:
    stmt = select(KnownAiToolHostname).order_by(KnownAiToolHostname.hostname)
    return list((await session.execute(stmt)).scalars().all())


async def get_known_ai_tool_hostname(
    session: AsyncSession, hostname: str
) -> KnownAiToolHostname | None:
    return (
        await session.execute(
            select(KnownAiToolHostname).where(KnownAiToolHostname.hostname == hostname)
        )
    ).scalar_one_or_none()


async def add_known_ai_tool_hostname(
    session: AsyncSession, *, hostname: str, tool_label: str, enabled: bool = True
) -> KnownAiToolHostname:
    row = KnownAiToolHostname(hostname=hostname, tool_label=tool_label, enabled=enabled)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise KnownAiToolHostnameAlreadyExistsError(hostname) from None
    await session.refresh(row)
    return row


async def update_known_ai_tool_hostname(
    session: AsyncSession,
    hostname: str,
    *,
    tool_label: str | None = None,
    enabled: bool | None = None,
) -> KnownAiToolHostname:
    row = await get_known_ai_tool_hostname(session, hostname)
    if row is None:
        raise KnownAiToolHostnameNotFoundError(hostname)
    if tool_label is not None:
        row.tool_label = tool_label
    if enabled is not None:
        row.enabled = enabled
    await session.commit()
    await session.refresh(row)
    return row


async def remove_known_ai_tool_hostname(session: AsyncSession, hostname: str) -> bool:
    row = await get_known_ai_tool_hostname(session, hostname)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


# ---------------------------------------------------------------------------
# Ingestion (AC5.1.1/AC5.1.3) - the data-minimization gate.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowAiIngestEventInput:
    """One normalized event from an ingested batch, per AC5.1.1's schema -
    the ingesting SASE/proxy tool's own generic-contract transform (design
    doc section 2.5) produces these, NOT any vendor-native log format."""

    user_identifier: str
    destination_host: str
    occurred_at: datetime
    source: str
    raw_metadata: dict | None = None


@dataclass(frozen=True)
class ShadowAiIngestResult:
    received: int
    persisted: int
    dropped: int
    persisted_event_ids: list[uuid.UUID]


async def _load_enabled_hostnames(session: AsyncSession) -> frozenset[str]:
    stmt = select(KnownAiToolHostname.hostname).where(KnownAiToolHostname.enabled.is_(True))
    return frozenset((await session.execute(stmt)).scalars().all())


def partition_events_by_hostname_match(
    events: list[ShadowAiIngestEventInput], enabled_hostnames: frozenset[str]
) -> tuple[list[ShadowAiIngestEventInput], int]:
    """Pure, DB-free predicate implementing AC5.1.1's data-minimization gate:
    `event.destination_host` must EXACT-match a currently-`enabled = true`
    `known_ai_tool_hostnames` row - no substring/suffix/case-insensitive
    matching (a caller-controlled `destination_host` string is never treated
    as a pattern). Returns `(matched_events, dropped_count)` - `ingest_events`
    below is the only caller that turns `matched_events` into actual
    `ShadowAiIngestEvent` DB rows; this function itself touches no session,
    so it's exhaustively unit-testable in isolation from Postgres (see
    `tests/unit/test_shadow_ai_service.py`)."""
    matched = [event for event in events if event.destination_host in enabled_hostnames]
    return matched, len(events) - len(matched)


def is_repeat_violator(event_count_in_trailing_window: int) -> bool:
    """AC5.1.8's fixed threshold - `>= 3` events for one `(user, tool)` pair
    within the trailing `_REPEAT_VIOLATOR_WINDOW_DAYS`-day window. A pure
    threshold check, isolated from the SQL aggregation
    `get_shadow_ai_report` performs to produce the count in the first place,
    so the threshold's own boundary behavior (2 vs. 3 vs. 4) is
    unit-testable without a database."""
    return event_count_in_trailing_window >= _REPEAT_VIOLATOR_THRESHOLD


async def _resolve_user_id_by_email(
    session: AsyncSession, user_identifier: str, org_id: uuid.UUID
) -> uuid.UUID | None:
    """Best-effort match of the ingested `user_identifier` against a known
    Gatekey user's email (AC5.1.1/AC5.1.5) - exact-match only (same `eq`-only
    discipline `services.scim.list_scim_users`'s `userName` filter already
    uses for this codebase's one other email-correlation lookup), never a
    fuzzy/case-insensitive match that could misattribute a detection to the
    wrong person."""
    stmt = select(User.id).where(User.org_id == org_id, User.sso_email == user_identifier)
    return (await session.execute(stmt)).scalar_one_or_none()


async def ingest_events(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    events: list[ShadowAiIngestEventInput],
) -> ShadowAiIngestResult:
    """The ONLY write path into `shadow_ai_ingest_events` - see module
    docstring "Data-minimization gate".

    For each submitted event: if `destination_host` does NOT match an
    `enabled = true` `known_ai_tool_hostnames` row, the event is dropped
    in-memory right here - it is never added to the session, never flushed,
    never logged (not even a count-only debug line naming the host, since a
    hostname itself is potentially identifying of the destination even
    without the rest of the event). Only a matched event is persisted, with
    `matched_user_id` best-effort resolved against a known Gatekey user's
    email (`_resolve_user_id_by_email`, `NULL` if no match - AC5.1.5).

    Commits once at the end (a single batch is one logical ingest
    transaction - either every matched row in the batch lands, or none do,
    on a DB error). Returns a summary + the list of newly-persisted event
    ids (consumed by `schedule_shadow_ai_enforcement` to know which events,
    if any, to fire notifications/webhooks for).
    """
    enabled_hostnames = await _load_enabled_hostnames(session)
    matched_events, _dropped_count = partition_events_by_hostname_match(events, enabled_hostnames)
    persisted_ids: list[uuid.UUID] = []
    for event in matched_events:
        matched_user_id = await _resolve_user_id_by_email(
            session, event.user_identifier, org_id
        )
        row = ShadowAiIngestEvent(
            org_id=org_id,
            user_identifier=event.user_identifier,
            matched_user_id=matched_user_id,
            destination_host=event.destination_host,
            occurred_at=event.occurred_at,
            source=event.source,
            raw_metadata=event.raw_metadata,
        )
        session.add(row)
        await session.flush()
        persisted_ids.append(row.id)
    await session.commit()
    return ShadowAiIngestResult(
        received=len(events),
        persisted=len(persisted_ids),
        dropped=len(events) - len(persisted_ids),
        persisted_event_ids=persisted_ids,
    )


# ---------------------------------------------------------------------------
# Report (AC5.1.5/AC5.1.6/AC5.1.8).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowAiReportRow:
    user_identifier: str
    matched_user_id: uuid.UUID | None
    destination_host: str
    tool_label: str
    frequency_per_week: float
    last_seen: datetime
    repeat_violator: bool


async def get_shadow_ai_report(
    session: AsyncSession,
    *,
    org_id: uuid.UUID = DEFAULT_ORG_ID,
    team_ids: frozenset[uuid.UUID] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[ShadowAiReportRow]:
    """`(user, tool, frequency_per_week, last_seen)` rows grouped by
    `(user_identifier, destination_host)` (AC5.1.5), with the derived
    "repeat violator" flag (AC5.1.8) merged in per row.

    `team_ids=None` means org-wide (Org Admin/Auditor, AC5.1.6); a non-None
    set restricts to rows whose `matched_user_id` is a member of one of
    those teams (Team Lead scoping - the caller, `api/v1/admin/shadow_ai.py`,
    is responsible for resolving/validating which team ids a given caller
    may pass here, per design doc wiring checklist row 6 - this function
    itself does no RBAC, it just applies whatever filter it's given).
    Unmatched rows (`matched_user_id IS NULL`) are ALWAYS excluded once a
    `team_ids` filter is active (there is no team membership to check them
    against) - they only ever appear in the unscoped, org-wide view.

    `since`/`until` default to a trailing `_DEFAULT_REPORT_WINDOW_DAYS`-day
    window (AC5.1.5 doesn't specify a default - see module docstring).
    `frequency_per_week` is `event_count * 7 / range_days` over that same
    window - a simple normalized rate, not a rolling/weighted average.

    `repeat_violator` (AC5.1.8) is evaluated over the FIXED trailing
    `_REPEAT_VIOLATOR_WINDOW_DAYS`-day window relative to "now", independent
    of the `since`/`until` filter applied to the rest of the report - a
    prioritization signal about CURRENT behavior, not about whatever
    historical slice the caller happens to be viewing.
    """
    resolved_until = until or datetime.now(timezone.utc)
    resolved_since = since or (resolved_until - timedelta(days=_DEFAULT_REPORT_WINDOW_DAYS))
    range_days = max((resolved_until - resolved_since).total_seconds() / 86400.0, 1.0)

    def _team_scope_filter():
        # `removed_at IS NULL` (added by `0049`) - scope to CURRENT members
        # of these teams, matching a team lead's live "my team" view, not
        # anyone who has ever belonged to it.
        return ShadowAiIngestEvent.matched_user_id.in_(
            select(TeamMembership.user_id).where(
                TeamMembership.team_id.in_(team_ids), TeamMembership.removed_at.is_(None)
            )
        )

    filters = [
        ShadowAiIngestEvent.org_id == org_id,
        ShadowAiIngestEvent.occurred_at >= resolved_since,
        ShadowAiIngestEvent.occurred_at < resolved_until,
    ]
    if team_ids is not None:
        filters.append(_team_scope_filter())

    agg_stmt = (
        select(
            ShadowAiIngestEvent.user_identifier,
            ShadowAiIngestEvent.matched_user_id,
            ShadowAiIngestEvent.destination_host,
            func.count(ShadowAiIngestEvent.id),
            func.max(ShadowAiIngestEvent.occurred_at),
        )
        .where(*filters)
        .group_by(
            ShadowAiIngestEvent.user_identifier,
            ShadowAiIngestEvent.matched_user_id,
            ShadowAiIngestEvent.destination_host,
        )
    )
    agg_rows = (await session.execute(agg_stmt)).all()

    hostname_labels: dict[str, str] = {
        hostname: tool_label
        for hostname, tool_label in (
            await session.execute(select(KnownAiToolHostname.hostname, KnownAiToolHostname.tool_label))
        ).all()
    }

    violator_cutoff = datetime.now(timezone.utc) - timedelta(days=_REPEAT_VIOLATOR_WINDOW_DAYS)
    violator_filters = [
        ShadowAiIngestEvent.org_id == org_id,
        ShadowAiIngestEvent.occurred_at >= violator_cutoff,
    ]
    if team_ids is not None:
        violator_filters.append(_team_scope_filter())
    violator_stmt = (
        select(
            ShadowAiIngestEvent.user_identifier,
            ShadowAiIngestEvent.destination_host,
            func.count(ShadowAiIngestEvent.id),
        )
        .where(*violator_filters)
        .group_by(ShadowAiIngestEvent.user_identifier, ShadowAiIngestEvent.destination_host)
    )
    # The threshold itself is the pure, unit-tested `is_repeat_violator()`
    # predicate (AC5.1.8) - the SQL above only does the trailing-7-day
    # grouped count, never re-encodes the ">= 3" boundary a second time.
    violator_keys = {
        (row[0], row[1])
        for row in (await session.execute(violator_stmt)).all()
        if is_repeat_violator(row[2])
    }

    report = [
        ShadowAiReportRow(
            user_identifier=row[0],
            matched_user_id=row[1],
            destination_host=row[2],
            tool_label=hostname_labels.get(row[2], row[2]),
            frequency_per_week=round(row[3] * 7.0 / range_days, 2),
            last_seen=row[4],
            repeat_violator=(row[0], row[2]) in violator_keys,
        )
        for row in agg_rows
    ]
    report.sort(key=lambda r: r.last_seen, reverse=True)
    return report


# ---------------------------------------------------------------------------
# Enforcement delivery (AC5.1.7) - mirrors `services.rotation.
# RotationEmailNotifier`/`RotationWebhookNotifier`'s mechanics (see module
# docstring's "Enforcement" section for why these are mirrored, not shared).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowAiEnforcementEvent:
    user_identifier: str
    matched_user_email: str | None
    team_lead_emails: list[str]
    tool_label: str
    destination_host: str
    occurred_at: datetime


def _shadow_ai_human_summary(event: ShadowAiEnforcementEvent) -> str:
    return (
        f"Gatekey Shadow AI Discovery: '{event.user_identifier}' was observed using "
        f"an unsanctioned AI tool ({event.tool_label}, {event.destination_host}) "
        f"outside of Gatekey at {event.occurred_at.isoformat()}."
    )


class ShadowAiWebhookNotifier:
    """Same shape as `services.notifiers.WebhookNotifier`/`services.rotation.
    RotationWebhookNotifier` - POSTs to a pre-resolved URL via the shared
    pooled `httpx.AsyncClient`, never logs the URL."""

    def __init__(self, url: str, http_client: "httpx.AsyncClient") -> None:
        self._url = url
        self._http_client = http_client

    async def send(self, event: ShadowAiEnforcementEvent) -> None:
        payload = {
            "event": "shadow_ai_detected",
            "user_identifier": event.user_identifier,
            "tool": event.tool_label,
            "destination_host": event.destination_host,
            "occurred_at": event.occurred_at.isoformat(),
            "message": _shadow_ai_human_summary(event),
        }
        response = await self._http_client.post(self._url, json=payload, timeout=10.0)
        response.raise_for_status()


class ShadowAiEmailNotifier:
    """UNVERIFIED-LIVE (same caveat class as `services.notifiers.
    EmailNotifier`/`services.rotation.RotationEmailNotifier`) - same SMTP
    delivery mechanics as those classes, mirrored here (not called directly)
    since their `send()` is typed to a different event shape. Recipients:
    the flagged user (if matched to a known Gatekey user - an unmatched
    `user_identifier` has no email address to notify) plus every Team Lead
    of any team that user belongs to."""

    def __init__(self, settings: "Settings") -> None:
        self._settings = settings

    async def send(self, event: ShadowAiEnforcementEvent) -> None:
        if not self._settings.smtp_enabled():
            logger.info("shadow_ai_email_skipped_smtp_unconfigured")
            return
        to_addresses = [
            addr for addr in [event.matched_user_email, *event.team_lead_emails] if addr
        ]
        if not to_addresses:
            logger.info(
                "shadow_ai_email_skipped_no_recipients",
                extra={"user_identifier": event.user_identifier},
            )
            return

        def _send_sync() -> None:
            import smtplib
            from email.message import EmailMessage

            settings = self._settings
            # Guaranteed by the `smtp_enabled()` check above (`self.
            # GATEKEY_SMTP_HOST is not None`) - mypy can't narrow across
            # the `asyncio.to_thread` closure boundary into this nested
            # function.
            assert settings.GATEKEY_SMTP_HOST is not None
            message = EmailMessage()
            message["Subject"] = "Gatekey Shadow AI Discovery: unsanctioned AI tool usage detected"
            message["From"] = settings.GATEKEY_SMTP_FROM_ADDRESS
            message["To"] = ", ".join(to_addresses)
            message.set_content(_shadow_ai_human_summary(event))
            with smtplib.SMTP(settings.GATEKEY_SMTP_HOST, settings.GATEKEY_SMTP_PORT) as smtp:
                if settings.GATEKEY_SMTP_USE_TLS:
                    smtp.starttls()
                if settings.GATEKEY_SMTP_USERNAME and settings.GATEKEY_SMTP_PASSWORD:
                    smtp.login(settings.GATEKEY_SMTP_USERNAME, settings.GATEKEY_SMTP_PASSWORD)
                smtp.send_message(message)

        await asyncio.to_thread(_send_sync)


async def _load_shadow_ai_recipients(
    session: AsyncSession, matched_user_id: uuid.UUID
) -> tuple[str | None, list[str]]:
    """`(matched_user_email, team_lead_emails)` for one matched user - every
    Team Lead of every team this user is currently a member of, deduplicated.
    Mirrors `services.notifiers._load_recipients`'s "team leads + the
    subject" shape, but scoped to the FLAGGED user's own teams (there is no
    single team this event belongs to, unlike a budget-threshold alert)."""
    user = (await session.execute(select(User).where(User.id == matched_user_id))).scalar_one_or_none()
    user_email = user.sso_email if user is not None else None

    # `removed_at IS NULL` (added by `0049`) on both sides - the recipient
    # must be a CURRENT team_lead, of a team the flagged user is CURRENTLY
    # on (same "live, not historical" scoping as `_load_recipients`).
    leads_stmt = (
        select(User.sso_email)
        .join(TeamMembership, TeamMembership.user_id == User.id)
        .where(
            TeamMembership.role == TeamRole.TEAM_LEAD,
            TeamMembership.removed_at.is_(None),
            TeamMembership.team_id.in_(
                select(TeamMembership.team_id).where(
                    TeamMembership.user_id == matched_user_id,
                    TeamMembership.removed_at.is_(None),
                )
            ),
        )
        .distinct()
    )
    lead_emails = [
        email for email in (await session.execute(leads_stmt)).scalars().all() if email
    ]
    return user_email, lead_emails


def schedule_shadow_ai_enforcement(
    background_tasks: "BackgroundTasks", app: "FastAPI", *, event_ids: list[uuid.UUID]
) -> None:
    """The ingest route's single hook, called after `ingest_events()`
    commits - schedules delivery only when at least one event was actually
    persisted this call. `deliver_shadow_ai_enforcement` itself re-checks
    the config's current `enforcement_mode` (it may have changed between
    request start and this background task running) and no-ops for
    `detect_only`."""
    if not event_ids:
        return
    background_tasks.add_task(deliver_shadow_ai_enforcement, app, event_ids=event_ids)


async def deliver_shadow_ai_enforcement(app: "FastAPI", *, event_ids: list[uuid.UUID]) -> None:
    """`BackgroundTasks` entry point - runs after the ingest endpoint's
    response is on the wire, on a fresh DB session (same shape as
    `services.notifiers.deliver_threshold_alerts`/`services.rotation.
    deliver_service_account_rotation_notification`). Never raises - every
    failure is caught and logged, since a notification/webhook delivery bug
    must never surface anywhere near the ingest request/response cycle.
    """
    try:
        settings = app.state.settings
        async with app.state.db_session_factory() as session:
            config = await get_shadow_ai_ingest_config(session)
            if config is None or config.enforcement_mode == "detect_only":
                return

            events = (
                await session.execute(
                    select(ShadowAiIngestEvent).where(ShadowAiIngestEvent.id.in_(event_ids))
                )
            ).scalars().all()
            if not events:
                return

            hostname_labels: dict[str, str] = {
                hostname: tool_label
                for hostname, tool_label in (
                    await session.execute(
                        select(KnownAiToolHostname.hostname, KnownAiToolHostname.tool_label)
                    )
                ).all()
            }

            # Decrypted once per delivery batch (the URL is constant across
            # every event in this batch) - mirrors `services.notifiers.
            # deliver_threshold_alerts`'s identical once-per-batch decrypt
            # shape. A decrypt failure logs and simply yields no webhook
            # notifier this run (never raises - see this function's
            # docstring).
            decrypted_webhook_url: str | None = None
            if config.enforcement_mode == "webhook" and config.webhook_ciphertext is not None:
                # `webhook_ciphertext`/`webhook_nonce`/`webhook_auth_tag` are
                # written together as one envelope (never independently) -
                # see `db/models/shadow_ai_ingest_config.py`'s module
                # docstring - so ciphertext non-NULL guarantees these are too.
                assert config.webhook_nonce is not None
                assert config.webhook_auth_tag is not None
                try:
                    decrypted_webhook_url = decrypt_secret(
                        config.webhook_ciphertext,
                        nonce=config.webhook_nonce,
                        auth_tag=config.webhook_auth_tag,
                        aad=shadow_ai_webhook_aad(config.org_id),
                        key_provider=EnvKeyProvider.from_settings(settings),
                    ).decode("utf-8")
                except Exception:
                    logger.error(
                        "shadow_ai_webhook_decrypt_failed",
                        exc_info=True,
                        extra={"org_id": str(config.org_id)},
                    )

            for row in events:
                user_email: str | None = None
                lead_emails: list[str] = []
                if row.matched_user_id is not None:
                    user_email, lead_emails = await _load_shadow_ai_recipients(
                        session, row.matched_user_id
                    )

                notifiers: list[ShadowAiEmailNotifier | ShadowAiWebhookNotifier] = []
                if config.enforcement_mode == "notification" and settings.smtp_enabled():
                    notifiers.append(ShadowAiEmailNotifier(settings))
                elif config.enforcement_mode == "webhook" and decrypted_webhook_url:
                    notifiers.append(
                        ShadowAiWebhookNotifier(
                            decrypted_webhook_url, app.state.provider_http_client
                        )
                    )
                if not notifiers:
                    continue

                event = ShadowAiEnforcementEvent(
                    user_identifier=row.user_identifier,
                    matched_user_email=user_email,
                    team_lead_emails=lead_emails,
                    tool_label=hostname_labels.get(row.destination_host, row.destination_host),
                    destination_host=row.destination_host,
                    occurred_at=row.occurred_at,
                )
                for notifier in notifiers:
                    try:
                        await notifier.send(event)
                    except Exception:
                        logger.error(
                            "shadow_ai_enforcement_delivery_failed",
                            exc_info=True,
                            extra={
                                "channel": type(notifier).__name__,
                                "event_id": str(row.id),
                            },
                        )
    except Exception:
        logger.error("shadow_ai_enforcement_dispatch_failed", exc_info=True)
