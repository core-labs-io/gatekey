"""Admin endpoints for self-hosted inference-endpoint governance (Phase 5 -
Differentiators, 5.5 Unified Governance for BYOK + Self-Hosted OSS Models).
See `gatekey/phase-5-product-spec.md` AC5.5.1/AC5.5.3/AC5.5.9 and
`gatekey/phase-5-technical-design.md` sections 2.3/3.1.

RBAC per AC5.5.9/the design doc's API-contract table: Org Admin registers/
edits/removes/re-verifies (`require_role("org_admin")`); Org Admin +
Auditor list (`require_admin_or_auditor` - same read posture as every other
compliance-adjacent admin surface in this codebase).

`services/self_hosted_providers.py` (CRUD + `SelfHostedModelRouteCache`) is
imported here AND from `api/v1/gateway/common.py`/`api/v1/gateway/chat.py`
(the credential-fetch/dispatch path) - never from anywhere else. Every
handler below re-derives the FULL `SelfHostedModelRouteCache` mapping from a
fresh DB read and calls `cache.set_all(...)` immediately after its own
commit (design doc wiring checklist "5.3 (Self-Hosted Governance, 5.5)" row
11) - a full re-derive, not an incremental single-entry update, mirroring
`load_self_hosted_model_route_snapshot`'s own stated rationale.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import (
    AdminContext,
    get_key_provider,
    get_self_hosted_model_route_cache,
    get_source_ip,
    require_admin_or_auditor,
    require_role,
)
from gatekey.db.models.self_hosted_provider import SelfHostedProvider
from gatekey.db.session import get_db_session
from gatekey.schemas.self_hosted_provider import (
    SelfHostedProviderCreateRequest,
    SelfHostedProviderResponse,
    SelfHostedProviderUpdateRequest,
)
from gatekey.services.audit import write_audit_entry
from gatekey.services.encryption import KeyProvider
from gatekey.services.self_hosted_providers import (
    SelfHostedModelRouteCache,
    SelfHostedProviderNotFoundError,
    edit_self_hosted_provider,
    get_self_hosted_provider_by_id,
    list_self_hosted_providers,
    load_self_hosted_model_route_snapshot,
    register_self_hosted_provider,
    remove_self_hosted_provider,
    reverify_self_hosted_provider,
)
from gatekey.services.sessions import SessionContext

router = APIRouter(prefix="/v1/admin/self-hosted-providers", tags=["admin", "self-hosted-providers"])


async def _refresh_cache(session: AsyncSession, cache: SelfHostedModelRouteCache) -> None:
    """Full re-derive + `set_all()` - see module docstring."""
    snapshot = await load_self_hosted_model_route_snapshot(session)
    cache.set_all(snapshot)


def _to_response(row: SelfHostedProvider) -> SelfHostedProviderResponse:
    return SelfHostedProviderResponse.model_validate(row)


@router.get("", response_model=list[SelfHostedProviderResponse])
async def list_self_hosted_providers_endpoint(
    ctx: AdminContext = Depends(require_admin_or_auditor),
    session: AsyncSession = Depends(get_db_session),
) -> list[SelfHostedProviderResponse]:
    rows = await list_self_hosted_providers(session)
    return [_to_response(row) for row in rows]


@router.post("", response_model=SelfHostedProviderResponse, status_code=201)
async def register_self_hosted_provider_endpoint(
    payload: SelfHostedProviderCreateRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    key_provider: KeyProvider = Depends(get_key_provider),
    cache: SelfHostedModelRouteCache = Depends(get_self_hosted_model_route_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> SelfHostedProviderResponse:
    """`register_self_hosted_provider()` validates (model-registry/other-
    provider collisions) and commits internally - the audit entry is
    written to the SAME session first (add + flush, no commit - see
    `services.audit.write_audit_entry`'s docstring), using a
    router-generated id, so both land in the same transaction when the
    service call's own commit lands (identical "audit-before-because-the-
    service-call-commits" pattern `api/v1/admin/residency_rules.py`'s PUT
    handler already uses). If the service call raises (a collision/name
    conflict), the queued-but-uncommitted audit entry is discarded with it
    - no orphaned audit row for a write that never actually happened.
    """
    provider_id = uuid.uuid4()
    await write_audit_entry(
        session,
        actor=ctx,
        action="self_hosted_provider.register",
        target_type="self_hosted_provider",
        target_id=str(provider_id),
        old_value=None,
        new_value={
            "name": payload.name,
            "base_url": payload.base_url,
            "cost_basis_per_gpu_hour": str(payload.cost_basis_per_gpu_hour),
            "models": payload.models,
        },
        source_ip=source_ip,
    )
    row = await register_self_hosted_provider(
        session,
        provider_id=provider_id,
        name=payload.name,
        base_url=payload.base_url,
        bearer_token=payload.bearer_token,
        cost_basis_per_gpu_hour=payload.cost_basis_per_gpu_hour,
        models=payload.models,
        key_provider=key_provider,
    )
    await _refresh_cache(session, cache)
    return _to_response(row)


@router.put("/{provider_id}", response_model=SelfHostedProviderResponse)
async def edit_self_hosted_provider_endpoint(
    provider_id: uuid.UUID,
    payload: SelfHostedProviderUpdateRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    key_provider: KeyProvider = Depends(get_key_provider),
    cache: SelfHostedModelRouteCache = Depends(get_self_hosted_model_route_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> SelfHostedProviderResponse:
    existing = await get_self_hosted_provider_by_id(session, provider_id)
    if existing is None:
        raise SelfHostedProviderNotFoundError(provider_id)

    bearer_token_provided = "bearer_token" in payload.model_fields_set
    await write_audit_entry(
        session,
        actor=ctx,
        action="self_hosted_provider.update",
        target_type="self_hosted_provider",
        target_id=str(provider_id),
        old_value={
            "name": existing.name,
            "base_url": existing.base_url,
            "cost_basis_per_gpu_hour": str(existing.cost_basis_per_gpu_hour),
            "models": existing.models,
            "verified": existing.verified,
        },
        new_value=payload.model_dump(exclude={"bearer_token"}, exclude_unset=True)
        | ({"bearer_token": "***"} if bearer_token_provided else {}),
        source_ip=source_ip,
    )
    row = await edit_self_hosted_provider(
        session,
        provider_id,
        name=payload.name,
        base_url=payload.base_url,
        bearer_token=payload.bearer_token,
        bearer_token_provided=bearer_token_provided,
        cost_basis_per_gpu_hour=payload.cost_basis_per_gpu_hour,
        models=payload.models,
        key_provider=key_provider,
    )
    await _refresh_cache(session, cache)
    return _to_response(row)


@router.delete("/{provider_id}", status_code=204)
async def remove_self_hosted_provider_endpoint(
    provider_id: uuid.UUID,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    cache: SelfHostedModelRouteCache = Depends(get_self_hosted_model_route_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> None:
    existing = await get_self_hosted_provider_by_id(session, provider_id)
    if existing is None:
        raise SelfHostedProviderNotFoundError(provider_id)

    await write_audit_entry(
        session,
        actor=ctx,
        action="self_hosted_provider.remove",
        target_type="self_hosted_provider",
        target_id=str(provider_id),
        old_value={"name": existing.name, "base_url": existing.base_url, "models": existing.models},
        new_value=None,
        source_ip=source_ip,
    )
    await remove_self_hosted_provider(session, provider_id)
    await _refresh_cache(session, cache)


@router.post("/{provider_id}/verify", response_model=SelfHostedProviderResponse)
async def reverify_self_hosted_provider_endpoint(
    provider_id: uuid.UUID,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    key_provider: KeyProvider = Depends(get_key_provider),
    cache: SelfHostedModelRouteCache = Depends(get_self_hosted_model_route_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> SelfHostedProviderResponse:
    """AC5.5.3: manual re-verification only - reuses `OllamaValidator.
    validate()`'s live `GET {base_url}/v1/models` probe as-is. The audit
    entry is written AFTER the probe (unlike the other three handlers)
    since its `new_value` needs the probe's own outcome, and
    `reverify_self_hosted_provider()` always commits regardless of outcome
    (success or failure) - so writing the audit entry first would risk it
    landing in a transaction the service call's own commit doesn't actually
    cover if a bug were ever introduced there. Instead this handler commits
    the audit entry itself, in its own immediately-following statement -
    consistent with every OTHER handler in this router still writing audit
    before its mutating call where the mutating call's commit is what
    should also durably persist the audit row; here, since the mutating
    call already committed the verification/probe outcome by the time we
    have data for the audit entry, a second, small commit for the audit row
    alone is the correct, honest shape rather than forcing an artificial
    single-transaction fiction.
    """
    row = await reverify_self_hosted_provider(session, provider_id, key_provider=key_provider)
    await write_audit_entry(
        session,
        actor=ctx,
        action="self_hosted_provider.reverify",
        target_type="self_hosted_provider",
        target_id=str(provider_id),
        old_value=None,
        new_value={"verified": row.verified},
        source_ip=source_ip,
    )
    await session.commit()
    await _refresh_cache(session, cache)
    return _to_response(row)
