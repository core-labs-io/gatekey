"""Server-side browser sessions for SSO console login (Phase 2, BD-1).

See `docs/design/phase-2-multi-tenant-governance-design.md` sections 1.9 and
2.1-2.2. The raw session token (`secrets.token_urlsafe(32)`) lives only in
the httpOnly cookie; the DB stores its SHA-256 digest (`sessions.token_hash`)
- same lookup-hash discipline as `ServiceAccountKey.secret_hash`, reusing the
exact same `hash_secret` helper.

This module also hosts the two session-auth FastAPI dependencies
(`try_get_session_context` / `get_current_session`, per the BD-1 task
breakdown) so `api/deps.py`'s `require_admin`/`require_role` factories and
`api/v1/auth.py` share one implementation.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import Depends, Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.session import UserSession
from gatekey.db.models.user import User
from gatekey.db.session import get_db_session
from gatekey.errors import UnauthorizedError
from gatekey.services.service_accounts import hash_secret

logger = logging.getLogger("gatekey")

# Name of the httpOnly session cookie set by `/v1/auth/sso/callback`.
SESSION_COOKIE_NAME = "gatekey_session"

# Same entropy rationale as service-account secrets - see
# `services/service_accounts.py`.
_TOKEN_ENTROPY_BYTES = 32


@dataclass(frozen=True)
class SessionContext:
    """Identity of the session-authenticated (SSO) caller - design doc 2.2.

    `session_id`/`user_id` are None ONLY on `BREAK_GLASS_SESSION_CONTEXT`
    below - every context resolved from a real session cookie has both set.
    """

    session_id: uuid.UUID | None
    org_id: uuid.UUID
    user_id: uuid.UUID | None
    org_role: Literal["org_admin", "auditor"] | None
    display_label: str  # name/email snapshot, for audit actor_label

    def require_user_id(self) -> uuid.UUID:
        """`user_id`, narrowed to non-`None` for callers on a route that
        only ever receives a `SessionContext` from `get_current_session`
        (the cookie-only, personal-route dependency - see that function's
        docstring: "the base dependency for every non-admin,
        session-authenticated route"), never from the break-glass path.

        Post-ship fix: mypy flagged ~15 call sites across auth.py,
        onboarding.py, keys.py, auth_device.py, teams.py, and
        gateway/common.py passing `ctx.user_id` (typed `UUID | None`
        because the SAME dataclass also represents the break-glass caller,
        whose `user_id` is genuinely `None`) into functions expecting a
        real `UUID`. Those call sites are all behind `get_current_session`,
        which hard-fails 401 before ever returning a context with a `None`
        user_id - the nullability was real at the *type* level but not at
        the *reachable-value* level for any of them. This method documents
        and enforces that distinction once, at the type boundary, instead
        of a defensive `if ctx.user_id is None: raise ...` repeated at
        every call site for a case that cannot actually occur there. Do
        NOT call this after any dependency that can legitimately return
        `BREAK_GLASS_SESSION_CONTEXT` (`require_admin`/`require_role` and
        similar admin-surface dependencies) - it will raise for the one
        caller (a real break-glass request) that's supposed to be allowed
        through with no user_id at all.
        """
        if self.user_id is None:
            raise UnauthorizedError(
                "This route requires a real user session, not the break-glass admin token."
            )
        return self.user_id

    def require_session_id(self) -> uuid.UUID:
        """`session_id`, narrowed to non-`None` - same rationale as
        `require_user_id()` (both fields are set together or not at all,
        per this class's own docstring), used by routes that need the
        session row itself (e.g. `/v1/auth/logout`'s `revoke_session`
        call) rather than the user it belongs to."""
        if self.session_id is None:
            raise UnauthorizedError(
                "This route requires a real user session, not the break-glass admin token."
            )
        return self.session_id


# The break-glass GATEKEY_ADMIN_TOKEN acting as an org_admin-equivalent
# caller on Phase 2 session-RBAC surfaces (product spec locked decision #1:
# the token "keeps full Org Admin rights indefinitely"; A4: its mutations
# appear in the audit trail as "system:admin_token"). Returned only by
# `api.deps.try_get_privileged_context` after a constant-time token match -
# NEVER by `try_get_session_context`/`get_current_session`, which stay
# cookie-only (a token is not a person; personal-scope routes must not
# accept it).
BREAK_GLASS_SESSION_CONTEXT = SessionContext(
    session_id=None,
    org_id=DEFAULT_ORG_ID,
    user_id=None,
    org_role="org_admin",
    display_label="system:admin_token",
)


async def create_session(
    session: AsyncSession, *, user_id: uuid.UUID, org_id: uuid.UUID, ttl_hours: int
) -> tuple[UserSession, str]:
    """Create a session row; returns `(row, raw_token)`.

    `raw_token` exists only in this return value (destined for the httpOnly
    cookie) - never persisted, never logged.
    """
    raw_token = secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
    row = UserSession(
        org_id=org_id,
        user_id=user_id,
        token_hash=hash_secret(raw_token),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=ttl_hours),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, raw_token


async def revoke_session(session: AsyncSession, session_id: uuid.UUID) -> bool:
    """Revoke a session row, if it exists and is active.

    Same idempotent single-`UPDATE ... RETURNING` shape as
    `revoke_service_account` - returns True iff this call changed state.
    """
    stmt = (
        update(UserSession)
        .where(UserSession.id == session_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=func.now())
        .returning(UserSession.id)
    )
    result = await session.execute(stmt)
    changed = result.scalar_one_or_none() is not None
    await session.commit()
    return changed


def _display_label(user: User) -> str:
    if user.sso_email:
        return f"{user.name} <{user.sso_email}>"
    return user.name


async def try_get_session_context(
    request: Request, session: AsyncSession
) -> SessionContext | None:
    """Resolve the session cookie to a `SessionContext`, or None.

    Reads the session cookie, hashes it, looks up an active
    (`revoked_at IS NULL AND expires_at > now()`) sessions row joined to
    `users`. Returns None on any auth failure (no cookie, no matching row,
    expired, revoked) - never raises for those, so callers decide what
    "no session" means for their own route (`require_admin`'s break-glass
    fallback vs. `get_current_session`'s hard 401). Returns before touching
    the DB when no cookie is present at all.

    Phase 3 (design doc section 6.4): also requires `users.
    scim_deactivated_at IS NULL` - revoking a user's existing sessions on
    SCIM deactivation (`services.scim.revoke_scim_deactivated_user_
    credentials`) isn't sufficient on its own; without this check here, a
    deactivated user could still be mid-session (revoked between two
    requests is covered by `revoked_at`, but this closes a narrower gap: a
    session row that predates the revocation sweep, or any future session
    somehow issued after deactivation) and keep using it.
    """
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        return None

    stmt = (
        select(UserSession, User)
        .join(User, UserSession.user_id == User.id)
        .where(
            UserSession.token_hash == hash_secret(raw_token),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > func.now(),
            User.scim_deactivated_at.is_(None),
        )
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    session_row, user = row

    # Best-effort operational visibility only (design doc 2.2) - a write
    # failure must never fail the request it's riding on.
    try:
        await session.execute(
            update(UserSession)
            .where(UserSession.id == session_row.id)
            .values(last_seen_at=func.now())
        )
        await session.commit()
    except Exception:
        logger.debug("session_last_seen_touch_failed", exc_info=True)
        try:
            await session.rollback()
        except Exception:
            pass

    return SessionContext(
        session_id=session_row.id,
        org_id=session_row.org_id,
        user_id=session_row.user_id,
        org_role=user.org_role.value if user.org_role is not None else None,
        display_label=_display_label(user),
    )


async def get_current_session(
    request: Request, session: AsyncSession = Depends(get_db_session)
) -> SessionContext:
    """Hard-fail (401) if no valid session - the base dependency for every
    non-admin, session-authenticated route (My Usage, onboarding, etc.)."""
    ctx = await try_get_session_context(request, session)
    if ctx is None:
        raise UnauthorizedError("Missing or invalid session.")
    return ctx
