"""AC4.3.6 (phase-4-product-spec.md, and explicitly named in the QA task
brief's priority list): "Cache respects DLP/residency policy from Phase 3
... A cached response is never served across a policy boundary." specifically
verified end-to-end through the real gateway pipeline (not just the
low-level `ResponseCache` class unit tests in
`test_response_cache_service.py`, and not just the single-team case already
covered by `test_gateway_phase4_pipeline.py`'s
`test_cache_miss_then_hit_skips_provider_and_charges_zero`).

Team A's request populates the cache; Team B's IDENTICAL request (same
model/prompt/temperature/etc) must MISS and independently hit the provider -
never replay Team A's cached response.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from gatekey.api.deps import GatewayCallerContext, require_gateway_credential
from gatekey.api.v1.gateway import common as gateway_common
from gatekey.providers import openai as openai_mod
from gatekey.schemas.chat import (
    ChatCompletionChoice,
    ChatCompletionResponse,
    ChatCompletionUsage,
    ChatMessage,
)
from gatekey.services import budget as budget_service
from gatekey.services.response_cache import TeamCachingSettingsSnapshot

from tests.unit.gateway_test_support import build_authenticated_app

_CHAT_URL = "/v1/chat/completions"


async def _fake_credential(session, provider, *, key_provider):  # noqa: ANN001, ARG001
    from gatekey.services.proxy_keys import ApiKeyCredential

    return ApiKeyCredential(provider=provider, api_key="sk-test")


def _fake_response(native_model_id: str, text: str) -> ChatCompletionResponse:
    return ChatCompletionResponse(
        id="chatcmpl-test",
        created=1_700_000_000,
        model=native_model_id,
        choices=[
            ChatCompletionChoice(
                index=0, message=ChatMessage(role="assistant", content=text), finish_reason="stop"
            )
        ],
        usage=ChatCompletionUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
    )


def _basic_body(model: str = "gpt-4o") -> dict:
    return {"model": model, "messages": [{"role": "user", "content": "hello"}], "stream": False}


@pytest.mark.asyncio
async def test_identical_request_from_a_different_team_never_hits_team_as_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _fake_credential)

    call_count = 0

    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        nonlocal call_count
        call_count += 1
        return _fake_response(native_model_id, f"provider-response-{call_count}")

    monkeypatch.setattr(openai_mod, "create_chat_completion", _fake_create)

    async def _fake_record_team_charge(session, **kwargs):  # noqa: ANN001, ARG001
        return budget_service.ChargeResult(cost=budget_service.Decimal("0.01"))

    async def _fake_get_team_membership_budget_state(session, *, team_id, user_id):  # noqa: ANN001, ARG001
        from gatekey.services.team_periods import TeamPeriodInfo
        from gatekey.db.models.team import TeamPeriodType
        from datetime import datetime, timezone

        return budget_service.TeamMembershipBudgetState(
            membership_id=uuid.uuid4(),
            team_id=team_id,
            user_id=user_id,
            name="test-user",
            budget_usd=None,
            current_spend_usd=budget_service.Decimal("0"),
            period=TeamPeriodInfo(
                id=uuid.uuid4(),
                period_type=TeamPeriodType.MONTHLY,
                current_period_started_at=datetime.now(timezone.utc),
            ),
        )

    app = build_authenticated_app(monkeypatch)
    monkeypatch.setattr(budget_service, "record_team_membership_usage_charge", _fake_record_team_charge)
    monkeypatch.setattr(
        budget_service, "get_team_membership_budget_state", _fake_get_team_membership_budget_state
    )

    org_id = uuid.uuid4()
    team_a = uuid.uuid4()
    team_b = uuid.uuid4()
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    def _ctx_team_a():
        return GatewayCallerContext(
            org_id=org_id,
            credential_id=uuid.uuid4(),
            credential_type="service_account",
            user_id=user_a,
            team_id=team_a,
            name="team-a-service-account",
        )

    def _ctx_team_b():
        return GatewayCallerContext(
            org_id=org_id,
            credential_id=uuid.uuid4(),
            credential_type="service_account",
            user_id=user_b,
            team_id=team_b,
            name="team-b-service-account",
        )

    with TestClient(app) as client:
        # Only settable once the real lifespan has run - i.e. after
        # entering this `with` block. Both teams opt in (org settings
        # absent -> `enabled=True` default, see `resolve_effective_
        # caching_config()`'s docstring).
        app.state.caching_settings_cache.set_team_settings(
            team_a, TeamCachingSettingsSnapshot(cache_enabled=True, cache_ttl_minutes=5)
        )
        app.state.caching_settings_cache.set_team_settings(
            team_b, TeamCachingSettingsSnapshot(cache_enabled=True, cache_ttl_minutes=5)
        )
        # Team A populates the cache.
        app.dependency_overrides[require_gateway_credential] = _ctx_team_a
        first = client.post(_CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"})
        assert first.status_code == 200
        assert first.headers["X-Cache"] == "MISS"
        assert call_count == 1

        # Team A repeats the SAME request - legitimate hit for its own cache.
        second = client.post(_CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"})
        assert second.status_code == 200
        assert second.headers["X-Cache"] == "HIT"
        assert call_count == 1

        # Team B sends the IDENTICAL request body - must MISS (never see
        # Team A's cached entry) and independently reach the provider again.
        app.dependency_overrides[require_gateway_credential] = _ctx_team_b
        third = client.post(_CHAT_URL, json=_basic_body(), headers={"Authorization": "Bearer gk_sk_test"})
        assert third.status_code == 200
        assert third.headers["X-Cache"] == "MISS", (
            "AC4.3.6 violation: Team B received a cache hit populated by Team A's request"
        )
        assert call_count == 2, "Team B's request must independently reach the provider, not replay Team A's cache"
        assert third.json()["choices"][0]["message"]["content"] == "provider-response-2"
