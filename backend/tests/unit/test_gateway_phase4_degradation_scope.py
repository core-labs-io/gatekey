"""AC4.4.7 (phase-4-product-spec.md): "Degradation does not apply to
embeddings or completions that are not chat completions ... Embeddings and
non-chat completions still hit the hard budget block at limit." - and the
QA task brief's priority item 5b: "confirm degradation genuinely never
applies to /v1/embeddings or /v1/completions (only /v1/chat/completions) -
check this is actually tested, not just asserted true in a docstring."

`completions.py`/`embeddings.py` each carry a docstring/module-note
asserting they never call `check_and_apply_degradation()` (see
`completions.py`'s "Phase 4" section note), but before this file existed, no
test actually drove a REQUEST through either route with a degradation
policy configured+triggered and asserted the substitution never happens -
`test_gateway_phase4_pipeline.py`'s degradation tests only ever hit
`/v1/chat/completions`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from gatekey.api.v1.gateway import common as gateway_common
from gatekey.providers import openai as openai_mod
from gatekey.schemas.chat import EmbeddingItem, EmbeddingsResponse, EmbeddingsUsage
from gatekey.services import budget as budget_service
from gatekey.services import degradation as degradation_service
from gatekey.services.degradation import DegradationPolicySnapshot
from gatekey.services.proxy_keys import ApiKeyCredential

from tests.unit.gateway_test_support import build_authenticated_app

_COMPLETIONS_URL = "/v1/completions"
_EMBEDDINGS_URL = "/v1/embeddings"


async def _fake_credential(session, provider, *, key_provider):  # noqa: ANN001, ARG001
    return ApiKeyCredential(provider=provider, api_key="sk-test")


@pytest.fixture(autouse=True)
def _patch_credential_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_common, "get_decrypted_provider_credential", _fake_credential)


def _configure_triggerable_degradation_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact same policy + near-exhausted-budget combination
    `test_gateway_phase4_pipeline.py`'s
    `test_degradation_triggers_and_substitutes_model` uses to PROVE
    degradation fires on `/v1/chat/completions` - reused here so a negative
    result on `/v1/completions`/`/v1/embeddings` is a genuine "this route
    never even calls the check" result, not "the policy just never would
    have triggered anyway"."""
    policy = DegradationPolicySnapshot(
        enabled=True, threshold_pct_of_budget=Decimal("50"), downgrade_target_model="gpt-4o-mini"
    )

    async def _fake_load_policy(session, *, org_id, team_id):  # noqa: ANN001, ARG001
        return policy

    async def _fake_get_budget_state(session, user_id):  # noqa: ANN001, ARG001
        return budget_service.UserBudgetState(
            id=user_id, name="test-user", budget_usd=Decimal("100"), current_spend_usd=Decimal("99")
        )

    monkeypatch.setattr(degradation_service, "load_effective_degradation_policy", _fake_load_policy)
    monkeypatch.setattr(budget_service, "get_budget_state", _fake_get_budget_state)
    monkeypatch.setattr(degradation_service, "get_budget_state", _fake_get_budget_state)


def test_completions_never_degrades_even_with_triggerable_policy_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_native_model_ids: list[str] = []

    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        from gatekey.schemas.chat import ChatCompletionUsage, CompletionChoice, CompletionResponse

        seen_native_model_ids.append(native_model_id)
        return CompletionResponse(
            id="cmpl-test",
            created=1_700_000_000,
            model=native_model_id,
            choices=[CompletionChoice(text="once upon a time...", index=0, finish_reason="stop")],
            usage=ChatCompletionUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10),
        )

    monkeypatch.setattr(openai_mod, "create_completion", _fake_create)
    _configure_triggerable_degradation_policy(monkeypatch)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _COMPLETIONS_URL,
            json={"model": "gpt-4o", "prompt": "once upon a time"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 200, response.text
    assert "X-Gatekey-Degraded" not in response.headers
    assert "X-Gatekey-Degraded-From" not in response.headers
    assert "X-Gatekey-Degraded-To" not in response.headers
    # The ORIGINAL model was actually used - not silently substituted.
    assert seen_native_model_ids == ["gpt-4o"]


def test_embeddings_never_degrades_even_with_triggerable_policy_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_native_model_ids: list[str] = []

    async def _fake_create(client, native_model_id, request, credential, *, timeout_seconds=60.0):  # noqa: ANN001, ARG001
        seen_native_model_ids.append(native_model_id)
        return EmbeddingsResponse(
            data=[EmbeddingItem(embedding=[0.1, 0.2, 0.3], index=0)],
            model=native_model_id,
            usage=EmbeddingsUsage(prompt_tokens=3, total_tokens=3),
        )

    monkeypatch.setattr(openai_mod, "create_embeddings", _fake_create)
    _configure_triggerable_degradation_policy(monkeypatch)

    app = build_authenticated_app(monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            _EMBEDDINGS_URL,
            json={"model": "text-embedding-3-small", "input": "hello world"},
            headers={"Authorization": "Bearer gk_sk_test"},
        )
    assert response.status_code == 200, response.text
    assert "X-Gatekey-Degraded" not in response.headers
    assert "X-Gatekey-Degraded-From" not in response.headers
    assert "X-Gatekey-Degraded-To" not in response.headers
    assert seen_native_model_ids == ["text-embedding-3-small"]
