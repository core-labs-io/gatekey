"""`POST /v1/chat/completions` (Phase 1.2, BD-9; Phase 1.4 budget wiring;
Phase 1.5 persisted usage log).

Both the streaming and non-streaming branches share the exact same
auth -> resolve_model -> model-policy check -> capability-check ->
budget-check -> credential-fetch sequence (via `common.py`'s helpers) before
ever branching on `request.stream` - see `common.py`'s module docstring and
the design doc's Story 2 acceptance criteria. Nothing below duplicates or
shortcuts that sequence for either branch.

Phase 1.4 (Budget - Basic) / Phase 1.5 (Logging - Basic) additions
---------------------------------------------------------------------
`check_budget_available()` runs right before `fetch_credential()`;
`record_usage_charge()` runs only after a confirmed, complete usage figure
is available (non-streaming: right after the provider response; streaming:
only in `_sse_event_stream`'s terminal usage chunk, never on disconnect,
error, or usage-unavailable). A `UsageLog` row is persisted (best-effort,
never raises) at every terminal outcome, success or a `GatekeyError`
rejection, so the admin usage dashboard reflects denied/errored traffic
too, not just successful requests.

Phase 4 (Reliability & Cost Efficiency) additions
------------------------------------------------------
See `common.py`'s "Phase 4" section note for the exact pipeline ordering.
Summary for this route specifically:

  - `check_rate_limit()` is the very first step (before `resolve_route()`).
  - `check_response_cache()` runs right after `check_model_policy()`, for
    BOTH the streaming and non-streaming branches uniformly - a streaming
    request can never actually produce/consume a hit (the hashed request
    body includes `stream: true`, which no cached entry - always written
    with `stream: false`, see below - ever matches), so this is a harmless,
    always-miss lookup on the streaming path rather than a special case.
    A HIT returns immediately, before `check_residency()`/`run_dlp_scan()`/
    `check_budget_available()`/graceful degradation/the provider call.
  - Graceful degradation (chat completions ONLY, AC4.4.7) runs right after
    `check_budget_available()`, for BOTH branches, substituting the
    `ModelRoute` used for provider dispatch/charging - `effective_route`/
    `effective_model` below, never `route`/`body.model` directly, from that
    point on. A degraded response is deliberately NEVER written to the
    response cache under the ORIGINAL model's cache key (see
    `write_response_cache()`'s call site below) - caching a cheaper
    degraded response under the original, undegraded model's key would let
    a LATER request for that model silently receive a lower-quality
    response with no degradation signal at all (no live degradation event,
    since a cache hit never re-evaluates the budget-proximity threshold).
  - `call_provider_with_failover()` now returns a `FailoverCallResult`
    (`.result` / `.attempt` / `.used_key_id`) instead of a bare result -
    every call site below unwraps `.result` and threads the failover
    metadata into `X-Failover-*` headers and `usage_logs.failover_*`.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from decimal import Decimal
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Header, Request, Response
from fastapi.responses import StreamingResponse
from presidio_analyzer import AnalyzerEngine
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import (
    GatewayCallerContext,
    get_access_schedule_cache,
    get_caching_settings_cache,
    get_content_aware_rule_cache,
    get_db_session,
    get_degradation_policy_cache,
    get_dlp_analyzer_engine,
    get_key_provider,
    get_model_policy_cache,
    get_provider_http_client,
    get_rate_limit_cache,
    get_residency_rule_cache,
    get_self_hosted_model_route_cache,
    get_shared_state_store,
    get_source_ip,
    get_team_failover_override_cache,
    get_team_model_policy_cache,
    get_vertex_token_cache,
    require_gateway_credential,
)
from gatekey.api.v1.gateway.common import (
    LatencyTimer,
    build_cache_headers,
    build_degradation_headers,
    build_failover_headers,
    build_rate_limit_headers,
    call_provider_with_failover,
    call_self_hosted_provider,
    check_access_schedule,
    check_and_apply_degradation,
    check_budget_available,
    check_content_classification,
    check_model_policy,
    check_rate_limit,
    check_residency,
    check_response_cache,
    log_degradation_event,
    log_gateway_request,
    new_request_id,
    raise_hard_budget_block_after_degradation_skip,
    record_usage_charge,
    resolve_route,
    revalidate_degraded_model,
    run_dlp_scan,
    validate_idempotency_key,
    write_response_cache,
)
from gatekey.errors import GatekeyError, ProviderUpstreamError
from gatekey.errors import UnsupportedRequestError as HttpUnsupportedRequestError
from gatekey.providers import anthropic as anthropic_provider
from gatekey.providers import ollama as ollama_provider
from gatekey.providers import openai as openai_provider
from gatekey.providers import openrouter as openrouter_provider
from gatekey.providers import vertex_ai as vertex_provider
from gatekey.providers.base import ProviderCallError
from gatekey.providers.base import UnsupportedRequestError as ProviderUnsupportedRequestError
from gatekey.providers.model_registry import ModelCapability, ModelRoute
from gatekey.providers.pricing import PricingEntryMissingError, compute_self_hosted_cost
from gatekey.providers.vertex_ai import VertexAITokenCache
from gatekey.schemas.chat import ChatCompletionChunk, ChatCompletionRequest, ChatCompletionResponse
from gatekey.services.access_schedules import AccessScheduleCache
from gatekey.services.degradation import DegradationPolicyCache
from gatekey.services.encryption import KeyProvider
from gatekey.services.model_policy import ContentAwareRuleCache, ModelPolicyCache, TeamModelPolicyCache
from gatekey.services.provider_key_health import TeamFailoverOverrideCache
from gatekey.services.rate_limit import RateLimitCache
from gatekey.services.residency import ResidencyRuleCache
from gatekey.services.response_cache import CachingSettingsCache, ResponseCache
from gatekey.services.self_hosted_providers import SelfHostedModelRouteCache
from gatekey.services.shared_state import SharedStateStore
from gatekey.services.usage_logs import record_usage_log

logger = logging.getLogger("gatekey")

router = APIRouter(tags=["gateway"])

_ENDPOINT = "/v1/chat/completions"

_STREAM_EMPTY = object()


async def _create_non_streaming(
    provider: str,
    native_model_id: str,
    body: ChatCompletionRequest,
    credential: Any,
    http_client: httpx.AsyncClient,
    token_cache: VertexAITokenCache,
) -> ChatCompletionResponse:
    if provider == "openai":
        return await openai_provider.create_chat_completion(
            http_client, native_model_id, body, credential
        )
    if provider == "anthropic":
        return await anthropic_provider.create_chat_completion(
            http_client, native_model_id, body, credential
        )
    if provider == "vertex_ai":
        return await vertex_provider.create_chat_completion(
            http_client, native_model_id, body, credential, token_cache
        )
    if provider == "ollama":
        return await ollama_provider.create_chat_completion(
            http_client, native_model_id, body, credential
        )
    if provider == "self_hosted":
        # Phase 5 (5.5, AC5.5.2): reuses `providers.ollama.
        # create_chat_completion` verbatim - vLLM/Ollama both expose an
        # OpenAI-compatible surface, so `credential` (an `OllamaCredential`,
        # see `services.self_hosted_providers.get_decrypted_self_hosted_
        # credential`) is already the shape that function expects.
        return await ollama_provider.create_chat_completion(
            http_client, native_model_id, body, credential
        )
    if provider == "openrouter":
        return await openrouter_provider.create_chat_completion(
            http_client, native_model_id, body, credential
        )
    raise AssertionError(f"no chat-completion dispatch for provider {provider!r}")


def _create_streaming(
    provider: str,
    native_model_id: str,
    body: ChatCompletionRequest,
    credential: Any,
    http_client: httpx.AsyncClient,
    token_cache: VertexAITokenCache,
) -> AsyncGenerator[ChatCompletionChunk, None]:
    if provider == "openai":
        return openai_provider.stream_chat_completion(http_client, native_model_id, body, credential)
    if provider == "anthropic":
        return anthropic_provider.stream_chat_completion(
            http_client, native_model_id, body, credential
        )
    if provider == "vertex_ai":
        return vertex_provider.stream_chat_completion(
            http_client, native_model_id, body, credential, token_cache
        )
    if provider == "ollama":
        return ollama_provider.stream_chat_completion(http_client, native_model_id, body, credential)
    if provider == "self_hosted":
        # Phase 5 (5.5, AC5.5.2) - see `_create_non_streaming`'s identical
        # branch above for why reusing `providers.ollama.
        # stream_chat_completion` verbatim is correct here.
        return ollama_provider.stream_chat_completion(http_client, native_model_id, body, credential)
    if provider == "openrouter":
        return openrouter_provider.stream_chat_completion(
            http_client, native_model_id, body, credential
        )
    raise AssertionError(f"no streaming chat-completion dispatch for provider {provider!r}")


def _sse_frame(chunk: ChatCompletionChunk) -> bytes:
    return f"data: {chunk.model_dump_json()}\n\n".encode("utf-8")


def _is_usage_chunk(chunk: ChatCompletionChunk) -> bool:
    return not chunk.choices and chunk.usage is not None


async def _sse_event_stream(
    *,
    request: Request,
    first_item: Any,
    remaining: AsyncGenerator[ChatCompletionChunk, None],
    timer: LatencyTimer,
    request_id: str,
    provider: str,
    model: str,
    idempotency_key: str | None,
    session: AsyncSession,
    user_id: uuid.UUID,
    team_id: uuid.UUID | None,
    service_account_key_id: uuid.UUID | None,
    personal_api_key_id: uuid.UUID | None,
    client_wants_usage: bool,
    original_model: str | None = None,
    failover_attempt: int = 0,
    failover_key_id: uuid.UUID | None = None,
    degraded_from_model: str | None = None,
    degraded_to_model: str | None = None,
    background_tasks: BackgroundTasks | None = None,
    self_hosted_provider_id: uuid.UUID | None = None,
    self_hosted_cost_basis_per_gpu_hour: Decimal | None = None,
) -> AsyncIterator[bytes]:
    disconnected = False
    result_status = "ok"
    captured_usage = None

    def _handle(chunk: ChatCompletionChunk) -> bytes | None:
        nonlocal captured_usage
        if _is_usage_chunk(chunk):
            captured_usage = chunk.usage
            return _sse_frame(chunk) if client_wants_usage else None
        return _sse_frame(chunk)

    try:
        if first_item is not _STREAM_EMPTY:
            frame = _handle(first_item)
            if frame is not None:
                yield frame
        async for chunk in remaining:
            if await request.is_disconnected():
                disconnected = True
                await remaining.aclose()
                break
            frame = _handle(chunk)
            if frame is not None:
                yield frame
    except ProviderUnsupportedRequestError:
        result_status = "unsupported_request"
        logger.warning("gateway_stream_unsupported_request", extra={"request_id": request_id})
    except ProviderCallError as exc:
        result_status = "provider_error"
        logger.warning(
            "gateway_stream_provider_error",
            extra={"request_id": request_id, "upstream_status_code": exc.status_code},
        )
    finally:
        # Phase 5 (5.5): marked unconditionally, right when the provider
        # stream itself finished being consumed (success, error, OR
        # disconnect) - BEFORE any charge/logging work below. This is the
        # "provider's own round-trip time" proxy `compute_self_hosted_
        # cost()` needs (design doc section 2.3(c): "the delta between the
        # pre_dispatch mark and provider_response_received/flush_complete
        # mark") for a STREAMING self-hosted request specifically - the
        # full stream duration (not just time-to-first-byte) is the better
        # proxy for actual self-hosted GPU busy time. Purely additive: does
        # not change `flush_complete`'s own pre-existing position/semantics
        # a few lines below, which every non-self-hosted request's
        # `latency_ms`/log timing still derives from unchanged.
        timer.mark("provider_stream_complete")
        cost_usd: Decimal | None = None
        prompt_tokens: int | None = None
        completion_tokens: int | None = None
        usage_log_id: uuid.UUID | None = None
        if disconnected:
            result_status = "client_disconnected"
        else:
            if result_status == "ok":
                if captured_usage is not None:
                    prompt_tokens = captured_usage.prompt_tokens
                    completion_tokens = captured_usage.completion_tokens
                    try:
                        precomputed_cost_usd: Decimal | None = None
                        if self_hosted_cost_basis_per_gpu_hour is not None:
                            wall_clock_latency_seconds = (
                                timer.marks["provider_stream_complete"]
                                - timer.marks["pre_dispatch"]
                            )
                            precomputed_cost_usd = compute_self_hosted_cost(
                                self_hosted_cost_basis_per_gpu_hour,
                                wall_clock_latency_seconds=wall_clock_latency_seconds,
                            )
                        charge = await record_usage_charge(
                            session,
                            user_id=user_id,
                            team_id=team_id,
                            model=model,
                            prompt_tokens=captured_usage.prompt_tokens,
                            completion_tokens=captured_usage.completion_tokens,
                            # BD-18: tasks added mid-stream still run - the
                            # injected BackgroundTasks is attached to the
                            # StreamingResponse and awaited by Starlette
                            # only after the whole body has been sent.
                            background_tasks=background_tasks,
                            app=request.app if background_tasks is not None else None,
                            precomputed_cost_usd=precomputed_cost_usd,
                        )
                        cost_usd = charge.cost
                    except Exception:
                        result_status = "charge_failed"
                        logger.error(
                            "gateway_stream_charge_failed",
                            exc_info=True,
                            extra={"request_id": request_id},
                        )
                else:
                    result_status = "usage_unavailable"
                    logger.warning(
                        "gateway_stream_usage_unavailable",
                        extra={"request_id": request_id, "provider": provider, "model": model},
                    )
            yield b"data: [DONE]\n\n"
        timer.mark("flush_complete")
        deltas = timer.deltas_ms()
        latency_ms = int(deltas.get("flush_complete", 0.0))
        log_gateway_request(
            request_id=request_id,
            endpoint=_ENDPOINT,
            provider=provider,
            model=model,
            stream=True,
            status=result_status,
            timer=timer,
            idempotency_key=idempotency_key,
        )
        usage_log_id = await record_usage_log(
            session,
            request_id=request_id,
            endpoint=_ENDPOINT,
            provider=provider,
            model=model,
            user_id=user_id,
            service_account_key_id=service_account_key_id,
            team_id=team_id,
            personal_api_key_id=personal_api_key_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            raw_provider_cost_usd=cost_usd,
            latency_ms=latency_ms,
            stream=True,
            status=result_status,
            success=result_status == "ok",
            failover_attempt=failover_attempt,
            failover_key_id=failover_key_id,
            original_model=original_model,
            degraded_from_model=degraded_from_model,
            degraded_to_model=degraded_to_model,
            self_hosted_provider_id=self_hosted_provider_id,
        )
        if (
            result_status == "ok"
            and degraded_from_model is not None
            and degraded_to_model is not None
            and prompt_tokens is not None
        ):
            await log_degradation_event(
                session,
                team_id=team_id,
                user_id=user_id,
                usage_log_id=usage_log_id,
                original_model=degraded_from_model,
                degraded_model=degraded_to_model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )


@router.post(_ENDPOINT, response_model=None)
async def create_chat_completion(
    request: Request,
    response: Response,
    body: ChatCompletionRequest,
    background_tasks: BackgroundTasks,
    ctx: GatewayCallerContext = Depends(require_gateway_credential),
    session: AsyncSession = Depends(get_db_session),
    key_provider: KeyProvider = Depends(get_key_provider),
    http_client: httpx.AsyncClient = Depends(get_provider_http_client),
    token_cache: VertexAITokenCache = Depends(get_vertex_token_cache),
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
    degradation_policy_cache: DegradationPolicyCache = Depends(get_degradation_policy_cache),
    self_hosted_cache: SelfHostedModelRouteCache = Depends(get_self_hosted_model_route_cache),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    sensitivity_label: str | None = Header(default=None, alias="X-Gatekey-Sensitivity-Label"),
) -> ChatCompletionResponse | StreamingResponse:
    # Phase 2 (BD-11): exactly one of these two is set per usage-log row,
    # by credential type.
    service_account_key_id = (
        ctx.credential_id if ctx.credential_type == "service_account" else None
    )
    personal_api_key_id = ctx.credential_id if ctx.credential_type == "personal" else None
    timer = LatencyTimer()
    request_id = new_request_id()
    idempotency_key = validate_idempotency_key(idempotency_key)
    client_wants_usage = body.stream_options is not None and body.stream_options.include_usage
    response_cache = ResponseCache(shared_state_store)
    response_headers: dict[str, str] = {}

    provider_for_log: str | None = None
    # Phase 5 (5.5): mirrors `provider_for_log`'s established "set once
    # resolve_route() succeeds, safe to read from the outer `except
    # GatekeyError` block even if a LATER pipeline step raises" pattern -
    # `route`/`effective_route` themselves are not guaranteed bound in that
    # block (e.g. `resolve_route()` itself raising `ModelNotFoundError`).
    self_hosted_provider_id_for_log: uuid.UUID | None = None
    try:
        # Phase 3 (BD-19): design doc section 5.3 - runs FIRST, before
        # resolve_route(), since a schedule block has nothing to do with
        # which model was requested. source_ip resolved once here (not at
        # its previous later point) and threaded into every synchronous
        # audit write below - a gateway caller's request always has a live
        # Request object, so this is never the "genuinely unavailable" None
        # case.
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

        # Phase 5 (5.5, AC5.5.4): `chat.py` is the ONLY gateway route that
        # ever passes `self_hosted_cache` here - see `resolve_route()`'s
        # docstring for why that's what structurally enforces "self-hosted
        # models are chat-completions only" (completions.py/embeddings.py
        # call `resolve_route(body.model)` with no second argument, unchanged).
        route = resolve_route(body.model, self_hosted_cache)
        provider_for_log = route.provider
        self_hosted_provider_id_for_log = route.self_hosted_provider_id
        # Phase 5 (5.5): the self-hosted route's cost basis, captured ONCE
        # here (not re-read from the cache again at charge time) - see
        # `compute_self_hosted_cost`'s call sites below. `None` for every
        # non-self-hosted request.
        self_hosted_route_entry = (
            self_hosted_cache.get(body.model) if route.provider == "self_hosted" else None
        )
        check_model_policy(body.model, cache, team_cache, ctx.team_id)
        if route.capability != ModelCapability.CHAT:
            raise HttpUnsupportedRequestError(
                f"Model '{body.model}' does not support chat completions "
                f"(registered capability: {route.capability.value})."
            )

        # Phase 4 (AC4.3): cache lookup - see module docstring for why this
        # is safe to run unconditionally for both the streaming and
        # non-streaming branches (a streaming request can never hit).
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
            cached_response = ChatCompletionResponse.model_validate(cache_check.entry.response_body)
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
                self_hosted_provider_id=route.self_hosted_provider_id,
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
            texts=[m.content for m in body.messages],
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
            for message, redacted in zip(body.messages, dlp_result.redacted_texts, strict=True):
                message.content = redacted
        check_content_classification(
            body.model, content_aware_cache, category_findings=dlp_result.category_findings
        )
        await check_budget_available(session, ctx.user_id, team_id=ctx.team_id)
        timer.mark("pre_dispatch")

        # Phase 4 (AC4.4, chat completions ONLY): substitute the effective
        # route/model AFTER the budget check, BEFORE provider dispatch - see
        # module docstring.
        effective_route: ModelRoute = route
        effective_model = body.model
        degradation_outcome = await check_and_apply_degradation(
            session, ctx, original_model=body.model, degradation_policy_cache=degradation_policy_cache
        )
        if degradation_outcome.triggered:
            assert degradation_outcome.degraded_model is not None
            candidate_model = degradation_outcome.degraded_model
            candidate_route = resolve_route(candidate_model)
            # Fix 5 (security review finding, request-time half): policy
            # can have been tightened AFTER this degradation policy was
            # configured (config-time validation alone is not sufficient)
            # - re-check the SUBSTITUTED model before ever dispatching to
            # it. See `common.revalidate_degraded_model()`'s docstring.
            still_allowed = await revalidate_degraded_model(
                session,
                ctx,
                degraded_model=candidate_model,
                degraded_route=candidate_route,
                model_policy_cache=cache,
                team_model_policy_cache=team_cache,
                content_aware_cache=content_aware_cache,
                residency_cache=residency_cache,
                category_findings=dlp_result.category_findings,
                source_ip=source_ip,
            )
            if not still_allowed:
                # Design doc section 7.4: "Skip degradation; hard block at
                # budget" - never silently fall back to dispatching the
                # original (undegraded) model at full cost either.
                await raise_hard_budget_block_after_degradation_skip(
                    session, ctx.user_id, ctx.team_id
                )
            effective_model = candidate_model
            effective_route = candidate_route
        response_headers.update(build_degradation_headers(degradation_outcome))

        if body.stream:

            async def _streaming_call(credential: Any) -> tuple[Any, Any]:
                gen = _create_streaming(
                    effective_route.provider,
                    effective_route.native_model_id,
                    body,
                    credential,
                    http_client,
                    token_cache,
                )
                try:
                    first_item: Any = await gen.__anext__()
                except StopAsyncIteration:
                    first_item = _STREAM_EMPTY
                return gen, first_item

            try:
                # Phase 5 (5.5, design doc section 2.3(b)/wiring checklist
                # row 7): self-hosted endpoints never participate in Phase
                # 4's provider_keys-scoped backup-group failover mechanism -
                # `call_self_hosted_provider()` instead, a simpler sibling
                # with no retry/failover.
                if effective_route.provider == "self_hosted":
                    failover = await call_self_hosted_provider(
                        session,
                        route=effective_route,
                        key_provider=key_provider,
                        call_fn=_streaming_call,
                    )
                else:
                    failover = await call_provider_with_failover(
                        session,
                        request.app,
                        route=effective_route,
                        org_id=ctx.org_id,
                        team_id=ctx.team_id,
                        request_id=request_id,
                        key_provider=key_provider,
                        health_store=shared_state_store,
                        team_override_cache=team_override_cache,
                        call_fn=_streaming_call,
                    )
            except ProviderUnsupportedRequestError as exc:
                raise HttpUnsupportedRequestError(str(exc)) from None
            except ProviderCallError as exc:
                raise ProviderUpstreamError(str(exc), upstream_status_code=exc.status_code) from None
            gen, first_item = failover.result
            timer.mark("provider_response_received")
            response_headers.update(build_failover_headers(failover))

            return StreamingResponse(
                _sse_event_stream(
                    request=request,
                    first_item=first_item,
                    remaining=gen,
                    timer=timer,
                    request_id=request_id,
                    provider=effective_route.provider,
                    model=effective_model,
                    original_model=degradation_outcome.original_model,
                    idempotency_key=idempotency_key,
                    session=session,
                    user_id=ctx.user_id,
                    team_id=ctx.team_id,
                    service_account_key_id=service_account_key_id,
                    personal_api_key_id=personal_api_key_id,
                    client_wants_usage=client_wants_usage,
                    failover_attempt=failover.attempt,
                    failover_key_id=failover.used_key_id,
                    degraded_from_model=degradation_outcome.original_model,
                    degraded_to_model=degradation_outcome.degraded_model,
                    background_tasks=background_tasks,
                    self_hosted_provider_id=effective_route.self_hosted_provider_id,
                    self_hosted_cost_basis_per_gpu_hour=(
                        self_hosted_route_entry.cost_basis_per_gpu_hour
                        if effective_route.provider == "self_hosted" and self_hosted_route_entry is not None
                        else None
                    ),
                ),
                media_type="text/event-stream",
                headers=response_headers,
            )

        async def _non_streaming_call(credential: Any) -> ChatCompletionResponse:
            return await _create_non_streaming(
                effective_route.provider,
                effective_route.native_model_id,
                body,
                credential,
                http_client,
                token_cache,
            )

        try:
            # Phase 5 (5.5, design doc section 2.3(b)/wiring checklist row
            # 7) - see the identical branch in the streaming block above.
            if effective_route.provider == "self_hosted":
                failover = await call_self_hosted_provider(
                    session,
                    route=effective_route,
                    key_provider=key_provider,
                    call_fn=_non_streaming_call,
                )
            else:
                failover = await call_provider_with_failover(
                    session,
                    request.app,
                    route=effective_route,
                    org_id=ctx.org_id,
                    team_id=ctx.team_id,
                    request_id=request_id,
                    key_provider=key_provider,
                    health_store=shared_state_store,
                    team_override_cache=team_override_cache,
                    call_fn=_non_streaming_call,
                )
        except ProviderUnsupportedRequestError as exc:
            raise HttpUnsupportedRequestError(str(exc)) from None
        except ProviderCallError as exc:
            raise ProviderUpstreamError(str(exc), upstream_status_code=exc.status_code) from None
        provider_response = failover.result
        timer.mark("provider_response_received")
        response_headers.update(build_failover_headers(failover))

        cost_usd: Decimal | None = None
        precomputed_cost_usd: Decimal | None = None
        if effective_route.provider == "self_hosted" and self_hosted_route_entry is not None:
            # Phase 5 (5.5, AC5.5.7): the provider's own round-trip time -
            # the delta between `pre_dispatch` and `provider_response_
            # received` (design doc section 2.3(c)) - never total request
            # latency including DLP/budget-check overhead.
            wall_clock_latency_seconds = (
                timer.marks["provider_response_received"] - timer.marks["pre_dispatch"]
            )
            precomputed_cost_usd = compute_self_hosted_cost(
                self_hosted_route_entry.cost_basis_per_gpu_hour,
                wall_clock_latency_seconds=wall_clock_latency_seconds,
            )
        try:
            charge = await record_usage_charge(
                session,
                user_id=ctx.user_id,
                team_id=ctx.team_id,
                model=effective_model,
                prompt_tokens=provider_response.usage.prompt_tokens,
                completion_tokens=provider_response.usage.completion_tokens,
                background_tasks=background_tasks,
                app=request.app,
                precomputed_cost_usd=precomputed_cost_usd,
            )
            cost_usd = charge.cost
        except PricingEntryMissingError:
            timer.mark("flush_complete")
            await record_usage_log(
                session,
                request_id=request_id,
                endpoint=_ENDPOINT,
                provider=effective_route.provider,
                model=effective_model,
                user_id=ctx.user_id,
                service_account_key_id=service_account_key_id,
                team_id=ctx.team_id,
                personal_api_key_id=personal_api_key_id,
                prompt_tokens=provider_response.usage.prompt_tokens,
                completion_tokens=provider_response.usage.completion_tokens,
                cost_usd=None,
                latency_ms=int(timer.deltas_ms().get("flush_complete", 0.0)),
                stream=False,
                status="internal_error",
                success=False,
                failover_attempt=failover.attempt,
                failover_key_id=failover.used_key_id,
                original_model=degradation_outcome.original_model,
                degraded_from_model=degradation_outcome.original_model,
                degraded_to_model=degradation_outcome.degraded_model,
                self_hosted_provider_id=effective_route.self_hosted_provider_id,
            )
            raise

        # Phase 4 (AC4.3.7): write-through cache population - miss only,
        # never for a degraded response (see module docstring), and never
        # when this request's content was redacted by DLP (AC4.3.6).
        await write_response_cache(
            ctx,
            route,
            cache_check=cache_check,
            response_cache=response_cache,
            response_body=provider_response.model_dump(mode="json"),
            input_tokens=provider_response.usage.prompt_tokens,
            output_tokens=provider_response.usage.completion_tokens,
            skip_write=dlp_result.redacted_texts is not None or degradation_outcome.triggered,
        )

        timer.mark("flush_complete")
        log_gateway_request(
            request_id=request_id,
            endpoint=_ENDPOINT,
            provider=effective_route.provider,
            model=effective_model,
            stream=False,
            status="ok",
            timer=timer,
            idempotency_key=idempotency_key,
        )
        usage_log_id = await record_usage_log(
            session,
            request_id=request_id,
            endpoint=_ENDPOINT,
            provider=effective_route.provider,
            model=effective_model,
            user_id=ctx.user_id,
            service_account_key_id=service_account_key_id,
            team_id=ctx.team_id,
            personal_api_key_id=personal_api_key_id,
            prompt_tokens=provider_response.usage.prompt_tokens,
            completion_tokens=provider_response.usage.completion_tokens,
            cost_usd=cost_usd,
            raw_provider_cost_usd=cost_usd,
            latency_ms=int(timer.deltas_ms().get("flush_complete", 0.0)),
            stream=False,
            status="ok",
            success=True,
            failover_attempt=failover.attempt,
            failover_key_id=failover.used_key_id,
            original_model=degradation_outcome.original_model,
            degraded_from_model=degradation_outcome.original_model,
            degraded_to_model=degradation_outcome.degraded_model,
            self_hosted_provider_id=effective_route.self_hosted_provider_id,
        )
        if degradation_outcome.triggered:
            assert degradation_outcome.original_model is not None
            assert degradation_outcome.degraded_model is not None
            await log_degradation_event(
                session,
                team_id=ctx.team_id,
                user_id=ctx.user_id,
                usage_log_id=usage_log_id,
                original_model=degradation_outcome.original_model,
                degraded_model=degradation_outcome.degraded_model,
                prompt_tokens=provider_response.usage.prompt_tokens,
                completion_tokens=provider_response.usage.completion_tokens,
            )
        for key, value in response_headers.items():
            response.headers[key] = value
        return provider_response
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
            stream=body.stream,
            status=exc.code,
            success=False,
            self_hosted_provider_id=self_hosted_provider_id_for_log,
        )
        raise
