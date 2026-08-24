"""CLI-sync device-code auth flow + `GET /v1/me/current-key` (Phase 3,
BD-25) - design doc section 8.2 and API contract section 9.8.

Four routes, one module (mirrors `api/v1/auth.py`'s SSO-flow grouping):

- `POST /v1/auth/device/start` (no auth) - CLI-initiated, mints a pending
  `DeviceAuthRecord`.
- `POST /v1/auth/device/approve` (session auth) - browser-initiated once the
  user is logged in and confirms the `user_code`; mints the bound
  `PersonalApiKey` + `cli_refresh_credentials` row.
- `POST /v1/auth/device/poll` (no auth) - CLI-initiated, delivers the
  plaintext refresh credential exactly once.
- `GET /v1/me/current-key` (refresh-credential auth) - rotates + returns the
  bound personal key's plaintext (fork #3).

Every mutation writes exactly one `AuditEntry` in the same DB transaction,
same discipline as every other mutating route in this codebase.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import (
    AdminContext,
    CliRefreshCredentialContext,
    get_source_ip,
    require_cli_refresh_credential,
)
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.session import get_db_session
from gatekey.errors import NotFoundError
from gatekey.schemas.cli_sync import (
    CurrentKeyResponse,
    DeviceApproveRequest,
    DeviceApproveResponse,
    DevicePollRequest,
    DevicePollResponse,
    DeviceStartRequest,
    DeviceStartResponse,
)
from gatekey.services.audit import write_audit_entry
from gatekey.services.cli_refresh_credentials import (
    DEFAULT_DEVICE_CODE_TTL_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    compute_current_key_valid_until,
    create_cli_refresh_credential,
    resolve_team_id_for_device_approval,
)
from gatekey.services.personal_keys import create_personal_key, get_personal_key, regenerate_personal_key
from gatekey.services.sessions import SessionContext, get_current_session

router = APIRouter(prefix="/v1/auth/device", tags=["auth", "cli-sync"])
me_router = APIRouter(prefix="/v1/me", tags=["me", "cli-sync"])

# One generic, anti-enumeration message for "unknown or expired code" -
# never distinguishes the two, and reused for both the user_code (approve)
# and device_code (poll) lookup-failure paths (same posture as every other
# lookup-failure rejection in this codebase).
_UNKNOWN_OR_EXPIRED_CODE = "Unknown or expired device code."


def _device_store(request: Request):
    return request.app.state.device_auth_store


@router.post("/start", response_model=DeviceStartResponse)
async def start_device_auth(
    request: Request, payload: DeviceStartRequest | None = None
) -> DeviceStartResponse:
    """No auth (AC8a.2) - the CLI has no identity yet at this point.
    `payload` (and its `device_label`) is entirely optional (added by
    `0047`) - an older CLI-sync client sending no body at all still
    works unchanged."""
    settings = request.app.state.settings
    device_label = payload.device_label if payload is not None else None
    record = _device_store(request).start(
        ttl_seconds=DEFAULT_DEVICE_CODE_TTL_SECONDS, device_label=device_label
    )
    verification_uri = f"{settings.GATEKEY_FRONTEND_ORIGIN.rstrip('/')}/device"
    return DeviceStartResponse(
        device_code=record.device_code,
        user_code=record.user_code,
        verification_uri=verification_uri,
        expires_in=DEFAULT_DEVICE_CODE_TTL_SECONDS,
        interval=DEFAULT_POLL_INTERVAL_SECONDS,
    )


@router.post("/approve", response_model=DeviceApproveResponse)
async def approve_device_auth(
    payload: DeviceApproveRequest,
    request: Request,
    ctx: SessionContext = Depends(get_current_session),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> DeviceApproveResponse:
    """Session auth (AC8a.2) - the user is already logged into the console
    and confirms the `user_code` shown by the CLI. Mints a fresh
    `PersonalApiKey` (named after the device hint) + a bound
    `cli_refresh_credentials` row. The plaintext refresh credential is
    handed to the STORE for one-time delivery via `poll`, never returned
    here - the approving browser is not the device that needs it."""
    store = _device_store(request)
    if not store.is_pending(user_code=payload.user_code):
        raise NotFoundError(_UNKNOWN_OR_EXPIRED_CODE)
    # Added by `0047` - self-reported by the CLI at `start()` time, if at
    # all; `None` for an older CLI-sync client or one that opted not to
    # send it.
    device_label = store.get_device_label(user_code=payload.user_code)

    user_id = ctx.require_user_id()
    team_id = await resolve_team_id_for_device_approval(
        session, user_id=user_id, requested_team_id=payload.team_id
    )

    key_row, _key_secret = await create_personal_key(
        session,
        owner_user_id=user_id,
        created_by_user_id=user_id,
        team_id=team_id,
        name=f"CLI Sync ({payload.user_code})",
        expires_at=None,
        device_label=device_label,
    )
    await write_audit_entry(
        session,
        actor=ctx,
        action="personal_key.create",
        target_type="personal_api_key",
        target_id=str(key_row.id),
        old_value=None,
        new_value={
            "name": key_row.name,
            "owner_user_id": key_row.owner_user_id,
            "team_id": key_row.team_id,
            "key_prefix": key_row.key_prefix,
            "device_label": key_row.device_label,
        },
        source_ip=source_ip,
    )

    credential_row, credential_secret = await create_cli_refresh_credential(
        session,
        org_id=DEFAULT_ORG_ID,
        user_id=user_id,
        bound_personal_key_id=key_row.id,
    )
    await write_audit_entry(
        session,
        actor=ctx,
        action="cli_refresh_credential.create",
        target_type="cli_refresh_credential",
        target_id=str(credential_row.id),
        old_value=None,
        new_value={"bound_personal_key_id": str(key_row.id)},
        source_ip=source_ip,
    )
    await session.commit()

    approved = store.approve(
        user_code=payload.user_code, refresh_credential_plaintext=credential_secret
    )
    if not approved:
        # The pending request expired/vanished between the existence check
        # above and here (a real, if narrow, race) - the DB writes above
        # already committed, so the credential is valid, it's simply
        # undeliverable via this device-code handshake. Surfacing 404 here
        # (rather than silently succeeding) tells the browser the CLI will
        # never see it, so the user knows to retry the whole flow.
        raise NotFoundError(_UNKNOWN_OR_EXPIRED_CODE)
    return DeviceApproveResponse()


@router.post("/poll", response_model=DevicePollResponse)
async def poll_device_auth(
    payload: DevicePollRequest, request: Request, response: Response
) -> DevicePollResponse:
    """No auth (AC8a.2) - polled by the CLI at the `interval` `start`
    returned. `202` while pending, `200` + the plaintext refresh credential
    once approved (design doc section 9.8's exact contract) - `response.
    status_code` is overridden per-branch so the one `DevicePollResponse`
    shape still gets FastAPI's normal validation/OpenAPI docs."""
    outcome, plaintext = _device_store(request).poll(device_code=payload.device_code)
    if outcome == "not_found" or outcome == "expired":
        raise NotFoundError(_UNKNOWN_OR_EXPIRED_CODE)
    if outcome == "pending":
        response.status_code = 202
        return DevicePollResponse(status="pending")
    return DevicePollResponse(status="approved", refresh_credential=plaintext)


@me_router.get("/current-key", response_model=CurrentKeyResponse)
async def get_current_key(
    ctx: CliRefreshCredentialContext = Depends(require_cli_refresh_credential),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> CurrentKeyResponse:
    """Fork #3 (design doc section 8.2, section 10 item 3): EVERY call
    rotates the bound personal key via the same `regenerate_personal_key`
    `POST /v1/keys/{id}/regenerate` already uses - never a re-fetchable
    cached plaintext. `valid_until` currently always resolves to the
    `services.cli_refresh_credentials.DEFAULT_CURRENT_KEY_TTL` fallback (no
    personal-key rotation-policy scope exists this phase - see
    `compute_current_key_valid_until`'s docstring)."""
    key_row = await get_personal_key(session, ctx.bound_personal_key_id)
    if key_row is None:
        raise NotFoundError("Bound personal key no longer exists.")

    old_prefix = key_row.key_prefix
    key_row, secret = await regenerate_personal_key(session, key_row)

    # Lightweight actor shape for the audit write - same pattern
    # `api/v1/gateway/common.py`'s `check_residency()`/`run_dlp_scan()` use
    # for a gateway-authenticated (non-session) caller (see
    # `services/audit.py`'s module docstring).
    actor = AdminContext(
        actor_user_id=ctx.user_id, actor_label="system:cli_sync", org_id=ctx.org_id
    )
    await write_audit_entry(
        session,
        actor=actor,
        action="personal_key.regenerate",
        target_type="personal_api_key",
        target_id=str(key_row.id),
        old_value={"key_prefix": old_prefix},
        new_value={"key_prefix": key_row.key_prefix},
        source_ip=source_ip,
    )
    await session.commit()

    valid_until = compute_current_key_valid_until(
        now=datetime.now(timezone.utc), rotation_next_rotation_at=None
    )
    return CurrentKeyResponse(secret=secret, valid_until=valid_until)
