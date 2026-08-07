"""Integration tests (Phase 5, 5.3 Content-Classification-Aware Routing):
multi-category intersection through the REAL gateway pipeline - the design
doc's own NFR test (section 10: "Content-classification multi-category
blocking is real") and AC5.3.2's exact scenario: category A allows {X, Y},
category B allows {Y, Z}, both enabled and triggered -> only Y is allowed;
a disjoint configuration blocks everything.

Uses `financial_data` + `legal` (both new in Phase 5) rather than `pii`, to
exercise the NEW classifiers end-to-end (not just the pre-existing `pii`
path already covered by `test_content_classification_gateway.py`).
"""

from __future__ import annotations

import asyncpg
import pytest

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio

# Triggers BOTH "financial_data" (currency-near-"wire transfer"/"revenue"
# keyword proximity) and "legal" ("litigation" keyword) simultaneously.
_FINANCIAL_AND_LEGAL_PROMPT = (
    "Please review this wire transfer of $50,000 revenue as part of the ongoing litigation."
)


@pytest.fixture(autouse=True)
async def _clean_tables(migrated_database_url: str):
    async def _truncate() -> None:
        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            await conn.execute(
                "TRUNCATE TABLE content_aware_rules, dlp_policies, dlp_custom_patterns, "
                "team_dlp_action_overrides, dlp_scan_results, sensitivity_label_mappings CASCADE"
            )
        finally:
            await conn.close()

    await _truncate()
    yield
    await _truncate()


async def _make_service_account_secret(client, auth_headers, *, user_id: str, team_id: str) -> str:
    response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "multi-category-test-key", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["secret"]


async def test_multi_category_intersection_allows_only_shared_model(
    client, auth_headers, default_user_id, default_team_id
) -> None:
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    rule_resp = await client.put(
        "/v1/admin/content-aware-rules",
        json={
            "rules": [
                {
                    "category": "financial_data",
                    "enabled": True,
                    "allowed_models": ["gpt-4o", "claude-sonnet-5"],
                },
                {
                    "category": "legal",
                    "enabled": True,
                    "allowed_models": ["claude-sonnet-5", "gpt-4o-mini"],
                },
            ]
        },
        headers=auth_headers,
    )
    assert rule_resp.status_code == 200, rule_resp.text

    denied = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": _FINANCIAL_AND_LEGAL_PROMPT}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert denied.status_code == 403, denied.text
    assert denied.json()["error"]["code"] == "model_denied"

    allowed = await client.post(
        "/v1/chat/completions",
        json={
            "model": "claude-sonnet-5",
            "messages": [{"role": "user", "content": _FINANCIAL_AND_LEGAL_PROMPT}],
        },
        headers={"Authorization": f"Bearer {secret}"},
    )
    # "claude-sonnet-5" is in the intersection {claude-sonnet-5} - the
    # content-classification layer must NOT block it (it fails LATER for an
    # unrelated reason - no provider key configured - proving THIS layer
    # specifically didn't block it, same pattern as the existing 'pii'
    # integration test).
    assert allowed.json()["error"]["code"] == "provider_not_configured"


async def test_multi_category_disjoint_allowed_models_blocks_everything(
    client, auth_headers, default_user_id, default_team_id
) -> None:
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    rule_resp = await client.put(
        "/v1/admin/content-aware-rules",
        json={
            "rules": [
                {"category": "financial_data", "enabled": True, "allowed_models": ["gpt-4o"]},
                {"category": "legal", "enabled": True, "allowed_models": ["claude-sonnet-5"]},
            ]
        },
        headers=auth_headers,
    )
    assert rule_resp.status_code == 200, rule_resp.text

    response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": _FINANCIAL_AND_LEGAL_PROMPT}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"]["code"] == "model_denied"
    assert "content classification" in body["error"]["message"]


async def test_source_code_category_blocks_when_enabled_with_empty_allowlist(
    client, auth_headers, default_user_id, default_team_id
) -> None:
    """AC5.3.4: a previously-inert category (source_code) is now
    functionally enforced, same as pii/financial_data/legal."""
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    rule_resp = await client.put(
        "/v1/admin/content-aware-rules",
        json={"rules": [{"category": "source_code", "enabled": True, "allowed_models": []}]},
        headers=auth_headers,
    )
    assert rule_resp.status_code == 200, rule_resp.text

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": "```python\ndef foo():\n    return 1\n```\nWhat does this function do?",
                }
            ],
        },
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "model_denied"
