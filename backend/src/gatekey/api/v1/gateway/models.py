"""`GET /v1/models` / `GET /v1/models/{model}` - OpenAI-compatible model
discovery for gateway credential holders (Tier 4 ops/DX polish).

Fixes the OpenAI SDK's `client.models.list()` 404 and gives a key holder a
self-service answer to "which models am I allowed to call?" without asking
an admin.

Semantics, deliberately:
- Auth is the GATEWAY credential (`gk_sk_`/`gk_pk_`) - the same
  `require_gateway_credential` the inference routes use, so what this
  endpoint reports is what that same bearer token will experience.
- The candidate set and the policy resolution are IDENTICAL to the
  end-user Model Access screen (`api/v1/model_access.py`): static
  `MODEL_REGISTRY` UNION verified custom models UNION verified self-hosted
  models, each resolved through `resolve_model_access` (org baseline, then
  the caller's team narrowing). Only ALLOWED models are listed - this is
  discovery, not a policy debugger; the Model Access screen keeps the
  allowed/blocked/why view.
- Per-request-content layers (DLP, content-classification routing,
  residency, budget, schedules) cannot be evaluated statically and are
  deliberately not reflected here - a listed model can still be blocked at
  request time by those. Same caveat the Model Access screen carries.
- `GET /v1/models/{model}` returns 404 (`model_not_found`) for unknown AND
  denied models alike: a denied model is indistinguishable from a
  nonexistent one to this caller, mirroring the anti-enumeration posture
  used elsewhere (`require_team_role`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from gatekey.api.deps import (
    GatewayCallerContext,
    get_custom_model_route_cache,
    get_model_policy_cache,
    get_self_hosted_model_route_cache,
    get_team_model_policy_cache,
    require_gateway_credential,
)
from gatekey.errors import GATEWAY_ERROR_RESPONSES, ModelNotFoundError
from gatekey.providers.model_registry import MODEL_REGISTRY
from gatekey.services.custom_models import CustomModelRouteCache
from gatekey.services.model_policy import (
    ModelPolicyCache,
    TeamModelPolicyCache,
    resolve_model_access,
)
from gatekey.services.self_hosted_providers import SelfHostedModelRouteCache

router = APIRouter(prefix="/v1/models", tags=["gateway"], responses=GATEWAY_ERROR_RESPONSES)

# OpenAI's model objects carry a `created` unix timestamp. Gatekey doesn't
# track per-model registration times for static registry entries, so a
# fixed, obviously-symbolic epoch is used - SDKs only require the field to
# exist and be an int.
_CREATED_EPOCH = 0


class ModelObject(BaseModel):
    id: str
    object: str = "model"
    created: int = _CREATED_EPOCH
    owned_by: str = "gatekey"


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelObject]


def _allowed_models(
    ctx: GatewayCallerContext,
    org_cache: ModelPolicyCache,
    team_cache: TeamModelPolicyCache,
    custom_model_cache: CustomModelRouteCache,
    self_hosted_cache: SelfHostedModelRouteCache,
) -> list[str]:
    candidates = (
        MODEL_REGISTRY.keys()
        | custom_model_cache.known_model_ids()
        | self_hosted_cache.known_model_ids()
    )
    return sorted(
        model
        for model in candidates
        if resolve_model_access(
            model, org_cache=org_cache, team_cache=team_cache, team_id=ctx.team_id
        ).allowed
    )


@router.get("", response_model=ModelListResponse)
async def list_models(
    ctx: GatewayCallerContext = Depends(require_gateway_credential),
    org_cache: ModelPolicyCache = Depends(get_model_policy_cache),
    team_cache: TeamModelPolicyCache = Depends(get_team_model_policy_cache),
    custom_model_cache: CustomModelRouteCache = Depends(get_custom_model_route_cache),
    self_hosted_cache: SelfHostedModelRouteCache = Depends(get_self_hosted_model_route_cache),
) -> ModelListResponse:
    allowed = _allowed_models(ctx, org_cache, team_cache, custom_model_cache, self_hosted_cache)
    return ModelListResponse(data=[ModelObject(id=model) for model in allowed])


@router.get("/{model}", response_model=ModelObject)
async def retrieve_model(
    model: str,
    ctx: GatewayCallerContext = Depends(require_gateway_credential),
    org_cache: ModelPolicyCache = Depends(get_model_policy_cache),
    team_cache: TeamModelPolicyCache = Depends(get_team_model_policy_cache),
    custom_model_cache: CustomModelRouteCache = Depends(get_custom_model_route_cache),
    self_hosted_cache: SelfHostedModelRouteCache = Depends(get_self_hosted_model_route_cache),
) -> ModelObject:
    allowed = _allowed_models(ctx, org_cache, team_cache, custom_model_cache, self_hosted_cache)
    if model not in allowed:
        raise ModelNotFoundError(f"Model '{model}' does not exist or is not available to you.")
    return ModelObject(id=model)
