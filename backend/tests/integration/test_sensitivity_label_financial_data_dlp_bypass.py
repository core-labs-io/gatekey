"""Security-relevant finding, independently verified by QA (Phase 5 - 5.3),
and FIXED: the `X-Gatekey-Sensitivity-Label` short-circuit (AC5.3.5) used to,
when pre-trusting the `financial_data` category, skip not just that
category's CLASSIFICATION/routing signal but the underlying Presidio scan
for `financial_data` entities ENTIRELY for that request.

`services/dlp.py::scan_texts` now runs the `financial_data` Presidio entity
scan unconditionally whenever `financial_data` content-aware routing is
enabled, regardless of `skip_categories` - only the content-classification-
routing signal (which category `category_findings` attributes the request
to, for `resolve_content_classification`'s model-routing decision) is
short-circuited by a trusted label. See `services/dlp.py::scan_texts`'s and
`api/v1/gateway/common.py::run_dlp_scan`'s updated docstrings for the fixed
behavior.

This test now asserts the spec-intended behavior (design doc's Security
Considerations table: the sensitivity-label header "can never suppress DLP
redaction/block actions") and passes against the fixed implementation.
"""

from __future__ import annotations

import asyncpg
import pytest

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio

_MAPPINGS_URL = "/v1/admin/content-aware-rules/sensitivity-label-mappings"
# A syntactically valid-looking IBAN (matches `services/dlp.py`'s hand-written
# `GATEKEY_IBAN` pattern: 2-letter country code + 2-digit checksum + 11-30
# alphanumeric BBAN chars) embedded in an otherwise benign sentence.
_IBAN_PROMPT = "Please send the funds to IBAN GB29NWBK60161331926819 as discussed."


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
        json={"name": "financial-dlp-bypass-test-key", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["secret"]


async def test_pretrusted_financial_data_label_does_not_bypass_dlp_block_for_real_iban(
    client, auth_headers, default_user_id, default_team_id
) -> None:
    """Asserts the SPEC-INTENDED behavior (design doc's security invariant:
    the sensitivity-label header must never suppress a DLP redact/block
    action). With the org's DLP policy set to BLOCK financial-data findings,
    a real IBAN in the prompt must be blocked REGARDLESS of whether the
    request carries a pre-trusted `financial_data` label - pre-trusting
    only skips the CLASSIFICATION/routing signal for that category, never
    the underlying DLP scan itself."""
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )

    # Org DLP policy: BLOCK on any built-in-detector finding (financial_data
    # findings use this same `default_action`, per `resolve_builtin_action`).
    policy_resp = await client.put(
        "/v1/admin/dlp-policy",
        json={"default_action": "block"},
        headers=auth_headers,
    )
    assert policy_resp.status_code == 200, policy_resp.text

    # Enable financial_data content-aware routing (gates the sync scan path
    # for this category - allow gpt-4o so the classification layer itself
    # never blocks; only the DLP block matters for this test).
    rule_resp = await client.put(
        "/v1/admin/content-aware-rules",
        json={"rules": [{"category": "financial_data", "enabled": True, "allowed_models": ["gpt-4o"]}]},
        headers=auth_headers,
    )
    assert rule_resp.status_code == 200, rule_resp.text

    mapping_resp = await client.post(
        _MAPPINGS_URL,
        json={"external_label": "Purview: Financial", "gatekey_category": "financial_data"},
        headers=auth_headers,
    )
    assert mapping_resp.status_code == 201, mapping_resp.text

    # --- Without the label: the real IBAN is scanned and BLOCKED. ---
    without_label = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": _IBAN_PROMPT}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert without_label.status_code == 403, without_label.text
    assert without_label.json()["error"]["code"] == "dlp_blocked"

    # --- With the pre-trusted financial_data label: the SAME real IBAN
    # must STILL be blocked (spec-intended behavior, now fixed) - the
    # underlying Presidio scan for financial_data entities always runs
    # whenever financial_data content-aware routing is enabled, regardless
    # of the pre-trusted label; only the classification/routing signal is
    # short-circuited by the label.
    with_label = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": _IBAN_PROMPT}]},
        headers={
            "Authorization": f"Bearer {secret}",
            "X-Gatekey-Sensitivity-Label": "Purview: Financial",
        },
    )
    assert with_label.status_code == 403, with_label.text
    assert with_label.json()["error"]["code"] == "dlp_blocked"
