"""Admin CRUD + verification endpoints for the Custom Model Registry
(Admin-Managed BYOK Models). See `gatekey/custom-model-registry-technical-
design.md` section 3.1 (API contracts), section 5 (wiring checklist rows
18-23), section 6/8 (RBAC/security) and
`gatekey/custom-model-registry-product-spec.md` section 5/6 for the
governing product-level rationale.

RBAC per the technical design doc's API-contract table / product spec
section 6: Org Admin registers/edits/removes/verifies
(`require_role("org_admin")`); Org Admin + Auditor list/read
(`require_admin_or_auditor`) - the identical RBAC posture
`api/v1/admin/self_hosted_providers.py` already establishes for its own
CRUD surface, this router's direct structural precedent.

`services/custom_models.py` (CRUD + `CustomModelRouteCache` +
`verify_custom_model()`) is imported here AND from `api/v1/gateway/chat.py`/
`api/v1/gateway/embeddings.py` (the credential-fetch/dispatch path) - never
from anywhere else. Every mutating handler below re-derives the FULL
`CustomModelRouteCache` mapping from a fresh DB read and calls
`cache.set_all(...)` immediately after its own commit (technical design doc
section 5 row 21, mirroring `api/v1/admin/self_hosted_providers.py`'s
`_refresh_cache()` helper exactly).

Every exception type `services/custom_models.py` can raise is already a
`errors.GatekeyError` subclass with its own `status_code` (404 for
not-found/provider-not-configured, 422 for every write-time collision/
capability/pricing guard, 409 for the same-org name conflict, 429 for the
verify cooldown, 502-shaped for a real provider upstream error) - the
global `register_exception_handlers`' `GatekeyError` handler
(`gatekey/errors.py`) maps every one of them to the correct HTTP response
automatically. This router therefore needs NO manual exception-to-status
mapping anywhere, exactly like `self_hosted_providers.py`'s router - the
one exception is the verify endpoint below, which needs to intercept
`errors.ProviderUpstreamError` just long enough to write the
`custom_model.test_call` audit entry before re-raising the SAME exception
instance unmodified (never re-wrapped, so the real provider error message
still reaches the caller verbatim).
"""

from __future__ import annotations

import time
import uuid

import httpx
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import (
    AdminContext,
    get_custom_model_route_cache,
    get_key_provider,
    get_provider_http_client,
    get_source_ip,
    get_vertex_token_cache,
    require_admin_or_auditor,
    require_role,
)
from gatekey.db.models.custom_model import CustomModel
from gatekey.db.session import get_db_session
from gatekey.errors import ProviderUpstreamError
from gatekey.providers.model_registry import MODEL_REGISTRY, ModelCapability
from gatekey.providers.vertex_ai import VertexAITokenCache
from gatekey.schemas.custom_model import (
    AvailableModelEntry,
    CustomModelCreateRequest,
    CustomModelProvider,
    CustomModelResponse,
    CustomModelUpdateRequest,
    RegistryModelEntry,
    is_shadowed_by_registry,
)
from gatekey.services.audit import write_audit_entry
from gatekey.services.custom_models import (
    CustomModelNotFoundError,
    CustomModelRouteCache,
    edit_custom_model,
    get_custom_model_by_id,
    list_custom_models,
    load_custom_model_route_snapshot,
    register_custom_model,
    remove_custom_model,
    verify_custom_model,
)
from gatekey.services.encryption import KeyProvider
from gatekey.services.model_catalog import list_available_models
from gatekey.services.sessions import SessionContext

router = APIRouter(prefix="/v1/admin/custom-models", tags=["admin", "custom-models"])


async def _refresh_cache(session: AsyncSession, cache: CustomModelRouteCache) -> None:
    """Full re-derive + `set_all()` - see module docstring."""
    snapshot = await load_custom_model_route_snapshot(session)
    cache.set_all(snapshot)


def _to_response(row: CustomModel) -> CustomModelResponse:
    """`CustomModelResponse.shadowed_by_registry` has no ORM-model
    counterpart (technical design doc section 2.4b) - always computed fresh
    here via `is_shadowed_by_registry()`, never left to `model_validate`'s
    `from_attributes` (which could never populate it anyway)."""
    return CustomModelResponse(
        id=row.id,
        name=row.name,
        provider=row.provider,
        native_model_id=row.native_model_id,
        capability=row.capability.value,
        input_price_per_million_usd=row.input_price_per_million_usd,
        output_price_per_million_usd=row.output_price_per_million_usd,
        pricing_source=row.pricing_source,
        pricing_as_of=row.pricing_as_of,
        verified=row.verified,
        shadowed_by_registry=is_shadowed_by_registry(row.name),
        fallback_model_names=list(row.fallback_model_names) if row.fallback_model_names else [],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=list[CustomModelResponse])
async def list_custom_models_endpoint(
    ctx: AdminContext = Depends(require_admin_or_auditor),
    session: AsyncSession = Depends(get_db_session),
) -> list[CustomModelResponse]:
    rows = await list_custom_models(session)
    return [_to_response(row) for row in rows]


@router.get("/registry-model-names", response_model=list[str])
async def list_registry_model_names_endpoint(
    ctx: AdminContext = Depends(require_admin_or_auditor),
) -> list[str]:
    """Every static `MODEL_REGISTRY` key, sorted - zero I/O.

    Exists solely so the admin console's fallback-chain picker (Model
    Catalog technical design doc section 6, task 14) can offer the built-in
    registry models as fallback TARGETS alongside the org's own custom
    models (`GET /v1/admin/custom-models`) and self-hosted model ids (`GET
    /v1/admin/self-hosted-providers`) - both of those already have list
    endpoints; this was the one missing source, since `MODEL_REGISTRY` is a
    code-only dict with no existing admin-facing enumeration. Deliberately
    NOT `GET /v1/models` (`api/v1/gateway/models.py`) - that endpoint
    requires a gateway credential (`gk_sk_`/`gk_pk_`), not an admin session,
    and filters to only the caller's OWN policy-allowed models; an admin
    configuring a fallback chain needs to see every registry model that
    EXISTS, regardless of any one team's policy narrowing. Path placed
    before `/{custom_model_id}` for the same readability reason `available/
    {provider}` above is."""
    return sorted(MODEL_REGISTRY.keys())


@router.get("/registry-models", response_model=list[RegistryModelEntry])
async def list_registry_models_endpoint(
    ctx: AdminContext = Depends(require_admin_or_auditor),
) -> list[RegistryModelEntry]:
    """Every static `MODEL_REGISTRY` entry, `name` paired with `provider`,
    sorted by name - zero I/O. See `RegistryModelEntry`'s docstring: exists
    for Model Policy's provider-scoped checklist to source `vertex_ai`
    (no live-listing support) from something other than a hand-typed
    frontend list. Path placed alongside `registry-model-names` above for
    the same before-`/{custom_model_id}` readability reason."""
    return sorted(
        (RegistryModelEntry(name=name, provider=route.provider) for name, route in MODEL_REGISTRY.items()),
        key=lambda entry: entry.name,
    )


@router.get("/available/{provider}", response_model=list[AvailableModelEntry])
async def list_available_models_endpoint(
    provider: CustomModelProvider,
    ctx: AdminContext = Depends(require_admin_or_auditor),
    session: AsyncSession = Depends(get_db_session),
    key_provider: KeyProvider = Depends(get_key_provider),
    http_client: httpx.AsyncClient = Depends(get_provider_http_client),
) -> list[AvailableModelEntry]:
    """Live per-provider model catalog lookup (Model Catalog technical
    design doc section 1.3/1.5) - `services.model_catalog.
    list_available_models()` does all the real work (credential fetch,
    dispatch, pricing reverse-index join); this handler is a thin wire-up,
    identical in spirit to `verify_custom_model_endpoint`'s own
    `key_provider`/`http_client` dependency wiring above.

    Purely informational, no side effect of any kind - no DB write, no
    audit entry (unlike `verify_custom_model_endpoint`, which DOES write
    one because it performs a real, billable-adjacent provider probe; this
    endpoint never calls `verify_custom_model()` or anything that mutates a
    `custom_models` row). Path placed BEFORE `/{custom_model_id}` below only
    for readability (both routes are unambiguous regardless of declaration
    order - two path segments here vs. one there - but grouping the static
    `available/...` route near the top mirrors this router's own top-to-
    bottom "list, then read-by-id, then write" narrative).
    """
    return await list_available_models(
        session, provider, key_provider=key_provider, http_client=http_client
    )


@router.get("/{custom_model_id}", response_model=CustomModelResponse)
async def get_custom_model_endpoint(
    custom_model_id: uuid.UUID,
    ctx: AdminContext = Depends(require_admin_or_auditor),
    session: AsyncSession = Depends(get_db_session),
) -> CustomModelResponse:
    row = await get_custom_model_by_id(session, custom_model_id)
    if row is None:
        raise CustomModelNotFoundError(custom_model_id)
    return _to_response(row)


@router.post("", response_model=CustomModelResponse, status_code=201)
async def register_custom_model_endpoint(
    payload: CustomModelCreateRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    cache: CustomModelRouteCache = Depends(get_custom_model_route_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> CustomModelResponse:
    """`register_custom_model()` validates (static-registry/self-hosted/
    own-table name collisions, capability/pricing/embeddings-provider
    guards) and commits internally - the audit entry is written to the SAME
    session first (add + flush, no commit), using a router-generated id, so
    both land in the same transaction as the service call's own commit -
    identical "audit-before-because-the-service-call-commits" pattern
    `api/v1/admin/self_hosted_providers.py`'s POST handler already uses. If
    the service call raises (a collision/guard failure), the queued-but-
    uncommitted audit entry is discarded with it - no orphaned audit row
    for a write that never actually happened.
    """
    custom_model_id = uuid.uuid4()
    await write_audit_entry(
        session,
        actor=ctx,
        action="custom_model.register",
        target_type="custom_model",
        target_id=str(custom_model_id),
        old_value=None,
        new_value={
            "name": payload.name,
            "provider": payload.provider,
            "native_model_id": payload.native_model_id,
            "capability": payload.capability,
            "input_price_per_million_usd": payload.input_price_per_million_usd,
            "output_price_per_million_usd": payload.output_price_per_million_usd,
            "pricing_source": payload.pricing_source,
            "fallback_model_names": payload.fallback_model_names,
        },
        source_ip=source_ip,
    )
    row = await register_custom_model(
        session,
        custom_model_id=custom_model_id,
        name=payload.name,
        provider=payload.provider,
        native_model_id=payload.native_model_id,
        capability=ModelCapability(payload.capability),
        input_price_per_million_usd=payload.input_price_per_million_usd,
        output_price_per_million_usd=payload.output_price_per_million_usd,
        pricing_source=payload.pricing_source,
        fallback_model_names=payload.fallback_model_names,
    )
    await _refresh_cache(session, cache)
    return _to_response(row)


@router.put("/{custom_model_id}", response_model=CustomModelResponse)
async def edit_custom_model_endpoint(
    custom_model_id: uuid.UUID,
    payload: CustomModelUpdateRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    cache: CustomModelRouteCache = Depends(get_custom_model_route_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> CustomModelResponse:
    existing = await get_custom_model_by_id(session, custom_model_id)
    if existing is None:
        raise CustomModelNotFoundError(custom_model_id)

    # `output_price_per_million_usd`'s None-vs-omitted ambiguity (a
    # `capability` edit from "chat" to "embeddings" must be able to
    # explicitly clear a previously-required price to `null`) - identical
    # rationale/discipline to self-hosted's `bearer_token_provided`, see
    # `schemas.custom_model.CustomModelUpdateRequest`'s docstring and
    # `services.custom_models.edit_custom_model`'s
    # `output_price_per_million_usd_provided` parameter.
    output_price_provided = "output_price_per_million_usd" in payload.model_fields_set
    # Model Catalog + Cross-Provider Fallback Chains (Part B) - identical
    # `model_fields_set`-based disambiguation, see `schemas.custom_model.
    # CustomModelUpdateRequest`'s docstring / `services.custom_models.
    # edit_custom_model`'s `fallback_model_names_provided` parameter.
    fallback_model_names_provided = "fallback_model_names" in payload.model_fields_set

    await write_audit_entry(
        session,
        actor=ctx,
        action="custom_model.update",
        target_type="custom_model",
        target_id=str(custom_model_id),
        old_value={
            "name": existing.name,
            "provider": existing.provider,
            "native_model_id": existing.native_model_id,
            "capability": existing.capability,
            "input_price_per_million_usd": existing.input_price_per_million_usd,
            "output_price_per_million_usd": existing.output_price_per_million_usd,
            "pricing_source": existing.pricing_source,
            "verified": existing.verified,
            "fallback_model_names": list(existing.fallback_model_names)
            if existing.fallback_model_names
            else [],
        },
        new_value=payload.model_dump(exclude_unset=True),
        source_ip=source_ip,
    )
    row = await edit_custom_model(
        session,
        custom_model_id,
        name=payload.name,
        provider=payload.provider,
        native_model_id=payload.native_model_id,
        capability=ModelCapability(payload.capability) if payload.capability is not None else None,
        input_price_per_million_usd=payload.input_price_per_million_usd,
        output_price_per_million_usd=payload.output_price_per_million_usd,
        output_price_per_million_usd_provided=output_price_provided,
        pricing_source=payload.pricing_source,
        fallback_model_names=payload.fallback_model_names,
        fallback_model_names_provided=fallback_model_names_provided,
    )
    await _refresh_cache(session, cache)
    return _to_response(row)


@router.delete("/{custom_model_id}", status_code=204)
async def remove_custom_model_endpoint(
    custom_model_id: uuid.UUID,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    cache: CustomModelRouteCache = Depends(get_custom_model_route_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> None:
    """Hard delete (technical design doc section 2.1) - the row disappears
    from `CustomModelRouteCache` on this handler's post-commit refresh, and
    new requests for that name 404 immediately (no `usage_logs` FK to this
    table, so historical rows are structurally unaffected - see
    `db.models.custom_model.CustomModel`'s module docstring)."""
    existing = await get_custom_model_by_id(session, custom_model_id)
    if existing is None:
        raise CustomModelNotFoundError(custom_model_id)

    await write_audit_entry(
        session,
        actor=ctx,
        action="custom_model.remove",
        target_type="custom_model",
        target_id=str(custom_model_id),
        old_value={
            "name": existing.name,
            "provider": existing.provider,
            "native_model_id": existing.native_model_id,
            "capability": existing.capability,
        },
        new_value=None,
        source_ip=source_ip,
    )
    await remove_custom_model(session, custom_model_id)
    await _refresh_cache(session, cache)


@router.post("/{custom_model_id}/verify", response_model=CustomModelResponse)
async def verify_custom_model_endpoint(
    custom_model_id: uuid.UUID,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    key_provider: KeyProvider = Depends(get_key_provider),
    http_client: httpx.AsyncClient = Depends(get_provider_http_client),
    vertex_token_cache: VertexAITokenCache = Depends(get_vertex_token_cache),
    cache: CustomModelRouteCache = Depends(get_custom_model_route_cache),
    source_ip: str | None = Depends(get_source_ip),
) -> CustomModelResponse:
    """One live, minimal test call against `row.native_model_id`, using the
    org's EXISTING BYOK credential for `row.provider` (technical design doc
    section 2.3). `services.custom_models.verify_custom_model()` performs
    and commits the live probe itself (including the 429 cooldown check,
    which happens BEFORE any credential fetch/live call - see that
    function's docstring) - this handler's only remaining job is timing the
    call and writing the `custom_model.test_call` audit entry afterward,
    exactly the division of responsibility CMR-2's module docstring
    describes ("Audit entry for verify_custom_model(), deliberately NOT
    written here").

    `errors.CustomModelNotFoundError` (404) and
    `services.custom_models.CustomModelVerifyCooldownError` (429) are
    raised by `verify_custom_model()` BEFORE any provider call is attempted
    (row lookup / cooldown gate) - deliberately NOT audited as a
    `custom_model.test_call` (no call was actually attempted), and simply
    propagate to the global `GatekeyError` handler unchanged. A genuine
    attempted call - success OR `errors.ProviderUpstreamError` (the real
    upstream error, surfaced to the caller VERBATIM, never swallowed or
    re-wrapped) - is what gets one `custom_model.test_call` audit entry
    recording `success`/`latency_ms`, mirroring the technical design doc
    section 2.3 step 6 pseudocode. `errors.ProviderNotConfiguredError`
    (404, no `provider_keys` row configured yet for `row.provider`) is
    likewise raised before any real provider call is attempted, so it is
    also not audited as a test_call - same rationale as the cooldown/
    not-found cases above.

    Cache refresh (technical design doc section 5 row 21) runs on BOTH the
    success and the `ProviderUpstreamError` paths - `verified` can flip
    `True -> False` on a failed re-verify of a previously-verified row,
    which must be reflected in `CustomModelRouteCache` immediately (an
    unverified row must stop being routable the moment this commits).
    """
    started_at = time.perf_counter()
    try:
        row = await verify_custom_model(
            session,
            custom_model_id,
            key_provider=key_provider,
            http_client=http_client,
            vertex_token_cache=vertex_token_cache,
        )
    except ProviderUpstreamError:
        latency_ms = round((time.perf_counter() - started_at) * 1000)
        await write_audit_entry(
            session,
            actor=ctx,
            action="custom_model.test_call",
            target_type="custom_model",
            target_id=str(custom_model_id),
            old_value=None,
            new_value={"success": False, "latency_ms": latency_ms},
            source_ip=source_ip,
        )
        await session.commit()
        await _refresh_cache(session, cache)
        raise

    latency_ms = round((time.perf_counter() - started_at) * 1000)
    await write_audit_entry(
        session,
        actor=ctx,
        action="custom_model.test_call",
        target_type="custom_model",
        target_id=str(custom_model_id),
        old_value=None,
        new_value={"success": True, "latency_ms": latency_ms},
        source_ip=source_ip,
    )
    await session.commit()
    await _refresh_cache(session, cache)
    return _to_response(row)
