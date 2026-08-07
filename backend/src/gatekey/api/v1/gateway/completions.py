"""`POST /v1/completions` - legacy, OpenAI-only, non-streaming (Phase 1.2,
BD-9; Phase 1.4 budget wiring; Phase 1.5 persisted usage log).

Design doc Q4 scopes this endpoint down to OpenAI only, non-streaming only,
in this phase: Anthropic/Vertex AI have no legacy-completion translation
(`providers/anthropic.py` and `providers/vertex_ai.py` expose no
`create_completion`), and streaming was judged not worth building a second
SSE translation path for a legacy, deprecated-upstream shape. `stream:
true` is rejected as a cheap, static check on the request body before any
DB/model-registry work happens; the non-OpenAI-provider and
wrong-capability rejections happen after `resolve_route` (the same
model-registry lookup every gateway endpoint uses - see `common.py`) but
*before* the budget check and credential fetch, so a model that was never
routable on this endpoint is rejected with 400 regardless of budget/key
state.

Phase 4 (Reliability & Cost Efficiency) additions
------------------------------------------------------
See `common.py`'s "Phase 4" section note for the exact pipeline ordering:
`check_rate_limit()` (first step), `check_response_cache()` (right after
`check_model_policy()`, HIT returns immediately), `X-Failover-*` headers
from `call_provider_with_failover()`'s new return shape. Graceful
degradation is explicitly OUT OF SCOPE here (AC4.4.7 - chat completions
only) - this route never calls `check_and_apply_degradation()`.

Custom Model Registry (Admin-Managed BYOK Models, CMR-4): deliberately NOT
wired here, and never will be. `resolve_route(body.model)` below is called
with NO cache arguments at all (neither `self_hosted_cache` - unchanged
since Phase 5.5 - nor the new `custom_model_cache`) - this is the same
call-site-level structural enforcement mechanism Phase 5.5 already
established for "self-hosted models are chat-completions only"
(AC5.5.4), reused here for the product spec's explicit non-goal (section
7): "/v1/completions does not gain custom-model support." A custom model's
capability is per-row (chat OR embeddings, admin's choice) - neither value
has anything to do with this legacy, OpenAI-only, non-streaming endpoint,
and the static registry itself has no completions-only models either, so
there is no precedent this endpoint needs to extend. See the Custom Model
Registry technical design doc section 2.2 ("Why completions.py is
deliberately never touched") / section 5 row 9.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, cast

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, Response
from presidio_analyzer import AnalyzerEngine
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import (
    GatewayCallerContext,
    get_access_schedule_cache,
    get_caching_settings_cache,
    get_content_aware_rule_cache,
    get_db_session,
    get_dlp_analyzer_engine,
    get_key_provider,
    get_model_policy_cache,
    get_provider_http_client,
    get_rate_limit_cache,
    get_residency_rule_cache,
    get_shared_state_store,
    get_source_ip,
    get_team_failover_override_cache,
    get_team_model_policy_cache,
    require_gateway_credential,
)
from gatekey.api.v1.gateway.common import (
    LatencyTimer,
    build_cache_headers,
    build_failover_headers,
    build_rate_limit_headers,
    call_provider_with_failover,
    check_access_schedule,
    check_budget_available,
    check_content_classification,
    check_model_policy,
    check_rate_limit,
    check_residency,
    check_response_cache,
    log_gateway_request,
    new_request_id,
    record_usage_charge,
    resolve_route,
    run_dlp_scan,
    validate_idempotency_key,
    write_response_cache,
)
from gatekey.errors import GatekeyError, ProviderUpstreamError
from gatekey.errors import UnsupportedRequestError as HttpUnsupportedRequestError
from gatekey.providers import openai as openai_provider
from gatekey.providers.base import ProviderCallError
from gatekey.providers.model_registry import ModelCapability
from gatekey.providers.pricing import PricingEntryMissingError
from gatekey.schemas.chat import CompletionRequest, CompletionResponse
from gatekey.services.access_schedules import AccessScheduleCache
from gatekey.services.encryption import KeyProvider
from gatekey.services.model_policy import ContentAwareRuleCache, ModelPolicyCache, TeamModelPolicyCache
from gatekey.services.provider_key_health import TeamFailoverOverrideCache
from gatekey.services.proxy_keys import ApiKeyCredential
from gatekey.services.rate_limit import RateLimitCache
from gatekey.services.residency import ResidencyRuleCache
from gatekey.services.response_cache import CachingSettingsCache, ResponseCache
from gatekey.services.shared_state import SharedStateStore
from gatekey.services.usage_logs import record_usage_log

router = APIRouter(tags=["gateway"])

_ENDPOINT = "/v1/completions"


@router.post(_ENDPOINT, response_model=CompletionResponse)
async def create_completion(
    request: Request,
    response: Response,
    body: CompletionRequest,
    background_tasks: BackgroundTasks,
    ctx: GatewayCallerContext = Depends(require_gateway_credential),
    session: AsyncSession = Depends(get_db_session),
    key_provider: KeyProvider = Depends(get_key_provider),
    http_client: httpx.AsyncClient = Depends(get_provider_http_client),
    cache: ModelPolicyCache = Depends(get_model_policy_cache),
    team_cache: TeamModelPolicyCache = Depends(get_team_model_policy_cache),
    residency_cache: ResidencyRuleCache = Depends(get_residency_rule_cache),
    content_aware_cache: ContentAwareRuleCache = Depends(get_content_aware_rule_cache),
    dlp_engine: AnalyzerEngine = Depends(get_dlp_analyzer_engine),
    access_schedule_cache: AccessScheduleCache = Depends(get_access_schedule_cache),
    shared_state_store: SharedStateStore = Depends(get_shared_state_store),
    team_override_cache: TeamFailoverOverrideCache = Depends(get_team_failover_override_cache),
    rate_limit_cache: RateLimitCache = Depends(get_rate_limit_cache),
    caching_settings_cache: CachingSettingsCache = Depends(get_caching_settings_cache),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    sensitivity_label: str | None = Header(default=None, alias="X-Gatekey-Sensitivity-Label"),
) -> CompletionResponse:
    # Phase 2 (BD-11): exactly one of these two is set per usage-log row,
    # by credential type - see chat.py's identical note.
    service_account_key_id = (
        ctx.credential_id if ctx.credential_type == "service_account" else None
    )
    personal_api_key_id = ctx.credential_id if ctx.credential_type == "personal" else None
    timer = LatencyTimer()
    request_id = new_request_id()
    idempotency_key = validate_idempotency_key(idempotency_key)
    provider_for_log: str | None = None
    response_cache = ResponseCache(shared_state_store)
    response_headers: dict[str, str] = {}

    try:
        # Phase 3 (BD-19): design doc section 5.3 - runs FIRST, before
        # resolve_route()/the stream-support check below - see chat.py's
        # identical note. source_ip resolved once here and threaded into
        # every synchronous audit write below.
        source_ip = get_source_ip(request, request.app.state.settings)
        await check_access_schedule(
            session, ctx, cache=access_schedule_cache, source_ip=source_ip
        )
        # Phase 4 (AC4.2): the very first pipeline step - see common.py's
        # "Phase 4" section note.
        rate_limit_decision = await check_rate_limit(
            session, ctx, store=shared_state_store, rate_limit_cache=rate_limit_cache
        )
        response_headers.update(build_rate_limit_headers(rate_limit_decision))

        if body.stream:
            raise HttpUnsupportedRequestError(
                "POST /v1/completions does not support streaming in this phase; "
                "use POST /v1/chat/completions with stream=true instead."
            )

        route = resolve_route(body.model)
        provider_for_log = route.provider
        check_model_policy(body.model, cache, team_cache, ctx.team_id)
        if route.provider != "openai":
            raise HttpUnsupportedRequestError(
                f"POST /v1/completions only supports OpenAI models in this phase "
                f"(model '{body.model}' routes to provider '{route.provider}'); "
                "use POST /v1/chat/completions instead."
            )
        if route.capability != ModelCapability.CHAT:
            raise HttpUnsupportedRequestError(
                f"Model '{body.model}' is not routable for POST /v1/completions "
                f"(registered capability: {route.capability.value})."
            )

        # Phase 4 (AC4.3): cache lookup - see common.py's "Phase 4" section
        # note.
        cache_check = await check_response_cache(
            session,
            ctx,
            route,
            request_body=body.model_dump(mode="json", exclude_none=True),
            response_cache=response_cache,
            background_tasks=background_tasks,
            app=request.app,
            caching_settings_cache=caching_settings_cache,
        )
        response_headers.update(build_cache_headers(cache_check))
        if cache_check.hit:
            assert cache_check.entry is not None
            timer.mark("pre_dispatch")
            timer.mark("provider_response_received")
            cached_response = CompletionResponse.model_validate(cache_check.entry.response_body)
            timer.mark("flush_complete")
            log_gateway_request(
                request_id=request_id,
                endpoint=_ENDPOINT,
                provider=route.provider,
                model=body.model,
                stream=False,
                status="ok",
                timer=timer,
                idempotency_key=idempotency_key,
            )
            await record_usage_log(
                session,
                request_id=request_id,
                endpoint=_ENDPOINT,
                provider=route.provider,
                model=body.model,
                user_id=ctx.user_id,
                service_account_key_id=service_account_key_id,
                team_id=ctx.team_id,
                personal_api_key_id=personal_api_key_id,
                prompt_tokens=cache_check.entry.input_tokens,
                completion_tokens=cache_check.entry.output_tokens,
                cost_usd=Decimal("0"),
                raw_provider_cost_usd=Decimal("0"),
                latency_ms=int(timer.deltas_ms().get("flush_complete", 0.0)),
                stream=False,
                status="ok",
                success=True,
                cache_hit=True,
            )
            for key, value in response_headers.items():
                response.headers[key] = value
            return cached_response

        # Phase 3 (BD-6): design doc section 3.3's exact ordering - see
        # common.py's module docstring.
        await check_residency(
            session,
            route,
            cache=residency_cache,
            team_id=ctx.team_id,
            org_id=ctx.org_id,
            actor_user_id=ctx.user_id,
            actor_label=ctx.name,
            source_ip=source_ip,
        )
        dlp_result = await run_dlp_scan(
            session,
            engine=dlp_engine,
            texts=[body.prompt],
            model=body.model,
            request_id=request_id,
            org_id=ctx.org_id,
            team_id=ctx.team_id,
            user_id=ctx.user_id,
            actor_label=ctx.name,
            content_aware_cache=content_aware_cache,
            background_tasks=background_tasks,
            app=request.app,
            source_ip=source_ip,
            sensitivity_label=sensitivity_label,
        )
        if dlp_result.redacted_texts is not None:
            body.prompt = dlp_result.redacted_texts[0]
        check_content_classification(
            body.model, content_aware_cache, category_findings=dlp_result.category_findings
        )
        await check_budget_available(session, ctx.user_id, team_id=ctx.team_id)
        timer.mark("pre_dispatch")

        async def _call(credential: Any) -> CompletionResponse:
            # `route.provider == "openai"` is checked above, which is
            # exactly what `get_decrypted_provider_credential_from_row`
            # uses to decide the credential shape - so this cast is a type
            # narrowing of an already-guaranteed invariant, not a runtime
            # check in its own right.
            return await openai_provider.create_completion(
                http_client, route.native_model_id, body, cast(ApiKeyCredential, credential)
            )

        try:
            failover = await call_provider_with_failover(
                session,
                request.app,
                route=route,
                org_id=ctx.org_id,
                team_id=ctx.team_id,
                request_id=request_id,
                key_provider=key_provider,
                health_store=shared_state_store,
                team_override_cache=team_override_cache,
                call_fn=_call,
            )
        except ProviderCallError as exc:
            raise ProviderUpstreamError(str(exc), upstream_status_code=exc.status_code) from None
        response_obj = failover.result
        timer.mark("provider_response_received")
        response_headers.update(build_failover_headers(failover))

        cost_usd: Decimal | None = None
        try:
            charge = await record_usage_charge(
                session,
                user_id=ctx.user_id,
                team_id=ctx.team_id,
                model=body.model,
                prompt_tokens=response_obj.usage.prompt_tokens,
                completion_tokens=response_obj.usage.completion_tokens,
                background_tasks=background_tasks,
                app=request.app,
                org_id=ctx.org_id,
                rate_limit_store=shared_state_store,
                rate_limit_cache=rate_limit_cache,
            )
            cost_usd = charge.cost
        except PricingEntryMissingError:
            timer.mark("flush_complete")
            await record_usage_log(
                session,
                request_id=request_id,
                endpoint=_ENDPOINT,
                provider=route.provider,
                model=body.model,
                user_id=ctx.user_id,
                service_account_key_id=service_account_key_id,
                team_id=ctx.team_id,
                personal_api_key_id=personal_api_key_id,
                prompt_tokens=response_obj.usage.prompt_tokens,
                completion_tokens=response_obj.usage.completion_tokens,
                cost_usd=None,
                latency_ms=int(timer.deltas_ms().get("flush_complete", 0.0)),
                stream=False,
                status="internal_error",
                success=False,
                failover_attempt=failover.attempt,
                failover_key_id=failover.used_key_id,
            )
            raise

        # Phase 4 (AC4.3.7): write-through cache population.
        await write_response_cache(
            ctx,
            route,
            cache_check=cache_check,
            response_cache=response_cache,
            response_body=response_obj.model_dump(mode="json"),
            input_tokens=response_obj.usage.prompt_tokens,
            output_tokens=response_obj.usage.completion_tokens,
            skip_write=dlp_result.redacted_texts is not None,
        )

        timer.mark("flush_complete")
        log_gateway_request(
            request_id=request_id,
            endpoint=_ENDPOINT,
            provider=route.provider,
            model=body.model,
            stream=False,
            status="ok",
            timer=timer,
            idempotency_key=idempotency_key,
        )
        await record_usage_log(
            session,
            request_id=request_id,
            endpoint=_ENDPOINT,
            provider=route.provider,
            model=body.model,
            user_id=ctx.user_id,
            service_account_key_id=service_account_key_id,
            team_id=ctx.team_id,
            personal_api_key_id=personal_api_key_id,
            prompt_tokens=response_obj.usage.prompt_tokens,
            completion_tokens=response_obj.usage.completion_tokens,
            cost_usd=cost_usd,
            raw_provider_cost_usd=cost_usd,
            latency_ms=int(timer.deltas_ms().get("flush_complete", 0.0)),
            stream=False,
            status="ok",
            success=True,
            failover_attempt=failover.attempt,
            failover_key_id=failover.used_key_id,
        )
        for key, value in response_headers.items():
            response.headers[key] = value
        return response_obj
    except GatekeyError as exc:
        timer.mark("flush_complete")
        await record_usage_log(
            session,
            request_id=request_id,
            endpoint=_ENDPOINT,
            provider=provider_for_log,
            model=body.model,
            user_id=ctx.user_id,
            service_account_key_id=service_account_key_id,
            team_id=ctx.team_id,
            personal_api_key_id=personal_api_key_id,
            prompt_tokens=None,
            completion_tokens=None,
            cost_usd=None,
            latency_ms=int(timer.deltas_ms().get("flush_complete", 0.0)),
            stream=False,
            status=exc.code,
            success=False,
        )
        raise
