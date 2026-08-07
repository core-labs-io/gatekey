"""DB-backed service for managing `User` rows (Phase 1.4 / 1.6).

`User` is the budget-owning cost-center a `ServiceAccountKey` attributes its
usage to - not an authentication principal. Every function here operates
against `constants.DEFAULT_ORG_ID` only, same as every other Phase 1 service
module (no multi-org signup flow yet).

`resolve_or_create_sso_user` (Phase 3, BD-21) implements the SSO-callback
upsert extension design doc
`phase-3-security-compliance-design.md` section 6.3 describes - see that
function's docstring.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.service_account_key import ServiceAccountKey
from gatekey.db.models.user import User


class UserNotFoundError(Exception):
    """Raised where a caller needs a clean not-found signal distinct from `None`."""


async def create_user(
    session: AsyncSession, *, name: str, budget_usd: Any = None
) -> User:
    row = User(org_id=DEFAULT_ORG_ID, name=name, budget_usd=budget_usd)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def list_users(session: AsyncSession) -> list[User]:
    stmt = select(User).where(User.org_id == DEFAULT_ORG_ID).order_by(User.created_at)
    return list((await session.execute(stmt)).scalars().all())


async def get_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    stmt = select(User).where(User.org_id == DEFAULT_ORG_ID, User.id == user_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def update_user(
    session: AsyncSession, user_id: uuid.UUID, updates: dict[str, Any]
) -> User | None:
    """`updates` should be `UserUpdateRequest.model_dump(exclude_unset=True)`
    (see module docstring on `schemas.user.UserUpdateRequest`). An empty
    `updates` dict (`PATCH {}`) is a legal no-op that still returns the
    current row.
    """
    row = await get_user(session, user_id)
    if row is None:
        return None
    for field, value in updates.items():
        setattr(row, field, value)
    await session.commit()
    await session.refresh(row)
    return row


async def resolve_or_create_sso_user(
    session: AsyncSession, *, org_id: uuid.UUID, sub: str, email: str | None, name: str | None
) -> User:
    """SSO callback upsert (Phase 2 design doc 2.1 step 4), extended per
    Phase 3 design doc section 6.3 with a SCIM-identity-reconciliation
    fallback:

    1. Look up by `sso_subject = sub` (the durable, never-email, OIDC
       lookup key - Phase 2 design doc 1.8).
    2. If not found and the IdP asserted an email: look up by
       `(org_id, sso_email = email)` among rows with `sso_subject IS NULL`
       (covers both SCIM-provisioned rows and pre-Phase-2 legacy rows). If
       found, backfill `sso_subject` onto that row rather than inserting a
       duplicate - the exact gap section 6.3 closes: left unaddressed, a
       SCIM-provisioned user's first SSO login would silently create a
       SECOND `User` row instead of attaching to the one SCIM already set
       up (wrong team, wrong history - a real correctness bug).
    3. If still not found: create a new row (Phase 2's existing behavior).

    The display-only `sso_email` refresh (Phase 2's existing behavior)
    applies only in case 1 - a row just claimed via case 2 already has a
    matching email by construction.
    """
    user = (
        await session.execute(select(User).where(User.sso_subject == sub))
    ).scalar_one_or_none()
    if user is not None:
        if email and user.sso_email != email:
            # Display-only refresh; `name` is user/admin-editable, left alone.
            user.sso_email = email
            await session.commit()
        return user

    if email:
        user = (
            await session.execute(
                select(User).where(
                    User.org_id == org_id,
                    User.sso_email == email,
                    User.sso_subject.is_(None),
                )
            )
        ).scalar_one_or_none()
        if user is not None:
            user.sso_subject = sub
            await session.commit()
            await session.refresh(user)
            return user

    user = User(
        org_id=org_id,
        name=name or email or sub,
        sso_subject=sub,
        sso_email=email,
        org_role=None,
        budget_usd=None,  # A6: flat budget stays unused for SSO users
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def delete_user(session: AsyncSession, user_id: uuid.UUID) -> bool | None:
    """Hard-delete a `User`.

    Returns `True` (deleted), `False` (blocked - still referenced by one or
    more `ServiceAccountKey` rows, active *or* revoked - see
    `db/models/service_account_key.py`'s module docstring for why a
    `RESTRICT` FK can't distinguish the two), or `None` (no such user).

    Pre-checks via a `SELECT` rather than attempting the `DELETE` and
    catching `IntegrityError` - gives a clean, specific log line and avoids
    a guaranteed-to-fail statement.
    """
    row = await get_user(session, user_id)
    if row is None:
        return None
    in_use_stmt = select(ServiceAccountKey.id).where(ServiceAccountKey.user_id == user_id).limit(1)
    in_use = (await session.execute(in_use_stmt)).scalar_one_or_none()
    if in_use is not None:
        return False
    await session.delete(row)
    await session.commit()
    return True
