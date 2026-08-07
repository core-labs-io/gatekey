"""Integration tests for the Phase 4 team-scoped RBAC fix: Team Leads must
be able to configure their OWN team's rate limit rules and degradation
policy (phase-4-product-spec.md section 2/4's "As an Org Admin or Team
Lead..." user stories), and must NOT be able to touch another team's.

Covers:
  - `POST/PUT/DELETE/GET /v1/admin/teams/{team_id}/rate-limit-rules[/{id}]`
  - `GET/PUT /v1/admin/teams/{team_id}/degradation-policy`
  - `GET/PUT /v1/admin/teams/{team_id}/cache-settings` (AC4.3.2/AC4.3.3
    admin-surface gap fix)
  - `GET/PUT /v1/admin/teams/{team_id}/failover-override` (AC4.1.3
    admin-surface gap fix)

Sessions are seeded directly via `services.sessions.create_session`, same
approach as `test_phase2_governance_api.py` - see `phase2_helpers.py`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal

import asyncpg
import httpx
import pytest
import pytest_asyncio

from gatekey.db.models.team_membership import TeamRole

from .conftest import to_asyncpg_dsn
from .phase2_helpers import (  # noqa: F401 - fixtures resolved by name
    _clean_phase2_tables,
    add_membership,
    make_team,
    make_user,
    session_cookie_headers,
    sf,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _truncate_rate_limit_and_degradation_tables(migrated_database_url: str) -> AsyncIterator[None]:
    """`phase2_helpers._clean_phase2_tables` (imported above, autouse)
    already truncates `teams`/`team_memberships`/`users`/`sessions`; this
    additionally clears the two Phase 4 config tables these tests write to,
    for the same reason `test_phase4_reliability_cost.py`'s own truncate
    fixture exists (the partial-unique-index-per-scope constraints)."""
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute("TRUNCATE TABLE rate_limit_rules, degradation_policies CASCADE")
    finally:
        await conn.close()
    yield


class TestTeamRateLimitRulesRbac:
    async def test_team_lead_can_create_and_list_own_teams_rate_limit_rule(
        self, client: httpx.AsyncClient, sf
    ) -> None:
        team_id = await make_team(sf, "lead-owns-this-team")
        lead_id = await make_user(sf, "lead-user")
        await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
        lead_headers = await session_cookie_headers(sf, lead_id)

        create_resp = await client.post(
            f"/v1/admin/teams/{team_id}/rate-limit-rules",
            json={"requests_per_minute": 100, "on_limit": "reject", "max_queue_wait_seconds": 30},
            headers=lead_headers,
        )
        assert create_resp.status_code == 201, create_resp.text
        data = create_resp.json()
        assert data["scope_type"] == "team"
        assert data["scope_team_id"] == str(team_id)
        assert data["requests_per_minute"] == 100

        list_resp = await client.get(f"/v1/admin/teams/{team_id}/rate-limit-rules", headers=lead_headers)
        assert list_resp.status_code == 200, list_resp.text
        rule_ids = [r["id"] for r in list_resp.json()["rules"]]
        assert data["id"] in rule_ids

    async def test_team_lead_can_update_and_delete_own_teams_rate_limit_rule(
        self, client: httpx.AsyncClient, sf
    ) -> None:
        team_id = await make_team(sf, "lead-update-delete-team")
        lead_id = await make_user(sf, "lead-user-2")
        await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
        lead_headers = await session_cookie_headers(sf, lead_id)

        create_resp = await client.post(
            f"/v1/admin/teams/{team_id}/rate-limit-rules",
            json={"requests_per_minute": 50, "on_limit": "reject", "max_queue_wait_seconds": 30},
            headers=lead_headers,
        )
        rule_id = create_resp.json()["id"]

        update_resp = await client.put(
            f"/v1/admin/teams/{team_id}/rate-limit-rules/{rule_id}",
            json={"requests_per_minute": 75, "on_limit": "queue_and_retry", "max_queue_wait_seconds": 45},
            headers=lead_headers,
        )
        assert update_resp.status_code == 200, update_resp.text
        assert update_resp.json()["requests_per_minute"] == 75
        assert update_resp.json()["on_limit"] == "queue_and_retry"

        delete_resp = await client.delete(
            f"/v1/admin/teams/{team_id}/rate-limit-rules/{rule_id}", headers=lead_headers
        )
        assert delete_resp.status_code == 204, delete_resp.text

        list_resp = await client.get(f"/v1/admin/teams/{team_id}/rate-limit-rules", headers=lead_headers)
        assert all(r["id"] != rule_id for r in list_resp.json()["rules"])

    async def test_team_member_cannot_create_rate_limit_rule_but_can_list(
        self, client: httpx.AsyncClient, sf
    ) -> None:
        team_id = await make_team(sf, "member-team")
        member_id = await make_user(sf, "plain-member")
        await add_membership(sf, team_id, member_id, role=TeamRole.MEMBER)
        member_headers = await session_cookie_headers(sf, member_id)

        # Read access: members can view their team's rate limit configuration.
        list_resp = await client.get(f"/v1/admin/teams/{team_id}/rate-limit-rules", headers=member_headers)
        assert list_resp.status_code == 200, list_resp.text

        # Write access: members cannot configure it - only the Team Lead can.
        create_resp = await client.post(
            f"/v1/admin/teams/{team_id}/rate-limit-rules",
            json={"requests_per_minute": 100, "on_limit": "reject", "max_queue_wait_seconds": 30},
            headers=member_headers,
        )
        assert create_resp.status_code == 403, create_resp.text

    async def test_team_lead_cannot_touch_another_teams_rate_limit_rule(
        self, client: httpx.AsyncClient, sf
    ) -> None:
        team_a = await make_team(sf, "team-a-owns-rule")
        team_b = await make_team(sf, "team-b-attacker")
        lead_a_id = await make_user(sf, "lead-of-team-a")
        lead_b_id = await make_user(sf, "lead-of-team-b")
        await add_membership(sf, team_a, lead_a_id, role=TeamRole.TEAM_LEAD)
        await add_membership(sf, team_b, lead_b_id, role=TeamRole.TEAM_LEAD)
        lead_a_headers = await session_cookie_headers(sf, lead_a_id)
        lead_b_headers = await session_cookie_headers(sf, lead_b_id)

        create_resp = await client.post(
            f"/v1/admin/teams/{team_a}/rate-limit-rules",
            json={"requests_per_minute": 100, "on_limit": "reject", "max_queue_wait_seconds": 30},
            headers=lead_a_headers,
        )
        assert create_resp.status_code == 201, create_resp.text
        rule_id = create_resp.json()["id"]

        # Team B's own Lead trying to reach it via Team A's path segment is
        # blocked by RBAC before the ownership check even matters (Team B's
        # lead has no role on team_a at all).
        update_resp = await client.put(
            f"/v1/admin/teams/{team_a}/rate-limit-rules/{rule_id}",
            json={"requests_per_minute": 999, "on_limit": "reject", "max_queue_wait_seconds": 30},
            headers=lead_b_headers,
        )
        assert update_resp.status_code == 403, update_resp.text

        # Team B's lead trying to reach team A's rule via THEIR OWN team_id
        # path segment (the ownership-ID-mismatch case, distinct from the
        # RBAC-membership case above) must 404, not succeed or leak that the
        # rule exists.
        update_resp_2 = await client.put(
            f"/v1/admin/teams/{team_b}/rate-limit-rules/{rule_id}",
            json={"requests_per_minute": 999, "on_limit": "reject", "max_queue_wait_seconds": 30},
            headers=lead_b_headers,
        )
        assert update_resp_2.status_code == 404, update_resp_2.text

        delete_resp = await client.delete(
            f"/v1/admin/teams/{team_b}/rate-limit-rules/{rule_id}", headers=lead_b_headers
        )
        assert delete_resp.status_code == 404, delete_resp.text

        # Confirm team A's rule is untouched.
        list_resp = await client.get(f"/v1/admin/teams/{team_a}/rate-limit-rules", headers=lead_a_headers)
        rules = {r["id"]: r for r in list_resp.json()["rules"]}
        assert rules[rule_id]["requests_per_minute"] == 100

    async def test_org_admin_bypass_still_works_for_team_scoped_rate_limit_routes(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str], sf
    ) -> None:
        team_id = await make_team(sf, "org-admin-bypass-team")
        create_resp = await client.post(
            f"/v1/admin/teams/{team_id}/rate-limit-rules",
            json={"requests_per_minute": 42, "on_limit": "reject", "max_queue_wait_seconds": 30},
            headers=auth_headers,
        )
        assert create_resp.status_code == 201, create_resp.text


class TestTeamDegradationPolicyRbac:
    async def test_team_lead_can_configure_own_teams_degradation_policy(
        self, client: httpx.AsyncClient, sf
    ) -> None:
        team_id = await make_team(sf, "degradation-lead-team")
        lead_id = await make_user(sf, "degradation-lead")
        await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
        lead_headers = await session_cookie_headers(sf, lead_id)

        put_resp = await client.put(
            f"/v1/admin/teams/{team_id}/degradation-policy",
            json={"enabled": True, "threshold_pct_of_budget": 15.0, "downgrade_target_model": "gpt-4o-mini"},
            headers=lead_headers,
        )
        assert put_resp.status_code == 200, put_resp.text
        data = put_resp.json()
        assert data["enabled"] is True
        assert Decimal(str(data["threshold_pct_of_budget"])) == Decimal("15.0")
        assert data["downgrade_target_model"] == "gpt-4o-mini"

        get_resp = await client.get(
            f"/v1/admin/teams/{team_id}/degradation-policy", headers=lead_headers
        )
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["enabled"] is True

    async def test_team_member_can_read_but_not_write_degradation_policy(
        self, client: httpx.AsyncClient, sf
    ) -> None:
        team_id = await make_team(sf, "degradation-member-team")
        member_id = await make_user(sf, "degradation-member")
        await add_membership(sf, team_id, member_id, role=TeamRole.MEMBER)
        member_headers = await session_cookie_headers(sf, member_id)

        get_resp = await client.get(
            f"/v1/admin/teams/{team_id}/degradation-policy", headers=member_headers
        )
        assert get_resp.status_code in (200, 404)

        put_resp = await client.put(
            f"/v1/admin/teams/{team_id}/degradation-policy",
            json={"enabled": True, "threshold_pct_of_budget": 15.0, "downgrade_target_model": "gpt-4o-mini"},
            headers=member_headers,
        )
        assert put_resp.status_code == 403, put_resp.text

    async def test_team_lead_cannot_configure_another_teams_degradation_policy(
        self, client: httpx.AsyncClient, sf
    ) -> None:
        team_a = await make_team(sf, "degradation-team-a")
        team_b = await make_team(sf, "degradation-team-b")
        lead_b_id = await make_user(sf, "degradation-lead-b")
        await add_membership(sf, team_b, lead_b_id, role=TeamRole.TEAM_LEAD)
        lead_b_headers = await session_cookie_headers(sf, lead_b_id)

        put_resp = await client.put(
            f"/v1/admin/teams/{team_a}/degradation-policy",
            json={"enabled": True, "threshold_pct_of_budget": 15.0, "downgrade_target_model": "gpt-4o-mini"},
            headers=lead_b_headers,
        )
        assert put_resp.status_code == 403, put_resp.text

    async def test_org_admin_bypass_still_works_for_team_scoped_degradation_policy(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str], sf
    ) -> None:
        team_id = await make_team(sf, "org-admin-bypass-degradation-team")
        put_resp = await client.put(
            f"/v1/admin/teams/{team_id}/degradation-policy",
            json={"enabled": False, "threshold_pct_of_budget": 20.0, "downgrade_target_model": "gpt-4o-mini"},
            headers=auth_headers,
        )
        assert put_resp.status_code == 200, put_resp.text


class TestTeamCacheSettingsRbac:
    """`GET/PUT /v1/admin/teams/{team_id}/cache-settings` (AC4.3.2/AC4.3.3)."""

    async def test_get_returns_defaults_for_a_freshly_created_team(
        self, client: httpx.AsyncClient, sf
    ) -> None:
        team_id = await make_team(sf, "cache-defaults-team")
        lead_id = await make_user(sf, "cache-defaults-lead")
        await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
        lead_headers = await session_cookie_headers(sf, lead_id)

        get_resp = await client.get(
            f"/v1/admin/teams/{team_id}/cache-settings", headers=lead_headers
        )
        assert get_resp.status_code == 200, get_resp.text
        data = get_resp.json()
        assert data["team_id"] == str(team_id)
        assert data["cache_enabled"] is False
        assert data["cache_ttl_minutes"] == 5

    async def test_team_lead_can_update_own_teams_cache_settings(
        self, client: httpx.AsyncClient, sf
    ) -> None:
        team_id = await make_team(sf, "cache-update-team")
        lead_id = await make_user(sf, "cache-update-lead")
        await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
        lead_headers = await session_cookie_headers(sf, lead_id)

        put_resp = await client.put(
            f"/v1/admin/teams/{team_id}/cache-settings",
            json={"cache_enabled": True, "cache_ttl_minutes": 45},
            headers=lead_headers,
        )
        assert put_resp.status_code == 200, put_resp.text
        data = put_resp.json()
        assert data["cache_enabled"] is True
        assert data["cache_ttl_minutes"] == 45

        get_resp = await client.get(
            f"/v1/admin/teams/{team_id}/cache-settings", headers=lead_headers
        )
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json() == data

    async def test_put_rejects_out_of_bounds_ttl(self, client: httpx.AsyncClient, sf) -> None:
        team_id = await make_team(sf, "cache-bad-ttl-team")
        lead_id = await make_user(sf, "cache-bad-ttl-lead")
        await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
        lead_headers = await session_cookie_headers(sf, lead_id)

        too_high = await client.put(
            f"/v1/admin/teams/{team_id}/cache-settings",
            json={"cache_enabled": True, "cache_ttl_minutes": 1441},
            headers=lead_headers,
        )
        assert too_high.status_code == 400, too_high.text

        too_low = await client.put(
            f"/v1/admin/teams/{team_id}/cache-settings",
            json={"cache_enabled": True, "cache_ttl_minutes": 0},
            headers=lead_headers,
        )
        assert too_low.status_code == 400, too_low.text

    async def test_team_member_can_read_but_not_write_cache_settings(
        self, client: httpx.AsyncClient, sf
    ) -> None:
        team_id = await make_team(sf, "cache-member-team")
        member_id = await make_user(sf, "cache-member")
        await add_membership(sf, team_id, member_id, role=TeamRole.MEMBER)
        member_headers = await session_cookie_headers(sf, member_id)

        get_resp = await client.get(
            f"/v1/admin/teams/{team_id}/cache-settings", headers=member_headers
        )
        assert get_resp.status_code == 200, get_resp.text

        put_resp = await client.put(
            f"/v1/admin/teams/{team_id}/cache-settings",
            json={"cache_enabled": True, "cache_ttl_minutes": 10},
            headers=member_headers,
        )
        assert put_resp.status_code == 403, put_resp.text

    async def test_team_lead_cannot_touch_another_teams_cache_settings(
        self, client: httpx.AsyncClient, sf
    ) -> None:
        team_a = await make_team(sf, "cache-team-a")
        team_b = await make_team(sf, "cache-team-b")
        lead_b_id = await make_user(sf, "cache-lead-of-team-b")
        await add_membership(sf, team_b, lead_b_id, role=TeamRole.TEAM_LEAD)
        lead_b_headers = await session_cookie_headers(sf, lead_b_id)

        put_resp = await client.put(
            f"/v1/admin/teams/{team_a}/cache-settings",
            json={"cache_enabled": True, "cache_ttl_minutes": 10},
            headers=lead_b_headers,
        )
        assert put_resp.status_code == 403, put_resp.text

    async def test_org_admin_bypass_still_works_for_team_scoped_cache_settings(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str], sf
    ) -> None:
        team_id = await make_team(sf, "cache-org-admin-bypass-team")
        put_resp = await client.put(
            f"/v1/admin/teams/{team_id}/cache-settings",
            json={"cache_enabled": True, "cache_ttl_minutes": 20},
            headers=auth_headers,
        )
        assert put_resp.status_code == 200, put_resp.text


class TestTeamFailoverOverrideRbac:
    """`GET/PUT /v1/admin/teams/{team_id}/failover-override` (AC4.1.3)."""

    async def test_get_returns_default_false_when_no_override_row_exists(
        self, client: httpx.AsyncClient, sf
    ) -> None:
        team_id = await make_team(sf, "failover-defaults-team")
        lead_id = await make_user(sf, "failover-defaults-lead")
        await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
        lead_headers = await session_cookie_headers(sf, lead_id)

        get_resp = await client.get(
            f"/v1/admin/teams/{team_id}/failover-override", headers=lead_headers
        )
        assert get_resp.status_code == 200, get_resp.text
        data = get_resp.json()
        assert data["team_id"] == str(team_id)
        assert data["failover_disabled"] is False

    async def test_team_lead_can_narrow_off_and_re_enable_own_teams_failover(
        self, client: httpx.AsyncClient, sf
    ) -> None:
        team_id = await make_team(sf, "failover-update-team")
        lead_id = await make_user(sf, "failover-update-lead")
        await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
        lead_headers = await session_cookie_headers(sf, lead_id)

        put_resp = await client.put(
            f"/v1/admin/teams/{team_id}/failover-override",
            json={"failover_disabled": True},
            headers=lead_headers,
        )
        assert put_resp.status_code == 200, put_resp.text
        assert put_resp.json()["failover_disabled"] is True

        get_resp = await client.get(
            f"/v1/admin/teams/{team_id}/failover-override", headers=lead_headers
        )
        assert get_resp.status_code == 200, get_resp.text
        assert get_resp.json()["failover_disabled"] is True

        # Flip it back off (remove the narrowing) - a second PUT re-uses the
        # same row (upsert), never a second row.
        put_resp_2 = await client.put(
            f"/v1/admin/teams/{team_id}/failover-override",
            json={"failover_disabled": False},
            headers=lead_headers,
        )
        assert put_resp_2.status_code == 200, put_resp_2.text
        assert put_resp_2.json()["failover_disabled"] is False

    async def test_team_member_can_read_but_not_write_failover_override(
        self, client: httpx.AsyncClient, sf
    ) -> None:
        team_id = await make_team(sf, "failover-member-team")
        member_id = await make_user(sf, "failover-member")
        await add_membership(sf, team_id, member_id, role=TeamRole.MEMBER)
        member_headers = await session_cookie_headers(sf, member_id)

        get_resp = await client.get(
            f"/v1/admin/teams/{team_id}/failover-override", headers=member_headers
        )
        assert get_resp.status_code == 200, get_resp.text

        put_resp = await client.put(
            f"/v1/admin/teams/{team_id}/failover-override",
            json={"failover_disabled": True},
            headers=member_headers,
        )
        assert put_resp.status_code == 403, put_resp.text

    async def test_team_lead_cannot_touch_another_teams_failover_override(
        self, client: httpx.AsyncClient, sf
    ) -> None:
        team_a = await make_team(sf, "failover-team-a")
        team_b = await make_team(sf, "failover-team-b")
        lead_b_id = await make_user(sf, "failover-lead-of-team-b")
        await add_membership(sf, team_b, lead_b_id, role=TeamRole.TEAM_LEAD)
        lead_b_headers = await session_cookie_headers(sf, lead_b_id)

        put_resp = await client.put(
            f"/v1/admin/teams/{team_a}/failover-override",
            json={"failover_disabled": True},
            headers=lead_b_headers,
        )
        assert put_resp.status_code == 403, put_resp.text

    async def test_org_admin_bypass_still_works_for_team_scoped_failover_override(
        self, client: httpx.AsyncClient, auth_headers: dict[str, str], sf
    ) -> None:
        team_id = await make_team(sf, "failover-org-admin-bypass-team")
        put_resp = await client.put(
            f"/v1/admin/teams/{team_id}/failover-override",
            json={"failover_disabled": True},
            headers=auth_headers,
        )
        assert put_resp.status_code == 200, put_resp.text
        assert put_resp.json()["failover_disabled"] is True
