"""Integration tests for the Phase 4 admin surfaces NOT already covered by
`test_phase4_reliability_cost.py` (rate-limits/caching-settings/
degradation-policy/backup-groups/failover-events CRUD already has coverage
there): provider-key health check, cache admin (entries/clear), degradation
events, and the usage-dashboard Phase 4 extension (summary fields + export).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import asyncpg
import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.degradation_event import DegradationEvent
from gatekey.db.models.team import Team
from gatekey.db.models.team_membership import TeamMembership, TeamRole
from gatekey.db.models.user import User

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio


async def _configured_openai_key_id(client: httpx.AsyncClient, auth_headers: dict, database_url: str) -> str:
    response = await client.put(
        "/v1/admin/providers/openai/key",
        json={"api_key": "sk-integration-test-marker"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        row = await conn.fetchrow(
            "SELECT id FROM provider_keys WHERE org_id = $1 AND provider = 'openai'",
            DEFAULT_ORG_ID,
        )
    finally:
        await conn.close()
    assert row is not None
    return str(row["id"])


class TestProviderKeyHealth:
    async def test_health_check_configured_key_returns_ok_or_error(
        self, client: httpx.AsyncClient, auth_headers: dict, migrated_database_url: str
    ) -> None:
        """Provider validators are monkeypatched to always-VALID by
        `conftest.py`'s autouse fixture, so this should report `ok`."""
        key_id = await _configured_openai_key_id(client, auth_headers, migrated_database_url)

        response = await client.post(f"/v1/admin/provider-keys/{key_id}/health", headers=auth_headers)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["status"] in ("ok", "error")
        assert isinstance(data["latency_ms"], int)
        assert data["latency_ms"] >= 0

    async def test_health_check_unknown_key_returns_404(
        self, client: httpx.AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.post(
            f"/v1/admin/provider-keys/{uuid.uuid4()}/health", headers=auth_headers
        )
        assert response.status_code == 404, response.text

    async def test_health_check_requires_admin(self, client: httpx.AsyncClient) -> None:
        response = await client.post(f"/v1/admin/provider-keys/{uuid.uuid4()}/health")
        assert response.status_code in (401, 403)


class TestMultiKeyLabels:
    """Gap 1 (audit finding): a second, distinctly-labeled key for the same
    provider must persist as a separate row, not overwrite the first -
    AC4.1.1/AC4.1.2."""

    async def test_second_labeled_key_persists_as_separate_row_and_is_not_primary(
        self, client: httpx.AsyncClient, auth_headers: dict, migrated_database_url: str
    ) -> None:
        first = await client.put(
            "/v1/admin/providers/openai/key",
            json={"api_key": "sk-primary-key-marker"},
            headers=auth_headers,
        )
        assert first.status_code == 200, first.text

        second = await client.put(
            "/v1/admin/providers/openai/key",
            json={"api_key": "sk-backup-key-marker", "label": "backup-1"},
            headers=auth_headers,
        )
        assert second.status_code == 200, second.text

        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            rows = await conn.fetch(
                "SELECT label, is_primary FROM provider_keys WHERE org_id = $1 AND provider = 'openai' "
                "ORDER BY label",
                DEFAULT_ORG_ID,
            )
        finally:
            await conn.close()

        assert len(rows) == 2
        by_label = {row["label"]: row["is_primary"] for row in rows}
        assert by_label == {"Default": True, "backup-1": False}

    async def test_label_omitted_still_upserts_the_same_default_row(
        self, client: httpx.AsyncClient, auth_headers: dict, migrated_database_url: str
    ) -> None:
        """Backward compatibility: every caller that never sends `label`
        keeps overwriting the single 'Default' row exactly as before this
        field existed."""
        first = await client.put(
            "/v1/admin/providers/openai/key",
            json={"api_key": "sk-first"},
            headers=auth_headers,
        )
        second = await client.put(
            "/v1/admin/providers/openai/key",
            json={"api_key": "sk-second"},
            headers=auth_headers,
        )
        assert first.status_code == 200
        assert second.status_code == 200

        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            count = await conn.fetchval(
                "SELECT count(*) FROM provider_keys WHERE org_id = $1 AND provider = 'openai'",
                DEFAULT_ORG_ID,
            )
        finally:
            await conn.close()
        assert count == 1

    async def test_delete_by_key_id_removes_only_that_key(
        self, client: httpx.AsyncClient, auth_headers: dict, migrated_database_url: str
    ) -> None:
        await client.put(
            "/v1/admin/providers/openai/key",
            json={"api_key": "sk-primary"},
            headers=auth_headers,
        )
        await client.put(
            "/v1/admin/providers/openai/key",
            json={"api_key": "sk-backup", "label": "backup-1"},
            headers=auth_headers,
        )

        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            backup_row = await conn.fetchrow(
                "SELECT id FROM provider_keys WHERE org_id = $1 AND provider = 'openai' AND label = 'backup-1'",
                DEFAULT_ORG_ID,
            )
        finally:
            await conn.close()
        backup_id = str(backup_row["id"])

        delete_response = await client.delete(
            f"/v1/admin/providers/openai/keys/{backup_id}", headers=auth_headers
        )
        assert delete_response.status_code == 204, delete_response.text

        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            remaining = await conn.fetch(
                "SELECT label FROM provider_keys WHERE org_id = $1 AND provider = 'openai'",
                DEFAULT_ORG_ID,
            )
        finally:
            await conn.close()
        assert [row["label"] for row in remaining] == ["Default"]

    async def test_delete_by_key_id_unknown_id_returns_404(
        self, client: httpx.AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.delete(
            f"/v1/admin/providers/openai/keys/{uuid.uuid4()}", headers=auth_headers
        )
        assert response.status_code == 404

    async def test_delete_by_key_id_requires_admin(self, client: httpx.AsyncClient) -> None:
        response = await client.delete(f"/v1/admin/providers/openai/keys/{uuid.uuid4()}")
        assert response.status_code in (401, 403)


class TestProviderKeysList:
    """Gap 2 (audit finding): `GET /v1/admin/provider-keys` - the per-KEY
    list view backing AC4.1.7's admin console screen and the per-key
    "Check now" health button."""

    async def test_list_all_keys_across_providers(
        self, client: httpx.AsyncClient, auth_headers: dict
    ) -> None:
        await client.put(
            "/v1/admin/providers/openai/key",
            json={"api_key": "sk-openai-marker"},
            headers=auth_headers,
        )
        await client.put(
            "/v1/admin/providers/openai/key",
            json={"api_key": "sk-openai-backup-marker", "label": "backup-1"},
            headers=auth_headers,
        )
        await client.put(
            "/v1/admin/providers/anthropic/key",
            json={"api_key": "sk-anthropic-marker"},
            headers=auth_headers,
        )

        response = await client.get("/v1/admin/provider-keys", headers=auth_headers)
        assert response.status_code == 200, response.text
        entries = response.json()
        assert len(entries) == 3

        for entry in entries:
            assert set(entry.keys()) == {
                "id",
                "provider",
                "label",
                "is_primary",
                "backup_group_id",
                "health_status",
                "last_health_check",
                "last_error",
                "availability_24h",
                "failover_enabled",
                "failover_target_id",
            }
            # Hardening pass item 3: default state for a key that has never
            # had failover configured - never absent, never null-vs-false
            # ambiguity.
            assert entry["failover_enabled"] is False
            assert entry["failover_target_id"] is None

        openai_entries = [e for e in entries if e["provider"] == "openai"]
        assert len(openai_entries) == 2
        assert {e["label"] for e in openai_entries} == {"Default", "backup-1"}
        primary_flags = {e["label"]: e["is_primary"] for e in openai_entries}
        assert primary_flags == {"Default": True, "backup-1": False}

    async def test_list_reflects_configured_failover_state_across_a_reload(
        self, client: httpx.AsyncClient, auth_headers: dict
    ) -> None:
        """Hardening pass item 3: before this fix, only `PUT .../failover-
        config`'s own response ever showed `failover_enabled`/`failover_
        target_id` - a page reload (a fresh `GET /v1/admin/provider-keys`)
        lost that visibility entirely. Configure failover via the real PUT
        endpoint, then confirm a SEPARATE, subsequent GET list call reflects
        the same state without depending on the PUT response at all."""
        primary_resp = await client.put(
            "/v1/admin/providers/openai/key",
            json={"api_key": "sk-openai-marker"},
            headers=auth_headers,
        )
        assert primary_resp.status_code == 200, primary_resp.text
        backup_resp = await client.put(
            "/v1/admin/providers/openai/key",
            json={"api_key": "sk-openai-backup-marker", "label": "backup-1"},
            headers=auth_headers,
        )
        assert backup_resp.status_code == 200, backup_resp.text

        list_resp = await client.get("/v1/admin/provider-keys", headers=auth_headers)
        rows = list_resp.json()
        primary_id = next(r["id"] for r in rows if r["is_primary"] is True)
        backup_id = next(r["id"] for r in rows if r["is_primary"] is False)

        config_resp = await client.put(
            f"/v1/admin/provider-keys/{primary_id}/failover-config",
            json={"failover_enabled": True, "failover_target_id": backup_id},
            headers=auth_headers,
        )
        assert config_resp.status_code == 200, config_resp.text

        # A fresh GET, independent of the PUT response above - this is the
        # actual regression this fix closes.
        reload_resp = await client.get("/v1/admin/provider-keys", headers=auth_headers)
        assert reload_resp.status_code == 200, reload_resp.text
        reloaded = {r["id"]: r for r in reload_resp.json()}
        assert reloaded[primary_id]["failover_enabled"] is True
        assert reloaded[primary_id]["failover_target_id"] == backup_id
        # The backup itself was never configured - stays at the default.
        assert reloaded[backup_id]["failover_enabled"] is False
        assert reloaded[backup_id]["failover_target_id"] is None

    async def test_list_filters_by_provider(
        self, client: httpx.AsyncClient, auth_headers: dict
    ) -> None:
        await client.put(
            "/v1/admin/providers/openai/key",
            json={"api_key": "sk-openai-marker"},
            headers=auth_headers,
        )
        await client.put(
            "/v1/admin/providers/anthropic/key",
            json={"api_key": "sk-anthropic-marker"},
            headers=auth_headers,
        )

        response = await client.get(
            "/v1/admin/provider-keys", params={"provider": "anthropic"}, headers=auth_headers
        )
        assert response.status_code == 200, response.text
        entries = response.json()
        assert len(entries) == 1
        assert entries[0]["provider"] == "anthropic"

    async def test_list_never_leaks_secret_material_in_raw_response(
        self, client: httpx.AsyncClient, auth_headers: dict
    ) -> None:
        secret = "sk-super-secret-marker-should-never-leak"
        await client.put(
            "/v1/admin/providers/openai/key",
            json={"api_key": secret},
            headers=auth_headers,
        )

        response = await client.get("/v1/admin/provider-keys", headers=auth_headers)
        assert response.status_code == 200
        for forbidden in ("ciphertext", "nonce", "auth_tag", secret):
            assert forbidden not in response.text

    async def test_list_requires_admin(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/v1/admin/provider-keys")
        assert response.status_code in (401, 403)

    async def test_list_empty_when_nothing_configured(
        self, client: httpx.AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.get("/v1/admin/provider-keys", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == []


class TestCacheAdmin:
    async def test_cache_entries_empty_when_no_shared_state_activity(
        self, client: httpx.AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.get("/v1/admin/cache/entries", headers=auth_headers)
        assert response.status_code == 200, response.text
        assert response.json() == []

    async def test_cache_entries_lists_teaser_metadata_not_full_body(
        self, app, client: httpx.AsyncClient, auth_headers: dict
    ) -> None:
        from gatekey.services.response_cache import ResponseCache

        store = app.state.shared_state_store
        cache = ResponseCache(store)
        team_id = uuid.uuid4()
        user_id = uuid.uuid4()
        await cache.set(
            team_id,
            user_id,
            "openai",
            "gpt-4o",
            "deadbeef" * 8,
            "us",
            {"choices": [{"message": {"content": "super secret prompt reply"}}]},
            ttl_seconds=300,
            input_tokens=10,
            output_tokens=5,
        )

        response = await client.get(
            "/v1/admin/cache/entries", params={"team_id": str(team_id)}, headers=auth_headers
        )
        assert response.status_code == 200, response.text
        entries = response.json()
        assert len(entries) == 1
        entry = entries[0]
        assert entry["team_id"] == str(team_id)
        assert entry["provider"] == "openai"
        assert entry["model"] == "gpt-4o"
        # AC4.3.9: teaser only, never the full cached response body/prompt.
        assert "response_body" not in entry
        assert "super secret prompt reply" not in str(entry)

    async def test_cache_clear_team_scoped_removes_only_that_teams_entries(
        self, app, client: httpx.AsyncClient, auth_headers: dict
    ) -> None:
        from gatekey.services.response_cache import ResponseCache

        store = app.state.shared_state_store
        cache = ResponseCache(store)
        team_a = uuid.uuid4()
        team_b = uuid.uuid4()
        for team_id in (team_a, team_b):
            await cache.set(
                team_id,
                uuid.uuid4(),
                "openai",
                "gpt-4o",
                "cafebabe" * 8,
                "us",
                {"choices": []},
                ttl_seconds=300,
            )

        response = await client.post(
            "/v1/admin/cache/clear", json={"team_id": str(team_a)}, headers=auth_headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["entries_cleared"] == 1

        remaining_a = await client.get(
            "/v1/admin/cache/entries", params={"team_id": str(team_a)}, headers=auth_headers
        )
        assert remaining_a.json() == []
        remaining_b = await client.get(
            "/v1/admin/cache/entries", params={"team_id": str(team_b)}, headers=auth_headers
        )
        assert len(remaining_b.json()) == 1

    async def test_cache_clear_org_wide_requires_org_admin(self, client: httpx.AsyncClient) -> None:
        response = await client.post("/v1/admin/cache/clear", json={})
        assert response.status_code in (401, 403)


class TestDegradationEvents:
    @pytest_asyncio.fixture
    async def sf(self, migrated_database_url: str):
        engine = create_async_engine(migrated_database_url)
        try:
            yield async_sessionmaker(engine, expire_on_commit=False)
        finally:
            await engine.dispose()

    async def test_list_degradation_events_filters_by_team_and_date(
        self, client: httpx.AsyncClient, auth_headers: dict, sf: async_sessionmaker
    ) -> None:
        async with sf() as session:
            user = User(org_id=DEFAULT_ORG_ID, name="degradation-test-user")
            session.add(user)
            await session.flush()
            team = Team(org_id=DEFAULT_ORG_ID, name=f"degradation-team-{uuid.uuid4().hex[:8]}")
            session.add(team)
            await session.flush()
            session.add(TeamMembership(team_id=team.id, user_id=user.id, role=TeamRole.MEMBER))
            event = DegradationEvent(
                team_id=team.id,
                user_id=user.id,
                request_id=None,
                original_model="gpt-4o",
                degraded_model="gpt-4o-mini",
                original_cost=Decimal("1.0000"),
                degraded_cost=Decimal("0.2000"),
            )
            session.add(event)
            await session.commit()
            team_id = team.id

        response = await client.get(
            "/v1/admin/degradation-events", params={"team_id": str(team_id)}, headers=auth_headers
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert len(data) == 1
        assert data[0]["original_model"] == "gpt-4o"
        assert data[0]["degraded_model"] == "gpt-4o-mini"
        assert Decimal(data[0]["cost_saved"]) == Decimal("0.8000")

        # A date range that excludes "now" returns nothing.
        far_future = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
        empty_response = await client.get(
            "/v1/admin/degradation-events",
            params={"team_id": str(team_id), "from": far_future},
            headers=auth_headers,
        )
        assert empty_response.status_code == 200
        assert empty_response.json() == []

    async def test_degradation_events_requires_admin_or_auditor(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/v1/admin/degradation-events")
        assert response.status_code in (401, 403)


class TestUsageDashboardPhase4Extension:
    async def test_summary_includes_phase4_fields(
        self, client: httpx.AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.get("/v1/admin/usage/summary", headers=auth_headers)
        assert response.status_code == 200, response.text
        data = response.json()
        for field in (
            "cache_hit_rate",
            "cache_hits",
            "cache_misses",
            "failover_events_count",
            "degraded_requests_count",
            "cost_saved_caching_usd",
            "cost_saved_degradation_usd",
            "cost_saved_total_usd",
        ):
            assert field in data, f"missing Phase 4 field: {field}"

    async def test_summary_accepts_provider_filter_and_90d_range(
        self, client: httpx.AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.get(
            "/v1/admin/usage/summary",
            params={"range": "90d", "provider": "openai"},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text

    async def test_export_csv_has_descriptive_header_row(
        self, client: httpx.AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.get(
            "/v1/admin/usage/export", params={"format": "csv"}, headers=auth_headers
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/csv")
        lines = response.text.strip().splitlines()
        header = lines[0].split(",")
        assert "cache_hit_rate" in header
        assert "failover_events_count" in header
        assert "cost_saved_total_usd" in header

    async def test_export_json_is_single_object(
        self, client: httpx.AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.get(
            "/v1/admin/usage/export", params={"format": "json"}, headers=auth_headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert isinstance(body, dict)
        assert "cost_saved_total_usd" in body

    async def test_cost_efficiency_report_shortcut_forces_org_wide_30d(
        self, client: httpx.AsyncClient, auth_headers: dict
    ) -> None:
        response = await client.get(
            "/v1/admin/usage/export",
            params={"format": "json", "report": "cost_efficiency", "team_id": str(uuid.uuid4())},
            headers=auth_headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["team_id"] == "all"
