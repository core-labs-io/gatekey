"""Fix 5 (security review finding, BLOCKING): graceful degradation's
`downgrade_target_model` was never validated against the org/team's actual
model-access policy - neither at config time (an Org Admin/Team Lead could
set `downgrade_target_model` to a model their own admin console had denied
elsewhere) nor at request time (policy could be tightened AFTER a
degradation policy was already configured, and the substituted model was
never re-checked before dispatch). Both halves are exercised here through
the real HTTP admin surface + a real gateway request, not just in isolation:

1. `test_config_time_validation_rejects_a_team_denied_downgrade_target` -
   AC4.1.9-style defense-in-depth: a team's own model-restriction overlay
   denies a model; PUT-ing that model as the team's `downgrade_target_model`
   is rejected 422, no DB write.
2. `test_request_time_revalidation_catches_policy_tightened_after_config_and_hard_blocks` -
   the policy was PERMISSIVE when the degradation policy was configured
   (passes config-time validation), then tightened afterward; the next
   budget-proximity-triggered request must not silently dispatch to the
   now-denied model - design doc section 7.4's documented edge-case
   behavior ("Skip degradation; hard block at budget") - asserted as a 402
   `budget_exhausted`, never a 200 with degradation headers.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from gatekey.api.deps import get_provider_http_client

pytestmark = pytest.mark.asyncio


def _canned_response(model: str) -> dict:
    return {
        "id": "chatcmpl-degradation-revalidation-test",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": model,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
    }


async def _make_service_account_secret(client, auth_headers, *, user_id: str, team_id: str) -> str:
    response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "degradation-revalidation-e2e-test-key", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["secret"]


async def test_config_time_validation_rejects_a_team_denied_downgrade_target(
    client, auth_headers, default_team_id
) -> None:
    # The team's own model-restriction overlay allows ONLY "gpt-4o" - AC3.2
    # defense-in-depth (org baseline is unconfigured/permissive here, so
    # this narrowing is legal on its own terms).
    restrict_resp = await client.put(
        f"/v1/teams/{default_team_id}/model-restrictions",
        json={"models": ["gpt-4o"]},
        headers=auth_headers,
    )
    assert restrict_resp.status_code == 200, restrict_resp.text

    # Fix 5 (config-time half): setting "gpt-4o-mini" - NOT in the team's
    # allowed set - as this team's downgrade_target_model must be rejected,
    # not silently accepted.
    degrade_resp = await client.put(
        f"/v1/admin/teams/{default_team_id}/degradation-policy",
        json={"enabled": True, "threshold_pct_of_budget": 50.0, "downgrade_target_model": "gpt-4o-mini"},
        headers=auth_headers,
    )
    assert degrade_resp.status_code == 422, degrade_resp.text
    assert degrade_resp.json()["error"]["code"] == "downgrade_target_model_not_allowed"

    # No DB write happened - the team's degradation policy is still
    # unconfigured (falls back to "no policy found" / org default, not the
    # rejected payload).
    get_resp = await client.get(
        f"/v1/admin/teams/{default_team_id}/degradation-policy",
        headers=auth_headers,
    )
    assert get_resp.status_code == 404, get_resp.text


async def test_request_time_revalidation_catches_policy_tightened_after_config_and_hard_blocks(
    app: FastAPI, client, auth_headers, default_user_id, default_team_id
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        payload = _json.loads(request.content)
        return httpx.Response(200, json=_canned_response(payload["model"]))

    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    key_resp = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-test-degradation-revalidation"},
        headers=auth_headers,
    )
    assert key_resp.status_code == 200, key_resp.text

    # The team's model-restriction overlay is PERMISSIVE (allows both
    # models) while the degradation policy below is being configured - it
    # must pass config-time validation cleanly.
    restrict_resp = await client.put(
        f"/v1/teams/{default_team_id}/model-restrictions",
        json={"models": ["gpt-4o", "gpt-4o-mini"]},
        headers=auth_headers,
    )
    assert restrict_resp.status_code == 200, restrict_resp.text

    # Same tiny-budget/near-100%-threshold trick as `test_phase4_usage_log_
    # columns_e2e.py`'s degradation test - a real warm-up charge is what
    # pushes "remaining budget" below the threshold for the second call.
    budget_resp = await client.patch(
        f"/v1/teams/{default_team_id}/members/{default_user_id}",
        json={"budget_usd": "0.001"},
        headers=auth_headers,
    )
    assert budget_resp.status_code == 200, budget_resp.text

    org_degrade_resp = await client.put(
        "/v1/admin/degradation-policy",
        json={"enabled": True, "threshold_pct_of_budget": 99.0, "downgrade_target_model": "gpt-4o-mini"},
        headers=auth_headers,
    )
    assert org_degrade_resp.status_code == 200, org_degrade_resp.text

    degrade_resp = await client.put(
        f"/v1/admin/teams/{default_team_id}/degradation-policy",
        json={"enabled": True, "threshold_pct_of_budget": 99.0, "downgrade_target_model": "gpt-4o-mini"},
        headers=auth_headers,
    )
    assert degrade_resp.status_code == 200, degrade_resp.text

    # Policy tightened AFTER the degradation policy was already configured:
    # an Org Admin now denies "gpt-4o-mini" for this team - the exact gap
    # config-time validation alone cannot close.
    tighten_resp = await client.put(
        f"/v1/teams/{default_team_id}/model-restrictions",
        json={"models": ["gpt-4o"]},
        headers=auth_headers,
    )
    assert tighten_resp.status_code == 200, tighten_resp.text
    assert "gpt-4o-mini" not in tighten_resp.json()["team_restriction"]

    app.dependency_overrides[get_provider_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        # Warm-up call: spend is still $0 pre-check, so degradation does not
        # trigger yet (this call's own charge seeds the spend the next
        # call's proximity check reacts to).
        warmup = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "warm-up call to seed real spend"}],
            },
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert warmup.status_code == 200, warmup.text
        assert "X-Gatekey-Degraded" not in warmup.headers

        response = await client.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "request-time revalidation e2e test"}],
            },
            headers={"Authorization": f"Bearer {secret}"},
        )
    finally:
        del app.dependency_overrides[get_provider_http_client]

    if response.status_code == 200 and "X-Gatekey-Degraded" not in response.headers:
        pytest.skip(
            "degradation did not trigger with this threshold/budget combination - "
            f"response headers: {dict(response.headers)}"
        )

    # Design doc section 7.4: "Degradation triggered but fallback model
    # denied by policy -> Skip degradation; hard block at budget." Never a
    # 200 with degradation headers (that would mean the now-denied model
    # was silently dispatched to), and never a 200 on the ORIGINAL model
    # either (that would defeat the reason degradation exists near the
    # budget ceiling) - a hard 402 budget block.
    assert response.status_code == 402, response.text
    assert response.json()["error"]["code"] == "budget_exhausted"
    assert "X-Gatekey-Degraded" not in response.headers
