"""Shared request-resolution and instrumentation helpers for the gateway
route handlers (Phase 1.2, BD-9; Phase 1.3 adds the model-policy check,
BD-6).

Every gateway endpoint (`/v1/chat/completions`, `/v1/completions`,
`/v1/embeddings`) must go through the identical auth -> resolve_model ->
model-policy check -> capability/provider check -> credential-fetch
sequence before doing anything endpoint/provider-specific (design doc,
Story 2 acceptance criteria: streaming and non-streaming chat completions
must not be able to diverge or bypass this sequence). `resolve_route`,
`check_model_policy`, and `fetch_credential` below are deliberately kept as
separate steps - not merged into one "resolve everything" call -
specifically so a route handler can reject on policy/capability/provider
*before* paying the DB round-trip + decryption cost of a credential fetch,
and so each rejection surfaces with the right shape: `errors.
ModelDeniedError` (403) for a model the org's model-access policy denies
(Phase 1.3, design doc section 3; checked immediately after
`resolve_route()` and before any capability/provider check, since it is a
pure in-memory check with zero I/O and should reject before spending any
further work on a request that is going to be denied regardless),
`errors.UnsupportedRequestError` (400) rather than `errors.
ProviderNotConfiguredError` (404) for a model whose provider was never
going to be usable on this endpoint regardless of whether a key happens to
be configured for it (e.g. an Anthropic model against `/v1/embeddings`).
Every gateway route handler is expected to call them in that order:
`resolve_route()`, then `check_model_policy()`, then its own
capability/provider check, then `check_budget_available()` (Phase 1.4),
then `fetch_credential()`, then the provider call, then
`record_usage_charge()` (Phase 1.4) on confirmed success only.

Phase 3 (Security & Compliance Hardening) pipeline additions
--------------------------------------------------------------
Design doc section 3.3's exact ordering, inserted between `check_model_
policy()` and `check_budget_available()`: `resolve_route -> check_model_
policy -> check_residency -> run_dlp_scan -> check_content_classification ->
check_budget_available -> fetch_credential`. `check_residency()` is a new,
cheap (near-zero-I/O) step - see its own docstring for exactly why it isn't
fully zero-I/O like `check_model_policy()`. `run_dlp_scan()` runs the
Presidio scan synchronously (redact/block) or schedules it via
`BackgroundTasks` (log-only, AC2.6) - see its docstring. `check_content_
classification()` is a second, `check_model_policy()`-shaped pass that only
runs once the DLP scan has determined whether the prompt was PII-flagged
(AC4.1/AC4.3) - it can only further restrict a model already allowed by the
static org/team baseline, never re-enable one. Every gateway route handler
(`api/v1/gateway/*.py`) calls all three, in this order, before `check_
budget_available()`.

Phase 3 (BD-19, access windows) pipeline addition
----------------------------------------------------
Design doc section 5.3: `check_access_schedule()` runs IMMEDIATELY AFTER
`require_gateway_credential` resolves the caller - BEFORE `resolve_route()`,
i.e. earlier than every check listed above (a schedule block has nothing to
do with which model was requested, and should reject as cheaply as every
other early-reject step in this pipeline). Every gateway route handler
resolves `source_ip` and calls this as the very first line inside its `try`
block.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, TypeVar

from fastapi import BackgroundTasks, FastAPI
from presidio_analyzer import AnalyzerEngine
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import AdminContext, GatewayCallerContext
from gatekey.db.models.cache_lookup_event import CacheLookupEvent
from gatekey.db.models.dlp_policy import DlpAction
from gatekey.db.models.failover_event import FailoverEvent
from gatekey.db.models.rate_limit_rejection_event import RateLimitRejectionOutcome
from gatekey.db.models.rate_limit_rule import RateLimitOnLimit
from gatekey.errors import (
    BudgetExhaustedError,
    DlpBlockedError,
    ModelDeniedError,
    ModelNotFoundError,
    OutsideAllowedScheduleError,
    ProviderNotConfiguredError,
    RateLimitExceededError,
    ResidencyViolationError,
)
from gatekey.errors import UnsupportedRequestError as HttpUnsupportedRequestError
from gatekey.providers.base import ProviderCallError
from gatekey.providers.model_registry import (
    ModelCapability,
    ModelRoute,
    UnknownModelError,
    resolve_model,
)
from gatekey.providers.pricing import PricingEntryMissingError
from gatekey.services.encryption import KeyProvider
from gatekey.services import access_schedules as access_schedule_service
from gatekey.services import budget as budget_service
from gatekey.services import degradation as degradation_service
from gatekey.services import dlp as dlp_service
from gatekey.services import notifiers
from gatekey.services import provider_key_health
from gatekey.services import provider_keys as provider_keys_service
from gatekey.services import rate_limit as rate_limit_service
from gatekey.services import residency as residency_service
from gatekey.services import response_cache as response_cache_service
from gatekey.services import team_periods
from gatekey.services.access_schedules import AccessScheduleCache
from gatekey.services.audit import write_audit_entry
from gatekey.services.emergency_overrides import get_active_override
from gatekey.services.model_policy import (
    ContentAwareRuleCache,
    ModelPolicyCache,
    TeamModelPolicyCache,
    resolve_content_classification,
    resolve_model_access,
)
from gatekey.services.provider_key_health import TeamFailoverOverrideCache
from gatekey.services.proxy_keys import (
    ProviderCredential,
    ProviderKeyNotConfiguredError,
    get_decrypted_provider_credential,
    get_decrypted_provider_credential_from_row,
)
from gatekey.services.residency import ResidencyRuleCache, resolve_residency
from gatekey.services.response_cache import ResponseCache
from gatekey.services.sensitivity_label_mappings import resolve_pretrusted_categories
from gatekey.services.self_hosted_providers import (
    SelfHostedCredentialNotConfiguredError,
    SelfHostedModelRouteCache,
    get_decrypted_self_hosted_credential,
)
from gatekey.services.shared_state import SharedStateStore

logger = logging.getLogger("gatekey")

# Bound on the optional `Idempotency-Key` request header (design doc section
# 6/Q3: plumb-through only, no caching/dedup logic in this phase). 255 chars
# comfortably covers a UUID, a ULID, or a reasonable caller-generated opaque
# token, while still rejecting obviously-malformed input (e.g. an entire
# request body accidentally pasted into the header) with a clear 400 rather
# than silently truncating or accepting it.
MAX_IDEMPOTENCY_KEY_LENGTH = 255


def resolve_route(
    model: str, self_hosted_cache: SelfHostedModelRouteCache | None = None
) -> ModelRoute:
    """Resolve `model` to a `ModelRoute`.

    The static `MODEL_REGISTRY` lookup (`resolve_model`) is ALWAYS tried
    first, unconditionally - a self-hosted model id can never shadow an
    existing static route (design doc section 7.3's edge-case table).

    Phase 5 (5.5, AC5.5.5): only when that lookup fails AND `self_hosted_
    cache` is not `None` does this fall back to an O(1), zero-I/O dict
    lookup in the (pre-warmed) cache - if the model id is found there, it
    routes to that entry's owning self-hosted provider (every cache entry
    is, by construction, already `verified = true` - see `services.
    self_hosted_providers.load_self_hosted_model_route_snapshot`'s
    docstring); if not found (unknown model id, OR a real but not-yet-
    verified self-hosted model id - both look identical from here), falls
    through to the same `ModelNotFoundError` as any other unknown model.

    `self_hosted_cache=None` (the default) is BYTE-FOR-BYTE the pre-Phase-5
    behavior - `api/v1/gateway/completions.py`/`embeddings.py` call this
    with no second argument at all, which is what structurally enforces
    AC5.5.4's "self-hosted models are chat-completions only" constraint at
    the call-site level, not just as a downstream capability check (only
    `api/v1/gateway/chat.py` ever passes a real cache here).

    Raises `errors.ModelNotFoundError` (404) if `model` isn't registered -
    see `UnknownModelError`'s docstring for why the model name is safe to
    include in the message. This is the one sanctioned place gateway route
    handlers do this lookup - see module docstring for calling order.
    """
    try:
        return resolve_model(model)
    except UnknownModelError as exc:
        if self_hosted_cache is not None:
            entry = self_hosted_cache.get(model)
            if entry is not None:
                return ModelRoute(
                    provider="self_hosted",
                    capability=ModelCapability.CHAT,
                    native_model_id=model,
                    self_hosted_provider_id=entry.provider_id,
                )
        raise ModelNotFoundError(str(exc)) from None


async def check_access_schedule(
    session: AsyncSession,
    ctx: GatewayCallerContext,
    *,
    cache: AccessScheduleCache,
    source_ip: str | None = None,
) -> None:
    """Enforce the caller's resolved scheduled access window (Phase 3,
    design doc section 5.3, AC9.1-AC9.6).

    Only applies when `ctx.credential_type == "service_account"` - AC9.1's
    `AccessSchedule.scope` values (org/team_id/service_account_id) never
    include a personal-key scope; personal keys (a logged-in human via SSO)
    are unaffected by this feature.

    Zero I/O in the common (allowed) case: `resolve_access_schedule_decision()`
    is a pure in-process cache-lookup walk (AC9.11). It checks every ENABLED
    layer (org, team, service-account) cumulatively, not just the single
    most-specific one - see `services.access_schedules`' module docstring
    for the staleness bug this fixes (a security review finding: a
    write-time-validated-narrower child layer can silently outlive a later
    tightening of a parent layer under an innermost-only read-time check).
    An emergency override is checked ONLY on the rejection path (design doc
    section 5.3) - a single indexed DB lookup, never paid when the request
    is already within its allowed window. Raises `errors.
    OutsideAllowedScheduleError` (403, `outside_allowed_schedule`) on a hard
    reject; the block itself is a synchronous, committed `AuditEntry`
    (`access_schedule.block`) - AC9.6 names this (along with residency/DLP)
    as the one deliberate exception to deferring audit writes via
    `BackgroundTasks`, since a raised exception has no live response for
    `BackgroundTasks` to run after.

    Call this FIRST, immediately after `require_gateway_credential` - before
    `resolve_route()` and every other pipeline step (see module docstring).
    """
    if ctx.credential_type != "service_account":
        return
    now = datetime.now(timezone.utc)
    decision = access_schedule_service.resolve_access_schedule_decision(
        cache=cache,
        team_id=ctx.team_id,
        service_account_id=ctx.credential_id,
        now=now,
        timezone_name=cache.get_timezone_name(),
        holiday_dates=cache.get_holiday_dates(),
    )
    if decision.allowed:
        return

    override = await get_active_override(session, ctx.credential_id, now=now)
    if override is not None:
        return

    await write_audit_entry(
        session,
        actor=AdminContext(actor_user_id=ctx.user_id, actor_label=ctx.name, org_id=ctx.org_id),
        action="access_schedule.block",
        target_type="service_account_key",
        target_id=str(ctx.credential_id),
        old_value=None,
        new_value={"team_id": str(ctx.team_id) if ctx.team_id is not None else None},
        source_ip=source_ip,
    )
    await session.commit()
    raise OutsideAllowedScheduleError()


# Zero-restriction fallback for callers that pass no team cache (unit tests,
# non-gateway call sites) - an empty cache means "no team has any
# restriction", so combined with `team_id=None` the check is byte-for-byte
# the Phase 1.3 org-baseline behavior.
_EMPTY_TEAM_CACHE = TeamModelPolicyCache()


def check_model_policy(
    model: str,
    cache: ModelPolicyCache,
    team_cache: TeamModelPolicyCache | None = None,
    team_id: uuid.UUID | None = None,
) -> None:
    """Enforce the org's model access policy for `model` (Phase 1.3).

    `model` MUST be the exact same string already passed to
    `resolve_route()` in this same request - never re-read from the request
    body and never normalized - see `docs/design/phase-1.3-model-
    governance.md` section 3.1 for why that is what makes this bypass-
    proof: by the time control reaches this function, `model` is provably a
    literal `MODEL_REGISTRY` key (an exact-match dict lookup already
    succeeded), and every entry in a policy's `models` list is validated at
    `PUT` time (`services.model_policy.set_policy()`) to also be a literal
    `MODEL_REGISTRY` key. Both sides of the membership check therefore draw
    from the same closed, exact-string universe - no `.lower()`/`.strip()`/
    alias table belongs anywhere near this function.

    Raises `errors.ModelDeniedError` (403) if denied. Pure, synchronous,
    zero I/O - reads only `cache.get()` plus one in-process dict lookup for
    the team overlay (AC-3a / Phase 2 AC1.7); never touches the database.
    Call this only *after* `resolve_route()` has already succeeded, and
    *before* any capability/provider check or credential fetch - see module
    docstring for the full ordering.

    Phase 2 (BD-13): delegates to `resolve_model_access` - org baseline
    first, then the team's narrowing overlay when `team_id` is not None.
    The raised error's message names the blocking layer; its
    `code`/`status_code` are unchanged.
    """
    decision = resolve_model_access(
        model,
        org_cache=cache,
        team_cache=team_cache if team_cache is not None else _EMPTY_TEAM_CACHE,
        team_id=team_id,
    )
    if not decision.allowed:
        assert decision.blocking_layer is not None  # None only when allowed
        raise ModelDeniedError(model, blocking_layer=decision.blocking_layer)


# ---------------------------------------------------------------------------
# Phase 3 (BD-6): residency check, DLP scan, content-classification recheck.
# See module docstring for the exact insertion point in the pipeline.
# ---------------------------------------------------------------------------


async def _resolve_provider_key_metadata(
    session: AsyncSession, route: ModelRoute
) -> dict[str, Any] | None:
    """The target provider's non-secret `key_metadata` - `None` for every
    provider except vertex_ai/ollama (the only two `services.residency.
    resolve_model_region()`/`services.response_cache.
    resolve_cache_residency_zone()` actually read region data out of; see
    those functions' own docstrings), and `None` there too if no key is
    configured yet. Shared by `check_residency()` and `check_response_
    cache()` below (Fix 4, security review finding) so both perform the
    SAME real lookup rather than one doing the real thing and the other
    hardcoding `None`."""
    if route.provider not in ("vertex_ai", "ollama"):
        return None
    key_row = await provider_keys_service.get_key(session, route.provider)
    return key_row.key_metadata if key_row is not None else None


async def check_residency(
    session: AsyncSession,
    route: ModelRoute,
    *,
    cache: ResidencyRuleCache,
    team_id: uuid.UUID | None,
    org_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_label: str,
    source_ip: str | None = None,
) -> None:
    """Enforce data-residency (Phase 3, design doc section 3.2/3.3).

    Region resolution needs the target provider's non-secret `key_metadata`
    for vertex_ai/ollama only (openai/anthropic are a static in-process
    lookup, openrouter is always unknown - see `services.residency.
    resolve_model_region`), so unlike `check_model_policy()` this is NOT
    zero-I/O - but it is a single non-secret metadata read, no decryption,
    cheaper than `fetch_credential()`.

    Raises `errors.ResidencyViolationError` (403) on a hard-block violation
    - never a silent reroute (AC3.6). On EITHER outcome (hard_block or warn,
    ratified #12/AC3.5) writes a synchronous audit entry (`residency.
    hard_block` / `residency.warn`) and commits it immediately - design doc
    section 7.1 names residency (along with DLP/schedule) blocks as the one
    deliberate exception written synchronously rather than deferred via
    `BackgroundTasks`: a raised exception has no live response for
    `BackgroundTasks` to run after in this codebase's exception-handler
    wiring (`errors.register_exception_handlers`), so deferring is not an
    option on the block path, and the warn path is written the same way for
    consistency (a single indexed insert, already the same cost class as
    every other synchronous audit write in this system).

    Call this only *after* `check_model_policy()` has already succeeded, and
    *before* the DLP scan step - see module docstring for the full ordering.
    """
    # Fast, zero-I/O path: no rule configured anywhere that could apply to
    # this caller -> `resolve_residency` would return "unrestricted"
    # regardless of region, so skip the metadata read entirely. This is what
    # keeps this step cheap for the (expected-common, pre-Phase-3-adoption)
    # case where no org has configured residency at all yet.
    if cache.get_org_rule() is None and (team_id is None or cache.get_team_rule(team_id) is None):
        return

    key_metadata = await _resolve_provider_key_metadata(session, route)
    region = residency_service.resolve_model_region(route, key_metadata)
    decision = resolve_residency(region, cache=cache, team_id=team_id)
    if not decision.violated:
        return

    action = "residency.hard_block" if decision.behavior == "hard_block" else "residency.warn"
    await write_audit_entry(
        session,
        actor=AdminContext(actor_user_id=actor_user_id, actor_label=actor_label, org_id=org_id),
        action=action,
        target_type="residency_rule",
        target_id=str(team_id) if team_id is not None else str(org_id),
        old_value=None,
        new_value={"provider": route.provider, "region": decision.region, "behavior": decision.behavior},
        source_ip=source_ip,
    )
    await session.commit()
    if not decision.allowed:
        raise ResidencyViolationError(route.provider, decision.region)


@dataclass(frozen=True)
class DlpPipelineResult:
    """Outcome of `run_dlp_scan()` that the calling route handler must act
    on: apply `redacted_texts` back onto the request body (if not None,
    AC2.5's "redact before forward"), and pass `category_findings` into
    `check_content_classification()`.

    Phase 5 (5.3): `category_findings` generalizes the pre-Phase-5
    `pii_detected: bool` field (see `services.dlp.DlpScanOutcome`'s
    identical generalization). `pii_detected` stays on this dataclass too -
    a pure derivation (`"pii" in category_findings`), computed once at
    construction - so any existing direct reader of `.pii_detected` keeps
    working unchanged."""

    redacted_texts: list[str] | None
    category_findings: frozenset[str] = frozenset()
    pii_detected: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "pii_detected", "pii" in self.category_findings)


async def _deliver_async_dlp_scan(
    app: FastAPI,
    *,
    texts: list[str],
    model: str,
    request_id: str,
    org_id: uuid.UUID,
    team_id: uuid.UUID | None,
    user_id: uuid.UUID,
) -> None:
    """`BackgroundTasks` entry point for the log-only DLP path (AC2.6) -
    runs after the gateway response is already on the wire, on a fresh DB
    session (the request's own session is closed by then) - mirrors
    `services.notifiers.deliver_threshold_alerts`'s established shape
    exactly. Never raises: a scan-recording bug must never surface anywhere
    near a live request."""
    try:
        async with app.state.db_session_factory() as session:
            policy = await dlp_service.load_dlp_policy(session)
            custom_patterns = await dlp_service.load_custom_patterns(session)
            team_override = (
                await dlp_service.get_team_dlp_override(session, team_id)
                if team_id is not None
                else None
            )
            outcome = await dlp_service.scan_texts(
                app.state.dlp_analyzer_engine,
                texts,
                policy=policy,
                custom_patterns=custom_patterns,
                team_override=team_override,
            )
            if outcome.ran:
                await dlp_service.record_scan_result(
                    session,
                    org_id=org_id,
                    request_id=request_id,
                    team_id=team_id,
                    user_id=user_id,
                    model=model,
                    ran_sync=False,
                    findings=outcome.findings,
                    raw_texts=texts if outcome.findings else None,
                    store_raw=policy.store_raw_flagged_content,
                )
    except Exception:
        logger.error("dlp_async_scan_failed", exc_info=True, extra={"request_id": request_id})


_CONTENT_AWARE_CATEGORIES: tuple[str, ...] = ("pii", "financial_data", "source_code", "legal")


def _enabled_content_aware_categories(cache: ContentAwareRuleCache) -> frozenset[str]:
    """Phase 5 (5.3): the subset of the four content-classification
    categories that has an ENABLED `content_aware_rules` row for this org -
    used both for `run_dlp_scan`'s early no-op gate and to decide which of
    the three NEW classifiers (`financial_data`/`source_code`/`legal`) even
    need to run for this request (see `services.dlp.scan_texts`'s
    `content_aware_categories_enabled` parameter)."""
    return frozenset(
        category
        for category in _CONTENT_AWARE_CATEGORIES
        if (rule := cache.get(category)) is not None and rule.enabled
    )


async def run_dlp_scan(
    session: AsyncSession,
    *,
    engine: AnalyzerEngine,
    texts: list[str],
    model: str,
    request_id: str,
    org_id: uuid.UUID,
    team_id: uuid.UUID | None,
    user_id: uuid.UUID,
    actor_label: str,
    content_aware_cache: ContentAwareRuleCache,
    background_tasks: BackgroundTasks,
    app: FastAPI,
    source_ip: str | None = None,
    sensitivity_label: str | None = None,
) -> DlpPipelineResult:
    """DLP scan step (Phase 3, design doc section 3.2/9.2, AC2.5-AC2.9;
    Phase 5, design doc section 2.4, AC5.3.1/AC5.3.2/AC5.3.5).

    Loads the org's DLP policy config fresh every call (a single cheap
    indexed read - the same "not zero-I/O, but cheap" tradeoff `check_
    budget_available()` already makes on this hot path; design doc section
    2's NFR accounting scopes the <50ms p99 budget to the Presidio scan
    itself, not config loading). Decides sync-vs-async from POLICY CONFIG
    ALONE (`services.dlp.requires_sync_scan`) - never from this request's
    actual findings, which aren't known before the scan runs.

    Phase 5 (5.3) gating: the fast no-op path now also considers whether
    ANY of the four content-classification categories (not just "pii") has
    an enabled `content_aware_rules` row - an org that has enabled
    `source_code` routing but no PII/DLP detector must still get a real,
    synchronous classification pass (see `_enabled_content_aware_categories`
    and `services.dlp.requires_sync_scan`'s widened
    `content_aware_classification_enabled` parameter). Byte-for-byte the old
    fast no-op path for an org that has configured NEITHER DLP detectors NOR
    any content-aware category.

    Sensitivity-label short-circuit (AC5.3.5): an optional `sensitivity_
    label` (from the `X-Gatekey-Sensitivity-Label` request header) is looked
    up against this org's `sensitivity_label_mappings`. A match pre-trusts
    (adds directly to the returned `category_findings`, bypassing Gatekey's
    own classifier for) exactly the ONE mapped category; every OTHER
    category not covered by the label still runs Gatekey's own classifier
    normally. An unrecognized/absent label resolves to no pre-trusted
    categories - never a hard error, always falls through to running every
    classifier (`services.sensitivity_label_mappings.
    resolve_pretrusted_categories`).

    Security note (mirrors the design doc's Security Considerations table:
    the sensitivity-label header "can never suppress DLP redaction/block
    actions"): the sensitivity-label header is purely a routing hint. For
    "source_code"/"legal" (categories with NO DLP action - AC5.3.1)
    pre-trusting skips real, otherwise-wasted classifier work with zero
    security consequence. For "financial_data" (which DOES have a DLP
    redact/block action, via the same Presidio engine as "pii") pre-trusting
    skips ONLY that category's content-classification-routing signal -
    the underlying Presidio scan for `_FINANCIAL_DATA_ENTITY_TYPES` always
    still runs (via `services.dlp.scan_texts`'s `skip_categories` parameter,
    which deliberately never subtracts from `financial_data_needed` - see
    that function's docstring) whenever "financial_data" content-aware
    routing is enabled, so redaction/blocking for a genuine finding is
    never bypassed by this header. "pii" is never affected either way
    (there is no `pii` short-circuit path in the sensitivity-label mapping
    design - the header only ever pre-trusts the categories an admin has
    explicitly mapped, and "pii" keeps its own independent detector-toggle
    gating regardless).

    Synchronous path (redact/block anywhere in the configured policy, or an
    enabled content-aware category - AC2.9/AC5.3.2): scans and records the
    result INLINE before returning (AC2.8's "redaction/block must complete
    before the request is forwarded"). Raises `errors.DlpBlockedError` (403)
    if any finding resolved to `block` - the scan-result row (`action_taken=
    "block"`) and a synchronous `dlp.block` audit entry (design doc section
    7.1) are both written before raising, so the block is durably recorded
    even though the request itself never reaches the provider.

    Log-only path (AC2.6): schedules the scan via `BackgroundTasks` - it
    genuinely never runs before the response starts returning (mirrors
    `services.notifiers.schedule_threshold_alerts`'s mechanism) - and
    returns immediately with no redaction. Nothing to redact under this path
    by construction: if any finding could redact/block, `requires_sync_scan`
    would already have selected the synchronous path above.

    Call this only *after* `check_residency()` has already succeeded, and
    *before* `check_content_classification()` - see module docstring.
    """
    policy = await dlp_service.load_dlp_policy(session)
    custom_patterns = await dlp_service.load_custom_patterns(session)
    team_override = (
        await dlp_service.get_team_dlp_override(session, team_id) if team_id is not None else None
    )

    enabled_categories = _enabled_content_aware_categories(content_aware_cache)
    content_aware_needs_classification = bool(enabled_categories)

    if not dlp_service.has_any_scanning_enabled(policy, custom_patterns) and not content_aware_needs_classification:
        return DlpPipelineResult(redacted_texts=None, category_findings=frozenset())

    # AC5.3.5 - a fresh, cheap indexed read (same tier as `load_dlp_policy`
    # above, not a warmed `*Cache` - see `services.sensitivity_label_
    # mappings`'s module docstring for why).
    pretrusted_categories = await resolve_pretrusted_categories(session, sensitivity_label)

    effective_builtin_action = dlp_service.resolve_builtin_action(policy.default_action, team_override)
    sync_required = dlp_service.requires_sync_scan(
        effective_builtin_action=effective_builtin_action,
        custom_patterns=custom_patterns,
        content_aware_classification_enabled=content_aware_needs_classification,
    )

    if not sync_required:
        background_tasks.add_task(
            _deliver_async_dlp_scan,
            app,
            texts=texts,
            model=model,
            request_id=request_id,
            org_id=org_id,
            team_id=team_id,
            user_id=user_id,
        )
        return DlpPipelineResult(redacted_texts=None, category_findings=frozenset())

    outcome = await dlp_service.scan_texts(
        engine,
        texts,
        policy=policy,
        custom_patterns=custom_patterns,
        team_override=team_override,
        content_aware_categories_enabled=enabled_categories - {"pii"},
        skip_categories=pretrusted_categories,
    )
    if outcome.ran:
        await dlp_service.record_scan_result(
            session,
            org_id=org_id,
            request_id=request_id,
            team_id=team_id,
            user_id=user_id,
            model=model,
            ran_sync=True,
            findings=outcome.findings,
            raw_texts=texts if outcome.findings else None,
            store_raw=policy.store_raw_flagged_content,
        )
    if outcome.blocked:
        blocking_names = [f.name for f in outcome.findings if f.action == DlpAction.BLOCK]
        await write_audit_entry(
            session,
            actor=AdminContext(actor_user_id=user_id, actor_label=actor_label, org_id=org_id),
            action="dlp.block",
            target_type="dlp_scan_result",
            target_id=request_id,
            old_value=None,
            new_value={"findings": blocking_names, "model": model},
            source_ip=source_ip,
        )
        await session.commit()
        raise DlpBlockedError(blocking_names)

    return DlpPipelineResult(
        redacted_texts=outcome.redacted_texts,
        category_findings=outcome.category_findings | pretrusted_categories,
    )


def check_content_classification(
    model: str, cache: ContentAwareRuleCache, *, category_findings: frozenset[str]
) -> None:
    """The DLP-driven content-aware routing recheck (Phase 3, design doc
    section 3.4/9.4, AC4.1; Phase 5, AC5.3.2). A second, `check_model_
    policy()`-shaped pass, run only once the DLP scan step has determined
    which content-classification categories (if any) this request
    triggered - applies strictly AFTER (and can only further restrict) the
    static org/team baseline `check_model_policy()` already enforced earlier
    in this same request (AC4.3). Pure, synchronous, zero I/O.

    Raises `errors.ModelDeniedError` (403, `blocking_layer=
    "content_classification"`) if denied. Call this only *after* `run_dlp_
    scan()` has already succeeded, and *before* `check_budget_available()` -
    see module docstring for the full ordering.
    """
    decision = resolve_content_classification(model, cache=cache, category_findings=category_findings)
    if not decision.allowed:
        assert decision.blocking_layer is not None
        raise ModelDeniedError(model, blocking_layer=decision.blocking_layer)


async def fetch_credential(
    session: AsyncSession,
    provider: str,
    *,
    key_provider: KeyProvider,
) -> ProviderCredential:
    """Fetch and decrypt the configured key for `provider`.

    Raises `errors.ProviderNotConfiguredError` (404) if no key is
    configured. Call this only *after* any capability/provider check for
    the target endpoint has already passed - see module docstring.
    """
    try:
        return await get_decrypted_provider_credential(session, provider, key_provider=key_provider)
    except ProviderKeyNotConfiguredError as exc:
        raise ProviderNotConfiguredError(exc.message) from None


T = TypeVar("T")


@dataclass(frozen=True)
class FailoverCallResult:
    """Wraps `call_provider_with_failover()`'s result with the failover
    metadata AC4.1.7/the technical design's section 3.3 response headers
    need (`X-Failover-Attempt`/`X-Failover-Used-Key`) and `usage_logs.
    failover_attempt`/`failover_key_id` (migration 0031) want.

    `attempt=0` (and `used_key_id=None`) means the primary key succeeded -
    the overwhelming majority of every request, including every request on
    a team that never configures failover at all (byte-for-byte the
    pre-Phase-4 case). `attempt=1` means the documented "retry EXACTLY
    ONCE against the backup" path fired and the backup succeeded -
    `call_provider_with_failover()` never loops past one retry (see its own
    docstring), so `attempt` is always 0 or 1, never higher.
    """

    result: Any
    attempt: int
    used_key_id: uuid.UUID | None


async def call_provider_with_failover(
    session: AsyncSession,
    app: FastAPI,
    *,
    route: ModelRoute,
    org_id: uuid.UUID,
    team_id: uuid.UUID | None,
    request_id: str,
    key_provider: KeyProvider,
    health_store: SharedStateStore,
    team_override_cache: TeamFailoverOverrideCache,
    call_fn: Callable[[ProviderCredential], Awaitable[T]],
) -> FailoverCallResult:
    """Failover-aware key selection + provider call (Phase 4, design doc
    section 3.3/8). Replaces plain `fetch_credential()` + a bare provider
    call at the exact point those two steps already sat in the pipeline -
    every gateway route handler calls this instead, threading its own
    provider-call coroutine through as `call_fn`.

    `call_fn(credential)` must perform (and await) the actual outbound
    provider call and return its result, or raise `providers.base.
    ProviderCallError` on a call failure - the ONE signal this wrapper reacts
    to (a `ProviderUnsupportedRequestError`/anything else propagates through
    unchanged, exactly as before this wrapper existed, and does not touch
    health state or trigger a retry - it isn't a call failure, it's a
    shape/capability mismatch).

    Mechanic (design doc section 3.3, exact):
      1. `select_provider_key()` - proactive skip straight to a known-Down
         key's configured backup, if failover applies (AC1.9).
      2. `call_fn(credential)` against whichever key was selected.
      3. Success: record success into `health_store` for that key; return.
      4. `ProviderCallError`, AND the selected key was the actual primary
         (not already a proactively-selected backup), AND failover applies,
         AND a `failover_target_id` is configured: record the failure for
         the primary, fetch+decrypt the backup, retry `call_fn` EXACTLY ONCE
         against it (AC1.7 - never a loop).
           - Backup succeeds: record success for the backup, write one
             `failover_events` row, return the backup's result - no trace of
             the primary's failure surfaced to the caller (AC1.8).
           - Backup also fails: record the failure for the backup too,
             re-raise the PRIMARY's original `ProviderCallError` UNCHANGED
             (AC1.7) - never the backup's error.
      5. `ProviderCallError` with no failover applicable (including when the
         selected key was already a proactively-chosen backup - no second
         hop): record the failure, re-raise unchanged - byte-for-byte
         Phase 1-3 behavior for any org that never configures failover
         (design doc section 3.4).

    Raises `errors.ProviderNotConfiguredError` if no key at all is
    configured for `route.provider` (from `select_provider_key`).

    Returns a `FailoverCallResult` (`.result` is `call_fn`'s own return
    value, unchanged) - NOT `call_fn`'s bare result directly, so callers can
    attach the `X-Failover-*` response headers / `usage_logs.failover_*`
    columns (design doc section 3.3) without re-deriving which key actually
    served the request.
    """
    selected, failover_applies = await provider_key_health.select_provider_key(
        session,
        route.provider,
        team_id=team_id,
        health_store=health_store,
        team_override_cache=team_override_cache,
    )
    credential = await get_decrypted_provider_credential_from_row(
        selected, route.provider, key_provider=key_provider
    )

    try:
        result = await call_fn(credential)
    except ProviderCallError as primary_exc:
        await provider_key_health.record_failure(health_store, selected.id, error_summary=str(primary_exc))
        can_retry = selected.is_primary and failover_applies and selected.failover_target_id is not None
        if not can_retry:
            raise
        detected_at = datetime.now(timezone.utc)
        backup = await provider_keys_service.get_key_by_id(session, selected.failover_target_id)
        if backup is None:
            raise
        backup_credential = await get_decrypted_provider_credential_from_row(
            backup, route.provider, key_provider=key_provider
        )
        try:
            result = await call_fn(backup_credential)
        except ProviderCallError:
            await provider_key_health.record_failure(health_store, backup.id, error_summary=str(primary_exc))
            raise primary_exc from None
        await provider_key_health.record_success(health_store, backup.id)
        session.add(
            FailoverEvent(
                org_id=org_id,
                from_provider_key_id=selected.id,
                to_provider_key_id=backup.id,
                request_id=request_id,
                detected_at=detected_at,
                switched_at=datetime.now(timezone.utc),
            )
        )
        await session.commit()
        return FailoverCallResult(result=result, attempt=1, used_key_id=backup.id)
    else:
        await provider_key_health.record_success(health_store, selected.id)
        return FailoverCallResult(result=result, attempt=0, used_key_id=None)


async def call_self_hosted_provider(
    session: AsyncSession,
    *,
    route: ModelRoute,
    key_provider: KeyProvider,
    call_fn: Callable[[ProviderCredential], Awaitable[T]],
) -> FailoverCallResult:
    """Self-hosted-governance sibling of `call_provider_with_failover()`
    (Phase 5 - Differentiators, 5.5, design doc section 2.3(b)) - called
    INSTEAD OF (never alongside) that function whenever `route.provider ==
    "self_hosted"`.

    `self_hosted_providers` is a completely separate table from
    `provider_keys` - `call_provider_with_failover()`'s `provider_key_
    health.select_provider_key()` looks up `provider_keys` rows keyed on
    `route.provider`, which finds NOTHING for `"self_hosted"` (there is no
    such row, and `provider_name_enum` doesn't even have a `self_hosted`
    member). Multi-key/failover is explicitly out of scope for self-hosted
    endpoints this phase (product spec section 3's deferred list) - this
    function never retries and never records provider-key health state.

    `call_fn(credential)` must perform (and await) the actual outbound
    provider call and return its result, or raise `providers.base.
    ProviderCallError` on failure - identical contract to `call_provider_
    with_failover`'s own `call_fn` parameter, EXCEPT this function does not
    itself catch `ProviderCallError` at all: on failure it propagates
    straight to the caller unchanged (no retry, no failover, single
    endpoint - design doc: "On ProviderCallError: no retry, no failover
    ... re-raise unchanged"). Every existing gateway route-handler call
    site already wraps its `call_provider_with_failover`/`call_self_hosted_
    provider` call in the identical `except ProviderCallError` block, so
    this needs no special handling here.

    Raises `errors.ProviderNotConfiguredError` (404) if the self-hosted
    provider referenced by `route.self_hosted_provider_id` no longer exists
    (e.g. deleted between `resolve_route()`'s cache read and dispatch - a
    narrow race, same class of race `call_provider_with_failover` already
    tolerates for a deleted `provider_keys` row).

    Returns a `FailoverCallResult` with `attempt=0, used_key_id=None` -
    the SAME shape `call_provider_with_failover()` produces on its own
    primary-key-succeeded path, so every downstream consumer of `.result`/
    `.attempt`/`.used_key_id` (header building, usage-log writes) needs
    zero further branching between the two call sites.
    """
    assert route.self_hosted_provider_id is not None, (
        "call_self_hosted_provider() requires route.provider == 'self_hosted' "
        "with a populated self_hosted_provider_id - see resolve_route()."
    )
    try:
        credential = await get_decrypted_self_hosted_credential(
            session, route.self_hosted_provider_id, key_provider=key_provider
        )
    except SelfHostedCredentialNotConfiguredError as exc:
        raise ProviderNotConfiguredError(exc.message) from None
    result = await call_fn(credential)
    return FailoverCallResult(result=result, attempt=0, used_key_id=None)


async def check_budget_available(
    session: AsyncSession, user_id: uuid.UUID, team_id: uuid.UUID | None = None
) -> None:
    """Enforce the per-user hard spend cutoff (Phase 1.4; Phase 2 team-aware).

    Phase 2 (BD-11/A6): when `team_id` is not None (every personal key and
    every team-attributed service-account key), the counter is the
    `TeamMembership` row for `(team_id, user_id)`, never the legacy flat
    `User` one - same NULL=unmetered / `>=`-exhausted semantics, same 402.
    `ensure_current_period` runs first on this path (design doc section
    3.5): the common not-yet-crossed case is a single datetime comparison
    against fields the one joined budget-state query already pulled - no
    extra round trip; only a genuine boundary crossing pays the locked
    reset, after which the (just-zeroed) state is re-read before the
    exhaustion check. `team_id=None` is the byte-for-byte Phase 1.4 path.

    Raises `errors.BudgetExhaustedError` (402) if the user's `budget_usd` is
    not NULL and `current_spend_usd >= budget_usd`. This can only ever check
    whether the user is *already* over budget from previous requests, never
    whether *this* request will push them over (a specific request's cost is
    unknowable before the provider responds, so no pre-call estimate is used
    or permitted) - see `docs/design/phase-1.4-budget-basic-design.md`
    section 5 for the accepted "N completes, N+1 is blocked" semantics.

    Unlike `check_model_policy()`, this is deliberately NOT zero-I/O:
    `current_spend_usd`/`budget_usd` are per-user mutable state that changes
    on every charged request, so this is not a candidate for
    `ModelPolicyCache`'s in-process-cache pattern - it reads through to the
    database on every call. It is still cheaper than `fetch_credential()`
    (a single indexed point lookup vs. decrypt), so the existing ordering
    still saves work on the reject path.

    Call this only *after* `resolve_route()`, `check_model_policy()`, and
    the endpoint's own capability/provider check have already succeeded,
    and *before* `fetch_credential()` - see module docstring.
    """
    state: budget_service.UserBudgetState | budget_service.TeamMembershipBudgetState | None
    if team_id is not None:
        state = await budget_service.get_team_membership_budget_state(
            session, team_id=team_id, user_id=user_id
        )
        if state is None:
            # Guaranteed by construction (design doc section 3.1): a
            # personal/team-attributed key can only be created while its
            # owner holds this membership, and membership removal is
            # blocked while such a key exists (ADR-4).
            raise AssertionError(
                f"authenticated caller's (team_id={team_id}, user_id={user_id}) "
                "does not reference an existing team membership"
            )
        if await team_periods.ensure_current_period(session, state.period):
            # A boundary crossing was just applied - the spend counters were
            # reset; re-read so the exhaustion check sees post-reset state.
            state = await budget_service.get_team_membership_budget_state(
                session, team_id=team_id, user_id=user_id
            )
            if state is None:
                raise AssertionError(
                    f"team membership (team_id={team_id}, user_id={user_id}) "
                    "vanished during period rollover"
                )
    else:
        state = await budget_service.get_budget_state(session, user_id)
        if state is None:
            # Should be unreachable: user_id is FK-enforced off the
            # authenticated ServiceAccountKey row, and a user referenced by
            # any service-account key (active or revoked) can never be
            # deleted (ON DELETE RESTRICT).
            raise AssertionError(
                f"authenticated caller's user_id {user_id} does not reference an existing user"
            )
    if budget_service.is_budget_exhausted(state):
        # `is_budget_exhausted()` only returns True when `budget_usd` is not
        # None (see its docstring) - narrow the type explicitly for mypy
        # rather than widening BudgetExhaustedError's signature to accept
        # None, which would misrepresent that an unmetered user can ever
        # reach this branch.
        assert state.budget_usd is not None
        raise BudgetExhaustedError(
            name=state.name, budget_usd=state.budget_usd, current_spend_usd=state.current_spend_usd
        )


async def record_usage_charge(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    team_id: uuid.UUID | None = None,
    model: str,
    prompt_tokens: int,
    completion_tokens: int | None,
    background_tasks: BackgroundTasks | None = None,
    app: FastAPI | None = None,
    precomputed_cost_usd: Decimal | None = None,
) -> budget_service.ChargeResult:
    """Charge the caller for actual provider-reported usage on `model`
    (Phase 1.4; Phase 2 team-aware).

    Thin dispatcher: `team_id=None` -> the legacy flat
    `services.budget.record_usage_charge` (byte-for-byte the Phase 1.4
    path); otherwise `record_team_membership_usage_charge` (the A6
    `TeamMembership` counter plus the team's ADR-7 aggregate) - see those
    functions' docstrings for the atomic-write/idempotency contract this
    relies on. Returns a `ChargeResult` (`.cost` for the usage log;
    `.team_old_total`/`.team_new_total` feed BD-18's threshold detection).
    `completion_tokens=None` selects the embeddings cost formula; an int
    (including `0`) selects the chat/completions formula.

    Phase 5 (5.5, design doc section 2.3(c)): `precomputed_cost_usd`, when
    given, is used DIRECTLY as the charge amount instead of calling
    `pricing.compute_cost()` - the one and only way a self-hosted request
    (`model` never a `PRICING_TABLE` key) gets charged at all, since
    `compute_cost()` would otherwise raise `PricingEntryMissingError` on
    every single self-hosted request. Callers compute this via `providers.
    pricing.compute_self_hosted_cost()` ONLY when `effective_route.provider
    == "self_hosted"` - `None` (every other request) preserves byte-for-byte
    pre-Phase-5 behavior (`compute_cost()` still runs against `model`).

    BD-18 (threshold alerts): this is the single shared choke point every
    gateway charge - all three routes, streaming and non-streaming - flows
    through, so the threshold-crossing check is wired exactly ONCE here.
    When `background_tasks`/`app` are provided and the team charge crossed
    80%/100% of the team's ceiling (false->true transition on the RETURNING
    values - zero extra queries), delivery is scheduled via
    `BackgroundTasks`, running only after the response has been sent. Both
    None (legacy callers/tests) = detection skipped, charge unchanged.

    Call this ONLY after a provider response with confirmed, complete usage
    has been received - see each gateway route handler for exactly where.
    `model` MUST be the exact same string already passed to
    `resolve_route()`/`check_model_policy()` in this same request.

    Raises `providers.pricing.PricingEntryMissingError` if `model` has no
    pricing entry AND `precomputed_cost_usd` is `None` - callers must let
    this propagate uncaught in the non-streaming path (it becomes a logged
    500 via the app-wide unhandled-exception handler); never catch this and
    charge $0.
    """
    if team_id is None:
        cost = await budget_service.record_usage_charge(
            session,
            user_id=user_id,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            precomputed_cost_usd=precomputed_cost_usd,
        )
        return budget_service.ChargeResult(cost=cost)
    result = await budget_service.record_team_membership_usage_charge(
        session,
        team_id=team_id,
        user_id=user_id,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        precomputed_cost_usd=precomputed_cost_usd,
    )
    if background_tasks is not None and app is not None:
        notifiers.schedule_threshold_alerts(
            background_tasks, app, team_id=team_id, charge=result
        )
    return result


# ---------------------------------------------------------------------------
# Phase 4 (Reliability & Cost Efficiency): rate limiting, response caching,
# graceful degradation, and failover response headers. Gateway-pipeline
# wiring task note (all four features' service-layer code already existed;
# this section is what actually calls it from a live request):
#
#   check_access_schedule -> check_rate_limit -> resolve_route ->
#   check_model_policy -> check_response_cache (HIT -> return immediately,
#   skipping everything below) -> check_residency -> run_dlp_scan ->
#   check_content_classification -> check_budget_available ->
#   check_and_apply_degradation (chat.py ONLY, AC4.4.7) ->
#   call_provider_with_failover -> write_response_cache (miss only) ->
#   log_degradation_event (if triggered) -> response
#
# Fix 6 (NFR gap, AC4.3.4 - security/QA review): `check_rate_limit`/`check_
# response_cache`/`check_and_apply_degradation` previously each read their
# own config through to the database on every call rather than through a
# process-wide cache warmed at `main.py` startup (unlike every sibling
# Phase 1-3 policy check in this module) - `RateLimitCache`/
# `CachingSettingsCache`/`DegradationPolicyCache` existed but were never
# constructed/warmed. All three are now wired into `main.py`'s lifespan
# (same pattern `ModelPolicyCache`/`ResidencyRuleCache` already use) and
# each function below reads from its cache (`resolve_effective_rate_limit_
# rules`/`resolve_effective_caching_config`/`resolve_effective_degradation_
# policy` - each a zero-I/O, pure resolver) instead of issuing a live query.
# ---------------------------------------------------------------------------


async def check_rate_limit(
    session: AsyncSession,
    ctx: GatewayCallerContext,
    *,
    store: SharedStateStore,
    rate_limit_cache: rate_limit_service.RateLimitCache,
) -> rate_limit_service.RateLimitDecision:
    """Enforce rate limiting (Phase 4, design doc section 2.2, AC4.2) - the
    FIRST pipeline step after auth/context resolution, before
    `resolve_route()`/DLP/any provider work (see this module's "Phase 4"
    section note above; also see this function's own gateway-route callers
    for the exact call site).

    Additively enforces the team pool rule (falling back to the org
    default-per-user rule when no team rule is configured) AND the caller's
    personal per-user rule (AC4.2.2/AC4.2.9) - see `services.rate_limit.
    check_and_consume_rate_limit()`. On a trip: `on_limit == queue_retry`
    polls (bounded by the tripped rule's own `max_queue_wait_seconds`,
    AC4.2.5) until the limit clears or the window expires; `on_limit ==
    reject` (the default) rejects immediately (AC4.2.4). Either outcome
    logs one `RateLimitRejectionEvent` row before raising `errors.
    RateLimitExceededError` (429, `Retry-After` header).

    Returns the (passing) `RateLimitDecision` so the caller can attach
    `X-RateLimit-*` headers via `build_rate_limit_headers()` -
    `decision.configured=False` means no rule applies at all anywhere in
    this caller's org/team/user chain (the common, pre-Phase-4-adoption
    case), and `build_rate_limit_headers()` attaches no headers for that
    case (byte-for-byte pre-Phase-4 behavior).
    """
    org_rule, team_rule, user_rule = rate_limit_service.resolve_effective_rate_limit_rules(
        rate_limit_cache, org_id=ctx.org_id, team_id=ctx.team_id, user_id=ctx.user_id
    )
    decision = await rate_limit_service.check_and_consume_rate_limit(
        store,
        org_rule=org_rule,
        team_rule=team_rule,
        user_rule=user_rule,
        team_id=ctx.team_id,
        user_id=ctx.user_id,
    )
    if decision.allowed:
        return decision

    if decision.on_limit == RateLimitOnLimit.QUEUE_RETRY:
        deadline = time.monotonic() + decision.max_queue_wait_seconds
        poll_interval_seconds = 0.5
        while time.monotonic() < deadline:
            await asyncio.sleep(min(poll_interval_seconds, max(0.0, deadline - time.monotonic())))
            org_rule, team_rule, user_rule = rate_limit_service.resolve_effective_rate_limit_rules(
                rate_limit_cache, org_id=ctx.org_id, team_id=ctx.team_id, user_id=ctx.user_id
            )
            decision = await rate_limit_service.check_and_consume_rate_limit(
                store,
                org_rule=org_rule,
                team_rule=team_rule,
                user_rule=user_rule,
                team_id=ctx.team_id,
                user_id=ctx.user_id,
            )
            if decision.allowed:
                return decision
        await rate_limit_service.log_rate_limit_rejection(
            session,
            org_id=ctx.org_id,
            rule=decision.rule,
            team_id=ctx.team_id,
            user_id=ctx.user_id,
            outcome=RateLimitRejectionOutcome.QUEUE_TIMEOUT,
        )
        raise RateLimitExceededError(retry_after_seconds=0, limit=decision.limit, hard_limit=False)

    await rate_limit_service.log_rate_limit_rejection(
        session,
        org_id=ctx.org_id,
        rule=decision.rule,
        team_id=ctx.team_id,
        user_id=ctx.user_id,
        outcome=RateLimitRejectionOutcome.REJECT,
    )
    raise RateLimitExceededError(
        retry_after_seconds=decision.retry_after_seconds, limit=decision.limit, hard_limit=True
    )


def build_rate_limit_headers(decision: rate_limit_service.RateLimitDecision) -> dict[str, str]:
    """AC4.2.6/AC4.2.7: attached on EVERY request a rule applied to,
    limited or not - real sliding-window values, never estimates. Empty
    when no rule was configured at all (`decision.configured=False`)."""
    if not decision.configured:
        return {}
    return {
        "X-RateLimit-Remaining": str(decision.remaining),
        "X-RateLimit-Limit": str(decision.limit),
        "X-RateLimit-Reset": decision.reset_at.isoformat(),
    }


def build_failover_headers(failover: FailoverCallResult) -> dict[str, str]:
    """AC4.1.7/design doc section 3.3. `X-Failover-Attempt` is always
    present (`"0"` on the overwhelming majority of requests - the primary
    key succeeded); `X-Failover-Used-Key` is present only when a backup was
    actually used (never an empty-string/`"null"` placeholder on the common
    path)."""
    headers = {"X-Failover-Attempt": str(failover.attempt)}
    if failover.used_key_id is not None:
        headers["X-Failover-Used-Key"] = str(failover.used_key_id)
    return headers


async def _deliver_cache_lookup_event(
    app: FastAPI,
    *,
    org_id: uuid.UUID,
    team_id: uuid.UUID | None,
    hit: bool,
    provider: str,
    model: str,
    prompt_tokens: int | None,
    completion_tokens: int | None,
) -> None:
    """`BackgroundTasks` entry point for `CacheLookupEvent` recording
    (design doc section 1.6/7.1) - deferred so recording a cache lookup
    never adds to the synchronous cache-lookup critical path on either a
    hit or a miss (AC3.9's ~10ms budget), mirroring `_deliver_async_dlp_
    scan()`'s established shape exactly. Never raises."""
    try:
        async with app.state.db_session_factory() as session:
            session.add(
                CacheLookupEvent(
                    org_id=org_id,
                    team_id=team_id,
                    hit=hit,
                    provider=provider,
                    model=model,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
            )
            await session.commit()
    except Exception:
        logger.error("cache_lookup_event_persist_failed", exc_info=True)


@dataclass(frozen=True)
class CacheCheckResult:
    """Outcome of `check_response_cache()` - threaded through to
    `write_response_cache()` after a miss, and used to build `X-Cache*`
    headers either way."""

    enabled: bool
    hit: bool
    entry: response_cache_service.CacheEntryDetail | None
    prompt_hash: str
    residency_zone: str
    ttl_seconds: int


_CACHE_DISABLED_RESULT = CacheCheckResult(
    enabled=False, hit=False, entry=None, prompt_hash="", residency_zone="", ttl_seconds=0
)


async def check_response_cache(
    session: AsyncSession,
    ctx: GatewayCallerContext,
    route: ModelRoute,
    *,
    request_body: dict[str, Any],
    response_cache: ResponseCache,
    background_tasks: BackgroundTasks,
    app: FastAPI,
    caching_settings_cache: response_cache_service.CachingSettingsCache,
) -> CacheCheckResult:
    """Response-cache lookup (Phase 4, design doc section 2.3/4.3, AC4.3) -
    runs right after `check_rate_limit()` passes and `check_model_policy()`
    has already approved the model, and BEFORE `check_residency()`/`run_dlp_
    scan()` (see this module's "Phase 4" section note above) - a hit skips
    both entirely: the cached entry was only ever written by
    `write_response_cache()` for a request that already passed both checks
    with no redaction/block (AC4.3.6) at WRITE time, so re-running them on
    every hit would be pure overhead. Residency/DLP policy CAN still change
    AFTER a response was cached, for up to `cache_ttl_minutes` (max 24h) -
    Fix 3 (security review, BLOCKING) closes that gap by INVALIDATING the
    affected cache entries the moment residency/DLP policy actually changes
    (`services.residency.set_org_residency_rule`/`set_team_residency_rule`/
    `services.dlp.set_dlp_policy`/`set_team_dlp_override`/custom-pattern
    writes - see each of those, and `services.response_cache.
    CacheInvalidator`), rather than re-running both checks on every hit
    (which would blunt the entire point of caching for latency - the
    ~10ms-overhead NFR, AC4.3.4). A HIT is therefore never served across a
    policy boundary that has since tightened (AC4.3.6) without also being
    architecturally cheap on the hot path.

    `team_id=None` (a personal key with no team context) always misses
    without touching the database at all - see `services.response_cache.
    load_effective_caching_config()`. Every lookup (hit or miss) schedules a
    `CacheLookupEvent` write via `BackgroundTasks` (deferred, off the
    synchronous critical path - AC3.9's ~10ms budget).
    """
    enabled, ttl_seconds = response_cache_service.resolve_effective_caching_config(
        caching_settings_cache, org_id=ctx.org_id, team_id=ctx.team_id
    )
    if not enabled:
        return _CACHE_DISABLED_RESULT
    # `enabled=True` is only ever returned when `team_id is not None` - see
    # `load_effective_caching_config()`'s own contract - narrows the type
    # for the `ResponseCache` calls below (both here and in
    # `write_response_cache()`).
    assert ctx.team_id is not None

    prompt_hash = response_cache_service.compute_prompt_hash(request_body)
    # Fix 4 (security review finding, also independently found by QA): the
    # REAL key_metadata lookup - `check_residency()` a few functions above
    # already does this; hardcoding `None` here meant every vertex_ai/ollama
    # cache entry bucketed into the constant zone "unknown" regardless of
    # the key's actual configured region, defeating the whole point of
    # `residency_zone` being part of the cache key for those two providers.
    key_metadata = await _resolve_provider_key_metadata(session, route)
    residency_zone = response_cache_service.resolve_cache_residency_zone(route, key_metadata)
    entry = await response_cache.get_entry(
        ctx.team_id, ctx.user_id, route.provider, route.native_model_id, prompt_hash, residency_zone
    )
    background_tasks.add_task(
        _deliver_cache_lookup_event,
        app,
        org_id=ctx.org_id,
        team_id=ctx.team_id,
        hit=entry is not None,
        provider=route.provider,
        model=route.native_model_id,
        prompt_tokens=entry.input_tokens if entry is not None else None,
        completion_tokens=entry.output_tokens if entry is not None else None,
    )
    return CacheCheckResult(
        enabled=True,
        hit=entry is not None,
        entry=entry,
        prompt_hash=prompt_hash,
        residency_zone=residency_zone,
        ttl_seconds=ttl_seconds,
    )


async def write_response_cache(
    ctx: GatewayCallerContext,
    route: ModelRoute,
    *,
    cache_check: CacheCheckResult,
    response_cache: ResponseCache,
    response_body: dict[str, Any],
    input_tokens: int,
    output_tokens: int,
    skip_write: bool,
) -> None:
    """Write-through cache population (Phase 4, design doc section 2.3,
    AC4.3.7) - call ONLY after a successful, complete provider response.
    A no-op if caching wasn't enabled for this request (`cache_check.
    enabled=False`) or the request was already a hit (`cache_check.
    hit=True` - a hit's route handler never reaches the provider at all, so
    there is nothing new to write), or `skip_write=True`.

    `skip_write` covers two independent "never cache this" gates a caller
    combines with `or` before calling this function:

    - AC4.3.6/design doc section 5.2 (security section 8.2): NEVER caches
      when this request's DLP scan redacted its content (i.e.
      `DlpPipelineResult.redacted_texts is not None`) - a `block` outcome
      never reaches this far at all (`run_dlp_scan()` already raised, and
      the route handler's `except GatekeyError` branch never calls this
      function). Write happens strictly AFTER the DLP check, gated on its
      actual result - never before.
    - `chat.py` ONLY: NEVER caches a graceful-degradation-substituted
      response under the ORIGINAL (undegraded) model's cache key - see
      `chat.py`'s module docstring for why (a later, non-degraded request
      for that model must never silently receive a cheaper model's cached
      response with no live degradation signal on that later hit).
    """
    if not cache_check.enabled or cache_check.hit or skip_write:
        return
    assert ctx.team_id is not None  # implied by cache_check.enabled - see load_effective_caching_config
    await response_cache.set(
        ctx.team_id,
        ctx.user_id,
        route.provider,
        route.native_model_id,
        cache_check.prompt_hash,
        cache_check.residency_zone,
        response_body,
        ttl_seconds=cache_check.ttl_seconds,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def build_cache_headers(cache_check: CacheCheckResult) -> dict[str, str]:
    """AC4.3.6: `X-Cache: HIT`/`MISS` + `X-Cache-TTL` (seconds remaining,
    HIT only) - attached whenever caching was enabled for this request
    (hit or miss); nothing attached when it wasn't (byte-for-byte
    pre-Phase-4 behavior for a team that never opts in)."""
    if not cache_check.enabled:
        return {}
    if cache_check.hit:
        assert cache_check.entry is not None
        return {"X-Cache": "HIT", "X-Cache-TTL": str(cache_check.entry.ttl_remaining_seconds)}
    return {"X-Cache": "MISS"}


@dataclass(frozen=True)
class DegradationOutcome:
    """Outcome of `check_and_apply_degradation()`. `triggered=False` is the
    overwhelming majority of every request, including every request on a
    team/org that never configures degradation (byte-for-byte pre-Phase-4
    behavior)."""

    triggered: bool
    original_model: str | None
    degraded_model: str | None


_DEGRADATION_NOT_TRIGGERED = DegradationOutcome(
    triggered=False, original_model=None, degraded_model=None
)


async def check_and_apply_degradation(
    session: AsyncSession,
    ctx: GatewayCallerContext,
    *,
    original_model: str,
    degradation_policy_cache: degradation_service.DegradationPolicyCache,
) -> DegradationOutcome:
    """Graceful degradation check (Phase 4, design doc section 2.4/4.4,
    AC4.4) - `POST /v1/chat/completions` ONLY (AC4.4.7: embeddings and
    legacy `/v1/completions` are explicitly out of scope for this phase;
    `completions.py`/`embeddings.py` must never call this). Runs after
    `check_budget_available()` has already confirmed the caller isn't
    already hard-exhausted (design doc: "a reasonable insertion point ...
    since it needs current budget state anyway") and before
    `call_provider_with_failover()` - see this module's "Phase 4" section
    note above.

    A no-op (zero I/O at all, Fix 6) when no degradation policy is
    configured/enabled anywhere in this caller's org/team chain -
    `services.degradation.resolve_effective_degradation_policy()` (cache-
    backed, not the live-DB `load_effective_degradation_policy()`) returns
    `None` and this returns immediately without ever reading budget state,
    preserving byte-for-byte pre-Phase-4 behavior for every org/team that
    never configures this feature.
    """
    policy = degradation_service.resolve_effective_degradation_policy(
        degradation_policy_cache, team_id=ctx.team_id
    )
    if policy is None:
        return _DEGRADATION_NOT_TRIGGERED

    # `policy` is already the fully-resolved effective policy (org-vs-team
    # cumulative-on-`enabled` resolution already applied by
    # `load_effective_degradation_policy()`) - re-threading it through
    # `services.degradation.check_degradation()`'s own `org_policy`/`team_
    # policy` resolution as `org_policy=policy, team_policy=None` is
    # provably a no-op re-resolution (an already-enabled policy passed as
    # the org slot with no team slot always resolves back to itself - see
    # `resolve_degradation_policy()`), done this way so this function
    # doesn't have to re-implement `check_degradation()`'s budget-threshold
    # arithmetic here.
    decision = await degradation_service.check_degradation(
        session,
        ctx.user_id,
        ctx.team_id,
        original_model,
        org_policy=policy,
        team_policy=None,
    )
    if not decision.triggered or decision.degraded_model is None:
        return _DEGRADATION_NOT_TRIGGERED
    return DegradationOutcome(
        triggered=True, original_model=original_model, degraded_model=decision.degraded_model
    )


def build_degradation_headers(outcome: DegradationOutcome) -> dict[str, str]:
    """AC4.4.4: headers are ABSENT entirely when no degradation occurred -
    never `X-Gatekey-Degraded: false`."""
    if not outcome.triggered:
        return {}
    assert outcome.original_model is not None and outcome.degraded_model is not None
    return degradation_service.get_degradation_headers(outcome.original_model, outcome.degraded_model)


# ---------------------------------------------------------------------------
# Fix 5 (security review finding, request-time half): a degraded model's
# `downgrade_target_model` is validated against the model access policy at
# CONFIG time (`services.model_policy.validate_downgrade_target_model()`,
# called from `api.v1.admin.degradation_policy`'s two PUT handlers) - but
# policy can be tightened AFTER a degradation policy was already configured,
# and `check_and_apply_degradation()` above is a pure budget-proximity
# decision that never re-checks model policy/content-classification/
# residency. Without this second, request-time check, a Team Lead could
# configure degradation once (while permitted), have an Org Admin later
# deny that model, and every subsequent budget-proximity-triggered request
# would keep silently routing to the now-denied model - a self-escalation
# path around the org's own model/content/residency policy that a static,
# config-time-only check cannot close.
# ---------------------------------------------------------------------------


async def revalidate_degraded_model(
    session: AsyncSession,
    ctx: GatewayCallerContext,
    *,
    degraded_model: str,
    degraded_route: ModelRoute,
    model_policy_cache: ModelPolicyCache,
    team_model_policy_cache: TeamModelPolicyCache,
    content_aware_cache: ContentAwareRuleCache,
    residency_cache: ResidencyRuleCache,
    category_findings: frozenset[str],
    source_ip: str | None,
) -> bool:
    """Re-run `check_model_policy()`, `check_content_classification()`, and
    `check_residency()` against the SUBSTITUTED (degraded) model/route -
    the exact same three checks already run for the original model earlier
    in this same request, applied a second time to the model degradation is
    about to dispatch to instead.

    Call this only *after* `check_and_apply_degradation()` returned a
    triggered outcome, and *before* dispatching to `degraded_route` - see
    `api.v1.gateway.chat`'s call site for the full ordering.

    Returns `True` if the degraded model is still allowed (dispatch may
    proceed against it, byte-for-byte the existing behavior); `False` if it
    is no longer allowed by any of the three checks. `category_findings`
    (Phase 5, 5.3 - generalized from the pre-Phase-5 `pii_detected: bool`)
    is reused from this same request's already-completed DLP scan (see
    `run_dlp_scan()`) - re-scanning the (unchanged) prompt text a second
    time for this substituted model would be redundant work for a signal
    that does not depend on which model is targeted.
    """
    try:
        check_model_policy(degraded_model, model_policy_cache, team_model_policy_cache, ctx.team_id)
        check_content_classification(degraded_model, content_aware_cache, category_findings=category_findings)
        await check_residency(
            session,
            degraded_route,
            cache=residency_cache,
            team_id=ctx.team_id,
            org_id=ctx.org_id,
            actor_user_id=ctx.user_id,
            actor_label=ctx.name,
            source_ip=source_ip,
        )
    except (ModelDeniedError, ResidencyViolationError):
        return False
    return True


async def raise_hard_budget_block_after_degradation_skip(
    session: AsyncSession, user_id: uuid.UUID, team_id: uuid.UUID | None
) -> None:
    """Design doc section 7.4's edge-case table: "Degradation triggered but
    fallback model denied by policy -> Skip degradation; hard block at
    budget." Call this when `revalidate_degraded_model()` above returns
    `False` - never dispatch to the original (undegraded) model instead.

    This is a deliberate architectural choice, not an approximation of
    `check_budget_available()`: degradation exists specifically as a safety
    valve to avoid a hard block while a caller is within the configured
    threshold of their budget ceiling. If that valve is unavailable because
    its configured target is no longer policy-permitted, silently falling
    back to the original, more expensive model would defeat the entire
    reason degradation exists (continuing to serve requests near the
    ceiling without also continuing to spend at the undegraded rate) - so
    this blocks unconditionally rather than re-running the ordinary
    (not-yet-exhausted) budget check a second time. Always raises
    `errors.BudgetExhaustedError` (402) - mirrors `check_budget_available()`'s
    own team-vs-flat-user budget-state resolution (Phase 2, BD-11/A6), since
    `check_and_apply_degradation()` only ever triggers when a metered
    (`budget_usd is not None`) budget state exists for this same caller.
    """
    state: budget_service.UserBudgetState | budget_service.TeamMembershipBudgetState | None
    if team_id is not None:
        state = await budget_service.get_team_membership_budget_state(
            session, team_id=team_id, user_id=user_id
        )
        if state is None:
            raise AssertionError(
                f"authenticated caller's (team_id={team_id}, user_id={user_id}) "
                "does not reference an existing team membership"
            )
    else:
        state = await budget_service.get_budget_state(session, user_id=user_id)
    # Guaranteed by `check_and_apply_degradation()` only ever triggering on
    # a metered budget - see this function's docstring.
    assert state is not None and state.budget_usd is not None
    raise BudgetExhaustedError(
        name=state.name, budget_usd=state.budget_usd, current_spend_usd=state.current_spend_usd
    )


async def log_degradation_event(
    session: AsyncSession,
    *,
    team_id: uuid.UUID | None,
    user_id: uuid.UUID,
    usage_log_id: uuid.UUID | None,
    original_model: str,
    degraded_model: str,
    prompt_tokens: int,
    completion_tokens: int | None,
) -> None:
    """Persist one `DegradationEvent` row (AC4.4's cost-savings dashboard) -
    call ONLY after a successful, complete provider response on the
    (already-substituted) degraded model, with the ACTUAL token counts that
    response reported - never the pre-call estimate `services.degradation.
    check_degradation()`'s own `original_cost_estimate`/`degraded_cost_
    estimate` fields compute (those exist for that function's own
    return-shape completeness but are deliberately NOT used here, per
    design doc section 1.7's "original_cost/degraded_cost computed from
    actual provider charges").

    `original_cost` is what the ORIGINAL (undegraded) model would have cost
    for these same actual token counts; `degraded_cost` is what was
    actually charged. `team_id=None` is a no-op (`DegradationEvent.team_id`
    is NOT NULL - see design doc AC4.4.7/this module's `check_and_apply_
    degradation()`, which is only ever called on the chat pipeline, where a
    legacy no-team caller is a real, if rare, possibility). Best-effort:
    logs and swallows any failure (missing pricing entry, DB error) rather
    than turning an already-successful, already-charged response into a
    500 - mirrors `services.usage_logs.record_usage_log()`'s own
    never-raises contract.
    """
    if team_id is None:
        return
    try:
        original_cost_usd = budget_service.compute_cost(
            original_model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )
    except PricingEntryMissingError:
        original_cost_usd = Decimal("0")
    try:
        degraded_cost_usd = budget_service.compute_cost(
            degraded_model, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens
        )
    except PricingEntryMissingError:
        degraded_cost_usd = Decimal("0")
    try:
        await degradation_service.DegradationEventLogger(session).log_degradation(
            team_id=team_id,
            user_id=user_id,
            request_id=usage_log_id,
            original_model=original_model,
            degraded_model=degraded_model,
            original_cost=original_cost_usd,
            degraded_cost=degraded_cost_usd,
        )
    except Exception:
        logger.error("degradation_event_persist_failed", exc_info=True)


def validate_idempotency_key(idempotency_key: str | None) -> str | None:
    """Validate an optional `Idempotency-Key` header value.

    Plumb-through only (design doc section 6/Q3) - this deliberately
    implements no caching/dedup logic; it only bounds-checks the value so a
    grossly malformed header gets clear 400 feedback instead of being
    silently accepted or truncated, then threads the (stripped) value into
    structured logging for correlation. Returns `None` unchanged if no
    header was sent.
    """
    if idempotency_key is None:
        return None
    key = idempotency_key.strip()
    if not key or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise HttpUnsupportedRequestError(
            "Idempotency-Key header must be a non-empty opaque string of at "
            f"most {MAX_IDEMPOTENCY_KEY_LENGTH} characters."
        )
    return key


def new_request_id() -> str:
    """Generate an opaque per-request correlation id for structured logging."""
    return uuid.uuid4().hex


class LatencyTimer:
    """Collects `time.perf_counter()` markers through one gateway request's lifecycle.

    Design doc section 6 (explicit non-functional requirement): every
    gateway route handler records entry, pre-dispatch (right before calling
    the provider), provider-response-received, and flush-complete (end of a
    non-streaming response / end of a streaming generator) markers and emits
    them as structured log fields. This is debug/perf instrumentation only -
    never persisted to any DB table (the 1.5 usage-log schema is a later
    phase's concern).
    """

    def __init__(self) -> None:
        self._start = time.perf_counter()
        self.marks: dict[str, float] = {"entry": self._start}

    def mark(self, name: str) -> None:
        self.marks[name] = time.perf_counter()

    def deltas_ms(self) -> dict[str, float]:
        """Every recorded mark, expressed as milliseconds since `entry`."""
        return {name: round((t - self._start) * 1000, 3) for name, t in self.marks.items()}


def log_gateway_request(
    *,
    request_id: str,
    endpoint: str,
    provider: str | None,
    model: str | None,
    stream: bool,
    status: str,
    timer: LatencyTimer,
    idempotency_key: str | None,
) -> None:
    """Emit one structured `gateway_request` log line for latency/correlation.

    Debug/perf instrumentation only (see `LatencyTimer` docstring) - not a
    persisted usage-accounting record. None of these fields are secret
    material: `idempotency_key` is a caller-supplied opaque correlation
    token (not a credential), and `provider`/`model` are non-secret routing
    metadata.
    """
    logger.info(
        "gateway_request",
        extra={
            "request_id": request_id,
            "endpoint": endpoint,
            "provider": provider,
            "model": model,
            "stream": stream,
            "status": status,
            "idempotency_key": idempotency_key,
            "latency_ms": timer.deltas_ms(),
        },
    )
