"""Unit tests for Phase 4 (Reliability & Cost Efficiency) gateway-pipeline
wiring - rate limiting, response caching, graceful degradation, and
failover response headers, actually exercised end-to-end through
`POST /v1/chat/completions` (and, for rate limiting/caching, the other two
gateway routes) - see `api.v1.gateway.common`'s "Phase 4" section note for
the pipeline ordering this wiring implements.

Fix 6 (NFR gap): `check_rate_limit()`/`check_response_cache()`/`check_and_
apply_degradation()` now read from `RateLimitCache`/`CachingSettingsCache`/
`DegradationPolicyCache` on `app.state` (warmed - empty, by default - at
real lifespan startup, same as `app.state.model_policy_cache`) instead of a
live DB read, so tests below that need non-default behavior push directly
into the relevant cache (`app.state.rate_limit_cache.set_org_rule(...)`
etc.) rather than monkeypatching a `load_effective_*` DB function - the
exact same "push into the real app.state cache" convention `test_gateway_
chat.py`'s `_deny_gpt_4o_via_denylist()` already established for
`ModelPolicyCache`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from gatekey.api.v1.gateway import common as gateway_common
from gatekey.db.models.rate_limit_rule import RateLimitOnLimit, RateLimitScopeType
from gatekey.db.models.team import TeamPeriodType
from gatekey.providers import openai as openai_mod
from gatekey.schemas.chat import (
    ChatCompletionChoice,
    ChatCompletionResponse,
    ChatCompletionUsage,
    ChatCompletionResponseMessage,
)
from gatekey.services import budget as budget_service
from gatekey.services import degradation as degradation_service
from gatekey.services import rate_limit as rate_limit_service
from gatekey.services.degradation import DegradationPolicySnapshot
from gatekey.services.proxy_keys import ApiKeyCredential
from gatekey.services.rate_limit import RateLimitRuleSnapshot
from gatekey.services.response_cache import TeamCachingSettingsSnapshot
from gatekey.services.team_periods import TeamPeriodInfo

from tests.unit.gateway_test_support import build_authenticated_app

_CHAT_URL = "/v1/chat/completions"
_COMPLETIONS_URL = "/v1/completions"

_UNMETERED_TEAM_PERIOD = TeamPeriodInfo(
    id=uuid.uuid4(), period_type=TeamPeriodType.MONTHLY, current_period_started_at=datetime.now(timezone.utc)
)


async def _fake_credential(session, provider, *, key_provider):  # noqa: ANN001, ARG001
    return ApiKeyCredential(provider=provider, api_key="sk-test")


@pytest.fixture(autouse=True)
def _patch_credential_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _fake_credential)


def _basic_body(model: str = "gpt-4o", *, stream: bool = False) -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "stream": stream,
    }


def _fake_response(native_model_id: str, text: str = "hi") -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="chatcmpl-test",
        created=1_700_000_000,
        model=native_model_id,
        choices=[
            ChatCompletionChoice(
                index=0,
                message=ChatCompletionResponseMessage(role="assistant", content=text),
                finish_reason="stop",
            )
        ],
        usage=ChatCompletionUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
    )


def _make_rate_limit_rule(
    *,
    requests_per_min: int | None,
    on_limit: RateLimitOnLimit = RateLimitOnLimit.REJECT,
    max_queue_wait_seconds: int = 1,
    scope_type: RateLimitScopeType = RateLimitScopeType.ORG_DEFAULT_PER_USER,
) -> RateLimitRuleSnapshot:
    """A `RateLimitCache`-shaped snapshot (Fix 6: the cache holds
    `RateLimitRuleSnapshot`, not a real `RateLimitRule` ORM row) - pushed
    directly into `app.state.rate_limit_cache` by each test below via
    `set_org_rule()`."""
    return RateLimitRuleSnapshot(
        id=uuid.uuid4(),
        scope_type=scope_type,
        requests_per_min=requests_per_min,
        tokens_per_min=None,
        on_limit=on_limit,
        max_queue_wait_seconds=max_queue_wait_seconds,
    )


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_rate_limit_headers_attached_when_configured_and_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    rule = _make_rate_limit_rule(requests_per_min=10)

    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return _fake_response(native_model_id)

    monkeypatch.setattr(openai_mod, "create_chat_completion", _fake_create)

    org_id = uuid.uuid4()
    app = build_authenticated_app(monkeypatch, org_id=org_id)
    with TestClient(app) as client:
        # `app.state.rate_limit_cache` only exists once the real lifespan
        # has run - i.e. after entering this `with` block (mirrors
        # `test_gateway_chat.py`'s `_deny_gpt_4o_via_denylist()` call-site
        # ordering for `app.state.model_policy_cache`).
        app.state.rate_limit_cache.set_org_rule(org_id, rule)
        response = client.post(
            _CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"}
        )
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == "10"
    assert response.headers["X-RateLimit-Remaining"] == "9"
    assert "X-RateLimit-Reset" in response.headers


def test_rate_limit_no_headers_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default harness fakes (no rule configured) - byte-for-byte pre-Phase-4
    behavior: no `X-RateLimit-*` headers at all."""

    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return _fake_response(native_model_id)

    monkeypatch.setattr(openai_mod, "create_chat_completion", _fake_create)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"}
        )
    assert response.status_code == 200
    assert "X-RateLimit-Limit" not in response.headers
    assert "X-RateLimit-Remaining" not in response.headers


def test_rate_limit_immediate_reject_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    rule = _make_rate_limit_rule(requests_per_min=0, on_limit=RateLimitOnLimit.REJECT)

    async def _fake_log_rejection(session, **kwargs):  # noqa: ANN001, ARG001
        return None

    org_id = uuid.uuid4()
    app = build_authenticated_app(monkeypatch, org_id=org_id)
    monkeypatch.setattr(rate_limit_service, "log_rate_limit_rejection", _fake_log_rejection)
    with TestClient(app) as client:
        app.state.rate_limit_cache.set_org_rule(org_id, rule)
        response = client.post(
            _CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"}
        )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limit_exceeded"
    assert response.json()["error"]["hard_limit"] is True
    assert "Retry-After" in response.headers


def test_rate_limit_applies_to_completions_and_embeddings_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4.2 is not chat-only - `/v1/completions` (and `/v1/embeddings`,
    not separately re-tested here to keep this focused) go through the
    same `check_rate_limit()` first-step wiring."""
    rule = _make_rate_limit_rule(requests_per_min=0, on_limit=RateLimitOnLimit.REJECT)

    async def _fake_log_rejection(session, **kwargs):  # noqa: ANN001, ARG001
        return None

    org_id = uuid.uuid4()
    app = build_authenticated_app(monkeypatch, org_id=org_id)
    monkeypatch.setattr(rate_limit_service, "log_rate_limit_rejection", _fake_log_rejection)
    with TestClient(app) as client:
        app.state.rate_limit_cache.set_org_rule(org_id, rule)
        response = client.post(
            _COMPLETIONS_URL,
            json={"model": "gpt-4o", "prompt": "hi"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 429


def test_rate_limit_queue_and_retry_times_out_as_429_soft_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4.2.5: `on_limit=queue_and_retry` polls up to the tripped rule's
    own `max_queue_wait_seconds` before giving up - a rule that can never
    clear within that window (here, `requests_per_min=0` - always full)
    times out as a 429 with `hard_limit=False`/`Retry-After: 0`, not a
    hang. Uses a 1-second `max_queue_wait_seconds` to keep this test fast."""
    rule = _make_rate_limit_rule(
        requests_per_min=0, on_limit=RateLimitOnLimit.QUEUE_RETRY, max_queue_wait_seconds=1
    )

    async def _fake_log_rejection(session, **kwargs):  # noqa: ANN001, ARG001
        return None

    org_id = uuid.uuid4()
    app = build_authenticated_app(monkeypatch, org_id=org_id)
    monkeypatch.setattr(rate_limit_service, "log_rate_limit_rejection", _fake_log_rejection)
    with TestClient(app) as client:
        app.state.rate_limit_cache.set_org_rule(org_id, rule)
        response = client.post(
            _CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"}
        )
    assert response.status_code == 429
    assert response.json()["error"]["hard_limit"] is False
    assert response.headers["Retry-After"] == "0"


def _make_token_rate_limit_rule(
    *, tokens_per_min: int, on_limit: RateLimitOnLimit = RateLimitOnLimit.REJECT
) -> RateLimitRuleSnapshot:
    return RateLimitRuleSnapshot(
        id=uuid.uuid4(),
        scope_type=RateLimitScopeType.ORG_DEFAULT_PER_USER,
        requests_per_min=None,
        tokens_per_min=tokens_per_min,
        on_limit=on_limit,
        max_queue_wait_seconds=1,
    )


def test_tokens_per_min_allows_a_single_request_that_itself_exceeds_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC4.2.4/AC2.4 (Hardening pass item 4): a `tokens_per_min` gate is
    necessarily retrospective - it can only ever act on ALREADY-consumed
    tokens from PRIOR requests, never estimate/pre-charge the current
    request's own not-yet-known usage. `_fake_response()` reports 10 total
    tokens against a limit of 5 - the request is still allowed (never
    blocked by its own future usage)."""
    rule = _make_token_rate_limit_rule(tokens_per_min=5)

    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return _fake_response(native_model_id)

    monkeypatch.setattr(openai_mod, "create_chat_completion", _fake_create)

    org_id = uuid.uuid4()
    app = build_authenticated_app(monkeypatch, org_id=org_id)
    with TestClient(app) as client:
        app.state.rate_limit_cache.set_org_rule(org_id, rule)
        response = client.post(
            _CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"}
        )
    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == "5"
    # A pure pre-request read, before this response's own 10 tokens land.
    assert response.headers["X-RateLimit-Remaining"] == "5"


def test_tokens_per_min_blocks_the_next_request_once_prior_usage_reaches_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of AC4.2.4: once a PRIOR request's real, provider-
    reported usage has pushed the rolling-window total to/over the limit,
    the NEXT request is blocked - proving the post-response `record_token_
    usage()` accounting (wired into `record_usage_charge()`, the shared
    charge choke point) actually feeds back into the live gate."""
    rule = _make_token_rate_limit_rule(tokens_per_min=8)

    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return _fake_response(native_model_id)  # usage: 5 prompt + 5 completion = 10 total

    monkeypatch.setattr(openai_mod, "create_chat_completion", _fake_create)

    async def _fake_log_rejection(session, **kwargs):  # noqa: ANN001, ARG001
        return None

    org_id = uuid.uuid4()
    app = build_authenticated_app(monkeypatch, org_id=org_id)
    monkeypatch.setattr(rate_limit_service, "log_rate_limit_rejection", _fake_log_rejection)
    with TestClient(app) as client:
        app.state.rate_limit_cache.set_org_rule(org_id, rule)

        first = client.post(
            _CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"}
        )
        assert first.status_code == 200  # allowed - see the "never pre-charges" test above

        second = client.post(
            _CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"}
        )
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limit_exceeded"


def test_requests_and_tokens_headers_reflect_whichever_axis_is_closer_to_its_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both axes configured on the same rule: plenty of request headroom
    (100/min) but a much tighter token budget (15/min - `_fake_response()`'s
    10-token usage already burns most of it after one request) - the
    `X-RateLimit-*` headers on a still-ALLOWED request must reflect the axis
    actually closer to tripping (tokens: 5/15 = 33% headroom) rather than
    the requests axis (98/100 = 98% headroom), even though a naive
    raw-`remaining`-count comparison (98 vs. 5) would pick the wrong one."""
    rule = RateLimitRuleSnapshot(
        id=uuid.uuid4(),
        scope_type=RateLimitScopeType.ORG_DEFAULT_PER_USER,
        requests_per_min=100,
        tokens_per_min=15,
        on_limit=RateLimitOnLimit.REJECT,
        max_queue_wait_seconds=1,
    )

    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return _fake_response(native_model_id)

    monkeypatch.setattr(openai_mod, "create_chat_completion", _fake_create)

    org_id = uuid.uuid4()
    app = build_authenticated_app(monkeypatch, org_id=org_id)
    with TestClient(app) as client:
        app.state.rate_limit_cache.set_org_rule(org_id, rule)
        # First request: both axes still fully open - burns 1 request and
        # (post-response) 10 of the 15-token budget.
        client.post(_CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"})

        # Second request: tokens axis has only 5/15 (33%) headroom left,
        # requests axis still has 98/100 (98%) - tokens must win the header
        # selection despite its smaller raw `remaining` number too.
        second = client.post(
            _CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"}
        )
    assert second.status_code == 200
    assert second.headers["X-RateLimit-Limit"] == "15"
    assert second.headers["X-RateLimit-Remaining"] == "5"


# ---------------------------------------------------------------------------
# Response caching
# ---------------------------------------------------------------------------


def test_cache_miss_then_hit_skips_provider_and_charges_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    call_count = 0

    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        nonlocal call_count
        call_count += 1
        return _fake_response(native_model_id, "provider hit")

    monkeypatch.setattr(openai_mod, "create_chat_completion", _fake_create)

    charge_calls: list[Decimal] = []

    async def _fake_record_team_charge(session, **kwargs):  # noqa: ANN001, ARG001
        charge_calls.append(Decimal("0.01"))
        return budget_service.ChargeResult(cost=Decimal("0.01"))

    async def _fake_get_team_membership_budget_state(session, *, team_id, user_id):  # noqa: ANN001, ARG001
        return budget_service.TeamMembershipBudgetState(
            membership_id=uuid.uuid4(),
            team_id=team_id,
            user_id=user_id,
            name="test-user",
            budget_usd=None,
            current_spend_usd=Decimal("0"),
            period=_UNMETERED_TEAM_PERIOD,
        )

    # `team_id=None` (build_authenticated_app's default) always misses per
    # `load_effective_caching_config()`'s own contract - use a real team_id
    # for this test so the cache actually gates on the (faked) config
    # above, not on the team_id=None short-circuit.
    team_id = uuid.uuid4()
    app = build_authenticated_app(monkeypatch)
    monkeypatch.setattr(budget_service, "record_team_membership_usage_charge", _fake_record_team_charge)
    monkeypatch.setattr(
        budget_service, "get_team_membership_budget_state", _fake_get_team_membership_budget_state
    )

    from gatekey.api.deps import GatewayCallerContext, require_gateway_credential

    # A stable identity across both calls below - the cache key includes
    # team_id/user_id (AC4.3's residency/multi-tenant isolation), so a
    # dependency override that minted a FRESH random user_id per request
    # would never be able to hit regardless of whether caching worked.
    org_id = uuid.uuid4()
    user_id = uuid.uuid4()

    def _ctx_with_team():
        return GatewayCallerContext(
            org_id=org_id,
            credential_id=uuid.uuid4(),
            credential_type="service_account",
            user_id=user_id,
            team_id=team_id,
            name="test-service-account",
        )

    app.dependency_overrides[require_gateway_credential] = _ctx_with_team

    with TestClient(app) as client:
        # `TeamCachingSettingsSnapshot(cache_ttl_minutes=5)` -> 300s TTL,
        # matching the old `_fake_load_caching`'s `(True, 300)` return value
        # - see `resolve_effective_caching_config()`'s docstring for the
        # org-absent -> `enabled=True` default this relies on (no org entry
        # seeded here). Only settable once the real lifespan has run - see
        # the rate-limit tests above's identical note.
        app.state.caching_settings_cache.set_team_settings(
            team_id, TeamCachingSettingsSnapshot(cache_enabled=True, cache_ttl_minutes=5)
        )
        first = client.post(
            _CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"}
        )
        assert first.status_code == 200
        assert first.headers["X-Cache"] == "MISS"
        assert call_count == 1
        assert len(charge_calls) == 1

        second = client.post(
            _CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"}
        )
        assert second.status_code == 200
        assert second.headers["X-Cache"] == "HIT"
        assert "X-Cache-TTL" in second.headers
        # Cache hit never re-dispatches to the provider or charges again.
        assert call_count == 1
        assert len(charge_calls) == 1
        assert second.json()["choices"][0]["message"]["content"] == "provider hit"


def test_cache_disabled_by_default_no_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return _fake_response(native_model_id)

    monkeypatch.setattr(openai_mod, "create_chat_completion", _fake_create)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"}
        )
    assert response.status_code == 200
    assert "X-Cache" not in response.headers


# ---------------------------------------------------------------------------
# Graceful degradation (chat completions only)
# ---------------------------------------------------------------------------


def test_degradation_triggers_and_substitutes_model(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = DegradationPolicySnapshot(
        enabled=True,
        threshold_pct_of_budget=Decimal("50"),
        downgrade_target_model="gpt-4o-mini",
    )

    # Near-exhausted metered budget so `_check_budget_proximity` actually
    # triggers (the harness's default budget fake is unmetered, which never
    # degrades - see `_check_budget_proximity`'s own "unmetered - never
    # degrade" branch).
    async def _fake_get_budget_state(session, user_id):  # noqa: ANN001, ARG001
        return budget_service.UserBudgetState(
            id=user_id, name="test-user", budget_usd=Decimal("100"), current_spend_usd=Decimal("99")
        )

    seen_native_model_ids: list[str] = []

    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        seen_native_model_ids.append(native_model_id)
        return _fake_response(native_model_id)

    monkeypatch.setattr(openai_mod, "create_chat_completion", _fake_create)

    app = build_authenticated_app(monkeypatch)
    # `services.degradation` imports `get_budget_state` by name (`from
    # gatekey.services.budget import ... get_budget_state`), so it holds
    # its own independent binding - patching `budget_service.
    # get_budget_state` alone would not affect it; patch both targets.
    monkeypatch.setattr(budget_service, "get_budget_state", _fake_get_budget_state)
    monkeypatch.setattr(degradation_service, "get_budget_state", _fake_get_budget_state)
    with TestClient(app) as client:
        # Default harness context has `team_id=None` - only the org slot of
        # `DegradationPolicyCache` is consulted (see `resolve_effective_
        # degradation_policy()`). Only settable once the real lifespan has
        # run - see the rate-limit tests above's identical note.
        app.state.degradation_policy_cache.set_org_policy(policy)
        response = client.post(
            _CHAT_URL, json=_basic_body("gpt-4o"), headers={"Authorization": "Bearer gk_sk_test"}
        )
    assert response.status_code == 200
    assert response.headers["X-Gatekey-Degraded"] == "true"
    assert response.headers["X-Gatekey-Degraded-From"] == "gpt-4o"
    assert response.headers["X-Gatekey-Degraded-To"] == "gpt-4o-mini"
    assert seen_native_model_ids == ["gpt-4o-mini"]


def test_degradation_not_triggered_no_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default harness fakes (no policy configured) - byte-for-byte
    pre-Phase-4 behavior: headers absent entirely (AC4.4.4)."""

    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return _fake_response(native_model_id)

    monkeypatch.setattr(openai_mod, "create_chat_completion", _fake_create)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"}
        )
    assert response.status_code == 200
    assert "X-Gatekey-Degraded" not in response.headers


# ---------------------------------------------------------------------------
# Failover response headers
# ---------------------------------------------------------------------------


def test_failover_attempt_zero_header_on_primary_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """`build_authenticated_app()`'s `call_provider_with_failover` fake
    always returns `attempt=0`/`used_key_id=None` (the primary succeeded) -
    this asserts that outcome is actually surfaced as a response header,
    not just internally threaded."""

    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        return _fake_response(native_model_id)

    monkeypatch.setattr(openai_mod, "create_chat_completion", _fake_create)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"}
        )
    assert response.status_code == 200
    assert response.headers["X-Failover-Attempt"] == "0"
    assert "X-Failover-Used-Key" not in response.headers
