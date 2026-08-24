"""Teams resource tree (Phase 2, BD-14 + BD-15's team join-request queue) -
design doc sections 5.3 and 5.4.

One resource tree, per-route auth dependency (`require_role` /
`require_team_role` / plain session) - deliberately NOT duplicated into
separate `/admin/...` and `/team-lead/...` trees (design doc section 5's
own instruction). Org-admin bypass on team-scoped routes is resolved inside
`require_team_role`.

Every mutation writes exactly one `AuditEntry` in the same DB transaction
as the mutation (design doc section 7): service functions flush, this
module writes the audit entry, then commits.

Alert-config routes are `require_role(org_admin)`-only per the design's
flagged resolution (ADR-fork 8): a Team Lead receives alerts but does not
get a config surface this phase. The webhook URL is never returned in any
form - `webhook_configured` only (see `services/teams.py`).
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import (
    TeamRoleContext,
    get_access_schedule_cache,
    get_cache_invalidator,
    get_custom_model_route_cache,
    get_key_provider,
    get_member_model_policy_cache,
    get_residency_rule_cache,
    get_self_hosted_model_route_cache,
    get_source_ip,
    get_team_model_policy_cache,
    get_privileged_session,
    require_role,
    require_team_role,
)
from gatekey.db.models.dlp_policy import DlpAction
from gatekey.db.models.join_request import JoinRequest, JoinRequestStatus
from gatekey.db.models.team import Team, TeamPeriodEnd, TeamPeriodType
from gatekey.db.models.team_membership import TeamMembership, TeamRole
from gatekey.db.session import get_db_session
from gatekey.errors import ForbiddenError, GatekeyError, NotFoundError
from gatekey.providers.model_registry import MODEL_REGISTRY
from gatekey.schemas.dlp_policy import TeamDlpOverrideRequest, TeamDlpOverrideResponse
from gatekey.schemas.join_request import (
    JoinRequestApproveRequest,
    JoinRequestRejectRequest,
    JoinRequestResponse,
)
from gatekey.schemas.access_schedule import AccessSchedulePutRequest, AccessScheduleResponse
from gatekey.schemas.emergency_override import (
    EmergencyOverrideGrantRequest,
    EmergencyOverrideResponse,
)
from gatekey.schemas.residency_rule import ResidencyRulePutRequest, ResidencyRuleResponse
from gatekey.schemas.team import (
    ReassignBudgetRequest,
    ReassignBudgetResponse,
    RemovedTeamMemberResponse,
    TeamAlertConfigPutRequest,
    TeamAlertConfigResponse,
    TeamCreateRequest,
    TeamDetailResponse,
    TeamMemberAddRequest,
    TeamMemberModelRestrictionsPutRequest,
    TeamMemberModelRestrictionsResponse,
    TeamMemberResponse,
    TeamMemberUpdateRequest,
    TeamMemberUsageResponse,
    TeamModelRestrictionsPutRequest,
    TeamModelRestrictionsResponse,
    TeamPeriodConfigRequest,
    TeamResponse,
    TeamSpendByDayResponse,
    TeamSpendByModelResponse,
    TeamUpdateRequest,
    TeamUsageResponse,
)
from gatekey.services.access_schedules import (
    AccessScheduleCache,
    delete_team_access_schedule,
    get_team_access_schedule,
    set_team_access_schedule,
)
from gatekey.services.audit import write_audit_entry
from gatekey.services.dlp import get_team_dlp_override, set_team_dlp_override
from gatekey.services.emergency_overrides import (
    get_override,
    grant_emergency_override,
    revoke_emergency_override,
)
from gatekey.services.encryption import KeyProvider
from gatekey.services.join_requests import (
    get_team_join_request,
    list_team_join_requests,
    reject_join_request,
)
from gatekey.services.response_cache import CacheInvalidator
from gatekey.services.model_policy import (
    MemberModelPolicyCache,
    TeamModelPolicyCache,
    get_member_model_policy,
    get_policy,
    get_team_model_policy,
    set_member_model_policy,
    set_team_model_policy,
)
from gatekey.services.residency import (
    ResidencyRuleCache,
    delete_team_residency_rule,
    get_team_residency_rule,
    set_team_residency_rule,
)
from gatekey.services.custom_models import CustomModelRouteCache
from gatekey.services.self_hosted_providers import SelfHostedModelRouteCache
from gatekey.services.service_accounts import get_service_account
from gatekey.services.sessions import SessionContext
from gatekey.services.team_budget import (
    approve_join_request,
    create_team_membership,
    reassign_budget,
    set_team_budget_ceiling,
    update_team_membership_budget,
)
from gatekey.services.team_periods import ensure_current_period
from gatekey.services.teams import (
    create_team,
    delete_team,
    get_membership,
    get_team,
    get_team_usage_summary,
    list_removed_team_members,
    list_team_members,
    list_teams,
    list_teams_for_user,
    remove_team_member,
    restore_team_member,
    set_team_alert_config,
    webhook_configured,
)
from gatekey.services.users import get_user
from gatekey.api.v1.admin.usage import _RANGE_DELTAS  # shared range convention

from datetime import datetime, timezone

router = APIRouter(prefix="/v1/teams", tags=["teams"])

_ORG_WIDE_READ_ROLES = ("org_admin", "auditor")


# --- response builders --------------------------------------------------------


def _team_response(team: Team) -> TeamResponse:
    return TeamResponse(
        id=team.id,
        name=team.name,
        budget_ceiling_usd=team.budget_ceiling_usd,
        current_spend_usd=team.current_spend_usd,
        period_type=team.period_type.value,
        on_period_end=team.on_period_end.value,
        current_period_started_at=team.current_period_started_at,
        created_at=team.created_at,
        updated_at=team.updated_at,
    )


def _member_response(membership: TeamMembership, name: str) -> TeamMemberResponse:
    return TeamMemberResponse(
        user_id=membership.user_id,
        name=name,
        role=membership.role.value,
        budget_usd=membership.budget_usd,
        current_spend_usd=membership.current_spend_usd,
        created_at=membership.created_at,
    )


def _removed_member_response(membership: TeamMembership, name: str) -> RemovedTeamMemberResponse:
    assert membership.removed_at is not None
    return RemovedTeamMemberResponse(
        user_id=membership.user_id,
        name=name,
        role=membership.role.value,
        budget_usd=membership.budget_usd,
        current_spend_usd=membership.current_spend_usd,
        created_at=membership.created_at,
        removed_at=membership.removed_at,
    )


def _alert_config_response(team: Team) -> TeamAlertConfigResponse:
    return TeamAlertConfigResponse(
        threshold_80_enabled=team.alert_threshold_80_enabled,
        threshold_100_enabled=team.alert_threshold_100_enabled,
        webhook_enabled=team.webhook_alert_enabled,
        webhook_configured=webhook_configured(team),
        email_enabled=team.email_alert_enabled,
    )


def _join_request_response(row: JoinRequest, team_name: str | None = None) -> JoinRequestResponse:
    return JoinRequestResponse(
        id=row.id,
        team_id=row.team_id,
        team_name=team_name,
        requester_user_id=row.requester_user_id,
        requester_name=row.requester_name,
        status=row.status.value,
        routed_to=row.routed_to.value,
        requested_at=row.requested_at,
        resolved_at=row.resolved_at,
        resolved_by_user_id=row.resolved_by_user_id,
        approved_budget_usd=row.approved_budget_usd,
        rejection_reason=row.rejection_reason,
    )


async def _get_team_or_404(session: AsyncSession, team_id: uuid.UUID) -> Team:
    team = await get_team(session, team_id)
    if team is None:
        raise NotFoundError(f"No team found with id '{team_id}'.")
    return team


# --- Team CRUD (5.4) ---------------------------------------------------------


@router.post("", response_model=TeamResponse, status_code=201)
async def create_team_endpoint(
    payload: TeamCreateRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> TeamResponse:
    team = await create_team(session, name=payload.name)
    # Lock-ordering fix (CMR-14 security review): write the audit entry
    # BEFORE `set_team_budget_ceiling`, which takes `SELECT ... FOR UPDATE`
    # on `org_settings` (then `teams`). `write_audit_entry`, when the org's
    # hash chain is enabled, takes its own `SELECT ... FOR UPDATE` on
    # `compliance_settings` - `api/v1/admin/custom_models.py`'s/
    # `self_hosted_providers.py`'s POST/PUT handlers already acquire
    # compliance_settings before org_settings, so acquiring them in the
    # opposite order here (org_settings first) was a real, reproducible
    # cross-endpoint deadlock. `new_value`'s `budget_ceiling_usd` is taken
    # from the request payload rather than the (not-yet-locked) `team` row -
    # identical to what `set_team_budget_ceiling` below will actually
    # persist on success; a rejection there (422) rolls back the whole
    # transaction, discarding this queued-but-uncommitted audit entry too.
    await write_audit_entry(
        session,
        actor=ctx,
        action="team.create",
        target_type="team",
        target_id=str(team.id),
        old_value=None,
        new_value={"name": team.name, "budget_ceiling_usd": payload.budget_ceiling_usd},
    )
    if payload.budget_ceiling_usd is not None:
        # Reuses the ADR-5 locked org-ceiling check rather than reimplementing
        # it for the create path.
        team = await set_team_budget_ceiling(
            session, team_id=team.id, budget_ceiling_usd=payload.budget_ceiling_usd
        )
    await session.commit()
    await session.refresh(team)  # populate server defaults (period/spend/timestamps)
    return _team_response(team)


@router.get("", response_model=list[TeamResponse])
async def list_teams_endpoint(
    ctx: SessionContext = Depends(get_privileged_session),
    session: AsyncSession = Depends(get_db_session),
) -> list[TeamResponse]:
    """org_admin/auditor see all teams; everyone else only teams they hold a
    membership on (design doc 5.4). `get_privileged_session` (not plain
    `get_current_session`) so the break-glass bearer sees all teams too -
    locked decision #1."""
    if ctx.org_role in _ORG_WIDE_READ_ROLES:
        teams = await list_teams(session)
    else:
        # The break-glass bearer always resolves with org_role="org_admin"
        # (in _ORG_WIDE_READ_ROLES, caught by the `if` above) - this branch
        # only ever runs for a real per-user session, so require_user_id()
        # here is a correctness backstop, not a defensive guess.
        teams = await list_teams_for_user(session, ctx.require_user_id())
    return [_team_response(t) for t in teams]


@router.get("/{team_id}", response_model=TeamDetailResponse)
async def get_team_endpoint(
    team_id: uuid.UUID,
    ctx: SessionContext = Depends(get_privileged_session),
    session: AsyncSession = Depends(get_db_session),
) -> TeamDetailResponse:
    """org-admin/auditor unconditional (incl. the break-glass bearer, which
    resolves as org_admin via `get_privileged_session`); anyone else must
    belong to the team.
    The non-member rejection is the same generic 403 whether or not the team
    exists (anti-enumeration, matching `require_team_role`)."""
    if ctx.org_role not in _ORG_WIDE_READ_ROLES:
        # Same reasoning as list_teams_endpoint: break-glass never reaches
        # here (it's always org_admin, caught above).
        if await get_membership(session, team_id=team_id, user_id=ctx.require_user_id()) is None:
            raise ForbiddenError("You do not have the required role for this team.")
    team = await _get_team_or_404(session, team_id)
    await ensure_current_period(session, team)  # design doc 3.5's touch point
    members = await list_team_members(session, team_id)
    restriction = await get_team_model_policy(session, team_id)
    base = _team_response(team)
    return TeamDetailResponse(
        **base.model_dump(),
        members=[_member_response(m, u.name) for m, u in members],
        team_restriction=sorted(restriction) if restriction is not None else None,
        alert_config=_alert_config_response(team),
    )


@router.patch("/{team_id}", response_model=TeamResponse)
async def update_team_endpoint(
    team_id: uuid.UUID,
    payload: TeamUpdateRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> TeamResponse:
    team = await _get_team_or_404(session, team_id)
    old_value = {"name": team.name, "budget_ceiling_usd": team.budget_ceiling_usd}
    new_budget_ceiling_usd = (
        payload.budget_ceiling_usd
        if "budget_ceiling_usd" in payload.model_fields_set
        else team.budget_ceiling_usd
    )
    new_name = payload.name if payload.name is not None else team.name
    # Lock-ordering fix (CMR-14 security review): write the audit entry
    # BEFORE `set_team_budget_ceiling` (locks `org_settings` then `teams`) -
    # same rationale as `create_team_endpoint` above and `api/v1/admin/
    # org_settings.py::put_org_settings_endpoint`. `new_value` is built from
    # the request payload/current row rather than the post-write state,
    # since the org_settings/team locks haven't been taken yet - identical
    # to the final persisted state on success; any failure below (422 from
    # the ceiling check, or the 409 IntegrityError handled explicitly) rolls
    # back the whole transaction, discarding this queued-but-uncommitted
    # audit entry with it.
    await write_audit_entry(
        session,
        actor=ctx,
        action="team.update",
        target_type="team",
        target_id=str(team_id),
        old_value=old_value,
        new_value={"name": new_name, "budget_ceiling_usd": new_budget_ceiling_usd},
    )
    if "budget_ceiling_usd" in payload.model_fields_set:
        # ADR-5 locked check (422 budget_ceiling_exceeded /
        # budget_ceiling_below_current_allocation pass through).
        team = await set_team_budget_ceiling(
            session, team_id=team_id, budget_ceiling_usd=payload.budget_ceiling_usd
        )
    if payload.name is not None:
        team.name = payload.name
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            raise GatekeyError(
                f"A team named '{payload.name}' already exists.",
                code="team_already_exists",
                status_code=status.HTTP_409_CONFLICT,
            ) from None
    await session.commit()
    return _team_response(team)


@router.patch("/{team_id}/period-config", response_model=TeamResponse)
async def update_period_config_endpoint(
    team_id: uuid.UUID,
    payload: TeamPeriodConfigRequest,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
) -> TeamResponse:
    team = await _get_team_or_404(session, team_id)
    old_value = {"period_type": team.period_type, "on_period_end": team.on_period_end}
    if payload.period_type is not None:
        team.period_type = TeamPeriodType(payload.period_type)
    if payload.on_period_end is not None:
        team.on_period_end = TeamPeriodEnd(payload.on_period_end)
    await session.flush()
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="team.period_config.update",
        target_type="team",
        target_id=str(team_id),
        old_value=old_value,
        new_value={"period_type": team.period_type, "on_period_end": team.on_period_end},
    )
    await session.commit()
    return _team_response(team)


@router.delete("/{team_id}", status_code=204)
async def delete_team_endpoint(
    team_id: uuid.UUID,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    team = await _get_team_or_404(session, team_id)
    old_value = {"name": team.name, "budget_ceiling_usd": team.budget_ceiling_usd}
    # Audit first so it rides the same transaction as the DELETE (design doc
    # section 7); delete_team's 409 checks raise before any write.
    await write_audit_entry(
        session,
        actor=ctx,
        action="team.delete",
        target_type="team",
        target_id=str(team_id),
        old_value=old_value,
        new_value=None,
    )
    await delete_team(session, team)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Members (5.4) -----------------------------------------------------------


@router.get("/{team_id}/members/removed", response_model=list[RemovedTeamMemberResponse])
async def list_removed_members_endpoint(
    team_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
) -> list[RemovedTeamMemberResponse]:
    """Restore-UI counterpart to `list_members_endpoint` (added by `0049`).
    `team_lead`-only (not plain `member`) - same gate as removing/restoring
    itself, not the read-only member-list gate."""
    await _get_team_or_404(session, team_id)
    removed = await list_removed_team_members(session, team_id)
    return [_removed_member_response(m, u.name) for m, u in removed]


@router.get("/{team_id}/members", response_model=list[TeamMemberResponse])
async def list_members_endpoint(
    team_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead", "member")),
    session: AsyncSession = Depends(get_db_session),
) -> list[TeamMemberResponse]:
    team = await _get_team_or_404(session, team_id)
    await ensure_current_period(session, team)
    members = await list_team_members(session, team_id)
    return [_member_response(m, u.name) for m, u in members]


@router.post("/{team_id}/members", response_model=TeamMemberResponse, status_code=201)
async def add_member_endpoint(
    team_id: uuid.UUID,
    payload: TeamMemberAddRequest,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
) -> TeamMemberResponse:
    user = await get_user(session, payload.user_id)
    if user is None:
        raise NotFoundError(f"No user found with id '{payload.user_id}'.")
    membership_id = uuid.uuid4()
    # Lock-ordering fix (CMR-14 security review, broader systemic audit):
    # write the audit entry BEFORE `create_team_membership`, which takes
    # `SELECT ... FOR UPDATE` on `teams` - see `create_team_endpoint`'s
    # comment above / `services/team_budget.py`'s module docstring
    # addendum. The id is generated here (mirroring `custom_models.py`'s
    # `custom_model_id` pattern) so `target_id` is known before the lock.
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="team.member.add",
        target_type="team_membership",
        target_id=str(membership_id),
        old_value=None,
        new_value={
            "team_id": team_id,
            "user_id": payload.user_id,
            "role": payload.role,
            "budget_usd": payload.budget_usd,
        },
    )
    try:
        membership = await create_team_membership(
            session,
            team_id=team_id,
            user_id=payload.user_id,
            role=TeamRole(payload.role),
            budget_usd=payload.budget_usd,
            membership_id=membership_id,
        )
    except IntegrityError:
        await session.rollback()
        raise GatekeyError(
            "User is already a member of this team.",
            code="member_already_exists",
            status_code=status.HTTP_409_CONFLICT,
        ) from None
    await session.commit()
    await session.refresh(membership)  # server defaults (spend/timestamps)
    return _member_response(membership, user.name)


@router.patch("/{team_id}/members/{user_id}", response_model=TeamMemberResponse)
async def update_member_endpoint(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: TeamMemberUpdateRequest,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
) -> TeamMemberResponse:
    membership = await get_membership(session, team_id=team_id, user_id=user_id)
    if membership is None:
        raise NotFoundError("Team membership not found.")
    old_value = {"role": membership.role, "budget_usd": membership.budget_usd}
    new_budget_usd = (
        payload.budget_usd
        if "budget_usd" in payload.model_fields_set
        else membership.budget_usd
    )
    new_role = TeamRole(payload.role) if payload.role is not None else membership.role
    # Lock-ordering fix (CMR-14 security review, broader systemic audit):
    # write the audit entry BEFORE `update_team_membership_budget`, which
    # takes `SELECT ... FOR UPDATE` on `teams` - see `create_team_endpoint`'s
    # comment above. `new_value` is built from the payload/current row
    # rather than the post-write state, since the lock hasn't been taken
    # yet - identical to the final persisted state on success; a 422 from
    # the ceiling check rolls back the whole transaction, discarding this
    # queued-but-uncommitted audit entry with it.
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="team.member.update",
        target_type="team_membership",
        target_id=str(membership.id),
        old_value=old_value,
        new_value={"role": new_role, "budget_usd": new_budget_usd},
    )
    if "budget_usd" in payload.model_fields_set:
        # ADR-5 locked ceiling check (422 budget_ceiling_exceeded passes
        # through).
        membership = await update_team_membership_budget(
            session, team_id=team_id, user_id=user_id, budget_usd=payload.budget_usd
        )
    if payload.role is not None:
        membership.role = TeamRole(payload.role)
        await session.flush()
    await session.commit()
    user = await get_user(session, user_id)
    return _member_response(membership, user.name if user is not None else "")


@router.delete("/{team_id}/members/{user_id}", status_code=204)
async def remove_member_endpoint(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """Soft delete (added by `0049`) - takes effect immediately (every
    subsequent gateway request on this member's keys 403s), reversible via
    `restore_member_endpoint` below. See `services.teams.remove_team_
    member`'s docstring for why the old ADR-4 active-key guard is gone."""
    membership = await remove_team_member(session, team_id=team_id, user_id=user_id)
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="team.member.remove",
        target_type="team_membership",
        target_id=str(membership.id),
        old_value={
            "team_id": team_id,
            "user_id": user_id,
            "role": membership.role,
            "budget_usd": membership.budget_usd,
        },
        new_value=None,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{team_id}/members/{user_id}/restore", response_model=TeamMemberResponse)
async def restore_member_endpoint(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
) -> TeamMemberResponse:
    """Undo `remove_member_endpoint` (added by `0049`) - same role, budget,
    and spend history the removal left in place; no key re-issuance
    needed, the member's existing keys work again immediately."""
    membership = await restore_team_member(session, team_id=team_id, user_id=user_id)
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="team.member.restore",
        target_type="team_membership",
        target_id=str(membership.id),
        old_value=None,
        new_value={
            "team_id": team_id,
            "user_id": user_id,
            "role": membership.role,
            "budget_usd": membership.budget_usd,
        },
    )
    await session.commit()
    user = await get_user(session, user_id)
    return _member_response(membership, user.name if user is not None else "")


@router.post("/{team_id}/reassign-budget", response_model=ReassignBudgetResponse)
async def reassign_budget_endpoint(
    team_id: uuid.UUID,
    payload: ReassignBudgetRequest,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
) -> ReassignBudgetResponse:
    # Lock-ordering fix (CMR-14 security review, broader systemic audit):
    # write the audit entry BEFORE `reassign_budget`, which takes
    # `SELECT ... FOR UPDATE` on `teams` - see `create_team_endpoint`'s
    # comment above. Old/expected-new amounts are read here via a plain
    # (non-locking) membership lookup; `reassign_budget` below re-validates
    # and re-applies the SAME delta under its own lock, so on success these
    # values match exactly what gets persisted - any validation failure
    # there (404/422) rolls back the whole transaction, discarding this
    # queued-but-uncommitted audit entry with it.
    from_membership = await get_membership(session, team_id=team_id, user_id=payload.from_user_id)
    to_membership = await get_membership(session, team_id=team_id, user_id=payload.to_user_id)
    if from_membership is None or to_membership is None:
        raise NotFoundError("Team membership not found.")
    from_old = from_membership.budget_usd
    to_old = to_membership.budget_usd
    from_new = from_old - payload.amount_usd if from_old is not None else None
    to_new = to_old + payload.amount_usd if to_old is not None else None
    # AC2.4: exactly ONE audit entry recording both sides' old -> new.
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="team.budget.reassign",
        target_type="team",
        target_id=str(team_id),
        old_value={
            "from": {"user_id": payload.from_user_id, "budget_usd": from_old},
            "to": {"user_id": payload.to_user_id, "budget_usd": to_old},
        },
        new_value={
            "amount_usd": payload.amount_usd,
            "from": {"user_id": payload.from_user_id, "budget_usd": from_new},
            "to": {"user_id": payload.to_user_id, "budget_usd": to_new},
        },
    )
    result = await reassign_budget(
        session,
        team_id=team_id,
        from_user_id=payload.from_user_id,
        to_user_id=payload.to_user_id,
        amount_usd=payload.amount_usd,
    )
    await session.commit()
    return ReassignBudgetResponse(
        from_user_id=result.from_user_id,
        to_user_id=result.to_user_id,
        amount_usd=result.amount_usd,
        from_new_budget_usd=result.from_new_budget_usd,
        to_new_budget_usd=result.to_new_budget_usd,
    )


# --- Model restrictions (5.4) ------------------------------------------------


def _known_models(
    self_hosted_cache: SelfHostedModelRouteCache, custom_model_cache: CustomModelRouteCache
) -> set[str]:
    """The full universe of Gatekey-routable model names Model Policy can
    govern - static registry plus every VERIFIED self-hosted/custom model
    this org has registered. Matches `api.v1.model_access`'s own
    `all_models` union exactly, so the org baseline a team lead sees here
    is the same universe the gateway and the end-user self-service screen
    actually enforce against - not just the static registry subset (a
    previous, narrower version of this endpoint enumerated `MODEL_REGISTRY`
    alone, which silently produced an EMPTY baseline - and an unusable
    Team Model Restrictions checklist - for any org whose entire allowlist
    happened to be a custom/self-hosted/OpenRouter-discovered model rather
    than a static registry entry)."""
    return set(MODEL_REGISTRY) | self_hosted_cache.known_model_ids() | custom_model_cache.known_model_ids()


@router.get("/{team_id}/model-restrictions", response_model=TeamModelRestrictionsResponse)
async def get_model_restrictions_endpoint(
    team_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead", "member")),
    session: AsyncSession = Depends(get_db_session),
    self_hosted_cache: SelfHostedModelRouteCache = Depends(get_self_hosted_model_route_cache),
    custom_model_cache: CustomModelRouteCache = Depends(get_custom_model_route_cache),
) -> TeamModelRestrictionsResponse:
    await _get_team_or_404(session, team_id)
    org_snapshot = await get_policy(session)
    restriction = await get_team_model_policy(session, team_id)
    return TeamModelRestrictionsResponse(
        org_baseline=sorted(m for m in _known_models(self_hosted_cache, custom_model_cache) if org_snapshot.is_allowed(m)),
        team_restriction=sorted(restriction) if restriction is not None else None,
    )


@router.put("/{team_id}/model-restrictions", response_model=TeamModelRestrictionsResponse)
async def put_model_restrictions_endpoint(
    team_id: uuid.UUID,
    payload: TeamModelRestrictionsPutRequest,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
    cache: TeamModelPolicyCache = Depends(get_team_model_policy_cache),
    self_hosted_cache: SelfHostedModelRouteCache = Depends(get_self_hosted_model_route_cache),
    custom_model_cache: CustomModelRouteCache = Depends(get_custom_model_route_cache),
) -> TeamModelRestrictionsResponse:
    """422 `team_model_restricts_org_denied_model` passes straight through
    from `set_team_model_policy` (AC3.2 defense-in-depth). The audit entry
    is added BEFORE the service call because `set_team_model_policy` commits
    internally - its commit persists both in one transaction, and its
    validate-before-write means a 422 leaves the pending audit row
    uncommitted (rolled back with the session)."""
    await _get_team_or_404(session, team_id)  # existence gate only
    old_restriction = await get_team_model_policy(session, team_id)
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="team.model_restrictions.update",
        target_type="team",
        target_id=str(team_id),
        old_value={
            "models": sorted(old_restriction) if old_restriction is not None else None
        },
        new_value={"models": sorted(set(payload.models))},
    )
    committed = await set_team_model_policy(
        session,
        team_id,
        payload.models,
        cache=cache,
        self_hosted_cache=self_hosted_cache,
        custom_model_cache=custom_model_cache,
    )
    org_snapshot = await get_policy(session)
    return TeamModelRestrictionsResponse(
        org_baseline=sorted(m for m in _known_models(self_hosted_cache, custom_model_cache) if org_snapshot.is_allowed(m)),
        team_restriction=sorted(committed),
    )


async def _team_baseline_models(
    session: AsyncSession,
    team_id: uuid.UUID,
    self_hosted_cache: SelfHostedModelRouteCache,
    custom_model_cache: CustomModelRouteCache,
) -> list[str]:
    """Every model this TEAM can currently use - org baseline (see
    `_known_models`, the full static/self-hosted/custom universe, not just
    the static registry) intersected with the team's own restriction, if
    any."""
    org_snapshot = await get_policy(session)
    team_restriction = await get_team_model_policy(session, team_id)
    return sorted(
        m
        for m in _known_models(self_hosted_cache, custom_model_cache)
        if org_snapshot.is_allowed(m) and (team_restriction is None or m in team_restriction)
    )


@router.get(
    "/{team_id}/members/{user_id}/model-restrictions",
    response_model=TeamMemberModelRestrictionsResponse,
)
async def get_member_model_restrictions_endpoint(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead", "member")),
    session: AsyncSession = Depends(get_db_session),
    self_hosted_cache: SelfHostedModelRouteCache = Depends(get_self_hosted_model_route_cache),
    custom_model_cache: CustomModelRouteCache = Depends(get_custom_model_route_cache),
) -> TeamMemberModelRestrictionsResponse:
    """A plain `member` session may only fetch their OWN restriction (self-
    view) - a `team_lead` (or the org_admin bypass, which resolves `role`
    to `"org_admin"`) may fetch any member's. Mirrors the RBAC posture
    `update_member_endpoint` already applies to per-member budget - see
    that endpoint for the precedent."""
    await _get_team_or_404(session, team_id)
    if team_ctx.role == "member" and team_ctx.session.user_id != user_id:
        raise ForbiddenError("You can only view your own model access.")
    restriction = await get_member_model_policy(session, team_id, user_id)
    return TeamMemberModelRestrictionsResponse(
        team_baseline=await _team_baseline_models(session, team_id, self_hosted_cache, custom_model_cache),
        member_restriction=sorted(restriction) if restriction is not None else None,
    )


@router.put(
    "/{team_id}/members/{user_id}/model-restrictions",
    response_model=TeamMemberModelRestrictionsResponse,
)
async def put_member_model_restrictions_endpoint(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: TeamMemberModelRestrictionsPutRequest,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
    cache: MemberModelPolicyCache = Depends(get_member_model_policy_cache),
    self_hosted_cache: SelfHostedModelRouteCache = Depends(get_self_hosted_model_route_cache),
    custom_model_cache: CustomModelRouteCache = Depends(get_custom_model_route_cache),
) -> TeamMemberModelRestrictionsResponse:
    """Team-lead-only (same as the team-level PUT above) - a member can view
    their own restriction but never set it. 422
    `member_model_restricts_team_denied_model` passes straight through from
    `set_member_model_policy` (already carries its own status_code/code - no
    manual mapping needed, same as every other error this router lets
    propagate). Fetches the membership row FIRST (404 `NotFoundError` if
    `user_id` doesn't currently hold an active membership on `team_id`) both
    to give a clean 404 before any other work AND to get the membership's
    own id for the audit entry's `target_id` - same convention
    `update_member_endpoint`'s per-membership budget audit already
    establishes (`target_type="team_membership"`, `target_id=str(membership.
    id)`), not a composite string. `set_member_model_policy` re-checks
    membership internally too (defense in depth - it's a cheap, already-
    indexed lookup, and that function must also be safe to call from
    contexts that haven't already fetched the row)."""
    await _get_team_or_404(session, team_id)
    membership = await get_membership(session, team_id=team_id, user_id=user_id)
    if membership is None:
        raise NotFoundError("Team membership not found.")
    old_restriction = await get_member_model_policy(session, team_id, user_id)
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="team.member_model_restrictions.update",
        target_type="team_membership",
        target_id=str(membership.id),
        old_value={
            "models": sorted(old_restriction) if old_restriction is not None else None
        },
        new_value={"models": sorted(set(payload.models))},
    )
    committed = await set_member_model_policy(
        session,
        team_id,
        user_id,
        payload.models,
        cache=cache,
        self_hosted_cache=self_hosted_cache,
        custom_model_cache=custom_model_cache,
    )
    return TeamMemberModelRestrictionsResponse(
        team_baseline=await _team_baseline_models(session, team_id, self_hosted_cache, custom_model_cache),
        member_restriction=sorted(committed),
    )


# --- DLP action override (Phase 3, BD-2, design doc section 9.2) ------------


@router.get("/{team_id}/dlp-override", response_model=TeamDlpOverrideResponse)
async def get_team_dlp_override_endpoint(
    team_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead", "member")),
    session: AsyncSession = Depends(get_db_session),
) -> TeamDlpOverrideResponse:
    await _get_team_or_404(session, team_id)
    action = await get_team_dlp_override(session, team_id)
    return TeamDlpOverrideResponse(action=action.value if action is not None else None)


@router.put("/{team_id}/dlp-override", response_model=TeamDlpOverrideResponse)
async def put_team_dlp_override_endpoint(
    team_id: uuid.UUID,
    payload: TeamDlpOverrideRequest,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
    cache_invalidator: CacheInvalidator = Depends(get_cache_invalidator),
) -> TeamDlpOverrideResponse:
    """Action override only (AC2.4 - no per-team pattern authoring). `set_
    team_dlp_override` commits internally, so the audit entry is written
    first - same pattern as `put_model_restrictions_endpoint` above."""
    await _get_team_or_404(session, team_id)
    old_action = await get_team_dlp_override(session, team_id)
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="team.dlp_override.update",
        target_type="team",
        target_id=str(team_id),
        old_value={"action": old_action.value if old_action is not None else None},
        new_value={"action": payload.action},
    )
    row = await set_team_dlp_override(
        session, team_id, DlpAction(payload.action), cache_invalidator=cache_invalidator
    )
    return TeamDlpOverrideResponse(action=row.action.value)


# --- Residency rule (Phase 3, BD-4, design doc section 3.2/9.3) -------------


@router.get("/{team_id}/residency-rule", response_model=ResidencyRuleResponse | None)
async def get_team_residency_rule_endpoint(
    team_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead", "member")),
    session: AsyncSession = Depends(get_db_session),
) -> ResidencyRuleResponse | None:
    await _get_team_or_404(session, team_id)
    row = await get_team_residency_rule(session, team_id)
    return ResidencyRuleResponse.model_validate(row) if row is not None else None


@router.put("/{team_id}/residency-rule", response_model=ResidencyRuleResponse)
async def put_team_residency_rule_endpoint(
    team_id: uuid.UUID,
    payload: ResidencyRulePutRequest,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
    cache: ResidencyRuleCache = Depends(get_residency_rule_cache),
    cache_invalidator: CacheInvalidator = Depends(get_cache_invalidator),
) -> ResidencyRuleResponse:
    """422 `residency_rule_widens_org_rule` passes straight through from
    `set_team_residency_rule` (AC3.3 defense-in-depth) - no DB write in that
    case. `set_team_residency_rule` commits internally, so (mirroring `put_
    model_restrictions_endpoint`) the audit entry is written first."""
    await _get_team_or_404(session, team_id)
    old_row = await get_team_residency_rule(session, team_id)
    old_behavior = old_row.violation_behavior.value if old_row is not None else "hard_block"
    weakened = payload.violation_behavior == "warn" and old_behavior == "hard_block"
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="residency_rule.weakened" if weakened else "team_residency_rule.update",
        target_type="team",
        target_id=str(team_id),
        old_value={"allowed_regions": sorted(old_row.allowed_regions), "violation_behavior": old_behavior}
        if old_row is not None
        else None,
        new_value=payload.model_dump(),
    )
    row = await set_team_residency_rule(
        session,
        team_id,
        allowed_regions=payload.allowed_regions,
        violation_behavior=payload.violation_behavior,
        cache=cache,
        cache_invalidator=cache_invalidator,
    )
    return ResidencyRuleResponse.model_validate(row)


@router.delete("/{team_id}/residency-rule", status_code=204)
async def delete_team_residency_rule_endpoint(
    team_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
    cache: ResidencyRuleCache = Depends(get_residency_rule_cache),
    cache_invalidator: CacheInvalidator = Depends(get_cache_invalidator),
) -> Response:
    await _get_team_or_404(session, team_id)
    row = await get_team_residency_rule(session, team_id)
    if row is None:
        raise NotFoundError(f"No residency rule is configured for team '{team_id}'.")
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="team_residency_rule.delete",
        target_type="team",
        target_id=str(team_id),
        old_value={"allowed_regions": sorted(row.allowed_regions), "violation_behavior": row.violation_behavior.value},
        new_value=None,
    )
    await delete_team_residency_rule(session, team_id, cache=cache, cache_invalidator=cache_invalidator)
    return Response(status_code=204)


# --- Alert config (5.4, org_admin-only per ADR-fork 8) -----------------------


@router.get("/{team_id}/alert-config", response_model=TeamAlertConfigResponse)
async def get_alert_config_endpoint(
    team_id: uuid.UUID,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> TeamAlertConfigResponse:
    team = await _get_team_or_404(session, team_id)
    return _alert_config_response(team)


@router.put("/{team_id}/alert-config", response_model=TeamAlertConfigResponse)
async def put_alert_config_endpoint(
    team_id: uuid.UUID,
    payload: TeamAlertConfigPutRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    key_provider: KeyProvider = Depends(get_key_provider),
) -> TeamAlertConfigResponse:
    team = await _get_team_or_404(session, team_id)
    old_value = {
        "threshold_80_enabled": team.alert_threshold_80_enabled,
        "threshold_100_enabled": team.alert_threshold_100_enabled,
        "webhook_enabled": team.webhook_alert_enabled,
        "webhook_configured": webhook_configured(team),
        "email_enabled": team.email_alert_enabled,
    }
    team = await set_team_alert_config(
        session,
        team,
        threshold_80_enabled=payload.threshold_80_enabled,
        threshold_100_enabled=payload.threshold_100_enabled,
        webhook_enabled=payload.webhook_enabled,
        email_enabled=payload.email_enabled,
        webhook_url=payload.webhook_url,
        webhook_url_provided="webhook_url" in payload.model_fields_set,
        key_provider=key_provider,
    )
    # The audit record carries configured-state booleans only - never the
    # webhook URL in any form (design doc section 7 secret hygiene).
    await write_audit_entry(
        session,
        actor=ctx,
        action="team.alert_config.update",
        target_type="team",
        target_id=str(team_id),
        old_value=old_value,
        new_value={
            "threshold_80_enabled": team.alert_threshold_80_enabled,
            "threshold_100_enabled": team.alert_threshold_100_enabled,
            "webhook_enabled": team.webhook_alert_enabled,
            "webhook_configured": webhook_configured(team),
            "email_enabled": team.email_alert_enabled,
        },
    )
    await session.commit()
    return _alert_config_response(team)


# --- Usage (Team Dashboard, 5.4) ---------------------------------------------


@router.get("/{team_id}/usage", response_model=TeamUsageResponse)
async def get_team_usage_endpoint(
    team_id: uuid.UUID,
    range: Literal["24h", "7d", "30d"] = Query(default="7d"),
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead", "member")),
    session: AsyncSession = Depends(get_db_session),
) -> TeamUsageResponse:
    """Rolling-window ranges only (24h/7d/30d) - the admin dashboard's
    `custom` start/end variant isn't part of the Team Dashboard contract
    this phase."""
    team = await _get_team_or_404(session, team_id)
    await ensure_current_period(session, team)  # design doc 3.5's touch point
    now = datetime.now(timezone.utc)
    summary = await get_team_usage_summary(
        session, team_id=team_id, since=now - _RANGE_DELTAS[range], until=now
    )
    return TeamUsageResponse(
        total_spend_usd=summary.total_spend_usd,
        request_count=summary.request_count,
        spend_by_day=[
            TeamSpendByDayResponse(date=d.date, spend_usd=d.spend_usd)
            for d in summary.spend_by_day
        ],
        spend_by_model=[
            TeamSpendByModelResponse(model=m.model, spend_usd=m.spend_usd)
            for m in summary.spend_by_model
        ],
        spend_by_member=[
            TeamMemberUsageResponse(
                user_id=m.user_id,
                name=m.name,
                requests=m.requests,
                spend_usd=m.spend_usd,
                budget_usd=m.budget_usd,
                current_spend_usd=m.current_spend_usd,
            )
            for m in summary.spend_by_member
        ],
    )


# --- Join-request queue (5.3, BD-15) -----------------------------------------


@router.get("/{team_id}/join-requests", response_model=list[JoinRequestResponse])
async def list_join_requests_endpoint(
    team_id: uuid.UUID,
    status_filter: Literal["pending", "approved", "rejected"] | None = Query(
        default=None, alias="status"
    ),
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
) -> list[JoinRequestResponse]:
    rows = await list_team_join_requests(
        session,
        team_id=team_id,
        status=JoinRequestStatus(status_filter) if status_filter is not None else None,
    )
    return [_join_request_response(r) for r in rows]


@router.post(
    "/{team_id}/join-requests/{request_id}/approve",
    response_model=TeamMemberResponse,
    status_code=201,
)
async def approve_join_request_endpoint(
    team_id: uuid.UUID,
    request_id: uuid.UUID,
    payload: JoinRequestApproveRequest,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
) -> TeamMemberResponse:
    """AC6.7: the ceiling check, membership INSERT, and status flip are one
    locked transaction (`services.team_budget.approve_join_request`); 422
    `budget_ceiling_exceeded` passes through with the live headroom."""
    join_request = await get_team_join_request(
        session, team_id=team_id, request_id=request_id
    )
    if join_request is None:
        raise NotFoundError("Join request not found.")
    # Lock-ordering fix (CMR-14 security review, broader systemic audit):
    # write the audit entry BEFORE `approve_join_request`, which takes
    # `SELECT ... FOR UPDATE` on `teams` - see `create_team_endpoint`'s
    # comment above. Every field below is already fully determined by the
    # request/path (no DB read dependency), so no precompute-staleness risk.
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="join_request.approve",
        target_type="join_request",
        target_id=str(request_id),
        old_value={"status": "pending"},
        new_value={
            "status": "approved",
            "team_id": team_id,
            "user_id": join_request.requester_user_id,
            "budget_usd": payload.budget_usd,
        },
    )
    membership = await approve_join_request(
        session,
        request_id=request_id,
        team_id=team_id,
        requester_user_id=join_request.requester_user_id,
        budget_usd=payload.budget_usd,
        approved_by_user_id=team_ctx.session.user_id,
    )
    await session.commit()
    await session.refresh(membership)  # server defaults (spend/timestamps)
    user = await get_user(session, join_request.requester_user_id)
    return _member_response(
        membership, user.name if user is not None else join_request.requester_name
    )


@router.post(
    "/{team_id}/join-requests/{request_id}/reject", response_model=JoinRequestResponse
)
async def reject_join_request_endpoint(
    team_id: uuid.UUID,
    request_id: uuid.UUID,
    payload: JoinRequestRejectRequest,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
) -> JoinRequestResponse:
    row = await reject_join_request(
        session,
        request_id=request_id,
        team_id=team_id,
        resolved_by_user_id=team_ctx.session.user_id,
        reason=payload.reason,
    )
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="join_request.reject",
        target_type="join_request",
        target_id=str(request_id),
        old_value={"status": "pending"},
        new_value={"status": "rejected", "reason": payload.reason},
    )
    await session.commit()
    return _join_request_response(row)


# --- Access schedule (Phase 3, BD-16/BD-17, design doc section 5/9.7) --------


@router.get("/{team_id}/access-schedule", response_model=AccessScheduleResponse | None)
async def get_team_access_schedule_endpoint(
    team_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead", "member")),
    session: AsyncSession = Depends(get_db_session),
) -> AccessScheduleResponse | None:
    await _get_team_or_404(session, team_id)
    row = await get_team_access_schedule(session, team_id)
    return AccessScheduleResponse.model_validate(row) if row is not None else None


@router.put("/{team_id}/access-schedule", response_model=AccessScheduleResponse)
async def put_team_access_schedule_endpoint(
    team_id: uuid.UUID,
    payload: AccessSchedulePutRequest,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
    cache: AccessScheduleCache = Depends(get_access_schedule_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> AccessScheduleResponse:
    """422 `access_schedule_widens_parent` passes straight through from
    `set_team_access_schedule` (AC9.2 defense-in-depth) - no DB write in
    that case. That call commits internally, so (mirroring `put_team_
    residency_rule_endpoint`) the audit entry is written first."""
    await _get_team_or_404(session, team_id)
    old_row = await get_team_access_schedule(session, team_id)
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="team_access_schedule.update",
        target_type="team",
        target_id=str(team_id),
        old_value=AccessScheduleResponse.model_validate(old_row).model_dump(mode="json")
        if old_row is not None
        else None,
        new_value=payload.model_dump(mode="json"),
        source_ip=source_ip,
    )
    row = await set_team_access_schedule(
        session,
        team_id,
        enabled=payload.enabled,
        allowed_days=payload.allowed_days,
        allowed_hours_start=payload.allowed_hours_start,
        allowed_hours_end=payload.allowed_hours_end,
        cache=cache,
    )
    return AccessScheduleResponse.model_validate(row)


@router.delete("/{team_id}/access-schedule", status_code=204)
async def delete_team_access_schedule_endpoint(
    team_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
    cache: AccessScheduleCache = Depends(get_access_schedule_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> Response:
    await _get_team_or_404(session, team_id)
    row = await get_team_access_schedule(session, team_id)
    if row is None:
        raise NotFoundError(f"No access schedule is configured for team '{team_id}'.")
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="team_access_schedule.delete",
        target_type="team",
        target_id=str(team_id),
        old_value=AccessScheduleResponse.model_validate(row).model_dump(mode="json"),
        new_value=None,
        source_ip=source_ip,
    )
    await delete_team_access_schedule(session, team_id, cache=cache)
    return Response(status_code=204)


# --- Emergency overrides (Phase 3, BD-16/BD-18, design doc section 5.3, ------
# AC9.6-AC9.9) -----------------------------------------------------------------


async def _get_team_service_account_or_404(
    session: AsyncSession, *, team_id: uuid.UUID, key_id: uuid.UUID
):
    """A Team Lead may only act on a service account attributed to their OWN
    team (AC9.8); the org-admin bypass baked into `require_team_role`
    already covers the "any team" half of that AC. Deliberately the SAME
    generic 404 whether the key doesn't exist at all or belongs to a
    different team - anti-enumeration, mirrors `require_team_role`'s own
    "don't distinguish not-found from insufficient-role" discipline."""
    key = await get_service_account(session, key_id)
    if key is None or key.team_id != team_id:
        raise NotFoundError(f"No service-account key found with id '{key_id}' on this team.")
    return key


@router.post(
    "/{team_id}/service-account-keys/{key_id}/emergency-override",
    response_model=EmergencyOverrideResponse,
    status_code=201,
)
async def grant_emergency_override_endpoint(
    team_id: uuid.UUID,
    key_id: uuid.UUID,
    payload: EmergencyOverrideGrantRequest,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> EmergencyOverrideResponse:
    """AC9.8: Team Lead scoped to their own team (org-admin bypass built
    into `require_team_role`, AC9.8's "any team" half). AC9.7: `reason` is
    non-empty at the schema layer AND re-validated inside `grant_emergency_
    override` (server-side, not just a UI hint). `grant_emergency_override`
    commits internally (needs the row's generated id), so the audit entry
    is written after, same second-commit deviation documented on `services.
    service_accounts.create_service_account`'s route."""
    await _get_team_or_404(session, team_id)
    await _get_team_service_account_or_404(session, team_id=team_id, key_id=key_id)
    row = await grant_emergency_override(
        session,
        service_account_id=key_id,
        granted_by_user_id=team_ctx.session.user_id,
        reason=payload.reason,
        expires_at=payload.expires_at,
    )
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="emergency_override.grant",
        target_type="emergency_override",
        target_id=str(row.id),
        old_value=None,
        new_value={
            "service_account_id": str(key_id),
            "reason": row.reason,
            "expires_at": row.expires_at,
        },
        source_ip=source_ip,
    )
    await session.commit()
    return EmergencyOverrideResponse.model_validate(row)


@router.delete(
    "/{team_id}/service-account-keys/{key_id}/emergency-override/{override_id}",
    status_code=204,
)
async def revoke_emergency_override_endpoint(
    team_id: uuid.UUID,
    key_id: uuid.UUID,
    override_id: uuid.UUID,
    team_ctx: TeamRoleContext = Depends(require_team_role("team_lead")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> Response:
    await _get_team_or_404(session, team_id)
    await _get_team_service_account_or_404(session, team_id=team_id, key_id=key_id)
    # Ownership MUST be checked before revoking (not after) - otherwise a
    # caller scoped to their own team could revoke an override belonging to
    # a DIFFERENT service account by id, discovering the mismatch only
    # after the (wrong) row was already mutated.
    existing = await get_override(session, override_id)
    if existing is None or existing.service_account_id != key_id:
        raise NotFoundError(f"No active emergency override found with id '{override_id}'.")
    row = await revoke_emergency_override(
        session, override_id, revoked_by_user_id=team_ctx.session.user_id
    )
    if row is None:
        raise NotFoundError(f"No active emergency override found with id '{override_id}'.")
    await write_audit_entry(
        session,
        actor=team_ctx.session,
        action="emergency_override.revoke",
        target_type="emergency_override",
        target_id=str(override_id),
        old_value={"revoked_at": None},
        new_value={"revoked_at": row.revoked_at},
        source_ip=source_ip,
    )
    await session.commit()
    return Response(status_code=204)
