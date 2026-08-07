"""DB-backed service for Team CRUD, members, alert config, and team usage
(Phase 2, BD-14) - design doc section 5.4.

Transaction contract: every mutating function here FLUSHES but never
COMMITS - the route handler writes its `AuditEntry` on the same session
(`services.audit.write_audit_entry`) and commits, per design doc section
7's same-transaction rule. Ceiling/budget-assignment writes live in
`services/team_budget.py` (ADR-5 locking), not here.

Webhook URL at rest: AES-256-GCM envelope via `services/encryption.py`,
associated data bound to the team (`team_webhook_aad`) - a ciphertext row
copied to another team's columns fails authentication (design doc 1.2).
The plaintext URL is never returned by any read path in this module; reads
expose only `webhook_configured` (even a masked tail of a Slack-style
webhook URL would leak secret path bits, so none is offered).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.join_request import JoinRequest
from gatekey.db.models.personal_api_key import PersonalApiKey
from gatekey.db.models.service_account_key import ServiceAccountKey
from gatekey.db.models.team import Team
from gatekey.db.models.team_membership import TeamMembership, TeamRole
from gatekey.db.models.usage_log import UsageLog
from gatekey.db.models.user import User
from gatekey.errors import GatekeyError, NotFoundError
from gatekey.services.encryption import KeyProvider, encrypt_secret


def team_webhook_aad(team_id: uuid.UUID) -> bytes:
    """AAD binding a team's webhook-URL ciphertext to its `team_id` (design
    doc 1.2). Shared with the notifier's decrypt path (BD-18) - both sides
    must build AAD through this one function."""
    return f"team:{team_id}".encode("utf-8")


def webhook_configured(team: Team) -> bool:
    return team.webhook_ciphertext is not None


# --- Team CRUD ---------------------------------------------------------------


async def get_team(session: AsyncSession, team_id: uuid.UUID) -> Team | None:
    stmt = select(Team).where(Team.org_id == DEFAULT_ORG_ID, Team.id == team_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_teams(session: AsyncSession) -> list[Team]:
    stmt = select(Team).where(Team.org_id == DEFAULT_ORG_ID).order_by(Team.name)
    return list((await session.execute(stmt)).scalars().all())


async def list_team_ids_led_by_user(session: AsyncSession, user_id: uuid.UUID) -> frozenset[uuid.UUID]:
    """Every `team_id` this user holds a `team_lead` `TeamMembership` role on
    (Phase 5, 5.1 Shadow AI Discovery - AC5.1.6/design doc wiring checklist
    "5.5 (Shadow AI, 5.1)" row 6). Used to force/validate the `team_id`
    filter on the Shadow AI report endpoint server-side for a Team Lead
    caller, rather than trusting a client-supplied `team_id` - the same
    "never trust client-supplied scoping, always resolve it server-side"
    discipline `require_team_role` already applies to path-parameter-scoped
    routes, extended here to a query-parameter-scoped one."""
    stmt = select(TeamMembership.team_id).where(
        TeamMembership.user_id == user_id, TeamMembership.role == TeamRole.TEAM_LEAD
    )
    return frozenset((await session.execute(stmt)).scalars().all())


async def list_teams_for_user(session: AsyncSession, user_id: uuid.UUID) -> list[Team]:
    """Only the teams the user holds a `TeamMembership` on (design doc 5.4's
    non-privileged `GET /v1/teams` view)."""
    stmt = (
        select(Team)
        .join(TeamMembership, TeamMembership.team_id == Team.id)
        .where(Team.org_id == DEFAULT_ORG_ID, TeamMembership.user_id == user_id)
        .order_by(Team.name)
    )
    return list((await session.execute(stmt)).scalars().all())


async def create_team(session: AsyncSession, *, name: str) -> Team:
    """Flushes, does not commit. The `(org_id, name)` unique constraint maps
    to a clean 409 - schema-enforced, never pre-check-then-insert."""
    team = Team(org_id=DEFAULT_ORG_ID, name=name)
    session.add(team)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise GatekeyError(
            f"A team named '{name}' already exists.",
            code="team_already_exists",
            status_code=409,
        ) from None
    return team


async def delete_team(session: AsyncSession, team: Team) -> None:
    """Design doc 5.4: 409 `team_has_members` / 409 `team_has_join_requests`
    if not empty (`join_requests.team_id` is `ON DELETE RESTRICT` - request
    history, pending or resolved, pins the team row). Flushes, does not
    commit - the caller's `team.delete` audit entry rides the same
    transaction."""
    member_exists = (
        await session.execute(
            select(TeamMembership.id).where(TeamMembership.team_id == team.id).limit(1)
        )
    ).scalar_one_or_none()
    if member_exists is not None:
        raise GatekeyError(
            "Team still has members - remove or reassign them first.",
            code="team_has_members",
            status_code=409,
        )
    jr_exists = (
        await session.execute(
            select(JoinRequest.id).where(JoinRequest.team_id == team.id).limit(1)
        )
    ).scalar_one_or_none()
    if jr_exists is not None:
        raise GatekeyError(
            "Team has join-request history attached and cannot be deleted.",
            code="team_has_join_requests",
            status_code=409,
        )
    await session.delete(team)
    try:
        await session.flush()
    except IntegrityError:
        # Historical (revoked) personal/service-account keys still reference
        # this team via ON DELETE RESTRICT FKs - a durable credential record
        # pins the team row the same way it pins its owning user.
        await session.rollback()
        raise GatekeyError(
            "Team is still referenced by one or more (possibly revoked) API "
            "keys and cannot be deleted.",
            code="team_in_use",
            status_code=409,
        ) from None


# --- Members -----------------------------------------------------------------


async def get_membership(
    session: AsyncSession, *, team_id: uuid.UUID, user_id: uuid.UUID
) -> TeamMembership | None:
    stmt = select(TeamMembership).where(
        TeamMembership.team_id == team_id, TeamMembership.user_id == user_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_team_members(
    session: AsyncSession, team_id: uuid.UUID
) -> list[tuple[TeamMembership, User]]:
    stmt = (
        select(TeamMembership, User)
        .join(User, TeamMembership.user_id == User.id)
        .where(TeamMembership.team_id == team_id)
        .order_by(User.name)
    )
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


async def member_has_active_keys(
    session: AsyncSession, *, team_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    """ADR-4's removal gate: an active (non-revoked, non-expired) personal
    key scoped to this team, OR an active team-attributed service-account
    key of this user+team, blocks membership removal."""
    personal_stmt = (
        select(PersonalApiKey.id)
        .where(
            PersonalApiKey.owner_user_id == user_id,
            PersonalApiKey.team_id == team_id,
            PersonalApiKey.revoked_at.is_(None),
            or_(
                PersonalApiKey.expires_at.is_(None),
                PersonalApiKey.expires_at > func.now(),
            ),
        )
        .limit(1)
    )
    if (await session.execute(personal_stmt)).scalar_one_or_none() is not None:
        return True
    sa_stmt = (
        select(ServiceAccountKey.id)
        .where(
            ServiceAccountKey.user_id == user_id,
            ServiceAccountKey.team_id == team_id,
            ServiceAccountKey.revoked_at.is_(None),
        )
        .limit(1)
    )
    return (await session.execute(sa_stmt)).scalar_one_or_none() is not None


async def remove_team_member(
    session: AsyncSession, *, team_id: uuid.UUID, user_id: uuid.UUID
) -> TeamMembership:
    """Hard row delete (design doc 1.4 - history lives in `AuditEntry`),
    gated by ADR-4's active-key check (409 `member_has_active_keys`).
    Flushes, does not commit. Returns the removed row so the caller can
    record its final role/budget state in the audit entry.

    The membership row is selected `FOR UPDATE` (security review M-2) so
    this removal serializes with a concurrent key create, which locks the
    same row (`create_personal_key` / `create_service_account`): whichever
    transaction wins the lock, the loser sees a consistent state - the
    active-key check below can never miss a key committed after it ran.
    """
    membership = (
        await session.execute(
            select(TeamMembership)
            .where(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if membership is None:
        raise NotFoundError("Team membership not found.")
    if await member_has_active_keys(session, team_id=team_id, user_id=user_id):
        raise GatekeyError(
            "Member still holds one or more active API keys scoped to this "
            "team - revoke them first.",
            code="member_has_active_keys",
            status_code=409,
        )
    await session.delete(membership)
    await session.flush()
    return membership


# --- Alert config ------------------------------------------------------------


async def set_team_alert_config(
    session: AsyncSession,
    team: Team,
    *,
    threshold_80_enabled: bool,
    threshold_100_enabled: bool,
    webhook_enabled: bool,
    email_enabled: bool,
    webhook_url: str | None,
    webhook_url_provided: bool,
    key_provider: KeyProvider,
) -> Team:
    """Apply the full alert-config write (design doc 5.4's PUT). The webhook
    URL is encrypted at rest with AAD bound to `team.id` and never echoed
    back. `webhook_url_provided=False` keeps the stored envelope; `True`
    with a string replaces it; `True` with `None` clears it. Flushes, does
    not commit."""
    if webhook_url_provided:
        if webhook_url is None:
            team.webhook_ciphertext = None
            team.webhook_nonce = None
            team.webhook_auth_tag = None
        else:
            envelope = encrypt_secret(
                webhook_url.encode("utf-8"),
                aad=team_webhook_aad(team.id),
                key_provider=key_provider,
            )
            team.webhook_ciphertext = envelope.ciphertext
            team.webhook_nonce = envelope.nonce
            team.webhook_auth_tag = envelope.auth_tag
    if webhook_enabled and team.webhook_ciphertext is None:
        raise GatekeyError(
            "Cannot enable webhook alerts without a webhook URL configured.",
            code="webhook_url_required",
            status_code=422,
        )
    team.alert_threshold_80_enabled = threshold_80_enabled
    team.alert_threshold_100_enabled = threshold_100_enabled
    team.webhook_alert_enabled = webhook_enabled
    team.email_alert_enabled = email_enabled
    await session.flush()
    return team


# --- Team usage (Team Dashboard) ---------------------------------------------


@dataclass(frozen=True)
class TeamSpendByDay:
    date: str
    spend_usd: Decimal


@dataclass(frozen=True)
class TeamSpendByModel:
    model: str
    spend_usd: Decimal


@dataclass(frozen=True)
class TeamMemberUsage:
    user_id: uuid.UUID
    name: str
    requests: int
    spend_usd: Decimal
    budget_usd: Decimal | None
    current_spend_usd: Decimal


@dataclass(frozen=True)
class TeamUsageSummary:
    total_spend_usd: Decimal
    request_count: int
    spend_by_day: list[TeamSpendByDay]
    spend_by_model: list[TeamSpendByModel]
    spend_by_member: list[TeamMemberUsage]


async def get_team_usage_summary(
    session: AsyncSession, *, team_id: uuid.UUID, since: datetime, until: datetime
) -> TeamUsageSummary:
    """Aggregate `usage_logs` scoped to one team over `[since, until)` -
    same single-`GROUP BY`-per-aggregate shape as
    `services.usage_logs.get_usage_summary`, filtered on the indexed
    `usage_logs.team_id`. The per-member breakdown includes every CURRENT
    member (zero-usage rows included) with their live membership budget
    state - callers run `ensure_current_period` first so that state is
    period-correct."""
    base_filter = (
        UsageLog.team_id == team_id,
        UsageLog.created_at >= since,
        UsageLog.created_at < until,
    )

    totals_row = (
        await session.execute(
            select(
                func.coalesce(func.sum(UsageLog.cost_usd), 0),
                func.count(UsageLog.id),
            ).where(*base_filter)
        )
    ).one()
    total_spend_usd = Decimal(totals_row[0] or 0)
    request_count = int(totals_row[1] or 0)

    by_day_stmt = (
        select(
            func.to_char(UsageLog.created_at, "YYYY-MM-DD").label("day"),
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
        )
        .where(*base_filter)
        .group_by("day")
        .order_by("day")
    )
    spend_by_day = [
        TeamSpendByDay(date=row[0], spend_usd=Decimal(row[1] or 0))
        for row in (await session.execute(by_day_stmt)).all()
    ]

    by_model_stmt = (
        select(UsageLog.model, func.coalesce(func.sum(UsageLog.cost_usd), 0))
        .where(*base_filter, UsageLog.model.is_not(None))
        .group_by(UsageLog.model)
        .order_by(func.coalesce(func.sum(UsageLog.cost_usd), 0).desc())
    )
    spend_by_model = [
        TeamSpendByModel(model=row[0], spend_usd=Decimal(row[1] or 0))
        for row in (await session.execute(by_model_stmt)).all()
    ]

    by_user_stmt = (
        select(
            UsageLog.user_id,
            func.count(UsageLog.id),
            func.coalesce(func.sum(UsageLog.cost_usd), 0),
        )
        .where(*base_filter, UsageLog.user_id.is_not(None))
        .group_by(UsageLog.user_id)
    )
    usage_by_user = {
        row[0]: (int(row[1] or 0), Decimal(row[2] or 0))
        for row in (await session.execute(by_user_stmt)).all()
    }

    spend_by_member = [
        TeamMemberUsage(
            user_id=user.id,
            name=user.name,
            requests=usage_by_user.get(user.id, (0, Decimal(0)))[0],
            spend_usd=usage_by_user.get(user.id, (0, Decimal(0)))[1],
            budget_usd=membership.budget_usd,
            current_spend_usd=membership.current_spend_usd,
        )
        for membership, user in await list_team_members(session, team_id)
    ]
    spend_by_member.sort(key=lambda m: m.spend_usd, reverse=True)

    return TeamUsageSummary(
        total_spend_usd=total_spend_usd,
        request_count=request_count,
        spend_by_day=spend_by_day,
        spend_by_model=spend_by_model,
        spend_by_member=spend_by_member,
    )


# Re-exported so route modules import member-role enum coercion from one
# place alongside the rest of the team service surface.
__all__ = [
    "TeamRole",
    "create_team",
    "delete_team",
    "get_membership",
    "get_team",
    "get_team_usage_summary",
    "list_team_ids_led_by_user",
    "list_team_members",
    "list_teams",
    "list_teams_for_user",
    "member_has_active_keys",
    "remove_team_member",
    "set_team_alert_config",
    "team_webhook_aad",
    "webhook_configured",
    "TeamUsageSummary",
]
