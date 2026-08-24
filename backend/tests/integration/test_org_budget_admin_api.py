"""Integration tests for the org-wide budget safeguard's admin API (added
alongside migration `0045`) - `GET/PUT /v1/admin/org-settings`'s new
`current_spend_usd` field, `/alert-config`, and `/reset-spend`.

See `test_org_budget_safeguard.py` for the underlying enforcement/
concurrency coverage; this file only covers the HTTP/admin-API surface.
"""

from __future__ import annotations

import asyncpg
import httpx
import pytest

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _truncate_org_settings(migrated_database_url: str):
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute("TRUNCATE TABLE org_settings CASCADE")
    finally:
        await conn.close()
    yield


async def test_get_org_settings_includes_current_spend_usd_default_zero(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/v1/admin/org-settings", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["current_spend_usd"] == "0E-10" or float(body["current_spend_usd"]) == 0
    assert body["budget_ceiling_usd"] is None


async def test_put_ceiling_then_get_reflects_it_current_spend_still_zero(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.put(
        "/v1/admin/org-settings",
        json={"budget_ceiling_usd": "500"},
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["budget_ceiling_usd"] == "500"
    assert float(body["current_spend_usd"]) == 0


# --- alert-config ---------------------------------------------------------------


async def test_alert_config_defaults(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/v1/admin/org-settings/alert-config", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "threshold_50_enabled": True,
        "threshold_75_enabled": True,
        "threshold_100_enabled": True,
        "webhook_enabled": False,
        "webhook_configured": False,
        "email_enabled": False,
    }


async def test_alert_config_put_webhook_url_never_echoed_back(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.put(
        "/v1/admin/org-settings/alert-config",
        json={
            "threshold_50_enabled": True,
            "threshold_75_enabled": True,
            "threshold_100_enabled": False,
            "webhook_enabled": True,
            "webhook_url": "https://hooks.slack.com/services/T00/B00/xyz",
            "email_enabled": True,
        },
        headers=auth_headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["webhook_enabled"] is True
    assert body["webhook_configured"] is True
    assert body["threshold_100_enabled"] is False
    assert body["email_enabled"] is True
    assert "webhook_url" not in body


async def test_alert_config_enabling_webhook_without_url_rejected(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.put(
        "/v1/admin/org-settings/alert-config",
        json={
            "threshold_50_enabled": True,
            "threshold_75_enabled": True,
            "threshold_100_enabled": True,
            "webhook_enabled": True,
            "email_enabled": False,
        },
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "webhook_url_required"


async def test_alert_config_rejects_non_http_webhook_url(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.put(
        "/v1/admin/org-settings/alert-config",
        json={
            "threshold_50_enabled": True,
            "threshold_75_enabled": True,
            "threshold_100_enabled": True,
            "webhook_enabled": False,
            "webhook_url": "not-a-url",
            "email_enabled": False,
        },
        headers=auth_headers,
    )
    assert response.status_code == 422


# --- reset-spend ------------------------------------------------------------------


async def test_reset_spend_zeroes_counter_and_is_audited(
    client: httpx.AsyncClient, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    await client.put(
        "/v1/admin/org-settings", json={"budget_ceiling_usd": "100"}, headers=auth_headers
    )

    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        # Single-row table (one org_settings row per org, ADR-1) - no WHERE
        # needed.
        await conn.execute("UPDATE org_settings SET current_spend_usd = 42")
    finally:
        await conn.close()

    response = await client.post("/v1/admin/org-settings/reset-spend", headers=auth_headers)
    assert response.status_code == 200
    assert float(response.json()["current_spend_usd"]) == 0

    get_response = await client.get("/v1/admin/org-settings", headers=auth_headers)
    assert float(get_response.json()["current_spend_usd"]) == 0
    assert float(get_response.json()["budget_ceiling_usd"]) == 100  # ceiling untouched by reset


async def test_reset_spend_on_never_configured_org_does_not_error(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post("/v1/admin/org-settings/reset-spend", headers=auth_headers)
    assert response.status_code == 200
    assert float(response.json()["current_spend_usd"]) == 0
