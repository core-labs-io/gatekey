"""Canary suite execution for the Provider Drift Detector (Phase 5 -
Differentiators, 5.4). See `gatekey/phase-5-product-spec.md` AC5.4.x and
`gatekey/phase-5-technical-design.md` section 2.2 for the full design.

Imported ONLY from `services/scheduler.py` (the daily tick,
`run_drift_canary_if_due`) and `api/v1/admin/drift_detector.py` (read
endpoints) - design doc section 5's wiring checklist "5.2 (Drift Detector,
5.4)" row 3.

**Cost separation (the hard NFR here, AC5.4.9)**: `run_canary_suite_for_org`
dispatches canary prompts through the REAL provider call path (so latency/
output/refusal are real, not simulated) via `_dispatch_canary_call` below -
a small, deliberate, PARALLEL low-level dispatch that mirrors `api/v1/
gateway/chat.py`'s private `_create_non_streaming` provider-branch dispatch
one-for-one, but is never routed through `chat.py`, `call_provider_with_
failover`, or any budget/DLP/residency/rate-limit check - a canary run is an
internal admin operation, not user traffic (design doc section 2.2). Cost is
computed via the normal `services.budget.compute_cost()` path (BYOK/static
`MODEL_REGISTRY` models) or `providers.pricing.compute_self_hosted_cost()`
(self-hosted models - see "Self-hosted model resolution" below) and written
ONLY to `canary_runs.cost_usd` - `services.budget.record_usage_charge()` is
NEVER called here, so no `usage_logs` row is ever written and no team/user/
org budget ceiling is ever touched for canary traffic. This is what makes
"canary cost never touches user-attributable budget" a structurally
verifiable invariant, not a convention that could silently drift (see
`db/models/canary_run.py`'s own "Cost isolation" docstring note).

**Self-hosted model resolution (design doc section 9.1's mandatory test
scenario: "Self-hosted model canary-tested")**: `run_canary_suite_for_org`
resolves each actively-used model the SAME way `api/v1/gateway/common.
resolve_route()` does - `providers.model_registry.resolve_model()` (the
static `MODEL_REGISTRY` lookup) tried first, unconditionally, falling back
to a lookup in the pre-warmed `SelfHostedModelRouteCache`
(`app.state.self_hosted_model_route_cache`, the same process-wide cache
instance `resolve_route()` itself reads - never a second, independently
re-queried instance) only when that fails. A self-hosted canary target
dispatches via `services.self_hosted_providers.
get_decrypted_self_hosted_credential()`/the Ollama-compatible client (same
credential/client path the real gateway's self-hosted dispatch uses - see
`_dispatch_canary_call`'s `self_hosted` branch) instead of a BYOK provider
credential, and its cost is computed via `compute_self_hosted_cost()` (the
canary's own `max_tokens`/latency-based formula still applies) rather than
`compute_cost()` - still written ONLY to `canary_runs.cost_usd`, same as
every other provider (AC5.4.9's cost-separation guarantee applies
identically regardless of which cost formula was used).

Refusal detection (AC5.4.4) and the output-similarity metric (AC5.4.5) are
both deterministic, in-process, dependency-free pure functions (`detect_
refusal`/`compute_similarity`) - no ML classifier, no embeddings-API call,
consistent with this codebase's existing regex-based DLP engine and the
feature's own "must not consume meaningful budget" NFR. Drift-threshold
flagging (AC5.4.6) is likewise pure (`latency_drift_delta_pct`/`refusal_
rate_drift_delta_pp`/`similarity_drift_delta_pct`), fixed/global thresholds
per model, per the architect's resolution of the AC5.4.6/AC5.4.11 tension
(only per-model enable/disable is configurable - `canary_model_settings` -
never per-model thresholds, this phase).
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.canary_baseline import CanaryBaseline
from gatekey.db.models.canary_model_setting import CanaryModelSetting
from gatekey.db.models.canary_prompt import CanaryPrompt
from gatekey.db.models.canary_run import CanaryRun
from gatekey.db.models.drift_alert import DriftAlert
from gatekey.db.models.usage_log import UsageLog
from gatekey.providers import anthropic as anthropic_provider
from gatekey.providers import ollama as ollama_provider
from gatekey.providers import openai as openai_provider
from gatekey.providers import openrouter as openrouter_provider
from gatekey.providers import vertex_ai as vertex_provider
from gatekey.providers.base import ProviderCallError
from gatekey.providers.model_registry import (
    ModelCapability,
    ModelRoute,
    UnknownModelError,
    resolve_model,
)
from gatekey.providers.pricing import compute_self_hosted_cost
from gatekey.providers.vertex_ai import VertexAITokenCache
from gatekey.schemas.chat import ChatCompletionRequest, ChatCompletionResponse, ChatMessage
from gatekey.services.budget import compute_cost
from gatekey.services.encryption import DecryptionError, EnvKeyProvider, KeyProvider
from gatekey.services.proxy_keys import (
    CredentialDecodeError,
    ProviderKeyNotConfiguredError,
    UnsupportedProviderCredentialError,
    get_decrypted_provider_credential,
)
from gatekey.services.self_hosted_providers import (
    SelfHostedCredentialDecodeError,
    SelfHostedCredentialNotConfiguredError,
    SelfHostedModelRouteCache,
    SelfHostedRouteEntry,
    get_decrypted_self_hosted_credential,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("gatekey")

# design doc section 2.2 "Key Decision": sequential per-model execution,
# capped batch size, default 50 - bounds concurrent outbound canary calls
# to exactly 1 at all times (never `asyncio.gather`), and bounds how many
# models one tick can touch (a self-hosted org can register an unbounded
# number of additional models - see design doc section 5.5).
_CANARY_MAX_MODELS_PER_TICK = 50

# AC5.4.8: "actively used" = >=1 real (non-canary) usage_logs request in the
# trailing 7 days. `usage_logs` never gets a row from canary traffic (see
# module docstring "Cost separation"), so this query is inherently
# canary-free without any extra filtering.
_ACTIVE_MODEL_WINDOW_DAYS = 7

# AC5.4.2: a baseline is established once this many daily runs exist for a
# (model, prompt) pair with no baseline row yet.
_BASELINE_WINDOW_SIZE = 7

# AC5.4.6: rolling window size for drift comparison (same size as the
# baseline-establishment window - "last 7 daily runs vs. baseline").
_ROLLING_WINDOW_SIZE = 7

# AC5.4.6's three fixed, global thresholds (not admin-configurable this
# phase - see module docstring).
_LATENCY_DRIFT_THRESHOLD_FRACTION = Decimal("0.50")  # >50% relative deviation
_REFUSAL_RATE_DRIFT_THRESHOLD_PP = Decimal("20.00")  # >20 percentage points
_SIMILARITY_DRIFT_FLOOR = Decimal("0.70")  # average drops below 0.7
# A model is, by construction, perfectly self-similar to its own reference
# output at the moment a baseline is established - there is no separate
# "baseline similarity score" column to read, so 1.00 is the fixed
# reference point `similarity_drift_delta_pct` compares the rolling
# observed average against.
_BASELINE_SIMILARITY_REFERENCE = Decimal("1.00")


# ---------------------------------------------------------------------------
# AC5.4.4 - refusal detection: keyword/regex heuristic, not an ML
# classifier (consistent with this codebase's existing DLP engine).
# ---------------------------------------------------------------------------

_REFUSAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bi cannot help with\b",
        r"\bi can'?t help with\b",
        r"\bi'?m not able to\b",
        r"\bi am not able to\b",
        r"\bi won'?t provide\b",
        r"\bi cannot provide\b",
        r"\bi can'?t provide\b",
        r"\bi'?m unable to\b",
        r"\bi am unable to\b",
        r"\bi cannot assist\b",
        r"\bi can'?t assist\b",
        r"\bi must decline\b",
        r"\bi'?m sorry,? but i (?:can'?t|cannot)\b",
        r"\bas an ai\b.{0,40}\bcannot\b",
        r"\bi'?m not comfortable\b",
    )
)


def detect_refusal(output_text: str) -> bool:
    """AC5.4.4: a keyword/regex heuristic over the canary response text.
    Pure, deterministic, no external dependency."""
    return any(pattern.search(output_text) for pattern in _REFUSAL_PATTERNS)


# ---------------------------------------------------------------------------
# AC5.4.5 - output similarity: a lightweight, deterministic, in-process
# text-similarity metric (token-level Jaccard). No embeddings-API call.
# ---------------------------------------------------------------------------

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    return set(_TOKEN_PATTERN.findall(text.lower()))


def compute_similarity(text_a: str, text_b: str) -> Decimal:
    """AC5.4.5: token-level Jaccard similarity, `[0, 1]`. Pure, deterministic,
    no external dependency - two empty strings are trivially identical
    (`1.0`); one empty and one non-empty are maximally dissimilar (`0.0`)."""
    tokens_a = _tokenize(text_a)
    tokens_b = _tokenize(text_b)
    if not tokens_a and not tokens_b:
        return Decimal("1.0")
    if not tokens_a or not tokens_b:
        return Decimal("0.0")
    intersection = len(tokens_a & tokens_b)
    union = len(tokens_a | tokens_b)
    return (Decimal(intersection) / Decimal(union)).quantize(Decimal("0.0001"))


# ---------------------------------------------------------------------------
# AC5.4.6 - fixed-threshold drift flagging. Pure functions: given a
# baseline value and a rolling-window observed value, return the `delta_pct`
# to record IF the fixed threshold is crossed, else `None` (no drift).
# ---------------------------------------------------------------------------


def latency_drift_delta_pct(
    baseline_latency_ms: Decimal, observed_latency_ms: Decimal
) -> Decimal | None:
    """`>50%` relative deviation from baseline, either direction (a model
    that got dramatically FASTER is still a behavior change worth
    surfacing, not just a regression)."""
    if baseline_latency_ms <= 0:
        return None
    delta_fraction = (observed_latency_ms - baseline_latency_ms) / baseline_latency_ms
    if abs(delta_fraction) > _LATENCY_DRIFT_THRESHOLD_FRACTION:
        return (delta_fraction * 100).quantize(Decimal("0.01"))
    return None


def refusal_rate_drift_delta_pp(
    baseline_refusal_rate: Decimal, observed_refusal_rate: Decimal
) -> Decimal | None:
    """`>20` percentage-point INCREASE (a drop in refusal rate is never
    flagged - AC5.4.6 only names a rise as the drift signal)."""
    increase_pp = (observed_refusal_rate - baseline_refusal_rate) * 100
    if increase_pp > _REFUSAL_RATE_DRIFT_THRESHOLD_PP:
        return increase_pp.quantize(Decimal("0.01"))
    return None


def similarity_drift_delta_pct(observed_similarity: Decimal) -> Decimal | None:
    """Average rolling-window similarity drops below the fixed `0.70`
    floor - a direct threshold on the observed value itself (unlike
    latency/refusal_rate, AC5.4.6 does not phrase this one as a
    percentage-deviation-from-baseline check)."""
    if observed_similarity < _SIMILARITY_DRIFT_FLOOR:
        delta_fraction = (
            observed_similarity - _BASELINE_SIMILARITY_REFERENCE
        ) / _BASELINE_SIMILARITY_REFERENCE
        return (delta_fraction * 100).quantize(Decimal("0.01"))
    return None


# ---------------------------------------------------------------------------
# Canary provider dispatch - a small, deliberate, PARALLEL low-level call
# path. Mirrors `api/v1/gateway/chat.py::_create_non_streaming`'s per-
# provider branch one-for-one, but lives here (never imported by chat.py,
# never routed through it) - see module docstring.
# ---------------------------------------------------------------------------


async def _dispatch_canary_call(
    provider: str,
    native_model_id: str,
    request: ChatCompletionRequest,
    credential: Any,
    http_client: httpx.AsyncClient,
    token_cache: VertexAITokenCache,
) -> ChatCompletionResponse:
    if provider == "openai":
        return await openai_provider.create_chat_completion(
            http_client, native_model_id, request, credential
        )
    if provider == "anthropic":
        return await anthropic_provider.create_chat_completion(
            http_client, native_model_id, request, credential
        )
    if provider == "vertex_ai":
        return await vertex_provider.create_chat_completion(
            http_client, native_model_id, request, credential, token_cache
        )
    if provider == "ollama":
        return await ollama_provider.create_chat_completion(
            http_client, native_model_id, request, credential
        )
    if provider == "self_hosted":
        # Phase 5 (5.5, AC5.5.2) - mirrors `api/v1/gateway/chat.py`'s
        # `_create_non_streaming`'s identical `self_hosted` branch: vLLM/
        # Ollama both expose an OpenAI-compatible surface, so `credential`
        # (an `OllamaCredential`, see `services.self_hosted_providers.
        # get_decrypted_self_hosted_credential`) is already the shape
        # `providers.ollama.create_chat_completion` expects.
        return await ollama_provider.create_chat_completion(
            http_client, native_model_id, request, credential
        )
    if provider == "openrouter":
        return await openrouter_provider.create_chat_completion(
            http_client, native_model_id, request, credential
        )
    raise AssertionError(f"no canary dispatch for provider {provider!r}")


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def _get_actively_used_models(session: "AsyncSession") -> list[str]:
    """AC5.4.8's "actively used" definition: >=1 real, non-canary
    `usage_logs` request in the trailing 7 days for this org. Canary traffic
    never writes a `usage_logs` row (module docstring "Cost separation"),
    so this query cannot accidentally include canary-only models."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=_ACTIVE_MODEL_WINDOW_DAYS)
    stmt = (
        select(UsageLog.model)
        .where(
            UsageLog.org_id == DEFAULT_ORG_ID,
            UsageLog.created_at >= cutoff,
            UsageLog.model.is_not(None),
        )
        .distinct()
    )
    rows = (await session.execute(stmt)).scalars().all()
    return sorted({model for model in rows if model is not None})


async def _filter_canary_enabled_models(session: "AsyncSession", models: list[str]) -> list[str]:
    """`canary_model_settings` - absence of a row means "enabled" (design
    doc section 4.2's absence-of-row-means-default convention)."""
    settings_rows = (await session.execute(select(CanaryModelSetting))).scalars().all()
    disabled = {row.model for row in settings_rows if not row.enabled}
    return [model for model in models if model not in disabled]


async def _get_enabled_canary_prompts(session: "AsyncSession") -> list[CanaryPrompt]:
    stmt = select(CanaryPrompt).where(CanaryPrompt.enabled.is_(True))
    return list((await session.execute(stmt)).scalars().all())


async def establish_baseline_if_ready(
    session: "AsyncSession", *, model: str, prompt_id: uuid.UUID
) -> bool:
    """AC5.4.2: once `_BASELINE_WINDOW_SIZE` (7) `canary_runs` exist for a
    `(model, prompt_id)` pair with no baseline row yet, compute and insert
    exactly one `canary_baselines` row (rolling average latency/refusal
    rate over those runs, and a reference `output_text` for future
    similarity comparison - the most recent of the establishment window's
    runs, a deliberate v1 choice for "freshest representative sample").
    Never re-baselines an already-established `(model, prompt_id)` pair
    (`canary_baselines`' own docstring: "never updated in place"). Returns
    whether a baseline was established this call."""
    existing = (
        await session.execute(
            select(CanaryBaseline).where(
                CanaryBaseline.model == model, CanaryBaseline.prompt_id == prompt_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return False

    runs = (
        (
            await session.execute(
                select(CanaryRun)
                .where(CanaryRun.model == model, CanaryRun.prompt_id == prompt_id)
                .order_by(CanaryRun.run_at.asc())
                .limit(_BASELINE_WINDOW_SIZE)
            )
        )
        .scalars()
        .all()
    )
    if len(runs) < _BASELINE_WINDOW_SIZE:
        return False

    count = Decimal(len(runs))
    baseline_latency_ms = (Decimal(sum(run.latency_ms for run in runs)) / count).quantize(
        Decimal("0.01")
    )
    refusals = sum(1 for run in runs if run.refusal_detected)
    baseline_refusal_rate = (Decimal(refusals) / count).quantize(Decimal("0.0001"))
    baseline_output_text = runs[-1].output_text

    session.add(
        CanaryBaseline(
            model=model,
            prompt_id=prompt_id,
            baseline_latency_ms=baseline_latency_ms,
            baseline_refusal_rate=baseline_refusal_rate,
            baseline_output_text=baseline_output_text,
        )
    )
    await session.flush()
    return True


async def flag_drift(session: "AsyncSession", *, model: str) -> list[DriftAlert]:
    """AC5.4.6: for `model`, aggregate the rolling `_ROLLING_WINDOW_SIZE`-run
    window across every enabled canary prompt that already has an
    established baseline, and insert a `drift_alerts` row per metric that
    crosses its fixed, global threshold (`latency_drift_delta_pct`/
    `refusal_rate_drift_delta_pp`/`similarity_drift_delta_pct`). `model`
    (not `model, prompt`) is `drift_alerts`' own granularity (design doc
    section 4.2's literal DDL has no `prompt_id` column), so multi-prompt
    signals are combined here, not reported per-prompt. Returns the newly
    inserted alerts (empty if nothing crossed a threshold, or if `model` has
    no established baseline yet)."""
    baselines = (
        (await session.execute(select(CanaryBaseline).where(CanaryBaseline.model == model)))
        .scalars()
        .all()
    )
    if not baselines:
        return []

    baseline_latencies: list[Decimal] = []
    baseline_refusal_rates: list[Decimal] = []
    observed_latencies: list[Decimal] = []
    observed_refusal_rates: list[Decimal] = []
    observed_similarities: list[Decimal] = []

    for baseline in baselines:
        runs = (
            (
                await session.execute(
                    select(CanaryRun)
                    .where(CanaryRun.model == model, CanaryRun.prompt_id == baseline.prompt_id)
                    .order_by(CanaryRun.run_at.desc())
                    .limit(_ROLLING_WINDOW_SIZE)
                )
            )
            .scalars()
            .all()
        )
        if not runs:
            continue
        count = Decimal(len(runs))
        baseline_latencies.append(baseline.baseline_latency_ms)
        baseline_refusal_rates.append(baseline.baseline_refusal_rate)
        observed_latencies.append(Decimal(sum(run.latency_ms for run in runs)) / count)
        observed_refusal_rates.append(
            Decimal(sum(1 for run in runs if run.refusal_detected)) / count
        )
        similarity_scores = [
            run.similarity_score_vs_baseline
            for run in runs
            if run.similarity_score_vs_baseline is not None
        ]
        if similarity_scores:
            observed_similarities.append(
                sum(similarity_scores, Decimal(0)) / Decimal(len(similarity_scores))
            )

    if not observed_latencies:
        return []

    new_alerts: list[DriftAlert] = []

    baseline_latency_avg = sum(baseline_latencies, Decimal(0)) / Decimal(len(baseline_latencies))
    observed_latency_avg = sum(observed_latencies, Decimal(0)) / Decimal(len(observed_latencies))
    latency_delta = latency_drift_delta_pct(baseline_latency_avg, observed_latency_avg)
    if latency_delta is not None:
        new_alerts.append(
            DriftAlert(
                model=model,
                metric="latency",
                baseline_value=baseline_latency_avg,
                observed_value=observed_latency_avg,
                delta_pct=latency_delta,
            )
        )

    baseline_refusal_avg = sum(baseline_refusal_rates, Decimal(0)) / Decimal(
        len(baseline_refusal_rates)
    )
    observed_refusal_avg = sum(observed_refusal_rates, Decimal(0)) / Decimal(
        len(observed_refusal_rates)
    )
    refusal_delta = refusal_rate_drift_delta_pp(baseline_refusal_avg, observed_refusal_avg)
    if refusal_delta is not None:
        new_alerts.append(
            DriftAlert(
                model=model,
                metric="refusal_rate",
                baseline_value=baseline_refusal_avg * 100,
                observed_value=observed_refusal_avg * 100,
                delta_pct=refusal_delta,
            )
        )

    if observed_similarities:
        observed_similarity_avg = sum(observed_similarities, Decimal(0)) / Decimal(
            len(observed_similarities)
        )
        similarity_delta = similarity_drift_delta_pct(observed_similarity_avg)
        if similarity_delta is not None:
            new_alerts.append(
                DriftAlert(
                    model=model,
                    metric="output_similarity",
                    baseline_value=_BASELINE_SIMILARITY_REFERENCE,
                    observed_value=observed_similarity_avg,
                    delta_pct=similarity_delta,
                )
            )

    for alert in new_alerts:
        session.add(alert)
    if new_alerts:
        await session.flush()
    return new_alerts


@dataclass(frozen=True)
class CanarySuiteRunSummary:
    """Return shape of `run_canary_suite_for_org` - purely informational
    (logging/tests), not consumed by any budget/billing path."""

    models_tested: int
    runs_recorded: int
    baselines_established: int
    alerts_flagged: int


async def run_canary_suite_for_org(session: "AsyncSession", app: "FastAPI") -> CanarySuiteRunSummary:
    """AC5.4.8/design doc section 2.2: the full daily canary sweep for the
    (single, default) org. Sequential per model, capped at
    `_CANARY_MAX_MODELS_PER_TICK` - see module docstring. Never raises for
    a single model/prompt failure (caught, logged, sweep continues - design
    doc section 7.2) - only lets a truly unexpected error propagate to the
    scheduler tick's own try/except (`services/scheduler.py`)."""
    http_client: httpx.AsyncClient = app.state.provider_http_client
    token_cache: VertexAITokenCache = app.state.vertex_token_cache
    key_provider: KeyProvider = EnvKeyProvider.from_settings(app.state.settings)
    # Fix 2 (QA-confirmed gap): the SAME pre-warmed, process-wide
    # `SelfHostedModelRouteCache` instance `api/v1/gateway/common.
    # resolve_route()` reads - never a second, independently re-queried
    # instance - so a self-hosted model this scheduler tick sees is exactly
    # the set of currently-verified/routable self-hosted models the real
    # gateway would dispatch to.
    self_hosted_cache: SelfHostedModelRouteCache = app.state.self_hosted_model_route_cache

    active_models = await _get_actively_used_models(session)
    enabled_models = await _filter_canary_enabled_models(session, active_models)
    models_this_tick = enabled_models[:_CANARY_MAX_MODELS_PER_TICK]
    prompts = await _get_enabled_canary_prompts(session)

    runs_recorded = 0
    baselines_established = 0
    alerts_flagged = 0

    for model in models_this_tick:
        # Fix 2: mirror `api/v1/gateway/common.resolve_route()`'s exact
        # fallback logic - the static `MODEL_REGISTRY` lookup always tried
        # first, unconditionally; only on `UnknownModelError` does this fall
        # back to the self-hosted route cache. `self_hosted_entry` carries
        # the `cost_basis_per_gpu_hour` needed for `compute_self_hosted_
        # cost()` below - `None` for every non-self-hosted route.
        self_hosted_entry: SelfHostedRouteEntry | None = None
        try:
            route = resolve_model(model)
        except UnknownModelError:
            self_hosted_entry = self_hosted_cache.get(model)
            if self_hosted_entry is None:
                logger.warning("drift_canary_unknown_model_skipped", extra={"model": model})
                continue
            route = ModelRoute(
                provider="self_hosted",
                capability=ModelCapability.CHAT,
                native_model_id=model,
                self_hosted_provider_id=self_hosted_entry.provider_id,
            )

        if route.provider == "self_hosted":
            assert self_hosted_entry is not None
            try:
                credential = await get_decrypted_self_hosted_credential(
                    session, self_hosted_entry.provider_id, key_provider=key_provider
                )
            except (
                SelfHostedCredentialNotConfiguredError,
                DecryptionError,
                SelfHostedCredentialDecodeError,
            ):
                logger.warning(
                    "drift_canary_no_credential_skipped",
                    extra={"model": model, "provider": route.provider},
                )
                continue
        else:
            try:
                credential = await get_decrypted_provider_credential(
                    session, route.provider, key_provider=key_provider
                )
            except (
                ProviderKeyNotConfiguredError,
                DecryptionError,
                CredentialDecodeError,
                UnsupportedProviderCredentialError,
            ):
                logger.warning(
                    "drift_canary_no_credential_skipped",
                    extra={"model": model, "provider": route.provider},
                )
                continue

        for prompt in prompts:
            baseline = (
                await session.execute(
                    select(CanaryBaseline).where(
                        CanaryBaseline.model == model, CanaryBaseline.prompt_id == prompt.id
                    )
                )
            ).scalar_one_or_none()

            request = ChatCompletionRequest(
                model=route.native_model_id,
                messages=[ChatMessage(role="user", content=prompt.prompt_text)],
                max_tokens=prompt.max_tokens,
                stream=False,
            )
            started_at = time.monotonic()
            try:
                response = await _dispatch_canary_call(
                    route.provider, route.native_model_id, request, credential, http_client, token_cache
                )
            except ProviderCallError:
                logger.warning(
                    "drift_canary_provider_call_failed",
                    extra={"model": model, "provider": route.provider, "prompt_id": str(prompt.id)},
                )
                continue
            latency_ms = int((time.monotonic() - started_at) * 1000)

            output_text = response.choices[0].message.content if response.choices else ""
            refusal_detected = detect_refusal(output_text)
            similarity_score = (
                compute_similarity(output_text, baseline.baseline_output_text)
                if baseline is not None
                else None
            )
            # AC5.4.9 (hard NFR) - `compute_cost()` (BYOK/static-registry
            # models) or `compute_self_hosted_cost()` (self-hosted models -
            # Fix 2) only, written ONLY to `canary_runs.cost_usd`.
            # `record_usage_charge()` is NEVER called here - see module
            # docstring "Cost separation".
            if route.provider == "self_hosted":
                assert self_hosted_entry is not None
                cost_usd = compute_self_hosted_cost(
                    self_hosted_entry.cost_basis_per_gpu_hour,
                    wall_clock_latency_seconds=Decimal(latency_ms) / Decimal(1000),
                )
            else:
                cost_usd = compute_cost(
                    model,
                    prompt_tokens=response.usage.prompt_tokens,
                    completion_tokens=response.usage.completion_tokens,
                )
            session.add(
                CanaryRun(
                    model=model,
                    prompt_id=prompt.id,
                    output_text=output_text,
                    latency_ms=latency_ms,
                    refusal_detected=refusal_detected,
                    similarity_score_vs_baseline=similarity_score,
                    cost_usd=cost_usd,
                )
            )
            await session.flush()
            runs_recorded += 1

        for prompt in prompts:
            established = await establish_baseline_if_ready(session, model=model, prompt_id=prompt.id)
            if established:
                baselines_established += 1

        new_alerts = await flag_drift(session, model=model)
        alerts_flagged += len(new_alerts)

        await session.commit()

    return CanarySuiteRunSummary(
        models_tested=len(models_this_tick),
        runs_recorded=runs_recorded,
        baselines_established=baselines_established,
        alerts_flagged=alerts_flagged,
    )
