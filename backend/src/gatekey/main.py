"""FastAPI application factory for the Gatekey backend."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from gatekey.api.v1.admin.access_schedule import router as admin_access_schedule_router
from gatekey.api.v1.admin.audit_chain import router as admin_audit_chain_router
from gatekey.api.v1.admin.audit_entries import router as admin_audit_entries_router
from gatekey.api.v1.admin.backup_groups import router as admin_backup_groups_router
from gatekey.api.v1.admin.cache_admin import router as admin_cache_router
from gatekey.api.v1.admin.caching_settings import router as admin_caching_settings_router
from gatekey.api.v1.admin.caching_settings import team_router as admin_caching_settings_team_router
from gatekey.api.v1.admin.compliance_settings import router as admin_compliance_settings_router
from gatekey.api.v1.admin.content_aware_rules import router as admin_content_aware_rules_router
from gatekey.api.v1.admin.custom_models import router as admin_custom_models_router
from gatekey.api.v1.admin.degradation_events import router as admin_degradation_events_router
from gatekey.api.v1.admin.degradation_policy import router as admin_degradation_policy_router
from gatekey.api.v1.admin.degradation_policy import team_router as admin_degradation_policy_team_router
from gatekey.api.v1.admin.dlp_policy import router as admin_dlp_policy_router
from gatekey.api.v1.admin.drift_detector import router as admin_drift_detector_router
from gatekey.api.v1.admin.failover_events import router as admin_failover_events_router
from gatekey.api.v1.admin.failover_override import team_router as admin_failover_override_team_router
from gatekey.api.v1.admin.identity import router as admin_identity_router
from gatekey.api.v1.admin.join_requests import router as admin_join_requests_router
from gatekey.api.v1.admin.model_policy import router as admin_model_policy_router
from gatekey.api.v1.admin.org_settings import router as admin_org_settings_router
from gatekey.api.v1.admin.provider_key_health import router as admin_provider_key_health_router
from gatekey.api.v1.admin.providers import router as admin_providers_router
from gatekey.api.v1.admin.rate_limits import router as admin_rate_limits_router
from gatekey.api.v1.admin.rate_limits import team_router as admin_rate_limits_team_router
from gatekey.api.v1.admin.residency_rules import router as admin_residency_rules_router
from gatekey.api.v1.admin.rotation_policy import org_router as admin_rotation_policy_org_router
from gatekey.api.v1.admin.rotation_policy import provider_router as admin_rotation_policy_provider_router
from gatekey.api.v1.admin.scim_config import router as admin_scim_config_router
from gatekey.api.v1.admin.self_hosted_providers import router as admin_self_hosted_providers_router
from gatekey.api.v1.admin.sensitivity_label_mappings import (
    router as admin_sensitivity_label_mappings_router,
)
from gatekey.api.v1.admin.service_accounts import router as admin_service_accounts_router
from gatekey.api.v1.admin.shadow_ai import router as admin_shadow_ai_router
from gatekey.api.v1.admin.usage import router as admin_usage_router
from gatekey.api.v1.admin.bootstrap import router as admin_bootstrap_router
from gatekey.api.v1.admin.users import router as admin_users_router
from gatekey.api.v1.auth import router as auth_router
from gatekey.api.v1.auth_device import me_router as cli_sync_me_router
from gatekey.api.v1.auth_device import router as auth_device_router
from gatekey.api.v1.gateway import router as gateway_router
from gatekey.api.v1.keys import admin_router as admin_keys_router
from gatekey.api.v1.keys import delegated_router as delegated_keys_router
from gatekey.api.v1.keys import router as keys_router
from gatekey.api.v1.me import router as me_router
from gatekey.api.v1.model_access import router as model_access_router
from gatekey.api.v1.onboarding import router as onboarding_router
from gatekey.api.v1.scim import groups_router as scim_groups_router
from gatekey.api.v1.scim import users_router as scim_users_router
from gatekey.api.v1.shadow_ai_ingest import router as shadow_ai_ingest_router
from gatekey.api.v1.teams import router as teams_router
from gatekey.config import Settings, get_settings
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.session import create_engine, create_session_factory
from gatekey.errors import register_exception_handlers
from gatekey.observability import configure_logging, install_observability
from gatekey.providers.model_registry import MODEL_REGISTRY
from gatekey.providers.vertex_ai import VertexAITokenCache
from gatekey.services.dlp import build_analyzer_engine
from gatekey.services.model_policy import (
    ContentAwareRuleCache,
    MemberModelPolicyCache,
    ModelPolicyCache,
    ModelPolicySnapshot,
    TeamModelPolicyCache,
    load_content_aware_rule_snapshot,
    load_member_policy_snapshot,
    load_policy_snapshot,
    load_team_policy_snapshot,
)
from gatekey.services.access_schedules import (
    AccessScheduleCache,
    load_access_schedule_snapshot,
    load_holiday_dates,
)
from gatekey.services.cli_refresh_credentials import DeviceAuthStore
from gatekey.services.compliance_settings import get_effective_compliance_settings
from gatekey.services.degradation import DegradationPolicyCache, load_degradation_policy_snapshot
from gatekey.services.provider_key_health import (
    TeamFailoverOverrideCache,
    load_team_failover_override_snapshot,
)
from gatekey.services.rate_limit import RateLimitCache, load_rate_limit_cache_snapshot
from gatekey.services.residency import ResidencyRuleCache, load_residency_rule_snapshot
from gatekey.services.response_cache import CachingSettingsCache, load_caching_settings_snapshot
from gatekey.services.scheduler import run_scheduler_loop
from gatekey.services.custom_models import CustomModelRouteCache, load_custom_model_route_snapshot
from gatekey.services.self_hosted_providers import (
    SelfHostedModelRouteCache,
    load_self_hosted_model_route_snapshot,
)
from gatekey.services.shared_state import InProcessSharedStateStore, RedisSharedStateStore
from gatekey.services.scim import register_scim_exception_handlers
from gatekey.services.sessions import SESSION_COOKIE_NAME

logger = logging.getLogger("gatekey")

# Design doc section 6.1: bounded connection pool for the shared, process-
# lifetime outbound `httpx.AsyncClient` used for every provider inference
# call - see `_lifespan` below.
_PROVIDER_HTTP_CLIENT_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=100)

# Phase 1.3 (Model Access Governance - Basic), design doc section 2.2/ADR-3:
# bound on each individual startup query used to warm `ModelPolicyCache`
# (both the initial attempt and every self-heal retry below). A
# hung/unreachable DB at process start must not stall startup indefinitely -
# see `_lifespan` below for the fail-open behavior on timeout/any failure.
_MODEL_POLICY_BOOTSTRAP_TIMEOUT_SECONDS = 5.0

# Phase 1.3 design doc section 2.2/ADR-3 addendum: bounded, in-process
# self-heal for a failed initial bootstrap (security review finding - see
# `_model_policy_self_heal` below). Module-level so a test can monkeypatch
# these down to make the retry loop fast without changing production
# behavior.
_MODEL_POLICY_SELF_HEAL_MAX_ATTEMPTS = 5
_MODEL_POLICY_SELF_HEAL_INITIAL_BACKOFF_SECONDS = 2.0
_MODEL_POLICY_SELF_HEAL_BACKOFF_CEILING_SECONDS = 30.0


async def _load_model_policy_snapshot_bounded(app: FastAPI) -> ModelPolicySnapshot:
    """One bounded attempt to load the org's model policy from the DB.

    Shared by the initial startup bootstrap and the self-heal retry loop
    below (`_model_policy_self_heal`) so both go through the exact same
    timeout/session/query path - see `_lifespan` and that function for the
    differing handling of success/failure.
    """
    async with asyncio.timeout(_MODEL_POLICY_BOOTSTRAP_TIMEOUT_SECONDS):
        async with app.state.db_session_factory() as session:
            return await load_policy_snapshot(session)


async def _warm_team_model_policy_cache(app: FastAPI) -> None:
    """Bounded, fail-open warm of `TeamModelPolicyCache` (Phase 2, BD-12).

    Same ADR-3 pattern as the org-baseline bootstrap above: any failure
    (DB unreachable, timeout) is caught and logged, and the cache stays at
    its empty default - which means "no team restriction", the safe/
    permissive absence-of-row state, exactly like the org cache's
    `unconfigured` default. Never raises. Called from the lifespan's
    initial bootstrap and again at the end of a successful org-policy
    self-heal (the two share the same failure cause - a DB that wasn't
    ready yet)."""
    try:
        async with asyncio.timeout(_MODEL_POLICY_BOOTSTRAP_TIMEOUT_SECONDS):
            async with app.state.db_session_factory() as session:
                snapshot = await load_team_policy_snapshot(session)
        app.state.team_model_policy_cache.set_all(snapshot)
    except Exception:
        logger.warning("team_model_policy_bootstrap_failed", exc_info=True)


async def _warm_member_model_policy_cache(app: FastAPI) -> None:
    """Bounded, fail-open warm of `MemberModelPolicyCache` (per-team-member
    model narrowing, one layer below `TeamModelPolicyCache`).

    Identical ADR-3-style contract to `_warm_team_model_policy_cache` above:
    any failure (DB unreachable, timeout) is caught and logged, and the
    cache stays at its empty default - which means "no member overlay", the
    safe/permissive absence-of-row state, exactly like the team cache's own
    default. Never raises. Called from the lifespan's initial bootstrap and
    again at the end of a successful org-policy self-heal, alongside the
    team-overlay warm (same shared failure cause - a DB that wasn't ready
    yet)."""
    try:
        async with asyncio.timeout(_MODEL_POLICY_BOOTSTRAP_TIMEOUT_SECONDS):
            async with app.state.db_session_factory() as session:
                snapshot = await load_member_policy_snapshot(session)
        app.state.member_model_policy_cache.set_all(snapshot)
    except Exception:
        logger.warning("member_model_policy_bootstrap_failed", exc_info=True)


async def _warm_residency_and_content_aware_caches(app: FastAPI) -> None:
    """Bounded, fail-open warm of `ResidencyRuleCache`/`ContentAwareRuleCache`
    (Phase 3, BD-3/BD-5) - identical ADR-3-style contract to `_warm_team_
    model_policy_cache` above: any failure is caught and logged, and each
    cache stays at its empty default (residency: no rule configured anywhere
    -> unrestricted; content-aware: no category enabled -> no extra
    restriction), the same safe/permissive absence-of-row state every other
    Phase 1.3/Phase 2 cache already defaults to. Never raises."""
    try:
        async with asyncio.timeout(_MODEL_POLICY_BOOTSTRAP_TIMEOUT_SECONDS):
            async with app.state.db_session_factory() as session:
                org_rule, team_rules = await load_residency_rule_snapshot(session)
                content_aware_snapshot = await load_content_aware_rule_snapshot(session)
        app.state.residency_rule_cache.set_all(org_rule, team_rules)
        app.state.content_aware_rule_cache.set_all(content_aware_snapshot)
    except Exception:
        logger.warning("residency_content_aware_bootstrap_failed", exc_info=True)


async def _warm_access_schedule_cache(app: FastAPI) -> None:
    """Bounded, fail-open warm of `AccessScheduleCache` (Phase 3, BD-16) -
    identical ADR-3-style contract to `_warm_residency_and_content_aware_
    caches` above: any failure is caught and logged, and the cache stays at
    its empty/UTC/no-holidays default (= unrestricted, AC9.3's off-by-
    default posture) until the next admin write or process restart. Never
    raises."""
    try:
        async with asyncio.timeout(_MODEL_POLICY_BOOTSTRAP_TIMEOUT_SECONDS):
            async with app.state.db_session_factory() as session:
                org, team, service_account = await load_access_schedule_snapshot(session)
                holiday_dates = await load_holiday_dates(session)
                compliance = await get_effective_compliance_settings(session)
        app.state.access_schedule_cache.set_all(
            org=org,
            team=team,
            service_account=service_account,
            timezone_name=compliance.access_schedule_timezone,
            holiday_dates=holiday_dates,
        )
    except Exception:
        logger.warning("access_schedule_bootstrap_failed", exc_info=True)


async def _warm_rate_limit_cache(app: FastAPI) -> None:
    """Bounded, fail-open warm of `RateLimitCache` (Phase 4, BD-2) - Fix 6
    (NFR gap, AC4.3.4): this cache existed but was never constructed/warmed
    before this fix (`check_rate_limit()` read `RateLimitRule` rows straight
    from Postgres on every single gateway request instead). Same
    ADR-3-style contract as `_warm_residency_and_content_aware_caches`
    above: any failure is caught and logged, and the cache stays at its
    empty default (= no rule configured anywhere, the permissive
    absence-of-row state that is also `check_rate_limit()`'s own
    "unconfigured" fallback). Never raises."""
    try:
        async with asyncio.timeout(_MODEL_POLICY_BOOTSTRAP_TIMEOUT_SECONDS):
            async with app.state.db_session_factory() as session:
                org_rules, team_rules, user_rules = await load_rate_limit_cache_snapshot(session)
        app.state.rate_limit_cache.set_all(org_rules, team_rules, user_rules)
    except Exception:
        logger.warning("rate_limit_cache_bootstrap_failed", exc_info=True)


async def _warm_caching_settings_cache(app: FastAPI) -> None:
    """Bounded, fail-open warm of `CachingSettingsCache` (Phase 4, BD-3) -
    Fix 6 (NFR gap, AC4.3.4): same rationale/contract as `_warm_rate_limit_
    cache` above - `check_response_cache()` previously read the org kill
    switch AND every team's own cache columns straight from Postgres on
    every gateway request. An empty cache defaults every team to caching
    disabled (`resolve_effective_caching_config()`'s "no team cache entry"
    branch) - the same safe posture a missing `teams` row already produces
    on the live-DB path. Never raises."""
    try:
        async with asyncio.timeout(_MODEL_POLICY_BOOTSTRAP_TIMEOUT_SECONDS):
            async with app.state.db_session_factory() as session:
                org_settings, team_settings = await load_caching_settings_snapshot(session)
        app.state.caching_settings_cache.set_all(org_settings, team_settings)
    except Exception:
        logger.warning("caching_settings_cache_bootstrap_failed", exc_info=True)


async def _warm_degradation_policy_cache(app: FastAPI) -> None:
    """Bounded, fail-open warm of `DegradationPolicyCache` (Phase 4, BD-5) -
    Fix 6 (NFR gap, AC4.3.4): same rationale/contract as `_warm_rate_limit_
    cache` above - `check_and_apply_degradation()` previously ran two live
    DB point lookups on every gateway request. An empty cache means "no
    degradation policy configured anywhere", the same permissive/no-op
    default `load_effective_degradation_policy()`'s live-DB "no row" case
    already produces. Never raises."""
    try:
        async with asyncio.timeout(_MODEL_POLICY_BOOTSTRAP_TIMEOUT_SECONDS):
            async with app.state.db_session_factory() as session:
                org_policy, team_policies = await load_degradation_policy_snapshot(session)
        app.state.degradation_policy_cache.set_all(org_policy, team_policies)
    except Exception:
        logger.warning("degradation_policy_cache_bootstrap_failed", exc_info=True)


async def _warm_team_failover_override_cache(app: FastAPI) -> None:
    """Bounded, fail-open warm of `TeamFailoverOverrideCache` (Phase 4,
    BD-4) - identical ADR-3-style contract to `_warm_residency_and_content_
    aware_caches` above: any failure is caught and logged, and the cache
    stays at its empty default (= no team has narrowed failover off, the
    permissive absence-of-row state - design doc section 1.3). Never
    raises."""
    try:
        async with asyncio.timeout(_MODEL_POLICY_BOOTSTRAP_TIMEOUT_SECONDS):
            async with app.state.db_session_factory() as session:
                overrides = await load_team_failover_override_snapshot(session)
        app.state.team_failover_override_cache.set_all(overrides)
    except Exception:
        logger.warning("team_failover_override_bootstrap_failed", exc_info=True)


async def _warm_self_hosted_model_route_cache(app: FastAPI) -> None:
    """Bounded, fail-open warm of `SelfHostedModelRouteCache` (Phase 5 -
    Differentiators, 5.5) - identical ADR-3-style contract to
    `_warm_residency_and_content_aware_caches` above: any failure is caught
    and logged, and the cache stays at its empty default (= no self-hosted
    model is routable anywhere until the next admin write or process
    restart - the safe/permissive-in-the-"deny" direction default, since an
    empty cache means `resolve_route()`'s self-hosted fallback finds nothing
    and every self-hosted model id 404s exactly like any other unknown
    model). Never raises."""
    try:
        async with asyncio.timeout(_MODEL_POLICY_BOOTSTRAP_TIMEOUT_SECONDS):
            async with app.state.db_session_factory() as session:
                snapshot = await load_self_hosted_model_route_snapshot(session)
        app.state.self_hosted_model_route_cache.set_all(snapshot)
    except Exception:
        logger.warning("self_hosted_model_route_cache_bootstrap_failed", exc_info=True)


async def _warm_custom_model_route_cache(app: FastAPI) -> None:
    """Bounded, fail-open warm of `CustomModelRouteCache` (Custom Model
    Registry / Admin-Managed BYOK Models, CMR-6, technical design doc
    section 5 row 3) - identical ADR-3-style contract to `_warm_self_hosted_
    model_route_cache` above: any failure is caught and logged, and the
    cache stays at its empty default (= no custom model is routable
    anywhere until the next admin write or process restart - the safe/
    permissive-in-the-"deny" direction default, since an empty cache means
    `resolve_route()`'s custom-model fallback finds nothing and every
    custom-model name 404s exactly like any other unknown model). Never
    raises.

    Replaces CMR-4's deliberate empty-construction stopgap (see
    `_lifespan`'s comment at the `app.state.custom_model_route_cache =
    CustomModelRouteCache()` line) with the real DB-backed warm -
    `load_custom_model_route_snapshot()` already exists (CMR-2) and already
    filters to `verified = true` rows only, so this function's shape is a
    direct, line-for-line mirror of `_warm_self_hosted_model_route_cache`
    above, just against the new table/cache pair."""
    try:
        async with asyncio.timeout(_MODEL_POLICY_BOOTSTRAP_TIMEOUT_SECONDS):
            async with app.state.db_session_factory() as session:
                snapshot = await load_custom_model_route_snapshot(session)
        app.state.custom_model_route_cache.set_all(snapshot)
    except Exception:
        logger.warning("custom_model_route_cache_bootstrap_failed", exc_info=True)


def _log_custom_model_shadowing(app: FastAPI) -> None:
    """Startup-only shadowing cross-reference (technical design doc section
    2.4a/5 row 4/6.3) - called immediately after `_warm_custom_model_route_
    cache` succeeds (or fails-open to empty; either way the cache is in its
    final startup state by the time this runs).

    Cross-references `CustomModelRouteCache`'s now-warmed key set against
    the static `MODEL_REGISTRY`'s keys via a plain, zero-I/O `frozenset`
    intersection - a collision means an already-registered, already-
    verified custom model's name has been shadowed by a NEW static registry
    entry shipped in *this* release. This is deliberately the only
    detection point for that specific ordering: the write-time collision
    guard (`services.custom_models._validate_custom_model_write`, guard #1)
    only runs at registration time, before the colliding static key
    existed, so it cannot catch this inverse-order case - see design doc
    section 6.3's explicit "startup shadowing log is a one-time, per-process
    check" rationale.

    Logs one `ERROR`-level line per colliding name, naming both the org
    (single-org deployment - `load_custom_model_route_snapshot()` only ever
    loads `DEFAULT_ORG_ID`'s rows, so this is unambiguous) and the shadowed
    custom model's id, exactly per design doc section 2.4a's pseudocode.
    Never a `RuntimeError`, never raises for any reason - a code upgrade
    that introduces a shadowing collision must be a loud warning, never a
    reason the gateway fails to start (design doc section 2.4a). Purely
    synchronous / zero I/O by design - it only reads the already-warmed
    in-memory cache and the already-imported `MODEL_REGISTRY` dict, no
    `await` needed."""
    try:
        cache: CustomModelRouteCache = app.state.custom_model_route_cache
        colliding = cache.known_model_ids() & MODEL_REGISTRY.keys()
        for name in sorted(colliding):
            entry = cache.get(name)
            if entry is None:  # pragma: no cover - defensive; nothing mutates the cache concurrently here.
                continue
            logger.error(
                "custom_model_shadowed_by_static_registry",
                extra={
                    "org_id": str(DEFAULT_ORG_ID),
                    "model": name,
                    "custom_model_id": str(entry.id),
                },
            )
    except Exception:
        logger.warning("custom_model_shadowing_check_failed", exc_info=True)


async def _model_policy_self_heal(app: FastAPI) -> None:
    """Bounded, in-process retry loop for a failed model-policy bootstrap.

    Security review finding on Phase 1.3 (see design doc section 2.2/ADR-3
    addendum): without this, a transient DB-not-ready-yet condition at
    process start (ADR-3's own primary example - "Postgres still finishing
    its own startup in `docker-compose up`") permanently latches the cache
    onto the permissive `unconfigured` default for the rest of the
    process's life, even though every subsequent `fetch_credential()` call
    on the gateway hot path succeeds fine once the DB comes up seconds
    later. This coroutine is scheduled as a background task only when the
    one-shot bootstrap in `_lifespan` fails; it retries
    `load_policy_snapshot()` (via `_load_model_policy_snapshot_bounded`)
    with exponential backoff, capped at
    `_MODEL_POLICY_SELF_HEAL_MAX_ATTEMPTS` attempts and a backoff ceiling of
    `_MODEL_POLICY_SELF_HEAL_BACKOFF_CEILING_SECONDS` seconds - enough
    attempts/spread to ride out a several-seconds startup-ordering hiccup,
    without hammering a genuinely-down DB forever at high frequency.

    On first success, replaces the cache's snapshot with the real
    DB-backed value and returns. If every attempt fails, logs once and
    gives up - the cache remains at its permissive ADR-3 default until the
    next admin `PUT` or a process restart, i.e. exactly today's
    (pre-self-heal) behavior as the final fallback, not a new failure mode.

    Security review finding, second round (design doc section 2.2/ADR-3
    addendum): unlike the one-shot bootstrap in `_lifespan` (which always
    runs to completion before `yield`, i.e. strictly before the app serves
    any traffic), this coroutine runs as a background task *concurrently*
    with live traffic - including live admin `PUT /v1/admin/model-policy`
    calls - for up to about a minute after a failed bootstrap. If an admin's
    `PUT` commits and calls `cache.set()` while this loop's own
    `load_policy_snapshot()` SELECT for the current attempt is already in
    flight, this loop's stale read must not clobber the admin's newer
    write. It guards against that by capturing
    `app.state.model_policy_cache.get_generation()` immediately before
    issuing the read, and writing the result back via
    `ModelPolicyCache.set_if_current()` (a compare-and-set on that
    generation) instead of the unconditional `set()`. If the CAS reports it
    was superseded, the cache is already correct (the `PUT` fixed it) - this
    loop stops and logs that fact rather than treating it as a failure that
    should retry again.

    Cancelled cleanly on app shutdown by `_lifespan`'s `finally` block. This
    coroutine spends essentially all of its time inside `asyncio.sleep`,
    which responds to cancellation immediately (no in-flight DB call to
    unwind in the common case), which is also what keeps it harmless
    against the fake, unreachable DSN used by the existing gateway unit
    test harness (design doc section 6): those tests build the app, make
    one or two requests, and tear it down - well inside the initial
    backoff window - so this task is cancelled while still asleep and never
    gets a chance to retry against a DSN that will never resolve.
    """
    cache = app.state.model_policy_cache
    backoff = _MODEL_POLICY_SELF_HEAL_INITIAL_BACKOFF_SECONDS
    for attempt in range(1, _MODEL_POLICY_SELF_HEAL_MAX_ATTEMPTS + 1):
        await asyncio.sleep(backoff)
        # Captured immediately before the read this attempt is about to
        # start (not any earlier) - see this function's docstring and
        # `ModelPolicyCache.get_generation()`'s.
        expected_generation = cache.get_generation()
        try:
            snapshot = await _load_model_policy_snapshot_bounded(app)
        except Exception:
            logger.warning(
                "model_policy_bootstrap_retry_failed",
                exc_info=True,
                extra={"attempt": attempt},
            )
            backoff = min(backoff * 2, _MODEL_POLICY_SELF_HEAL_BACKOFF_CEILING_SECONDS)
            continue
        if cache.set_if_current(snapshot, expected_generation):
            logger.info("model_policy_bootstrap_self_healed", extra={"attempt": attempt})
            # Phase 2 (BD-12): the team-overlay warm shares the same failure
            # cause (DB not ready at startup) - retry it alongside the org
            # heal. Fail-open/never-raises; no CAS needed (the only other
            # writer, the future team-restriction PUT route, uses per-team
            # `set_team` on committed data, and an empty entry just means
            # "no restriction" - the permissive default either way).
            await _warm_team_model_policy_cache(app)
            # Same rationale, one layer further: the per-member overlay warm.
            await _warm_member_model_policy_cache(app)
        else:
            # A concurrent admin PUT committed (and called cache.set())
            # while this attempt's own read was in flight - the cache is
            # already correct via that PUT, and it's the authoritative
            # write. Nothing left to do; do NOT retry (that could re-race
            # or apply this attempt's now-stale value on a later attempt).
            logger.info(
                "model_policy_bootstrap_self_heal_superseded_by_put",
                extra={"attempt": attempt},
            )
        return
    logger.warning("model_policy_bootstrap_self_heal_exhausted")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI application.

    Loading `Settings` here (rather than only at process start) means any
    misconfiguration - e.g. a missing/malformed GATEKEY_MASTER_KEY - raises
    immediately and prevents the app from serving traffic with an unusable
    or insecure configuration.
    """
    settings = settings or get_settings()
    # Install the structured-logging formatter first, so lifespan startup
    # (migrations, cache warms, scheduler) already logs with extra fields
    # rendered - see `observability.configure_logging`.
    configure_logging(settings.GATEKEY_LOG_FORMAT, settings.GATEKEY_LOG_LEVEL)

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Built once here and reused for the life of the process (see
        # db/session.py) - a new engine per request would defeat connection
        # pooling. Disposed on shutdown.
        engine = create_engine(settings)
        app.state.db_engine = engine
        app.state.db_session_factory = create_session_factory(engine)

        # Pooled, process-lifetime client for outbound provider inference
        # calls (BD-9/design doc section 6.1) - constructing a new client
        # per gateway request would defeat connection pooling and add a
        # fresh TLS handshake's worth of latency to every call. Closed on
        # shutdown alongside the DB engine.
        app.state.provider_http_client = httpx.AsyncClient(limits=_PROVIDER_HTTP_CLIENT_LIMITS)

        # Single long-lived Vertex AI OAuth token cache (see
        # `providers.vertex_ai.VertexAITokenCache`'s docstring for why this
        # must be constructed exactly once per process and shared across
        # requests, never rebuilt per call).
        app.state.vertex_token_cache = VertexAITokenCache()

        # Phase 1.3 (Model Access Governance - Basic), design doc section
        # 2.2/ADR-3. Constructed with the safe, zero-I/O `unconfigured`
        # default *first* - the real DB-backed value is layered on top only
        # if the bootstrap load succeeds within the bound below. Any
        # failure (DB unreachable, timeout, unexpected row shape) is caught,
        # logged, and the app continues serving with the permissive
        # default: this is a deliberate fail-open design (see ADR-3's
        # rationale), not an oversight, and it is also load-bearing for
        # existing gateway unit tests (design doc section 6), which run the
        # real lifespan against a non-existent database and rely on this
        # bootstrap failing harmlessly rather than raising.
        app.state.model_policy_cache = ModelPolicyCache()
        # Phase 2 (BD-12): the team-restriction overlay cache, same
        # fail-open startup discipline - constructed empty (= "no team has
        # any restriction", the permissive absence-of-row default) and
        # warmed below only if the org-baseline bootstrap succeeds (both
        # loads share the same DB; if the org load failed, the team load
        # would too, and the self-heal retries both together).
        app.state.team_model_policy_cache = TeamModelPolicyCache()
        # Same fail-open discipline, one layer further: the per-team-member
        # overlay cache - constructed empty (= "no member has any overlay,
        # the team's own effective set applies unchanged") and warmed below
        # only if the org-baseline bootstrap succeeds.
        app.state.member_model_policy_cache = MemberModelPolicyCache()
        app.state.model_policy_self_heal_task = None
        try:
            snapshot = await _load_model_policy_snapshot_bounded(app)
            app.state.model_policy_cache.set(snapshot)
            await _warm_team_model_policy_cache(app)
            await _warm_member_model_policy_cache(app)
        except Exception:
            logger.warning("model_policy_bootstrap_failed", exc_info=True)
            # Cache stays at its zero-I/O default (unconfigured/permissive)
            # for now. ADR-3 addendum (security review finding): schedule a
            # bounded, in-process self-heal so a transient startup-ordering
            # hiccup (e.g. the DB still starting up) corrects itself without
            # a restart or an incidental admin PUT - see
            # `_model_policy_self_heal`'s docstring. Stashed on app.state so
            # the `finally` block below can cancel it cleanly on shutdown.
            app.state.model_policy_self_heal_task = asyncio.create_task(
                _model_policy_self_heal(app)
            )

        # Phase 3 (BD-3/BD-5): same fail-open startup discipline as the
        # model-policy caches above - constructed empty (= unrestricted)
        # first, warmed only if the DB is reachable within the bound. No
        # dedicated self-heal loop (unlike the org model-policy cache): the
        # design doesn't call for one, and a missed warm here just means
        # "no residency/content-aware restriction" until the next admin
        # write or process restart - the same permissive-default fallback
        # the org model-policy cache itself lands on if its own self-heal
        # is ever exhausted.
        app.state.residency_rule_cache = ResidencyRuleCache()
        app.state.content_aware_rule_cache = ContentAwareRuleCache()
        await _warm_residency_and_content_aware_caches(app)

        # Phase 5 (Differentiators, 5.5): same fail-open startup discipline
        # as every cache above - constructed empty (= no self-hosted model
        # routable) first, warmed only if the DB is reachable within the
        # bound. See `_warm_self_hosted_model_route_cache`'s docstring.
        app.state.self_hosted_model_route_cache = SelfHostedModelRouteCache()
        await _warm_self_hosted_model_route_cache(app)

        # Custom Model Registry (Admin-Managed BYOK Models): the cache
        # instance is constructed here empty FIRST (same fail-open "no
        # custom model routable yet" default the self-hosted cache above
        # starts from) so `api.deps.get_custom_model_route_cache` (and
        # therefore `chat.py`/`embeddings.py`'s `resolve_route()` calls)
        # never hit an `AttributeError` on `app.state`, in every
        # environment, even if the warm below fails-open. CMR-6: the
        # bounded-timeout DB warm (`_warm_custom_model_route_cache`,
        # mirroring `_warm_self_hosted_model_route_cache` above) and the
        # shadowing startup log (technical design doc section 2.4a/5 row
        # 3-4) now run immediately after construction, same block/ordering
        # convention as the self-hosted cache immediately above.
        app.state.custom_model_route_cache = CustomModelRouteCache()
        await _warm_custom_model_route_cache(app)
        _log_custom_model_shadowing(app)

        # Phase 3 (BD-1, design doc section 10 fork #2): the Presidio
        # AnalyzerEngine singleton - loading `en_core_web_sm` takes on the
        # order of 1-2s, so it is built exactly once here (off the event
        # loop, via `asyncio.to_thread` - spaCy's model loading is
        # synchronous/blocking) and shared for the life of the process, same
        # "expensive to build, cheap to reuse" contract as
        # `vertex_token_cache` above.
        app.state.dlp_analyzer_engine = await asyncio.to_thread(build_analyzer_engine)

        # Phase 3 (BD-16): same fail-open startup discipline as the
        # residency/content-aware caches above - constructed empty (=
        # unrestricted at every level, AC9.3's off-by-default posture)
        # first, warmed only if the DB is reachable within the bound.
        app.state.access_schedule_cache = AccessScheduleCache()
        await _warm_access_schedule_cache(app)

        # Phase 4 (BD-1/BD-2, design doc section 4.1/9.3): the one shared-
        # state mechanism used by key health (this track), and later by
        # rate limiting/caching. `GATEKEY_REDIS_URL` set -> Redis-backed
        # (a genuinely horizontally-scaled deployment, `--profile cache`);
        # unset (the default) -> in-process, accurate for this project's
        # actual shipped single-instance topology - see
        # `services/shared_state.py`'s module docstring.
        if settings.GATEKEY_REDIS_URL:
            app.state.shared_state_store = RedisSharedStateStore(settings.GATEKEY_REDIS_URL)
        else:
            app.state.shared_state_store = InProcessSharedStateStore()

        # Phase 4 (BD-4, design doc section 3.2): same fail-open startup
        # discipline as the residency/access-schedule caches above -
        # constructed empty (= no team has narrowed failover off) first,
        # warmed only if the DB is reachable within the bound.
        app.state.team_failover_override_cache = TeamFailoverOverrideCache()
        await _warm_team_failover_override_cache(app)

        # Fix 6 (NFR gap, AC4.3.4/security+QA review): `RateLimitCache`/
        # `CachingSettingsCache`/`DegradationPolicyCache` existed but were
        # never constructed or warmed here - `check_rate_limit()`/`check_
        # response_cache()`/`check_and_apply_degradation()` each paid a live
        # Postgres round trip on every single gateway request instead. Same
        # fail-open startup discipline as every other cache above:
        # constructed empty (= nothing configured, each check's own
        # permissive/no-op default) first, warmed only if the DB is
        # reachable within the bound.
        app.state.rate_limit_cache = RateLimitCache()
        await _warm_rate_limit_cache(app)
        app.state.caching_settings_cache = CachingSettingsCache()
        await _warm_caching_settings_cache(app)
        app.state.degradation_policy_cache = DegradationPolicyCache()
        await _warm_degradation_policy_cache(app)

        # Phase 3 (BD-11, design doc sections 4.2/10 fork #1): the single
        # in-process rotation/audit-purge scheduler loop - started once per
        # worker process, cancelled cleanly on shutdown below. Safe under
        # multiple worker processes via the atomic claim-and-advance
        # `UPDATE ... RETURNING` in `services.scheduler.run_due_rotations`
        # (see that module's docstring) - no distributed lock needed.
        app.state.scheduler_task = asyncio.create_task(run_scheduler_loop(app))

        # Phase 3 (BD-25, design doc section 8.2): the CLI-sync device-code
        # flow's ephemeral pending-approval state - see `DeviceAuthStore`'s
        # docstring for the documented single-process-only limitation. Zero
        # I/O to construct, no warm/bootstrap needed (starts empty - a
        # pending request only ever exists because a CLI called `/start`
        # after this process was already up).
        app.state.device_auth_store = DeviceAuthStore()

        try:
            yield
        finally:
            scheduler_task = app.state.scheduler_task
            scheduler_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task
            self_heal_task = app.state.model_policy_self_heal_task
            if self_heal_task is not None:
                self_heal_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self_heal_task
            await app.state.provider_http_client.aclose()
            await app.state.shared_state_store.aclose()
            await engine.dispose()

    app = FastAPI(
        title="Gatekey",
        description=(
            "Self-hostable enterprise AI gateway: an OpenAI-compatible proxy "
            "(`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, "
            "`/v1/models`) over your own provider keys, plus the admin API "
            "behind the management console.\n\n"
            "Every error response uses one structured envelope: "
            '`{"error": {"code", "message", "request_id", ...}}` - `code` is '
            "machine-readable and stable, and `request_id` matches the "
            "`X-Request-ID` response header for log correlation."
        ),
        version="0.1.0",
        lifespan=_lifespan,
    )
    app.state.settings = settings

    # Phase 2: the console authenticates with an httpOnly session COOKIE
    # (SSO), so the browser must send credentialed cross-origin requests
    # (frontend localhost:3000 -> backend localhost:8000) -
    # `allow_credentials=True` with an EXPLICIT origin list (never a
    # wildcard; `Settings.cors_allowed_origins()` drops any "*" entry) -
    # derived from GATEKEY_FRONTEND_ORIGIN plus any extra configured
    # origins. Phase 1's bearer-token admin flows are unaffected by the
    # credentials flag.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def _session_csrf_origin_guard(request: Request, call_next):
        """CSRF origin check for session-cookie-authenticated mutations
        (Phase 2 security review M-1).

        `SameSite=Lax` alone is insufficient: SameSite ignores ports, so a
        page on another localhost port can POST cross-origin with the
        session cookie attached. For any non-GET/HEAD/OPTIONS request that
        CARRIES the session cookie (bearer-only requests are untouched -
        the cookie's presence, not the auth outcome, is the trigger, which
        is why this lives here as one central middleware instead of inside
        each auth dependency), the browser-asserted `Origin` header (falling
        back to `Referer`'s origin) must be in the configured CORS
        allowlist. Requests with neither header (curl, scripts, non-browser
        clients, same-origin requests from older browsers) pass - the
        attack requires a browser, and browsers always send Origin on
        cross-origin POSTs. Note the backend's own origin is deliberately
        NOT allowlisted: no legitimate session-authenticated mutation
        originates from a page served by the backend itself.
        """
        if (
            request.method not in ("GET", "HEAD", "OPTIONS")
            and SESSION_COOKIE_NAME in request.cookies
        ):
            origin = request.headers.get("origin")
            if not origin:
                referer = request.headers.get("referer")
                if referer:
                    parsed = urlparse(referer)
                    origin = (
                        f"{parsed.scheme}://{parsed.netloc}"
                        if parsed.scheme and parsed.netloc
                        else None
                    )
            if origin and origin.rstrip("/") not in settings.cors_allowed_origins():
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": {
                            "code": "forbidden",
                            "message": "Cross-origin request rejected.",
                        }
                    },
                )
        return await call_next(request)

    # Added AFTER the CSRF guard above so it wraps it (last-added middleware
    # runs outermost): every response - including CSRF 403s - gets an
    # X-Request-ID header and lands in the /metrics counters.
    install_observability(app)

    register_exception_handlers(app)
    # Phase 3 (BD-20..24, design doc section 6.1): registered ALONGSIDE
    # `register_exception_handlers` above, not instead of it - every
    # `/scim/v2/...` route raises `services.scim.ScimError` (RFC 7644
    # shape), every other route keeps the generic envelope unchanged. See
    # `services/scim.py`'s module docstring.
    register_scim_exception_handlers(app)

    app.include_router(admin_access_schedule_router)
    app.include_router(admin_providers_router)
    app.include_router(admin_service_accounts_router)
    app.include_router(admin_model_policy_router)
    app.include_router(admin_users_router)
    # Tier 4 (ops/DX polish): one-call user+team+membership+key onboarding.
    app.include_router(admin_bootstrap_router)
    app.include_router(admin_usage_router)
    app.include_router(admin_join_requests_router)
    app.include_router(admin_org_settings_router)
    app.include_router(admin_identity_router)
    app.include_router(admin_audit_entries_router)
    # Phase 5 (Differentiators, 5.2 Hash-Chained Audit Ledger) - registered
    # alongside `admin_audit_entries_router` per the design doc's wiring
    # checklist "5.1 (Ledger, 5.2)" row 4.
    app.include_router(admin_audit_chain_router)
    app.include_router(admin_dlp_policy_router)
    app.include_router(admin_residency_rules_router)
    app.include_router(admin_content_aware_rules_router)
    # Phase 5 (Differentiators, 5.3 Content-Classification-Aware Routing) -
    # design doc wiring checklist "5.4 (Content-Classification Routing,
    # 5.3)" row 7 - registered alongside `admin_content_aware_rules_router`.
    app.include_router(admin_sensitivity_label_mappings_router)
    app.include_router(admin_compliance_settings_router)
    app.include_router(admin_rotation_policy_org_router)
    app.include_router(admin_rotation_policy_provider_router)
    app.include_router(admin_scim_config_router)
    app.include_router(admin_keys_router)
    # Phase 4 (Reliability & Cost Efficiency): rate limiting, caching,
    # graceful degradation, multi-key/backup-group failover, and their
    # admin-console read/config surfaces.
    app.include_router(admin_rate_limits_router)
    app.include_router(admin_rate_limits_team_router)
    app.include_router(admin_caching_settings_router)
    app.include_router(admin_caching_settings_team_router)
    app.include_router(admin_degradation_policy_router)
    app.include_router(admin_degradation_policy_team_router)
    app.include_router(admin_backup_groups_router)
    app.include_router(admin_failover_events_router)
    app.include_router(admin_failover_override_team_router)
    app.include_router(admin_degradation_events_router)
    app.include_router(admin_cache_router)
    app.include_router(admin_provider_key_health_router)
    # Phase 5 (Differentiators, 5.4 Provider Drift Detector) - design doc
    # wiring checklist "5.2 (Drift Detector, 5.4)" row 4.
    app.include_router(admin_drift_detector_router)
    # Phase 5 (Differentiators, 5.5 Unified Self-Hosted Governance) - design
    # doc wiring checklist "5.3 (Self-Hosted Governance, 5.5)" row 12.
    app.include_router(admin_self_hosted_providers_router)
    # Custom Model Registry (Admin-Managed BYOK Models) - technical design
    # doc section 5 row 20.
    app.include_router(admin_custom_models_router)
    # Phase 5 (Differentiators, 5.1 Shadow AI Discovery) - design doc wiring
    # checklist "5.5 (Shadow AI, 5.1)" rows 3/4. TWO deliberately SEPARATE
    # routers (see `api/v1/shadow_ai_ingest.py`'s module docstring): the
    # ingest router carries NO router-level `dependencies=[...]` (its one
    # route declares `Depends(require_shadow_ai_ingest_token)` itself), while
    # the admin config/report/token-gen/hostname-CRUD router uses the normal
    # session-cookie RBAC dependencies. Registering them separately, as here,
    # is what keeps an admin session from ever reaching the ingest route and
    # vice versa.
    app.include_router(shadow_ai_ingest_router)
    app.include_router(admin_shadow_ai_router)
    app.include_router(auth_router)
    app.include_router(auth_device_router)
    app.include_router(cli_sync_me_router)
    app.include_router(teams_router)
    app.include_router(delegated_keys_router)
    app.include_router(keys_router)
    app.include_router(me_router)
    app.include_router(model_access_router)
    app.include_router(onboarding_router)
    # Phase 3 (BD-22/BD-23): mounted at `/scim/v2/...` (design doc section
    # 6.1) - deliberately separate from every `/v1/...` router above.
    app.include_router(scim_users_router)
    app.include_router(scim_groups_router)
    app.include_router(gateway_router)

    @app.get("/healthz", tags=["meta"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


# Run with: uvicorn gatekey.main:create_app --factory
#
# Deliberately not instantiating a module-level `app = create_app()` here:
# doing so would call `get_settings()` (and its fail-fast validation) at
# *import* time, which breaks anything that imports this module without a
# fully configured environment (e.g. unit tests importing `create_app`
# directly to build an app with test settings). Using `--factory` still
# gives the desired "fail fast on bad config" behavior for the real
# process, just at process-start instead of import time.
