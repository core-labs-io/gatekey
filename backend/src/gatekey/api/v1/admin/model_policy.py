"""Admin endpoints for the org's model access policy (Phase 1.3, section 4).

Both endpoints require `require_admin` (the Phase 1.1 single-shared-
admin-token stub - see `api/deps.py`). Neither accepts an `org_id` - see
`constants.DEFAULT_ORG_ID` for why this slice only ever operates against
the single seeded default org.

Follows `api/v1/admin/providers.py`'s exact pattern: router-level
`require_admin` dependency, no `org_id` param, `GatekeyError`-based errors,
service-layer logic kept out of this route module.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import (
    get_custom_model_route_cache,
    get_model_policy_cache,
    get_self_hosted_model_route_cache,
    require_admin,
)
from gatekey.db.session import get_db_session
from gatekey.errors import GatekeyError
from gatekey.schemas.model_policy import ModelPolicyPutRequest, ModelPolicyResponse
from gatekey.services.custom_models import CustomModelRouteCache
from gatekey.services.model_policy import (
    ModelPolicyCache,
    UnknownModelInPolicyError,
    get_policy,
    set_policy,
)
from gatekey.services.self_hosted_providers import SelfHostedModelRouteCache

router = APIRouter(
    prefix="/v1/admin/model-policy",
    tags=["admin", "model-policy"],
    dependencies=[Depends(require_admin)],
)


@router.get("", response_model=ModelPolicyResponse)
async def get_model_policy(
    session: AsyncSession = Depends(get_db_session),
) -> ModelPolicyResponse:
    """Always 200 - default `{"mode": "unconfigured", "models": []}` if no
    row exists yet (AC-7). Reads the DB directly (not the in-process cache):
    this is a control-plane read, not the AC-3a hot path, and should
    reflect the latest committed row even on a worker whose own cache
    happens to be stale (design doc section 2.4).
    """
    snapshot = await get_policy(session)
    return ModelPolicyResponse(mode=snapshot.mode, models=sorted(snapshot.models))


@router.put("", response_model=ModelPolicyResponse)
async def put_model_policy(
    payload: ModelPolicyPutRequest,
    session: AsyncSession = Depends(get_db_session),
    cache: ModelPolicyCache = Depends(get_model_policy_cache),
    self_hosted_cache: SelfHostedModelRouteCache = Depends(get_self_hosted_model_route_cache),
    custom_model_cache: CustomModelRouteCache = Depends(get_custom_model_route_cache),
) -> ModelPolicyResponse:
    """Full-replace upsert (AC-8). 422 `unknown_model_in_policy` if any
    `models` entry isn't a known `MODEL_REGISTRY` id - no DB write in that
    case (AC-7). Pushes the new snapshot straight into this process's cache
    after commit (design doc section 2.3) - no second DB read to
    "invalidate"; the cache is replaced, not invalidated-and-refetched.

    Deliberately calls the unconditional `cache.set()`, not
    `set_if_current()`: this handler just committed the authoritative row
    (`set_policy()`'s atomic upsert), so it is this system's source of
    truth for the policy and must always win the cache - see
    `ModelPolicyCache.set()`'s docstring for the full reasoning (security
    review finding, second round, design doc section 2.2/ADR-3 addendum).
    The generation-guarded CAS path (`set_if_current()`) exists only on the
    self-heal side, which is the one background writer that can otherwise
    race and clobber this handler's write.
    """
    try:
        snapshot = await set_policy(
            session,
            payload.mode,
            payload.models,
            self_hosted_cache=self_hosted_cache,
            custom_model_cache=custom_model_cache,
        )
    except UnknownModelInPolicyError as exc:
        raise GatekeyError(exc.message, code="unknown_model_in_policy", status_code=422) from None
    cache.set(snapshot)
    return ModelPolicyResponse(mode=snapshot.mode, models=sorted(snapshot.models))
