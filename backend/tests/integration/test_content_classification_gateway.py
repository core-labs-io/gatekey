"""Integration test (Phase 3, BD-5/BD-6): PII detected by the DLP scan
actually restricts model routing through the REAL gateway pipeline (`run_
dlp_scan -> check_content_classification`), not just `services.model_
policy.resolve_content_classification` at the unit level (already covered
by `tests/unit/test_model_policy_service.py`).

AC4.1: the raised `ModelDeniedError.blocking_layer == "content_
classification"` - asserted via the error message (the JSON envelope
doesn't expose `blocking_layer` as a separate field - see `errors.py`), the
same "message names the blocking layer" contract `check_model_policy`'s
`"team"`/`"org"` layers already use.

AC4.4: an enabled 'pii' rule with `allowed_models=[]` blocks ALL traffic in
that category - real enforcement, not just a UI warning state.

AC2.9: enabling this rule forces a SYNCHRONOUS DLP scan even though the
org's DLP default action is `log` (which alone would be async/best-effort) -
proven by the block actually firing on the very first (only) request, with
no dependency on a background task having completed.
"""

from __future__ import annotations

import asyncpg
import pytest

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio

_SSN = "234-56-7890"


@pytest.fixture(autouse=True)
async def _clean_tables(migrated_database_url: str):
    async def _truncate() -> None:
        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            await conn.execute(
                "TRUNCATE TABLE content_aware_rules, dlp_policies, dlp_custom_patterns, "
                "team_dlp_action_overrides, dlp_scan_results CASCADE"
            )
        finally:
            await conn.close()

    await _truncate()
    yield
    await _truncate()


async def _make_service_account_secret(client, auth_headers, *, user_id: str, team_id: str) -> str:
    response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "content-aware-test-key", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["secret"]


async def test_pii_finding_with_empty_allowed_models_blocks_all_traffic_in_category(
    client, auth_headers, default_user_id, default_team_id
) -> None:
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    # Org DLP default action is deliberately "log" - AC2.9 requires the
    # content-aware rule to force a synchronous scan regardless.
    dlp_resp = await client.put(
        "/v1/admin/dlp-policy",
        json={"ssn_detector_enabled": True, "default_action": "log"},
        headers=auth_headers,
    )
    assert dlp_resp.status_code == 200, dlp_resp.text

    rule_resp = await client.put(
        "/v1/admin/content-aware-rules",
        json={"rules": [{"category": "pii", "enabled": True, "allowed_models": []}]},
        headers=auth_headers,
    )
    assert rule_resp.status_code == 200, rule_resp.text

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": f"my SSN is {_SSN} today"}],
        },
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"]["code"] == "model_denied"
    assert "content classification" in body["error"]["message"]


async def test_pii_finding_with_model_in_allowlist_is_not_blocked_by_content_layer(
    client, auth_headers, default_user_id, default_team_id
) -> None:
    """AC4.3: content-classification only further restricts - a model
    explicitly allowed for the 'pii' category proceeds past this layer (it
    then fails LATER for an unrelated reason - no provider key configured -
    proving THIS layer specifically didn't block it)."""
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    dlp_resp = await client.put(
        "/v1/admin/dlp-policy",
        json={"ssn_detector_enabled": True, "default_action": "log"},
        headers=auth_headers,
    )
    assert dlp_resp.status_code == 200, dlp_resp.text

    rule_resp = await client.put(
        "/v1/admin/content-aware-rules",
        json={"rules": [{"category": "pii", "enabled": True, "allowed_models": ["gpt-4o"]}]},
        headers=auth_headers,
    )
    assert rule_resp.status_code == 200, rule_resp.text

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": f"my SSN is {_SSN} today"}],
        },
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.json()["error"]["code"] == "provider_not_configured"


async def test_no_pii_finding_never_triggers_content_classification_layer(
    client, auth_headers, default_user_id, default_team_id
) -> None:
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    dlp_resp = await client.put(
        "/v1/admin/dlp-policy",
        json={"ssn_detector_enabled": True, "default_action": "log"},
        headers=auth_headers,
    )
    assert dlp_resp.status_code == 200, dlp_resp.text
    rule_resp = await client.put(
        "/v1/admin/content-aware-rules",
        json={"rules": [{"category": "pii", "enabled": True, "allowed_models": []}]},
        headers=auth_headers,
    )
    assert rule_resp.status_code == 200, rule_resp.text

    response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "no flagged content here"}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    # No PII in the prompt -> content-classification layer never triggers,
    # even though the category's allowlist is empty.
    assert response.json()["error"]["code"] == "provider_not_configured"
