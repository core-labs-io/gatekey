"""Auth & session routes (Phase 2, BD-4) - design doc sections 2.1 and 5.1.

`GET /v1/auth/sso/login` / `GET /v1/auth/sso/callback` implement the
authorization-code + PKCE flow via `services/oidc.py`; both return 404 when
SSO env vars are unset (SSO is fully optional - AC1.3). `/callback` upserts
the `User` by `sso_subject`, always issues a session (a pending-onboarding
user is authenticated, just not yet authorized beyond the onboarding
routes), and routes by state per design doc 2.1 step 5. The state is
computed fresh here AND on every `/me` call - never cached client-side as a
one-time decision.

Redirect targets are frontend routes; the exact paths below are the shared
contract with FE-1/FE-2.
"""

from __future__ import annotations

import hmac
import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.config import Settings
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.join_request import JoinRequest, JoinRequestStatus
from gatekey.db.models.team import Team
from gatekey.db.models.team_membership import TeamMembership
from gatekey.db.models.user import User
from gatekey.db.session import get_db_session
from gatekey.errors import NotFoundError, UnauthorizedError
from gatekey.services.oidc import (
    LOGIN_STATE_COOKIE_NAME,
    LOGIN_STATE_TTL_SECONDS,
    build_authorization_request,
    decode_login_state,
    encode_login_state,
    exchange_code,
    fetch_discovery_document,
    validate_id_token,
)
from gatekey.services.sessions import (
    SESSION_COOKIE_NAME,
    SessionContext,
    create_session,
    get_current_session,
    revoke_session,
)
from gatekey.services.users import resolve_or_create_sso_user

logger = logging.getLogger("gatekey")

router = APIRouter(prefix="/v1/auth", tags=["auth"])

# Frontend routes (shared contract with FE-1/FE-2) - design doc 2.1 step 5.
_REDIRECT_APP = "/"
_REDIRECT_PENDING_APPROVAL = "/onboarding/pending"
_REDIRECT_PROFILE = "/onboarding/profile"

# The login-state cookie only ever needs to travel back to these auth routes.
_LOGIN_STATE_COOKIE_PATH = "/v1/auth"


def _require_sso_configured(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    if not settings.oidc_enabled():
        # 404, not 403/501: with SSO unset these routes simply don't exist
        # (design doc 5.1).
        raise NotFoundError("SSO is not configured.")
    return settings


@router.get("/sso/login")
async def sso_login(request: Request) -> RedirectResponse:
    settings = _require_sso_configured(request)
    discovery = await fetch_discovery_document(
        request.app.state.provider_http_client, settings.GATEKEY_OIDC_ISSUER_URL
    )
    authorization_url, login_state = build_authorization_request(settings, discovery)
    response = RedirectResponse(authorization_url, status_code=status.HTTP_302_FOUND)
    response.set_cookie(
        LOGIN_STATE_COOKIE_NAME,
        encode_login_state(login_state, settings),
        max_age=LOGIN_STATE_TTL_SECONDS,
        httponly=True,
        secure=settings.GATEKEY_SESSION_COOKIE_SECURE,
        samesite="lax",
        path=_LOGIN_STATE_COOKIE_PATH,
    )
    return response


async def _resolve_post_login_redirect(session: AsyncSession, user: User) -> str:
    """Design doc 2.1 step 5 - route by state, computed fresh every time."""
    if user.org_role is not None:
        return _REDIRECT_APP
    membership_exists = (
        await session.execute(
            select(TeamMembership.id).where(TeamMembership.user_id == user.id).limit(1)
        )
    ).scalar_one_or_none()
    if membership_exists is not None:
        return _REDIRECT_APP
    pending = (
        await session.execute(
            select(JoinRequest.id)
            .where(
                JoinRequest.requester_user_id == user.id,
                JoinRequest.status == JoinRequestStatus.PENDING,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if pending is not None:
        return _REDIRECT_PENDING_APPROVAL
    return _REDIRECT_PROFILE


@router.get("/sso/callback")
async def sso_callback(
    request: Request,
    code: str,
    state: str,
    session: AsyncSession = Depends(get_db_session),
) -> RedirectResponse:
    settings = _require_sso_configured(request)

    raw_login_state = request.cookies.get(LOGIN_STATE_COOKIE_NAME)
    login_state = decode_login_state(raw_login_state, settings) if raw_login_state else None
    if login_state is None or not hmac.compare_digest(state, str(login_state.get("state", ""))):
        raise UnauthorizedError("Invalid or expired SSO login state.")

    http_client = request.app.state.provider_http_client
    discovery = await fetch_discovery_document(http_client, settings.GATEKEY_OIDC_ISSUER_URL)
    tokens = await exchange_code(
        http_client, discovery, settings, code=code, code_verifier=login_state["code_verifier"]
    )
    id_token = tokens.get("id_token")
    if not isinstance(id_token, str) or not id_token:
        raise UnauthorizedError("SSO login failed.")
    claims = await validate_id_token(
        http_client, discovery, settings, id_token=id_token, expected_nonce=login_state["nonce"]
    )

    # Upsert by the durable `sub` claim (never email - design doc 1.8), with
    # a Phase 3 SCIM-identity-reconciliation fallback (design doc section
    # 6.3) - see `services.users.resolve_or_create_sso_user`.
    sub = claims["sub"]
    email = claims.get("email")
    user = await resolve_or_create_sso_user(
        session, org_id=DEFAULT_ORG_ID, sub=sub, email=email, name=claims.get("name")
    )

    if user.scim_deactivated_at is not None:
        # Phase 3 (design doc section 6.4): a SCIM-deactivated user must not
        # be able to mint a fresh session by simply logging back in via SSO -
        # revoking existing sessions/keys alone isn't sufficient on its own.
        raise UnauthorizedError("This account has been deactivated.")

    # Absolute URL on the FRONTEND origin (split-origin deployment: frontend
    # :3000, backend :8000) - a relative 302 would land the browser on the
    # backend origin. Paths themselves are the FE-1/FE-2 contract, unchanged.
    redirect_target = settings.GATEKEY_FRONTEND_ORIGIN.rstrip("/") + (
        await _resolve_post_login_redirect(session, user)
    )
    _, raw_token = await create_session(
        session,
        user_id=user.id,
        org_id=user.org_id,
        ttl_hours=settings.GATEKEY_SESSION_TTL_HOURS,
    )

    response = RedirectResponse(redirect_target, status_code=status.HTTP_302_FOUND)
    response.delete_cookie(LOGIN_STATE_COOKIE_NAME, path=_LOGIN_STATE_COOKIE_PATH)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        max_age=settings.GATEKEY_SESSION_TTL_HOURS * 3600,
        httponly=True,
        secure=settings.GATEKEY_SESSION_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    ctx: SessionContext = Depends(get_current_session),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    await revoke_session(session, ctx.session_id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


class MeTeam(BaseModel):
    team_id: UUID
    team_name: str
    role: Literal["team_lead", "member"]


class MeResponse(BaseModel):
    user_id: UUID
    name: str
    email: str | None
    org_role: Literal["org_admin", "auditor"] | None
    teams: list[MeTeam]
    onboarding_status: Literal["resolved", "pending_profile", "pending_approval"]


@router.get("/me", response_model=MeResponse)
async def me(
    ctx: SessionContext = Depends(get_current_session),
    session: AsyncSession = Depends(get_db_session),
) -> MeResponse:
    user = (
        await session.execute(select(User).where(User.id == ctx.user_id))
    ).scalar_one()

    membership_rows = (
        await session.execute(
            select(TeamMembership, Team.name)
            .join(Team, TeamMembership.team_id == Team.id)
            .where(TeamMembership.user_id == user.id)
            .order_by(Team.name)
        )
    ).all()
    teams = [
        MeTeam(team_id=membership.team_id, team_name=team_name, role=membership.role.value)
        for membership, team_name in membership_rows
    ]

    # Same routing-by-state logic as the callback, recomputed fresh (2.1
    # step 5's explicit "never cached as a one-time decision").
    if user.org_role is not None or teams:
        onboarding_status = "resolved"
    else:
        pending = (
            await session.execute(
                select(JoinRequest.id)
                .where(
                    JoinRequest.requester_user_id == user.id,
                    JoinRequest.status == JoinRequestStatus.PENDING,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        onboarding_status = "pending_approval" if pending is not None else "pending_profile"

    return MeResponse(
        user_id=user.id,
        name=user.name,
        email=user.sso_email,
        org_role=user.org_role.value if user.org_role is not None else None,
        teams=teams,
        onboarding_status=onboarding_status,
    )
