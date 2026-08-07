"""Admin endpoints addressing individual `ProviderKey` ROWS by their own
`id` (Phase 4, Reliability & Cost Efficiency - AC4.1.6/AC4.1.7, technical
design section 6.2).

`POST /v1/admin/provider-keys/{id}/health` - the technical design's exact
endpoint name (`/api/v1/provider-keys/{id}/health`, section 3.1/6.2), a
distinct prefix from `api/v1/admin/providers.py`'s `/v1/admin/providers`
(that router addresses a `ProviderKey` by `{provider}` name only - the
single-primary-key-per-provider case; this one addresses any specific key
row by its own `id`, needed now that Phase 4 allows multiple keys per
provider). Reuses `services.provider_key_health.refresh_single_provider_
key_health` (extracted from the scheduled sweep's loop body specifically so
this endpoint and the 5-minute scheduled job never implement health-check
logic twice - see that function's docstring).

`GET /v1/admin/provider-keys` (optional `?provider=` filter) - AC4.1.7's
missing list surface: `GET /v1/admin/providers` (the sibling router) is a
one-ROW-PER-PROVIDER aggregate view with no `id`/`label`/health fields;
this is the one-row-per-KEY view the admin console's "Provider Keys" screen
and its per-key "Check now" button (the `POST .../health` route above)
actually need. Reuses `services.provider_keys.list_keys_for_provider`/
`list_keys` (already existed, was never wired to a route) - no new DB
plumbing. Response fields are exactly `ProviderKeyListItemResponse`'s (id,
provider, label, is_primary, backup_group_id, health_status,
last_health_check, last_error, availability_24h) - never ciphertext/nonce/
auth_tag/plaintext, matching the redaction discipline `schemas/
provider_key.py`'s module docstring already established for `GET
/v1/admin/providers`.

`PUT /v1/admin/provider-keys/{id}/failover-config` (QA/security review
finding, fixed here - Fix 1): `services.provider_keys.set_failover_config()`
(writes `ProviderKey.failover_enabled`/`failover_target_id`, including the
AC4.1.9 same-provider - a key is provider-scoped, never model-scoped, so
"same model(s) as the primary" collapses to "same provider" at this layer -
constraint already enforced by that function) had ZERO callers anywhere
under `src/gatekey/api/` before this endpoint: no admin surface ever let an
Org Admin actually enable the reactive failover retry
`api.v1.gateway.common.call_provider_with_failover()` implements, making the
entire §4.1 headline feature inert in production despite being proven
correct in isolation by unit tests. Reuses `set_failover_config()`'s
existing validation (`FailoverTargetInvalidError` - 422 - if
`failover_target_id` isn't a real, different key for the SAME provider)
rather than reimplementing it here. Org Admin only (`require_admin`,
router-level - matches every other route in this router/`providers.py`).
"""

from __future__ import annotations

import time
import uuid

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import get_key_provider, require_admin
from gatekey.db.models.provider_key import ProviderName
from gatekey.db.session import get_db_session
from gatekey.errors import NotFoundError
from gatekey.schemas.provider_key import ProviderKeyListItemResponse
from gatekey.services import provider_key_health
from gatekey.services import provider_keys as provider_keys_service
from gatekey.services.encryption import KeyProvider
from gatekey.services.provider_keys import ProviderKeyNotFoundError

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin", "provider_keys", "health"],
    dependencies=[Depends(require_admin)],
)


@router.get("/provider-keys", response_model=list[ProviderKeyListItemResponse])
async def list_provider_keys_endpoint(
    provider: ProviderName | None = Query(default=None),
    session: AsyncSession = Depends(get_db_session),
) -> list[ProviderKeyListItemResponse]:
    """Every individual `ProviderKey` row for the default org (every label,
    every provider) - or just one provider's rows if `?provider=` is given.
    Never includes ciphertext/nonce/auth_tag/plaintext - see module
    docstring."""
    if provider is not None:
        rows = await provider_keys_service.list_keys_for_provider(session, provider.value)
    else:
        rows = await provider_keys_service.list_keys(session)
    return [ProviderKeyListItemResponse.model_validate(row) for row in rows]


class ProviderKeyHealthCheckResponse(BaseModel):
    status: str  # "ok" | "error" - see technical design section 6.2
    latency_ms: int
    error: str | None


@router.post("/provider-keys/{key_id}/health", response_model=ProviderKeyHealthCheckResponse)
async def check_provider_key_health_endpoint(
    key_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    key_provider: KeyProvider = Depends(get_key_provider),
) -> ProviderKeyHealthCheckResponse:
    key = await provider_keys_service.get_key_by_id(session, key_id)
    if key is None:
        raise NotFoundError(f"No provider key found with id '{key_id}'.")

    health_store = request.app.state.shared_state_store
    started = time.monotonic()
    health_status, error_message = await provider_key_health.refresh_single_provider_key_health(
        session, health_store, key, key_provider=key_provider, timeout_seconds=3.0
    )
    latency_ms = int((time.monotonic() - started) * 1000)

    return ProviderKeyHealthCheckResponse(
        status="ok" if health_status == "healthy" else "error",
        latency_ms=latency_ms,
        error=error_message,
    )


class ProviderKeyFailoverConfigRequest(BaseModel):
    """Request body for `PUT /v1/admin/provider-keys/{id}/failover-config`
    (Fix 1 - see module docstring). `failover_target_id=null` clears the
    configured target (equivalent to disabling failover for this key, even
    if `failover_enabled` is left `true`, since `call_provider_with_
    failover()`'s retry path is gated on both being set - see that
    function's own docstring)."""

    failover_enabled: bool
    failover_target_id: uuid.UUID | None = None


class ProviderKeyFailoverConfigResponse(BaseModel):
    """Response schema for the failover-config endpoint - deliberately the
    same narrow, no-secret-material discipline as `ProviderKeyListItemResponse`."""

    id: uuid.UUID
    provider: str
    label: str
    failover_enabled: bool
    failover_target_id: uuid.UUID | None


def _failover_config_response(key: provider_keys_service.ProviderKey) -> ProviderKeyFailoverConfigResponse:
    provider_value = key.provider.value if hasattr(key.provider, "value") else key.provider
    return ProviderKeyFailoverConfigResponse(
        id=key.id,
        provider=provider_value,
        label=key.label,
        failover_enabled=key.failover_enabled,
        failover_target_id=key.failover_target_id,
    )


@router.put("/provider-keys/{key_id}/failover-config", response_model=ProviderKeyFailoverConfigResponse)
async def set_provider_key_failover_config_endpoint(
    key_id: uuid.UUID,
    payload: ProviderKeyFailoverConfigRequest = Body(...),
    session: AsyncSession = Depends(get_db_session),
) -> ProviderKeyFailoverConfigResponse:
    """Enable/configure reactive failover for one specific `ProviderKey` row
    (Fix 1 - see module docstring): the admin-console surface for `services.
    provider_keys.set_failover_config()`, which previously had no caller
    anywhere under `src/gatekey/api/`.

    404 if `key_id` doesn't exist. 422 `failover_target_invalid` (passed
    straight through from `set_failover_config()`) if `failover_target_id`
    is the key's own id, or doesn't reference an existing key for the SAME
    provider - no DB write in that case.
    """
    key = await provider_keys_service.get_key_by_id(session, key_id)
    if key is None:
        raise NotFoundError(f"No provider key found with id '{key_id}'.")

    provider_value = key.provider.value if hasattr(key.provider, "value") else key.provider
    try:
        updated = await provider_keys_service.set_failover_config(
            session,
            provider_value,
            key_id,
            failover_enabled=payload.failover_enabled,
            failover_target_id=payload.failover_target_id,
        )
    except ProviderKeyNotFoundError:
        # Unreachable in practice (`key` was already confirmed to exist
        # above), but `set_failover_config()` can raise this - handled for
        # completeness/a theoretical TOCTOU delete between the two calls.
        raise NotFoundError(f"No provider key found with id '{key_id}'.") from None

    return _failover_config_response(updated)
