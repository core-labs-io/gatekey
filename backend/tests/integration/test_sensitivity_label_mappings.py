"""Integration tests (Phase 5 - Differentiators, 5.3 Content-Classification-
Aware Routing, AC5.3.5/AC5.3.6/AC5.3.8): the `sensitivity_label_mappings`
admin CRUD surface, and the `X-Gatekey-Sensitivity-Label` gateway
short-circuit through the REAL gateway pipeline.

See `gatekey/phase-5-technical-design.md` section 2.4/3.1/3.3.
"""

from __future__ import annotations

import asyncpg
import pytest

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio

_MAPPINGS_URL = "/v1/admin/content-aware-rules/sensitivity-label-mappings"
_BENIGN_PROMPT = "Please summarize our quarterly engineering roadmap for the team."


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
        json={"name": "sensitivity-label-test-key", "user_id": user_id, "team_id": team_id},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["secret"]


# ---------------------------------------------------------------------------
# Admin CRUD
# ---------------------------------------------------------------------------


async def test_create_list_update_delete_sensitivity_label_mapping(client, auth_headers) -> None:
    create_resp = await client.post(
        _MAPPINGS_URL,
        json={"external_label": "Microsoft Purview: Highly Confidential", "gatekey_category": "financial_data"},
        headers=auth_headers,
    )
    assert create_resp.status_code == 201, create_resp.text
    mapping_id = create_resp.json()["id"]
    assert create_resp.json()["gatekey_category"] == "financial_data"

    list_resp = await client.get(_MAPPINGS_URL, headers=auth_headers)
    assert list_resp.status_code == 200, list_resp.text
    assert any(row["id"] == mapping_id for row in list_resp.json())

    update_resp = await client.put(
        f"{_MAPPINGS_URL}/{mapping_id}",
        json={"external_label": "Microsoft Purview: Highly Confidential", "gatekey_category": "legal"},
        headers=auth_headers,
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["gatekey_category"] == "legal"

    delete_resp = await client.delete(f"{_MAPPINGS_URL}/{mapping_id}", headers=auth_headers)
    assert delete_resp.status_code == 204, delete_resp.text

    list_after_delete = await client.get(_MAPPINGS_URL, headers=auth_headers)
    assert all(row["id"] != mapping_id for row in list_after_delete.json())


async def test_create_duplicate_external_label_is_rejected(client, auth_headers) -> None:
    payload = {"external_label": "Google DLP: SSN", "gatekey_category": "pii"}
    first = await client.post(_MAPPINGS_URL, json=payload, headers=auth_headers)
    assert first.status_code == 201, first.text

    second = await client.post(_MAPPINGS_URL, json=payload, headers=auth_headers)
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "sensitivity_label_mapping_conflict"


async def test_create_unknown_category_is_rejected_with_structured_422(client, auth_headers) -> None:
    response = await client.post(
        _MAPPINGS_URL,
        json={"external_label": "Some Label", "gatekey_category": "not_a_real_category"},
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text


async def test_update_unknown_mapping_id_returns_404(client, auth_headers) -> None:
    response = await client.put(
        f"{_MAPPINGS_URL}/00000000-0000-0000-0000-000000000099",
        json={"external_label": "X", "gatekey_category": "pii"},
        headers=auth_headers,
    )
    assert response.status_code == 404, response.text


async def test_delete_unknown_mapping_id_returns_404(client, auth_headers) -> None:
    response = await client.delete(
        f"{_MAPPINGS_URL}/00000000-0000-0000-0000-000000000099", headers=auth_headers
    )
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Gateway short-circuit (AC5.3.5)
# ---------------------------------------------------------------------------


async def test_configured_label_pretrusts_category_without_matching_content(
    client, auth_headers, default_user_id, default_team_id
) -> None:
    """A request carrying a configured `X-Gatekey-Sensitivity-Label` is
    treated as already classified into the mapped category WITHOUT running
    Gatekey's own classifier - even a prompt with no real financial-data
    signal at all is blocked once the category is enabled with an empty
    allowlist, purely because of the pre-trusted label."""
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    mapping_resp = await client.post(
        _MAPPINGS_URL,
        json={"external_label": "Purview: Highly Confidential", "gatekey_category": "financial_data"},
        headers=auth_headers,
    )
    assert mapping_resp.status_code == 201, mapping_resp.text

    rule_resp = await client.put(
        "/v1/admin/content-aware-rules",
        json={"rules": [{"category": "financial_data", "enabled": True, "allowed_models": []}]},
        headers=auth_headers,
    )
    assert rule_resp.status_code == 200, rule_resp.text

    with_label = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": _BENIGN_PROMPT}]},
        headers={
            "Authorization": f"Bearer {secret}",
            "X-Gatekey-Sensitivity-Label": "Purview: Highly Confidential",
        },
    )
    assert with_label.status_code == 403, with_label.text
    assert with_label.json()["error"]["code"] == "model_denied"

    # Same benign prompt, no header at all - Gatekey's own classifier finds
    # nothing financial in it, so the content-classification layer must NOT
    # block (fails later, for an unrelated reason).
    without_label = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": _BENIGN_PROMPT}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert without_label.json()["error"]["code"] == "provider_not_configured"


async def test_unrecognized_label_falls_through_silently_never_a_hard_error(
    client, auth_headers, default_user_id, default_team_id
) -> None:
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    mapping_resp = await client.post(
        _MAPPINGS_URL,
        json={"external_label": "Purview: Highly Confidential", "gatekey_category": "financial_data"},
        headers=auth_headers,
    )
    assert mapping_resp.status_code == 201, mapping_resp.text

    rule_resp = await client.put(
        "/v1/admin/content-aware-rules",
        json={"rules": [{"category": "financial_data", "enabled": True, "allowed_models": []}]},
        headers=auth_headers,
    )
    assert rule_resp.status_code == 200, rule_resp.text

    response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": _BENIGN_PROMPT}]},
        headers={
            "Authorization": f"Bearer {secret}",
            "X-Gatekey-Sensitivity-Label": "A Totally Unrecognized Label Value",
        },
    )
    # No hard error from the unrecognized label - falls through to
    # Gatekey's own classifier, which finds nothing in this benign prompt,
    # so the content-classification layer does not block.
    assert response.status_code != 403
    assert response.json()["error"]["code"] == "provider_not_configured"


async def test_label_only_pretrusts_its_own_mapped_category_not_others(
    client, auth_headers, default_user_id, default_team_id
) -> None:
    """A label pre-trusting "financial_data" must not suppress Gatekey's own
    classifier for an UNRELATED, independently-triggered category ("legal",
    via a real keyword match) - both categories end up in
    `category_findings`, and the (disjoint) "legal" rule alone is enough to
    block."""
    secret = await _make_service_account_secret(
        client, auth_headers, user_id=default_user_id, team_id=default_team_id
    )
    mapping_resp = await client.post(
        _MAPPINGS_URL,
        json={"external_label": "Purview: Highly Confidential", "gatekey_category": "financial_data"},
        headers=auth_headers,
    )
    assert mapping_resp.status_code == 201, mapping_resp.text

    rule_resp = await client.put(
        "/v1/admin/content-aware-rules",
        json={
            "rules": [
                {"category": "financial_data", "enabled": True, "allowed_models": ["gpt-4o"]},
                {"category": "legal", "enabled": True, "allowed_models": []},
            ]
        },
        headers=auth_headers,
    )
    assert rule_resp.status_code == 200, rule_resp.text

    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "This matter involves ongoing litigation."}],
        },
        headers={
            "Authorization": f"Bearer {secret}",
            "X-Gatekey-Sensitivity-Label": "Purview: Highly Confidential",
        },
    )
    # "financial_data" is pre-trusted (allows gpt-4o) but "legal" is
    # independently, genuinely triggered by the real classifier (empty
    # allowlist) - the intersection is empty, so the request is blocked.
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "model_denied"
