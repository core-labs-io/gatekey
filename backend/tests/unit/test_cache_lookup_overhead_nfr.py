"""AC4.3.4 / phase-4 NFR: "Cache lookup overhead: Under 10ms added latency
for a cache miss. Acceptance test: measure p99 gateway latency with cache
enabled vs. disabled on identical request patterns (no cache warm-up)."

Before this file existed, this NFR was never measured anywhere in the test
suite (grep for "10ms"/"p99"/"overhead" across `tests/` before this file
turns up nothing Phase-4-cache-related) - every existing cache test asserts
CORRECTNESS (hit/miss headers, content) but never timing.

Requires a real Redis (`GATEKEY_TEST_REDIS_URL`) to be a faithful
measurement of the NFR's own "no Redis network call optimized out" intent -
skips cleanly (not silently passes) when unavailable, same convention
`tests/integration/test_phase4_reliability_cost.py` already established.
Uses the real `RedisSharedStateStore` (not `InProcessSharedStateStore`,
which has no network round trip to measure at all) via a
`get_shared_state_store` dependency override on the fully-mocked
`build_authenticated_app()` harness - no database is needed for this
measurement (every DB-touching step besides the caching-config check is
already faked by the harness).

QA FINDING (this test currently FAILS, left in place deliberately, not
xfail'd - see the task's own instruction to leave a genuine-bug-revealing
test red rather than hide it): measured against a real `redis:7` container
reachable over loopback TCP (Docker Desktop on Windows), cache-miss overhead
is consistently ~30-36ms at both p99 and the median - roughly 3-4x the 10ms
budget - across repeated runs, not a one-off outlier. Caveats, honestly
stated: (1) this environment's Redis is Dockerized and reached via Docker
Desktop's Windows networking layer, not a colocated production Redis on the
same host/AZ, so the absolute numbers likely overstate a real deployment's
overhead somewhat; (2) `TestClient`'s own per-request ASGI-transport
overhead is nontrivial (the "disabled" baseline itself runs ~5-17ms per
request, not sub-millisecond), so this measures RELATIVE overhead (enabled
vs. disabled), which is the actual NFR quantity, not absolute cache-lookup
cost in isolation. Even accounting for both caveats, a consistent 3-4x-budget
relative overhead is a real signal worth the architect's attention, not
noise - and more importantly, this NFR had ZERO measurement of any kind
before this file, so "unverified" was the actual prior state regardless of
what the true production number turns out to be.

Fix 6 (NFR gap follow-up - security/QA review): the SUSPECTED root cause
("`check_response_cache()` reads `CachingSettings`/`Team.cache_enabled`
straight from Postgres on every request, with no process-wide warm cache")
has been fixed - `check_rate_limit()`/`check_response_cache()`/`check_and_
apply_degradation()` now read `RateLimitCache`/`CachingSettingsCache`/
`DegradationPolicyCache` off `app.state` (warmed at startup, refreshed on
every admin write - see `main.py`'s lifespan and `api.v1.admin.{rate_
limits,caching_settings,degradation_policy}.py`), a genuine, verified
latency/read-count improvement on the hot path. Re-measured AFTER that fix
with this same test: the overhead is STILL ~36ms at p99 (median ~25ms),
essentially unchanged from the pre-fix number - i.e. the Postgres reads were
NOT actually the dominant cost in this measurement. The real bottleneck this
benchmark surfaces is the Redis network round trip `ResponseCache.get_
entry()`/`.set()` itself pays on every cache lookup (a GET, present on the
"enabled" scenario and absent on the "disabled" baseline) - eliminating the
now-fixed Postgres reads (which this benchmark's own `_fake_load_caching`-
equivalent setup had already isolated away even before Fix 6, by seeding
`CachingSettingsCache` directly rather than exercising a live DB read either
way) could not have moved this specific number, and did not. Reported
honestly rather than declared resolved: AC4.3.4's ~10ms budget remains
unmet in THIS environment (Dockerized Redis over Docker Desktop's Windows
networking layer) even after Fix 6 - closing it further would need either a
colocated/lower-latency Redis (a deployment-topology concern, not a Gatekey
code defect) or eliminating the synchronous Redis round trip from the
critical path entirely (e.g. an async/best-effort lookup), both out of
scope for this fix pass and flagged back to the architect.
"""

from __future__ import annotations

import os
import statistics
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from gatekey.api.deps import GatewayCallerContext, get_shared_state_store, require_gateway_credential
from gatekey.api.v1.gateway import common as gateway_common
from gatekey.providers import openai as openai_mod
from gatekey.schemas.chat import (
    ChatCompletionChoice,
    ChatCompletionResponse,
    ChatCompletionUsage,
    ChatMessage,
)
from gatekey.services import budget as budget_service
from gatekey.services.proxy_keys import ApiKeyCredential
from gatekey.services.shared_state import RedisSharedStateStore

from tests.unit.gateway_test_support import build_authenticated_app

_CHAT_URL = "/v1/chat/completions"
_ITERATIONS = 60
_WARMUP_ITERATIONS = 5  # dropped from percentile calc - excludes TestClient/connection-pool cold start


def _skip_if_no_redis() -> str:
    url = os.environ.get("GATEKEY_TEST_REDIS_URL")
    if not url:
        pytest.skip("Redis not configured (GATEKEY_TEST_REDIS_URL) - cannot faithfully measure "
                     "real network-round-trip cache overhead without it.")
    return url


async def _fake_credential(session, provider, *, key_provider):  # noqa: ANN001, ARG001
    return ApiKeyCredential(provider=provider, api_key="sk-test")


def _fake_response(native_model_id: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="chatcmpl-test",
        created=1_700_000_000,
        model=native_model_id,
        choices=[
            ChatCompletionChoice(
                index=0, message=ChatMessage(role="assistant", content="hi"), finish_reason="stop"
            )
        ],
        usage=ChatCompletionUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
    )


def _p99(samples: list[float]) -> float:
    ordered = sorted(samples)
    idx = max(0, int(round(0.99 * (len(ordered) - 1))))
    return ordered[idx]


def _assert_overhead_under_budget(samples_disabled: list[float], samples_enabled_miss: list[float]) -> None:
    # Drop the first few samples of each series (connection-pool/TestClient
    # cold start) - AC4.3.4 explicitly asks for "no cache warm-up" on the
    # CACHE side (i.e. every request must be a genuine miss, which every
    # iteration here already is via a unique prompt), not "no client/HTTP
    # warm-up", and including cold-start noise on both sides would only
    # make the comparison less stable, not more faithful to the NFR.
    stable_disabled = samples_disabled[_WARMUP_ITERATIONS:]
    stable_enabled = samples_enabled_miss[_WARMUP_ITERATIONS:]
    p99_disabled = _p99(stable_disabled)
    p99_enabled = _p99(stable_enabled)
    median_disabled = statistics.median(stable_disabled)
    median_enabled = statistics.median(stable_enabled)
    overhead_ms = (p99_enabled - p99_disabled) * 1000
    median_overhead_ms = (median_enabled - median_disabled) * 1000
    assert overhead_ms < 10.0, (
        f"AC4.3.4 NFR violated: cache-miss overhead is {overhead_ms:.3f}ms at p99 "
        f"(cache disabled p99={p99_disabled * 1000:.3f}ms, cache enabled-miss "
        f"p99={p99_enabled * 1000:.3f}ms) and {median_overhead_ms:.3f}ms at the median "
        f"(disabled median={median_disabled * 1000:.3f}ms, enabled-miss median="
        f"{median_enabled * 1000:.3f}ms) against a real (Dockerized, loopback TCP) Redis, "
        "exceeding the 10ms budget."
    )


@pytest.mark.asyncio
async def test_cache_miss_lookup_overhead_under_10ms_p99(monkeypatch: pytest.MonkeyPatch) -> None:
    redis_url = _skip_if_no_redis()
    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _fake_credential)

    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return _fake_response(native_model_id)

    monkeypatch.setattr(openai_mod, "create_chat_completion", _fake_create)

    org_id = uuid.uuid4()
    team_id = uuid.uuid4()
    user_id = uuid.uuid4()

    def _ctx():
        return GatewayCallerContext(
            org_id=org_id,
            credential_id=uuid.uuid4(),
            credential_type="service_account",
            user_id=user_id,
            team_id=team_id,
            name="test-service-account",
        )

    async def _fake_get_team_membership_budget_state(session, *, team_id, user_id):  # noqa: ANN001, ARG001
        from datetime import datetime, timezone
        from decimal import Decimal

        from gatekey.db.models.team import TeamPeriodType
        from gatekey.services.team_periods import TeamPeriodInfo

        return budget_service.TeamMembershipBudgetState(
            membership_id=uuid.uuid4(),
            team_id=team_id,
            user_id=user_id,
            name="test-user",
            budget_usd=None,
            current_spend_usd=Decimal("0"),
            period=TeamPeriodInfo(
                id=uuid.uuid4(), period_type=TeamPeriodType.MONTHLY,
                current_period_started_at=datetime.now(timezone.utc),
            ),
        )

    async def _fake_record_team_charge(session, **kwargs):  # noqa: ANN001, ARG001
        from decimal import Decimal

        return budget_service.ChargeResult(cost=Decimal("0.01"))

    monkeypatch.setattr(
        budget_service, "get_team_membership_budget_state", _fake_get_team_membership_budget_state
    )
    monkeypatch.setattr(budget_service, "record_team_membership_usage_charge", _fake_record_team_charge)

    # --- Scenario 1: caching disabled entirely (harness default). ---
    app_disabled = build_authenticated_app(monkeypatch)
    app_disabled.dependency_overrides[require_gateway_credential] = _ctx
    disabled_latencies: list[float] = []
    with TestClient(app_disabled) as client:
        for i in range(_ITERATIONS):
            start = time.perf_counter()
            resp = client.post(
                _CHAT_URL,
                json={"model": "gpt-4o", "messages": [{"role": "user", "content": f"disabled-{i}"}], "stream": False},
                headers={"Authorization": "Bearer gk_sk_test"},
            )
            disabled_latencies.append(time.perf_counter() - start)
            assert resp.status_code == 200
            assert "X-Cache" not in resp.headers

    # --- Scenario 2: caching enabled, every request a guaranteed MISS
    # (unique content per iteration) against a REAL Redis. ---
    real_store = RedisSharedStateStore(redis_url)
    try:
        app_enabled = build_authenticated_app(monkeypatch)
        app_enabled.dependency_overrides[require_gateway_credential] = _ctx
        app_enabled.dependency_overrides[get_shared_state_store] = lambda: real_store

        enabled_miss_latencies: list[float] = []
        with TestClient(app_enabled) as client:
            # Fix 6: `check_response_cache()` now reads `CachingSettingsCache`
            # off `app.state` (only constructed once the real lifespan has
            # run, i.e. inside this `with` block) instead of
            # `load_effective_caching_config()` - seed this request's team
            # directly (org entry absent -> `enabled=True` default).
            from gatekey.services.response_cache import TeamCachingSettingsSnapshot

            app_enabled.state.caching_settings_cache.set_team_settings(
                team_id, TeamCachingSettingsSnapshot(cache_enabled=True, cache_ttl_minutes=5)
            )
            for i in range(_ITERATIONS):
                start = time.perf_counter()
                resp = client.post(
                    _CHAT_URL,
                    json={
                        "model": "gpt-4o",
                        "messages": [{"role": "user", "content": f"enabled-miss-{i}-{uuid.uuid4()}"}],
                        "stream": False,
                    },
                    headers={"Authorization": "Bearer gk_sk_test"},
                )
                enabled_miss_latencies.append(time.perf_counter() - start)
                assert resp.status_code == 200
                assert resp.headers["X-Cache"] == "MISS"
    finally:
        try:
            await real_store.aclose()
        except RuntimeError:
            # Windows/ProactorEventLoop teardown quirk closing the redis
            # connection after TestClient's own loop has already torn
            # itself down - harmless (the process is exiting the `with`
            # block regardless); not a real resource leak in practice for
            # this short-lived test connection. Not related to the actual
            # NFR measurement above, which has already completed by this
            # point.
            pass

    _assert_overhead_under_budget(disabled_latencies, enabled_miss_latencies)
