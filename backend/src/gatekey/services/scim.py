"""SCIM 2.0 provisioning/deprovisioning (Phase 3, BD-20..24) - design doc
`docs/design/phase-3-security-compliance-design.md` sections 6.1-6.4 and API
contract section 9.5; product spec section 5 (AC5.1-AC5.8).

**Deliberate protocol-boundary deviation, do not "fix" this to match the rest
of the codebase**: every other Gatekey error response uses the generic
`{"error": {"code", "message"}}` envelope (see `errors.py`). SCIM clients
(IdPs like Okta/Azure AD) expect RFC 7644 section 3.12's own error shape
(`{"schemas": [...], "status", "detail"[, "scimType"]}`), so `ScimError`
below is a completely separate exception hierarchy from `GatekeyError`, with
its own exception handler (`register_scim_exception_handlers`) registered in
`main.py` alongside (not instead of) `errors.register_exception_handlers`.
This is a real, reviewed decision (design doc section 6.1), not an
inconsistency to clean up.

Filtering is a deliberately scoped, documented subset (AC5.1): `eq`
comparisons on `userName`/`externalId` (Users) or `displayName`/`externalId`
(Groups) only, plus `startIndex`/`count` pagination - covers every real IdP's
actual usage (existence checks, correlation lookups), not the full RFC 7644
filter grammar.

Users/Groups map directly onto the existing `User`/`Team`/`TeamMembership`
tables - no new identity tables (design doc section 1.10). `POST /Users`
never reads any org-role-shaped attribute from the SCIM payload at all
(AC5.3/AC5.8) - `create_scim_user`'s signature has no `org_role` parameter,
so there is nothing to "ignore": the defense-in-depth guarantee is
structural, not a runtime check that could be forgotten. Group push creates
`TeamMembership` rows with `budget_usd=NULL` (ratified decision: unmetered,
not `$0`).

Deactivation (`PATCH active:false` or `DELETE /Users/{id}`) never deletes the
`User` row (AC5.6) - it sets `users.scim_deactivated_at` and fail-closed
auto-revokes that user's personal keys, team-attributed service-account
keys, sessions, and CLI refresh credentials
(`revoke_scim_deactivated_user_credentials`) - unlike Phase 2's ADR-4 (human
admin blocks removal while keys exist), SCIM is machine-driven with no admin
at the keyboard to resolve a block, so the ratified decision is to
auto-revoke instead of block. One `AuditEntry` per revoked credential, actor
= the `"system:scim"` sentinel (`build_system_scim_actor`, mirrors
`api.deps.require_admin`'s `"system:admin_token"` / `api/v1/auth_device.py`'s
`"system:cli_sync"` sentinel shape exactly).
"""

from __future__ import annotations

import hmac
import re
import secrets
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.cli_refresh_credential import CliRefreshCredential
from gatekey.db.models.personal_api_key import PersonalApiKey
from gatekey.db.models.scim_config import ScimConfig
from gatekey.db.models.service_account_key import ServiceAccountKey
from gatekey.db.models.session import UserSession
from gatekey.db.models.team import Team
from gatekey.db.models.user import User
from gatekey.errors import GatekeyError
from gatekey.services.audit import write_audit_entry
from gatekey.services.service_accounts import hash_secret
from gatekey.services.team_budget import create_team_membership
from gatekey.services.teams import get_membership, list_team_members, remove_team_member
from gatekey.services.teams import delete_team as _delete_team

if TYPE_CHECKING:
    from gatekey.api.deps import AdminContext

import logging

logger = logging.getLogger("gatekey")

# ---------------------------------------------------------------------------
# RFC 7644-shaped error handling (module docstring: deliberately NOT
# GatekeyError).
# ---------------------------------------------------------------------------

SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
SCIM_LIST_RESPONSE_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"


class ScimError(Exception):
    """RFC 7644 section 3.12-shaped error. Raised by every SCIM route/
    dependency (`api.deps.require_scim_token`, `api/v1/scim/*.py`) instead of
    `GatekeyError` - see module docstring."""

    def __init__(self, status_code: int, detail: str, *, scim_type: str | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.scim_type = scim_type


def scim_not_found(resource_type: str) -> ScimError:
    return ScimError(404, f"{resource_type} not found.")


def _scim_error_body(exc: ScimError) -> dict[str, Any]:
    body: dict[str, Any] = {
        "schemas": [SCIM_ERROR_SCHEMA],
        "status": str(exc.status_code),
        "detail": exc.detail,
    }
    if exc.scim_type:
        body["scimType"] = exc.scim_type
    return body


def register_scim_exception_handlers(app: FastAPI) -> None:
    """Register the `ScimError` handler on `app`. Called from `main.py`
    ALONGSIDE `errors.register_exception_handlers`, not instead of it -
    every non-SCIM route keeps the generic envelope unchanged."""

    @app.exception_handler(ScimError)
    async def _handle_scim_error(request: Request, exc: ScimError) -> JSONResponse:
        logger.info("scim_error", extra={"status_code": exc.status_code, "path": request.url.path})
        return JSONResponse(status_code=exc.status_code, content=_scim_error_body(exc))


# ---------------------------------------------------------------------------
# `system:scim` audit-actor sentinel (design doc section 6.4).
# ---------------------------------------------------------------------------

SYSTEM_SCIM_ACTOR_LABEL = "system:scim"


def build_system_scim_actor(org_id: uuid.UUID) -> "AdminContext":
    """Mirrors `api.deps.require_admin`'s `"system:admin_token"` sentinel
    (and `api/v1/auth_device.py`'s `"system:cli_sync"`) exactly - a
    lightweight `AdminContext` with no `actor_user_id` (SCIM is machine-
    driven, there is no human session on this request path). Local import to
    avoid a circular import (`api.deps` imports from this module for
    `require_scim_token`) - same deferred-import shape
    `api/v1/auth_device.py`'s `get_current_key` already uses for the
    identical reason."""
    from gatekey.api.deps import AdminContext

    return AdminContext(actor_user_id=None, actor_label=SYSTEM_SCIM_ACTOR_LABEL, org_id=org_id)


# ---------------------------------------------------------------------------
# `scim_config` (bearer-token auth + admin enable/rotate).
# ---------------------------------------------------------------------------

SCIM_TOKEN_PREFIX = "gk_scim_"
_TOKEN_ENTROPY_BYTES = 32


async def get_scim_config(session: AsyncSession, org_id: uuid.UUID = DEFAULT_ORG_ID) -> ScimConfig | None:
    return (
        await session.execute(select(ScimConfig).where(ScimConfig.org_id == org_id))
    ).scalar_one_or_none()


def scim_token_matches(config: ScimConfig | None, submitted_token: str) -> bool:
    """Constant-time bearer-token check (design doc section 6.2) - mirrors
    `api.deps._matches_break_glass_token`'s `hmac.compare_digest` discipline.

    SCIM is a single per-org secret (like `GATEKEY_ADMIN_TOKEN`), not a
    many-row lookup key like `ServiceAccountKey.secret_hash` - so this
    fetches the one candidate row (`get_scim_config`, by the caller) and
    compares in constant time, rather than doing a `WHERE bearer_token_hash =
    :hash` indexed-equality lookup (the shape `get_active_service_account_
    by_hash` uses, appropriate there because the org isn't known ahead of
    time; here it already is). Also enforces AC5.7's off-by-default toggle:
    a disabled config, or one with no token generated yet, never matches -
    same failure shape (generic False) either way, so a probing caller
    cannot distinguish "disabled" from "wrong token" from "no org
    configured".
    """
    if config is None or not config.enabled or config.bearer_token_hash is None:
        return False
    return hmac.compare_digest(hash_secret(submitted_token), config.bearer_token_hash)


async def set_scim_enabled(session: AsyncSession, *, enabled: bool) -> ScimConfig:
    """Upsert the org's `enabled` toggle, same `on_conflict_do_update` shape
    as `services.compliance_settings.set_compliance_settings`. Commits."""
    insert_stmt = postgresql.insert(ScimConfig).values(org_id=DEFAULT_ORG_ID, enabled=enabled)
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[ScimConfig.org_id],
        set_={"enabled": insert_stmt.excluded.enabled, "updated_at": func.now()},
    ).returning(ScimConfig)
    row = (await session.execute(upsert_stmt)).scalar_one()
    await session.commit()
    return row


async def rotate_scim_token(session: AsyncSession) -> tuple[ScimConfig, str]:
    """Mint a fresh bearer token, overwriting the prior one in place - AC5.2:
    no overlap window (this is an inbound credential the IdP holds, unlike
    the scheduled outbound rotations in `services.rotation`). Returns `(row,
    plaintext_token)` - the plaintext exists only in this return value, never
    persisted. Commits."""
    token = SCIM_TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
    token_hash = hash_secret(token)
    insert_stmt = postgresql.insert(ScimConfig).values(
        org_id=DEFAULT_ORG_ID, bearer_token_hash=token_hash, token_created_at=func.now()
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[ScimConfig.org_id],
        set_={
            "bearer_token_hash": insert_stmt.excluded.bearer_token_hash,
            "token_created_at": insert_stmt.excluded.token_created_at,
            "updated_at": func.now(),
        },
    ).returning(ScimConfig)
    row = (await session.execute(upsert_stmt)).scalar_one()
    await session.commit()
    return row, token


# ---------------------------------------------------------------------------
# SCIM <-> resource-shape helpers (filters, list envelopes, resource bodies).
# ---------------------------------------------------------------------------

_FILTER_RE = re.compile(r'^(\w+)\s+eq\s+"([^"]*)"$', re.IGNORECASE)


def parse_simple_eq_filter(
    filter_str: str | None, *, allowed_attributes: set[str]
) -> tuple[str, str] | None:
    """AC5.1's documented, scoped filter subset - see module docstring.
    Returns `(lowercased_attribute, value)`, or `None` for no filter. Raises
    `ScimError(400)` for anything outside the documented subset, rather than
    silently ignoring it or matching everything."""
    if not filter_str:
        return None
    match = _FILTER_RE.match(filter_str.strip())
    if not match:
        raise ScimError(
            400,
            "Unsupported filter expression - only 'eq' comparisons are supported.",
            scim_type="invalidFilter",
        )
    attribute, value = match.group(1).lower(), match.group(2)
    if attribute not in allowed_attributes:
        raise ScimError(
            400,
            f"Unsupported filter attribute '{attribute}' - only "
            f"{sorted(allowed_attributes)} are supported.",
            scim_type="invalidFilter",
        )
    return attribute, value


def list_response(resources: list[dict[str, Any]], *, total: int, start_index: int, count: int) -> dict[str, Any]:
    return {
        "schemas": [SCIM_LIST_RESPONSE_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


def user_to_scim_resource(user: User) -> dict[str, Any]:
    return {
        "schemas": [SCIM_USER_SCHEMA],
        "id": str(user.id),
        "externalId": user.scim_external_id,
        "userName": user.sso_email,
        "name": {"formatted": user.name},
        "active": user.scim_deactivated_at is None,
        "meta": {
            "resourceType": "User",
            "created": user.created_at.isoformat(),
            "lastModified": user.updated_at.isoformat(),
        },
    }


def group_to_scim_resource(team: Team, members: list[tuple[uuid.UUID, str]]) -> dict[str, Any]:
    return {
        "schemas": [SCIM_GROUP_SCHEMA],
        "id": str(team.id),
        "externalId": team.scim_external_id,
        "displayName": team.name,
        "members": [{"value": str(user_id), "display": name} for user_id, name in members],
        "meta": {
            "resourceType": "Group",
            "created": team.created_at.isoformat(),
            "lastModified": team.updated_at.isoformat(),
        },
    }


def parse_user_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract the fields `create_scim_user`/`replace_scim_user` understand
    from a SCIM User resource body (POST/PUT). AC5.3/AC5.8: deliberately
    extracts only `userName`/`externalId`/`name.formatted` (or `displayName`
    as a fallback)/`active` - any org-role-shaped custom attribute in the
    payload is simply never looked at (structural enforcement, not a runtime
    check that could be forgotten - see module docstring)."""
    user_name = payload.get("userName")
    if not user_name or not isinstance(user_name, str):
        raise ScimError(400, "'userName' is required.", scim_type="invalidValue")
    name_obj = payload.get("name")
    formatted = name_obj.get("formatted") if isinstance(name_obj, dict) else None
    display_name = formatted or payload.get("displayName") or user_name
    external_id = payload.get("externalId")
    active = payload.get("active", True)
    if not isinstance(active, bool):
        raise ScimError(400, "'active' must be a boolean.", scim_type="invalidValue")
    return {
        "user_name": user_name,
        "display_name": display_name,
        "external_id": external_id,
        "active": active,
    }


def parse_user_patch_active(operations: list[Any]) -> bool | None:
    """Scoped subset (AC5.1's documented-boundary posture): only an
    `add`/`replace` operation touching `active` is understood - either
    `{"op": "replace", "path": "active", "value": false}` or a path-less
    `{"op": "replace", "value": {"active": false}}` (some IdPs, e.g. Azure
    AD, send the latter shape). Returns `None` if no recognized
    active-changing operation is present (a no-op PATCH for this codebase's
    purposes)."""
    for op in operations:
        if not isinstance(op, dict):
            continue
        op_name = str(op.get("op", "")).lower()
        if op_name not in ("replace", "add"):
            continue
        path = op.get("path")
        value = op.get("value")
        if path is not None and str(path).lower() == "active" and isinstance(value, bool):
            return value
        if path is None and isinstance(value, dict) and isinstance(value.get("active"), bool):
            return value["active"]
    return None


def parse_group_payload(payload: dict[str, Any]) -> dict[str, Any]:
    display_name = payload.get("displayName")
    if not display_name or not isinstance(display_name, str):
        raise ScimError(400, "'displayName' is required.", scim_type="invalidValue")
    external_id = payload.get("externalId")
    member_ids: list[uuid.UUID] = []
    for entry in payload.get("members") or []:
        parsed = _parse_member_value(entry)
        if parsed is not None:
            member_ids.append(parsed)
    return {"display_name": display_name, "external_id": external_id, "member_ids": member_ids}


def _parse_member_value(entry: Any) -> uuid.UUID | None:
    raw = entry.get("value") if isinstance(entry, dict) else entry
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        return None


_MEMBER_FILTER_RE = re.compile(r'^members\[value\s+eq\s+"([^"]+)"\]$', re.IGNORECASE)


def parse_group_patch_member_ops(operations: list[Any]) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Scoped subset (AC5.1): `add`/`remove` on `members`, either a
    `path="members"` operation with a `value` array of `{"value": "<user-
    id>"}` entries, or a `remove` with a filtered path
    (`members[value eq "<user-id>"]`, no `value` body) - covers every real
    IdP's actual Group-push usage. Returns `(add_ids, remove_ids)`."""
    add_ids: list[uuid.UUID] = []
    remove_ids: list[uuid.UUID] = []
    for op in operations:
        if not isinstance(op, dict):
            continue
        op_name = str(op.get("op", "")).lower()
        path = str(op.get("path") or "").strip()
        value = op.get("value")
        if op_name == "add" and path.lower() in ("members", ""):
            for entry in value or []:
                parsed = _parse_member_value(entry)
                if parsed is not None:
                    add_ids.append(parsed)
        elif op_name == "remove":
            filtered = _MEMBER_FILTER_RE.match(path)
            if filtered:
                try:
                    remove_ids.append(uuid.UUID(filtered.group(1)))
                except ValueError:
                    pass
            elif path.lower() == "members":
                for entry in value or []:
                    parsed = _parse_member_value(entry)
                    if parsed is not None:
                        remove_ids.append(parsed)
    return add_ids, remove_ids


# ---------------------------------------------------------------------------
# User CRUD (AC5.3, AC5.5, AC5.6, AC5.8).
# ---------------------------------------------------------------------------


async def list_scim_users(
    session: AsyncSession, *, filter_str: str | None = None, start_index: int = 1, count: int = 100
) -> tuple[list[User], int]:
    stmt = select(User).where(User.org_id == DEFAULT_ORG_ID)
    parsed = parse_simple_eq_filter(filter_str, allowed_attributes={"username", "externalid"})
    if parsed is not None:
        attribute, value = parsed
        stmt = stmt.where(User.sso_email == value if attribute == "username" else User.scim_external_id == value)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    page_stmt = stmt.order_by(User.created_at).offset(max(start_index - 1, 0)).limit(count)
    rows = list((await session.execute(page_stmt)).scalars().all())
    return rows, total


async def get_scim_user(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    return (
        await session.execute(select(User).where(User.org_id == DEFAULT_ORG_ID, User.id == user_id))
    ).scalar_one_or_none()


async def create_scim_user(
    session: AsyncSession, *, user_name: str, display_name: str, external_id: str | None
) -> User:
    """AC5.3: `User(org_id, name, sso_email, scim_external_id, org_role=NULL)`
    - `org_role` is never a parameter here, so there is nothing to read from
    the SCIM payload at all (AC5.8, structural enforcement). Flushes; the
    caller writes its own audit entry and commits."""
    user = User(
        org_id=DEFAULT_ORG_ID,
        name=display_name,
        sso_email=user_name,
        scim_external_id=external_id,
        org_role=None,
        budget_usd=None,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ScimError(
            409, "A user with this externalId already exists.", scim_type="uniqueness"
        ) from None
    return user


async def revoke_scim_deactivated_user_credentials(
    session: AsyncSession, user_id: uuid.UUID, *, actor: "AdminContext", source_ip: str | None = None
) -> list[str]:
    """The ratified deactivation cascade (design doc section 6.4, extended):
    revokes every active personal key, team-attributed service-account key
    (`team_id IS NOT NULL`), session, and CLI refresh credential owned by
    `user_id`. One `AuditEntry` per revoked credential, actor = the
    `"system:scim"` sentinel.

    Idempotent by construction: each credential type is revoked via a single
    `UPDATE ... WHERE revoked_at IS NULL ... RETURNING id` - a second call
    (e.g. a duplicate `PATCH active:false` push) finds zero already-revoked
    rows and writes zero further audit entries, same "second call is a
    no-op" contract as `services.sessions.revoke_session`/`services.
    service_accounts.revoke_service_account`.

    Returns the list of action names actually written (for tests).
    """
    actions_taken: list[str] = []

    personal_key_ids = (
        await session.execute(
            update(PersonalApiKey)
            .where(PersonalApiKey.owner_user_id == user_id, PersonalApiKey.revoked_at.is_(None))
            .values(revoked_at=func.now())
            .returning(PersonalApiKey.id)
        )
    ).scalars().all()
    for key_id in personal_key_ids:
        await write_audit_entry(
            session,
            actor=actor,
            action="personal_key.revoke",
            target_type="personal_api_key",
            target_id=str(key_id),
            old_value={"active": True},
            new_value={"active": False},
            source_ip=source_ip,
        )
        actions_taken.append("personal_key.revoke")

    sa_key_ids = (
        await session.execute(
            update(ServiceAccountKey)
            .where(
                ServiceAccountKey.user_id == user_id,
                ServiceAccountKey.team_id.is_not(None),
                ServiceAccountKey.revoked_at.is_(None),
            )
            .values(revoked_at=func.now())
            .returning(ServiceAccountKey.id)
        )
    ).scalars().all()
    for key_id in sa_key_ids:
        await write_audit_entry(
            session,
            actor=actor,
            action="service_account_key.revoke",
            target_type="service_account_key",
            target_id=str(key_id),
            old_value={"active": True},
            new_value={"active": False},
            source_ip=source_ip,
        )
        actions_taken.append("service_account_key.revoke")

    session_ids = (
        await session.execute(
            update(UserSession)
            .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=func.now())
            .returning(UserSession.id)
        )
    ).scalars().all()
    for session_id in session_ids:
        await write_audit_entry(
            session,
            actor=actor,
            action="session.revoke",
            target_type="session",
            target_id=str(session_id),
            old_value={"active": True},
            new_value={"active": False},
            source_ip=source_ip,
        )
        actions_taken.append("session.revoke")

    # `cli_refresh_credentials` (Phase 3, BD-25) - landed alongside this
    # track, so this is wired in directly rather than skipped/flagged. A live
    # refresh credential can mint arbitrary future personal keys, so leaving
    # it active after deactivation would silently defeat the rest of this
    # cascade (design doc section 6.4's extension of ratified decision #8).
    cli_credential_ids = (
        await session.execute(
            update(CliRefreshCredential)
            .where(CliRefreshCredential.user_id == user_id, CliRefreshCredential.revoked_at.is_(None))
            .values(revoked_at=func.now())
            .returning(CliRefreshCredential.id)
        )
    ).scalars().all()
    for credential_id in cli_credential_ids:
        await write_audit_entry(
            session,
            actor=actor,
            action="cli_refresh_credential.revoke",
            target_type="cli_refresh_credential",
            target_id=str(credential_id),
            old_value={"active": True},
            new_value={"active": False},
            source_ip=source_ip,
        )
        actions_taken.append("cli_refresh_credential.revoke")

    return actions_taken


async def set_scim_user_active(
    session: AsyncSession, user: User, *, active: bool, actor: "AdminContext", source_ip: str | None = None
) -> User:
    """`PATCH active:<bool>` / `DELETE /Users/{id}` (which calls this with
    `active=False`) share this one implementation. Reactivation only clears
    the block flag - the design doc defines no "reinstatement" cascade
    (nothing to un-revoke; the user is issued new credentials the normal
    way, same as any other newly-provisioned member)."""
    was_active = user.scim_deactivated_at is None
    if active:
        if not was_active:
            user.scim_deactivated_at = None
            await session.flush()
        # QA fix: `updated_at` (`onupdate=func.now()`) is expired by ANY
        # ORM-flushed UPDATE to this row - including one that already
        # happened in an outer caller (`replace_scim_user`'s own
        # userName/name/externalId flush) before this function was even
        # called. Left expired, the route handler's later synchronous
        # `user_to_scim_resource(user)` call (after `session.commit()`,
        # outside any awaited/greenlet context) raises
        # `sqlalchemy.exc.MissingGreenlet` - a real 500 on every SCIM
        # PATCH/PUT/DELETE that actually changes the row, caught by the
        # QA integration test round-tripping through the real router+DB
        # (`tests/integration/test_scim_e2e.py`), not by the unit tests
        # against a fake session. Refreshing here - the one place every
        # active-state-affecting route (PATCH/PUT/DELETE) funnels through -
        # closes it once for all of them, root-cause rather than
        # per-caller.
        await session.refresh(user)
        return user
    if was_active:
        user.scim_deactivated_at = datetime.now(timezone.utc)
        await session.flush()
    await revoke_scim_deactivated_user_credentials(session, user.id, actor=actor, source_ip=source_ip)
    await session.refresh(user)
    return user


async def replace_scim_user(
    session: AsyncSession,
    user: User,
    *,
    user_name: str,
    display_name: str,
    external_id: str | None,
    active: bool,
    actor: "AdminContext",
    source_ip: str | None = None,
) -> User:
    """`PUT /Users/{id}` - full replace of the mapped fields, then delegates
    the `active` transition to `set_scim_user_active` (same cascade either
    way active changes)."""
    user.sso_email = user_name
    user.name = display_name
    user.scim_external_id = external_id
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ScimError(
            409, "A user with this externalId already exists.", scim_type="uniqueness"
        ) from None
    return await set_scim_user_active(session, user, active=active, actor=actor, source_ip=source_ip)


# ---------------------------------------------------------------------------
# Group CRUD (AC5.1, AC5.4).
# ---------------------------------------------------------------------------


async def list_scim_groups(
    session: AsyncSession, *, filter_str: str | None = None, start_index: int = 1, count: int = 100
) -> tuple[list[Team], int]:
    stmt = select(Team).where(Team.org_id == DEFAULT_ORG_ID)
    parsed = parse_simple_eq_filter(filter_str, allowed_attributes={"displayname", "externalid"})
    if parsed is not None:
        attribute, value = parsed
        stmt = stmt.where(Team.name == value if attribute == "displayname" else Team.scim_external_id == value)
    total = (await session.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    page_stmt = stmt.order_by(Team.created_at).offset(max(start_index - 1, 0)).limit(count)
    rows = list((await session.execute(page_stmt)).scalars().all())
    return rows, total


async def get_scim_group(session: AsyncSession, team_id: uuid.UUID) -> Team | None:
    return (
        await session.execute(select(Team).where(Team.org_id == DEFAULT_ORG_ID, Team.id == team_id))
    ).scalar_one_or_none()


async def create_scim_group(session: AsyncSession, *, display_name: str, external_id: str | None) -> Team:
    team = Team(org_id=DEFAULT_ORG_ID, name=display_name, scim_external_id=external_id)
    session.add(team)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise ScimError(
            409, "A group with this displayName or externalId already exists.", scim_type="uniqueness"
        ) from None
    return team


async def add_scim_group_members(
    session: AsyncSession,
    team: Team,
    member_user_ids: list[uuid.UUID],
    *,
    actor: "AdminContext",
    source_ip: str | None = None,
) -> Team:
    """Ratified decision: `budget_usd=NULL` on create (unmetered, not `$0`) -
    delegates to `services.team_budget.create_team_membership`, whose
    headroom check is a no-op for a `None` request (module docstring).
    Idempotent: a user already a member is silently skipped, no error, no
    duplicate audit entry."""
    for user_id in member_user_ids:
        if await get_membership(session, team_id=team.id, user_id=user_id) is not None:
            continue
        try:
            membership = await create_team_membership(session, team_id=team.id, user_id=user_id, budget_usd=None)
        except IntegrityError:
            raise ScimError(
                400, "One or more member values reference an unknown user.", scim_type="invalidValue"
            ) from None
        await write_audit_entry(
            session,
            actor=actor,
            action="scim_group.member.add",
            target_type="team_membership",
            target_id=str(membership.id),
            old_value=None,
            new_value={"team_id": str(team.id), "user_id": str(user_id)},
            source_ip=source_ip,
        )
    return team


async def remove_scim_group_members(
    session: AsyncSession,
    team: Team,
    member_user_ids: list[uuid.UUID],
    *,
    actor: "AdminContext",
    source_ip: str | None = None,
) -> Team:
    """Reuses `services.teams.remove_team_member` unchanged, including its
    ADR-4 active-key removal gate (translated to a SCIM 409 here) - this is
    plain group-membership removal, not the fail-closed full-deprovisioning
    cascade `revoke_scim_deactivated_user_credentials` implements for
    `DELETE /Users/{id}`; those are deliberately different operations.
    Idempotent: removing a user who isn't a member is a silent no-op."""
    for user_id in member_user_ids:
        try:
            membership = await remove_team_member(session, team_id=team.id, user_id=user_id)
        except GatekeyError as exc:
            if exc.status_code == 404:
                continue
            raise ScimError(409, exc.message, scim_type=None) from None
        await write_audit_entry(
            session,
            actor=actor,
            action="scim_group.member.remove",
            target_type="team_membership",
            target_id=str(membership.id),
            old_value={"team_id": str(team.id), "user_id": str(user_id)},
            new_value=None,
            source_ip=source_ip,
        )
    return team


async def replace_scim_group_members(
    session: AsyncSession,
    team: Team,
    target_user_ids: list[uuid.UUID],
    *,
    actor: "AdminContext",
    source_ip: str | None = None,
) -> Team:
    """`PUT /Groups/{id}` - full-replace membership diff (add what's missing,
    remove what's extra)."""
    current_ids = {membership.user_id for membership, _user in await list_team_members(session, team.id)}
    target_ids = set(target_user_ids)
    to_add = list(target_ids - current_ids)
    to_remove = list(current_ids - target_ids)
    if to_add:
        await add_scim_group_members(session, team, to_add, actor=actor, source_ip=source_ip)
    if to_remove:
        await remove_scim_group_members(session, team, to_remove, actor=actor, source_ip=source_ip)
    return team


async def delete_scim_group(
    session: AsyncSession, team: Team, *, actor: "AdminContext", source_ip: str | None = None
) -> None:
    """`DELETE /Groups/{id}` - delegates to `services.teams.delete_team`
    unchanged (its own `team_has_members`/`team_has_join_requests` 409 gates
    apply, translated to a SCIM error here)."""
    old_value = {"name": team.name, "scim_external_id": team.scim_external_id}
    team_id = team.id
    try:
        await _delete_team(session, team)
    except GatekeyError as exc:
        raise ScimError(409, exc.message, scim_type=None) from None
    await write_audit_entry(
        session,
        actor=actor,
        action="scim_group.delete",
        target_type="team",
        target_id=str(team_id),
        old_value=old_value,
        new_value=None,
        source_ip=source_ip,
    )
