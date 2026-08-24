"""Admin endpoints for credential rotation policy (Phase 3, BD-15) - design
doc section 9.6.

`require_role(org_admin)` throughout, matching the design contract. Three
routers in one module (org-default, provider-key rotation-policy/guided
rotate) - the per-service-account-key routes (`GET/PUT /v1/admin/keys/{id}/
rotation-policy`, `POST /v1/admin/keys/{id}/rotate-now`) live in
`api/v1/keys.py`'s existing `admin_router` instead, alongside every other
per-key admin mutation (regenerate/revoke) - not duplicated here.
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import (
    get_key_provider,
    get_source_ip,
    get_validator_registry,
    require_role,
)
from gatekey.db.models.provider_key import ProviderName
from gatekey.db.session import get_db_session
from gatekey.errors import GatekeyError, NotFoundError
from gatekey.providers.base import ProviderValidator
from gatekey.schemas.rotation_policy import (
    ProviderKeyRotateRequest,
    RotationPolicyPutRequest,
    RotationPolicyResponse,
)
from gatekey.services.audit import write_audit_entry
from gatekey.services.encryption import KeyProvider
from gatekey.services.provider_keys import (
    InvalidProviderKeyError,
    ProviderUnreachableError,
    ProviderValidationUnknownError,
    get_key,
    rotate_provider_key,
)
from gatekey.services.rotation import deliver_provider_key_rotation_notification
from gatekey.db.models.rotation_policy import RotationPolicy
from gatekey.services.rotation_policy import (
    get_org_rotation_policy,
    get_provider_key_rotation_policy,
    set_org_rotation_policy,
    set_provider_key_rotation_policy,
)
from gatekey.services.sessions import SessionContext

org_router = APIRouter(prefix="/v1/admin/rotation-policy", tags=["admin", "rotation"])
provider_router = APIRouter(prefix="/v1/admin/provider-keys", tags=["admin", "rotation"])

_DISABLED_ORG_DEFAULT = RotationPolicyResponse(
    enabled=False,
    interval_days=None,
    rotate_at_local_time=None,
    overlap_buffer_minutes=5,
    next_rotation_at=None,
    last_rotated_at=None,
    mode="automatic",
)
_DISABLED_PROVIDER_KEY_DEFAULT = RotationPolicyResponse(
    enabled=False,
    interval_days=None,
    rotate_at_local_time=None,
    overlap_buffer_minutes=5,
    next_rotation_at=None,
    last_rotated_at=None,
    mode="manual_guided",
)


# --- org-wide default (AC7.2: disabled by default) ----------------------------


@org_router.get("", response_model=RotationPolicyResponse)
async def get_org_rotation_policy_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> RotationPolicyResponse:
    row = await get_org_rotation_policy(session)
    return RotationPolicyResponse.model_validate(row) if row else _DISABLED_ORG_DEFAULT


@org_router.put("", response_model=RotationPolicyResponse)
async def put_org_rotation_policy_endpoint(
    payload: RotationPolicyPutRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> RotationPolicyResponse:
    if payload.enabled and payload.interval_days is None:
        raise GatekeyError(
            "interval_days is required when enabling the org-wide rotation default.",
            code="rotation_interval_required",
            status_code=422,
        )
    old = await get_org_rotation_policy(session)
    old_response = RotationPolicyResponse.model_validate(old) if old else _DISABLED_ORG_DEFAULT
    row = await set_org_rotation_policy(
        session,
        enabled=payload.enabled,
        interval_days=payload.interval_days,
        rotate_at_local_time=payload.rotate_at_local_time,
        overlap_buffer_minutes=payload.overlap_buffer_minutes,
    )
    await write_audit_entry(
        session,
        actor=ctx,
        action="rotation_policy.update",
        target_type="rotation_policy",
        target_id="org",
        old_value=old_response.model_dump(mode="json"),
        new_value=payload.model_dump(mode="json"),
        source_ip=source_ip,
    )
    await session.commit()
    return RotationPolicyResponse.model_validate(row)


# --- provider-key rotation policy + guided rotate (AC7.7) ---------------------


def _provider_key_response(row: object | None) -> RotationPolicyResponse:
    if row is None:
        return _DISABLED_PROVIDER_KEY_DEFAULT
    return RotationPolicyResponse.model_validate(row)


@provider_router.get("/{provider}/rotation-policy", response_model=RotationPolicyResponse)
async def get_provider_key_rotation_policy_endpoint(
    provider: ProviderName,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
) -> RotationPolicyResponse:
    key = await get_key(session, provider.value)
    if key is None:
        raise NotFoundError(f"No key configured for provider '{provider.value}'.")
    return _provider_key_response(await get_provider_key_rotation_policy(session, key.id))


@provider_router.put("/{provider}/rotation-policy", response_model=RotationPolicyResponse)
async def put_provider_key_rotation_policy_endpoint(
    provider: ProviderName,
    payload: RotationPolicyPutRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> RotationPolicyResponse:
    key = await get_key(session, provider.value)
    if key is None:
        raise NotFoundError(f"No key configured for provider '{provider.value}'.")
    if payload.enabled and payload.interval_days is None:
        raise GatekeyError(
            "interval_days is required when enabling rotation reminders for this provider key.",
            code="rotation_interval_required",
            status_code=422,
        )
    old = _provider_key_response(await get_provider_key_rotation_policy(session, key.id))
    row = await set_provider_key_rotation_policy(
        session,
        provider_key_id=key.id,
        enabled=payload.enabled,
        interval_days=payload.interval_days,
        overlap_buffer_minutes=payload.overlap_buffer_minutes,
    )
    await write_audit_entry(
        session,
        actor=ctx,
        action="provider_key.rotation_policy.update",
        target_type="provider_key",
        target_id=str(key.id),
        old_value=old.model_dump(mode="json"),
        new_value=payload.model_dump(mode="json"),
        source_ip=source_ip,
    )
    await session.commit()
    return RotationPolicyResponse.model_validate(row)


@provider_router.post("/{provider}/rotate", response_model=RotationPolicyResponse)
async def rotate_provider_key_endpoint(
    provider: ProviderName,
    payload: ProviderKeyRotateRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    validator_registry: dict[str, ProviderValidator] = Depends(get_validator_registry),
    key_provider: KeyProvider = Depends(get_key_provider),
    source_ip: str | None = Depends(get_source_ip),
) -> RotationPolicyResponse:
    """AC7.7's guided flow: paste new key -> validate live against the
    provider (same three structured error states as `PUT .../key`) ->
    on success, overlap-swap with a FIXED short buffer (never
    access-schedule-anchored - a provider key backs potentially many
    teams/apps at once). 404 if no key is currently configured (guided
    rotation presupposes an existing key)."""
    policy = await get_provider_key_rotation_policy_for_provider(session, provider.value)
    overlap_buffer_minutes = policy.overlap_buffer_minutes if policy else 5

    try:
        provider_key = await rotate_provider_key(
            session,
            provider.value,
            payload.payload,
            overlap_buffer_minutes=overlap_buffer_minutes,
            validator_registry=validator_registry,
            key_provider=key_provider,
        )
    except InvalidProviderKeyError as exc:
        raise GatekeyError(exc.message, code="invalid_key", status_code=422) from None
    except ProviderUnreachableError as exc:
        raise GatekeyError(exc.message, code="provider_unreachable", status_code=502) from None
    except ProviderValidationUnknownError as exc:
        raise GatekeyError(exc.message, code="unknown_error", status_code=500) from None

    if provider_key is None:
        raise NotFoundError(
            f"No key configured for provider '{provider.value}' - use "
            "PUT /v1/admin/providers/{provider}/key for first-time setup."
        )

    await write_audit_entry(
        session,
        actor=ctx,
        action="provider_key.rotate",
        target_type="provider_key",
        target_id=str(provider_key.id),
        old_value={"provider": provider.value},
        new_value={"provider": provider.value, "rotated": True},
        source_ip=source_ip,
    )
    await session.commit()

    # `previous_valid_until` is nullable in general (a never-rotated key has
    # none) but `rotate_provider_key` unconditionally sets it as part of
    # every successful rotation (services/provider_keys.py) - guaranteed
    # non-None on the `provider_key` this function just got back.
    assert provider_key.previous_valid_until is not None
    background_tasks.add_task(
        deliver_provider_key_rotation_notification,
        request.app,
        provider=provider.value,
        rotated_at=provider_key.updated_at,
        overlap_expires_at=provider_key.previous_valid_until,
    )
    return _provider_key_response(await get_provider_key_rotation_policy(session, provider_key.id))


async def get_provider_key_rotation_policy_for_provider(
    session: AsyncSession, provider: str
) -> RotationPolicy | None:
    key = await get_key(session, provider)
    if key is None:
        return None
    return await get_provider_key_rotation_policy(session, key.id)
