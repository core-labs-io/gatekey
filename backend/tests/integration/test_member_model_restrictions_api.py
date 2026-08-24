"""Integration tests for the per-team-member model-restriction overlay
(`GET`/`PUT /v1/teams/{team_id}/members/{user_id}/model-restrictions`) - the
third layer below org model policy (`model_policies`) and team model policy
(`team_model_policies`). Mirrors `test_phase2_governance_api.py`'s
`test_team_restriction_with_org_denied_model_is_422_and_writes_nothing`
(the team-layer precedent one level up) for style/fixtures.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import asyncpg
import httpx
import pytest
from fastapi import FastAPI

from gatekey.api.deps import get_custom_model_route_cache, get_provider_http_client
from gatekey.db.models.team_membership import TeamRole
from gatekey.providers.model_registry import MODEL_REGISTRY, ModelCapability
from gatekey.services.custom_models import CustomModelCacheEntry, CustomModelRouteCache

from .conftest import to_asyncpg_dsn
from .phase2_helpers import (  # noqa: F401 - fixtures resolved by name
    _clean_phase2_tables,
    add_membership,
    canned_chat_response,
    fetch_val,
    make_team,
    make_user,
    session_cookie_headers,
    sf,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _clean_model_policies_after(migrated_database_url: str):
    """`_clean_phase2_tables` (imported above, autouse) already truncates
    `model_policies` BEFORE each test in this file, so this file's own
    tests are self-consistent regardless of run order - but two tests here
    (`test_self_service_model_access_view_reflects_member_layer`,
    `test_gateway_request_blocked_by_member_layer_not_team_layer`) call
    `PUT /v1/admin/model-policy` to set up their scenario, and that write
    OUTLIVES this file's own test run (nothing truncates it afterward).
    `test_model_fallback_chains_gateway_wiring.py` (an unrelated file, no
    `_clean_phase2_tables` of its own, no org-policy setup of its own -
    it relies on the default permissive "unconfigured" state) failed when
    run after this file in the full suite for exactly this reason - a real
    test-isolation bug, not a code bug. Mirrors `test_residency_gateway.py`'s
    `_clean_residency_tables`'s identical before-AND-after shape."""

    async def _truncate() -> None:
        conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
        try:
            await conn.execute("TRUNCATE TABLE model_policies CASCADE")
        finally:
            await conn.close()

    yield
    await _truncate()


async def test_lead_narrows_a_members_model_access_within_team_baseline(
    client: httpx.AsyncClient, sf, migrated_database_url: str
) -> None:
    allowed_a, allowed_b = list(MODEL_REGISTRY)[:2]
    lead_id = await make_user(sf, "mmr-lead-1")
    member_id = await make_user(sf, "mmr-member-1")
    team_id = await make_team(sf, "mmr-team-1")
    await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
    await add_membership(sf, team_id, member_id, role=TeamRole.MEMBER)
    lead_cookie = await session_cookie_headers(sf, lead_id)

    team_resp = await client.put(
        f"/v1/teams/{team_id}/model-restrictions",
        json={"models": [allowed_a, allowed_b]},
        headers=lead_cookie,
    )
    assert team_resp.status_code == 200, team_resp.text

    resp = await client.put(
        f"/v1/teams/{team_id}/members/{member_id}/model-restrictions",
        json={"models": [allowed_a]},
        headers=lead_cookie,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["member_restriction"] == [allowed_a]
    assert sorted(body["team_baseline"]) == sorted([allowed_a, allowed_b])

    audit_count = await fetch_val(
        migrated_database_url,
        "SELECT COUNT(*) FROM audit_entries WHERE action = 'team.member_model_restrictions.update'",
    )
    assert int(audit_count) >= 1


async def test_member_restriction_outside_team_baseline_is_422_and_writes_nothing(
    client: httpx.AsyncClient, sf, migrated_database_url: str
) -> None:
    team_allowed, team_denied = list(MODEL_REGISTRY)[:2]
    lead_id = await make_user(sf, "mmr-lead-2")
    member_id = await make_user(sf, "mmr-member-2")
    team_id = await make_team(sf, "mmr-team-2")
    await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
    await add_membership(sf, team_id, member_id, role=TeamRole.MEMBER)
    lead_cookie = await session_cookie_headers(sf, lead_id)

    team_resp = await client.put(
        f"/v1/teams/{team_id}/model-restrictions",
        json={"models": [team_allowed]},
        headers=lead_cookie,
    )
    assert team_resp.status_code == 200, team_resp.text

    rejected = await client.put(
        f"/v1/teams/{team_id}/members/{member_id}/model-restrictions",
        json={"models": [team_allowed, team_denied]},
        headers=lead_cookie,
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "member_model_restricts_team_denied_model"

    row_count = await fetch_val(
        migrated_database_url,
        "SELECT COUNT(*) FROM team_member_model_policies WHERE team_id = $1 AND user_id = $2",
        team_id,
        member_id,
    )
    assert int(row_count) == 0


async def test_member_restriction_for_non_member_is_404(
    client: httpx.AsyncClient, sf
) -> None:
    lead_id = await make_user(sf, "mmr-lead-3")
    non_member_id = await make_user(sf, "mmr-non-member-3")
    team_id = await make_team(sf, "mmr-team-3")
    await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
    lead_cookie = await session_cookie_headers(sf, lead_id)

    resp = await client.put(
        f"/v1/teams/{team_id}/members/{non_member_id}/model-restrictions",
        json={"models": []},
        headers=lead_cookie,
    )
    assert resp.status_code == 404, resp.text


async def test_plain_member_cannot_set_another_members_restriction(
    client: httpx.AsyncClient, sf
) -> None:
    member_id = await make_user(sf, "mmr-member-4")
    other_member_id = await make_user(sf, "mmr-other-4")
    team_id = await make_team(sf, "mmr-team-4")
    await add_membership(sf, team_id, member_id, role=TeamRole.MEMBER)
    await add_membership(sf, team_id, other_member_id, role=TeamRole.MEMBER)
    member_cookie = await session_cookie_headers(sf, member_id)

    resp = await client.put(
        f"/v1/teams/{team_id}/members/{other_member_id}/model-restrictions",
        json={"models": []},
        headers=member_cookie,
    )
    assert resp.status_code == 403, resp.text


async def test_plain_member_can_view_own_restriction_but_not_a_teammates(
    client: httpx.AsyncClient, sf
) -> None:
    lead_id = await make_user(sf, "mmr-lead-5")
    member_id = await make_user(sf, "mmr-member-5")
    other_member_id = await make_user(sf, "mmr-other-5")
    team_id = await make_team(sf, "mmr-team-5")
    await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
    await add_membership(sf, team_id, member_id, role=TeamRole.MEMBER)
    await add_membership(sf, team_id, other_member_id, role=TeamRole.MEMBER)
    member_cookie = await session_cookie_headers(sf, member_id)

    own = await client.get(
        f"/v1/teams/{team_id}/members/{member_id}/model-restrictions", headers=member_cookie
    )
    assert own.status_code == 200, own.text

    teammates = await client.get(
        f"/v1/teams/{team_id}/members/{other_member_id}/model-restrictions",
        headers=member_cookie,
    )
    assert teammates.status_code == 403, teammates.text


async def test_lead_can_view_any_members_restriction(client: httpx.AsyncClient, sf) -> None:
    lead_id = await make_user(sf, "mmr-lead-6")
    member_id = await make_user(sf, "mmr-member-6")
    team_id = await make_team(sf, "mmr-team-6")
    await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
    await add_membership(sf, team_id, member_id, role=TeamRole.MEMBER)
    lead_cookie = await session_cookie_headers(sf, lead_id)

    resp = await client.get(
        f"/v1/teams/{team_id}/members/{member_id}/model-restrictions", headers=lead_cookie
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["member_restriction"] is None  # no restriction row yet


async def test_member_restriction_clears_back_to_team_baseline(
    client: httpx.AsyncClient, sf
) -> None:
    allowed_a, allowed_b = list(MODEL_REGISTRY)[:2]
    lead_id = await make_user(sf, "mmr-lead-7")
    member_id = await make_user(sf, "mmr-member-7")
    team_id = await make_team(sf, "mmr-team-7")
    await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
    await add_membership(sf, team_id, member_id, role=TeamRole.MEMBER)
    lead_cookie = await session_cookie_headers(sf, lead_id)

    await client.put(
        f"/v1/teams/{team_id}/model-restrictions",
        json={"models": [allowed_a, allowed_b]},
        headers=lead_cookie,
    )
    narrowed = await client.put(
        f"/v1/teams/{team_id}/members/{member_id}/model-restrictions",
        json={"models": [allowed_a]},
        headers=lead_cookie,
    )
    assert narrowed.json()["member_restriction"] == [allowed_a]

    cleared = await client.put(
        f"/v1/teams/{team_id}/members/{member_id}/model-restrictions",
        json={"models": [allowed_a, allowed_b]},
        headers=lead_cookie,
    )
    assert cleared.status_code == 200, cleared.text
    assert sorted(cleared.json()["member_restriction"]) == sorted([allowed_a, allowed_b])


async def test_member_restriction_set_to_empty_list_is_a_real_lockout_not_no_restriction(
    client: httpx.AsyncClient, sf
) -> None:
    """QA gap (item 3b): `models: []` (an explicit empty list) must mean
    "this member can use NOTHING" - a real, intentional lockout - and round
    -trip as an empty list (`[]`), NOT `null`, through the GET. `null` is
    reserved for "no restriction row at all" (team baseline applies
    unchanged) - see `test_lead_can_view_any_members_restriction` for that
    contrasting case. Neither the unit suite nor this file previously
    asserted this distinction at the HTTP/DB layer."""
    allowed_a, allowed_b = list(MODEL_REGISTRY)[:2]
    lead_id = await make_user(sf, "mmr-lead-8")
    member_id = await make_user(sf, "mmr-member-8")
    team_id = await make_team(sf, "mmr-team-8")
    await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
    await add_membership(sf, team_id, member_id, role=TeamRole.MEMBER)
    lead_cookie = await session_cookie_headers(sf, lead_id)

    await client.put(
        f"/v1/teams/{team_id}/model-restrictions",
        json={"models": [allowed_a, allowed_b]},
        headers=lead_cookie,
    )
    locked = await client.put(
        f"/v1/teams/{team_id}/members/{member_id}/model-restrictions",
        json={"models": []},
        headers=lead_cookie,
    )
    assert locked.status_code == 200, locked.text
    # Must be an empty LIST, not None/null - that is the whole point of the
    # distinction this test exists to protect.
    assert locked.json()["member_restriction"] == []
    assert locked.json()["member_restriction"] is not None

    fetched = await client.get(
        f"/v1/teams/{team_id}/members/{member_id}/model-restrictions", headers=lead_cookie
    )
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["member_restriction"] == []


async def test_member_restriction_empty_list_blocks_gateway_request_end_to_end(
    app: FastAPI, auth_headers: dict[str, str], sf
) -> None:
    """Real end-to-end proof of item 3b: an EMPTY member restriction (`models:
    []`) blocks a real `/v1/chat/completions` request for EVERY model the
    team itself allows, not just a narrower subset - contrast with
    `test_gateway_request_blocked_by_member_layer_not_team_layer` (existing
    coverage), which only ever excludes one of two models."""
    model_a = "openrouter/openai/gpt-4o-mini"
    model_b = "openrouter/meta/muse-spark-1.2"
    owner_id = await make_user(sf, "mmr-empty-gw-owner")
    team_id = await make_team(sf, "mmr-empty-gw-team")
    await add_membership(sf, team_id, owner_id, role=TeamRole.TEAM_LEAD, budget=None)
    cookie = await session_cookie_headers(sf, owner_id)

    def handler(request: httpx.Request) -> httpx.Response:
        import json as json_module

        requested_model = json_module.loads(request.content)["model"]
        return httpx.Response(200, json=canned_chat_response(requested_model))

    app.dependency_overrides[get_provider_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                put_key = await client.put(
                    "/v1/admin/providers/openrouter/key",
                    json={"api_key": "sk-or-mmr-empty-gw"},
                    headers=auth_headers,
                )
                assert put_key.status_code == 200, put_key.text

                org_resp = await client.put(
                    "/v1/admin/model-policy",
                    json={"mode": "allowlist", "models": [model_a, model_b]},
                    headers=auth_headers,
                )
                assert org_resp.status_code == 200, org_resp.text

                team_resp = await client.put(
                    f"/v1/teams/{team_id}/model-restrictions",
                    json={"models": [model_a, model_b]},
                    headers=cookie,
                )
                assert team_resp.status_code == 200, team_resp.text

                member_resp = await client.put(
                    f"/v1/teams/{team_id}/members/{owner_id}/model-restrictions",
                    json={"models": []},
                    headers=cookie,
                )
                assert member_resp.status_code == 200, member_resp.text
                assert member_resp.json()["member_restriction"] == []

                created = await client.post(
                    "/v1/keys",
                    json={"name": "mmr-empty-gw-key", "team_id": str(team_id)},
                    headers=cookie,
                )
                assert created.status_code == 201, created.text
                secret = created.json()["secret"]

                for model in (model_a, model_b):
                    denied_chat = await client.post(
                        "/v1/chat/completions",
                        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
                        headers={"Authorization": f"Bearer {secret}"},
                    )
                    assert denied_chat.status_code == 403, denied_chat.text
                    assert denied_chat.json()["error"]["code"] == "model_denied"

                # Streaming path must be blocked too - this exact class of
                # gap ("only one of streaming/non-streaming updated") has
                # bitten this codebase before (Model Catalog fallback
                # chains). See `chat.py`'s single, pre-branch
                # `check_model_policy()` call site.
                denied_stream = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": model_a,
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                    },
                    headers={"Authorization": f"Bearer {secret}"},
                )
                assert denied_stream.status_code == 403, denied_stream.text
                assert denied_stream.json()["error"]["code"] == "model_denied"
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)


async def test_member_restriction_survives_removed_membership_soft_delete_field(
    client: httpx.AsyncClient, sf, migrated_database_url: str
) -> None:
    """QA gap (item 4): removing then restoring a team member does NOT clear
    their old member-model-restriction row - `services.teams.remove_team_
    member`/`restore_team_member` never touch `team_member_model_policies`
    (see that table's own docstring: "an orphaned overlay row ... is
    cleaned up automatically as a courtesy the next time set_member_model_
    policy() is called", i.e. NOT on remove/restore themselves). This test
    pins down that this is really what happens today (documenting current
    behavior, not asserting it is the only correct choice) - a team lead who
    removes-then-restores a member without realizing this will find the
    OLD restriction silently back in force with no new action on their
    part. Mirrors `TeamMembership` restore's own "role/budget/spend history
    all untouched" precedent (`restore_team_member`'s docstring), so this is
    plausibly intentional - but it is untested today and worth a QA callout,
    since a team lead's mental model of "remove = clean slate" could easily
    be wrong here."""
    allowed_a, allowed_b = list(MODEL_REGISTRY)[:2]
    lead_id = await make_user(sf, "mmr-lead-9")
    member_id = await make_user(sf, "mmr-member-9")
    team_id = await make_team(sf, "mmr-team-9")
    await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
    await add_membership(sf, team_id, member_id, role=TeamRole.MEMBER)
    lead_cookie = await session_cookie_headers(sf, lead_id)

    await client.put(
        f"/v1/teams/{team_id}/model-restrictions",
        json={"models": [allowed_a, allowed_b]},
        headers=lead_cookie,
    )
    set_resp = await client.put(
        f"/v1/teams/{team_id}/members/{member_id}/model-restrictions",
        json={"models": [allowed_a]},
        headers=lead_cookie,
    )
    assert set_resp.status_code == 200, set_resp.text

    row_before = await fetch_val(
        migrated_database_url,
        "SELECT COUNT(*) FROM team_member_model_policies WHERE team_id = $1 AND user_id = $2",
        team_id,
        member_id,
    )
    assert int(row_before) == 1

    removed = await client.delete(
        f"/v1/teams/{team_id}/members/{member_id}", headers=lead_cookie
    )
    assert removed.status_code == 204, removed.text

    # The overlay row is untouched by removal (no cascade/clear on
    # removed_at) - still present in the DB while the member is removed.
    row_during_removal = await fetch_val(
        migrated_database_url,
        "SELECT COUNT(*) FROM team_member_model_policies WHERE team_id = $1 AND user_id = $2",
        team_id,
        member_id,
    )
    assert int(row_during_removal) == 1

    restored = await client.post(
        f"/v1/teams/{team_id}/members/{member_id}/restore", headers=lead_cookie
    )
    assert restored.status_code == 200, restored.text

    # The OLD restriction is silently back in force after restore, with no
    # new PUT from the team lead - current, documented-in-code behavior.
    after_restore = await client.get(
        f"/v1/teams/{team_id}/members/{member_id}/model-restrictions", headers=lead_cookie
    )
    assert after_restore.status_code == 200, after_restore.text
    assert after_restore.json()["member_restriction"] == [allowed_a]


async def test_get_member_restrictions_for_a_user_never_on_the_team_does_not_404(
    client: httpx.AsyncClient, sf
) -> None:
    """QA gap: the PUT endpoint 404s (`member_not_on_team`,
    `test_member_restriction_for_non_member_is_404`) for a `user_id` that
    was never a member of `team_id`, but the GET endpoint has no equivalent
    membership check - it happily returns 200 with `member_restriction:
    null` for ANY `user_id`, member or not. This is not a cross-team RBAC
    leak (the caller must already hold `team_lead`/`org_admin` on THIS
    team, and the response reveals nothing about the target user beyond the
    team's own baseline the caller can already see) - but it is an
    inconsistency worth flagging: the GET and PUT siblings disagree on
    whether a non-member `user_id` is a 404. Pinning down TODAY's actual
    behavior so a future change to either endpoint is a deliberate,
    reviewed decision, not an accidental regression either way."""
    lead_id = await make_user(sf, "mmr-lead-10")
    never_a_member_id = await make_user(sf, "mmr-never-member-10")
    team_id = await make_team(sf, "mmr-team-10")
    await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
    lead_cookie = await session_cookie_headers(sf, lead_id)

    resp = await client.get(
        f"/v1/teams/{team_id}/members/{never_a_member_id}/model-restrictions",
        headers=lead_cookie,
    )
    # Documents CURRENT behavior (200, not 404) - see docstring above.
    assert resp.status_code == 200, resp.text
    assert resp.json()["member_restriction"] is None


async def test_team_lead_of_one_team_cannot_read_or_write_another_teams_member_restriction(
    client: httpx.AsyncClient, sf
) -> None:
    """RBAC boundary check (this codebase's Phase 2 failure-mode class: "a
    Team Lead seeing/affecting another team"). A team lead of team A holds
    no membership at all on team B - `require_team_role`'s dependency scopes
    its membership lookup to the route's own `team_id` path parameter, so
    this must 403 on both GET and PUT for team B's member-restriction
    endpoints, exactly like it already does for team B's team-level
    endpoint. Not previously exercised for the new member-layer endpoints
    specifically."""
    lead_a_id = await make_user(sf, "mmr-lead-11a")
    team_a = await make_team(sf, "mmr-team-11a")
    await add_membership(sf, team_a, lead_a_id, role=TeamRole.TEAM_LEAD)
    lead_a_cookie = await session_cookie_headers(sf, lead_a_id)

    lead_b_id = await make_user(sf, "mmr-lead-11b")
    member_b_id = await make_user(sf, "mmr-member-11b")
    team_b = await make_team(sf, "mmr-team-11b")
    await add_membership(sf, team_b, lead_b_id, role=TeamRole.TEAM_LEAD)
    await add_membership(sf, team_b, member_b_id, role=TeamRole.MEMBER)

    get_resp = await client.get(
        f"/v1/teams/{team_b}/members/{member_b_id}/model-restrictions",
        headers=lead_a_cookie,
    )
    assert get_resp.status_code == 403, get_resp.text

    put_resp = await client.put(
        f"/v1/teams/{team_b}/members/{member_b_id}/model-restrictions",
        json={"models": []},
        headers=lead_a_cookie,
    )
    assert put_resp.status_code == 403, put_resp.text

    # Confirm team B's own restriction is genuinely untouched by the
    # rejected cross-team write attempt (still the default "no row yet").
    lead_b_cookie = await session_cookie_headers(sf, lead_b_id)
    still_unset = await client.get(
        f"/v1/teams/{team_b}/members/{member_b_id}/model-restrictions",
        headers=lead_b_cookie,
    )
    assert still_unset.status_code == 200, still_unset.text
    assert still_unset.json()["member_restriction"] is None


async def test_team_tightened_after_member_restriction_set_blocks_at_team_layer_end_to_end(
    app: FastAPI, auth_headers: dict[str, str], sf
) -> None:
    """QA gap (item 3a), real end-to-end proof (not just the unit-level
    `resolve_model_access` check): a member restriction is set WIDE (both
    team models), the team lead THEN tightens the team's own restriction to
    exclude one of those models, and - with NO further write to the
    member's own restriction - a live gateway request for the now-team-
    excluded model is blocked at the TEAM layer. The member's stale, wider
    restriction never causes an over-permit, because `resolve_model_access`
    checks the team layer independently and first on every request (see
    `services.model_policy.resolve_model_access`)."""
    model_a = "openrouter/openai/gpt-4o-mini"
    model_b = "openrouter/meta/muse-spark-1.2"
    owner_id = await make_user(sf, "mmr-tighten-gw-owner")
    team_id = await make_team(sf, "mmr-tighten-gw-team")
    await add_membership(sf, team_id, owner_id, role=TeamRole.TEAM_LEAD, budget=None)
    cookie = await session_cookie_headers(sf, owner_id)

    def handler(request: httpx.Request) -> httpx.Response:
        import json as json_module

        requested_model = json_module.loads(request.content)["model"]
        return httpx.Response(200, json=canned_chat_response(requested_model))

    app.dependency_overrides[get_provider_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                put_key = await client.put(
                    "/v1/admin/providers/openrouter/key",
                    json={"api_key": "sk-or-mmr-tighten-gw"},
                    headers=auth_headers,
                )
                assert put_key.status_code == 200, put_key.text

                org_resp = await client.put(
                    "/v1/admin/model-policy",
                    json={"mode": "allowlist", "models": [model_a, model_b]},
                    headers=auth_headers,
                )
                assert org_resp.status_code == 200, org_resp.text

                team_resp = await client.put(
                    f"/v1/teams/{team_id}/model-restrictions",
                    json={"models": [model_a, model_b]},
                    headers=cookie,
                )
                assert team_resp.status_code == 200, team_resp.text

                # Member restriction set WIDE - both models.
                member_resp = await client.put(
                    f"/v1/teams/{team_id}/members/{owner_id}/model-restrictions",
                    json={"models": [model_a, model_b]},
                    headers=cookie,
                )
                assert member_resp.status_code == 200, member_resp.text

                created = await client.post(
                    "/v1/keys",
                    json={"name": "mmr-tighten-gw-key", "team_id": str(team_id)},
                    headers=cookie,
                )
                assert created.status_code == 201, created.text
                secret = created.json()["secret"]

                # Sanity: both models work before the team is tightened.
                ok_a = await client.post(
                    "/v1/chat/completions",
                    json={"model": model_a, "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": f"Bearer {secret}"},
                )
                assert ok_a.status_code == 200, ok_a.text

                # Team lead now tightens the TEAM's own restriction to
                # exclude model_b - the member's own restriction (still
                # listing both) is deliberately NOT re-written here.
                retighten = await client.put(
                    f"/v1/teams/{team_id}/model-restrictions",
                    json={"models": [model_a]},
                    headers=cookie,
                )
                assert retighten.status_code == 200, retighten.text

                # model_b must now be blocked at the TEAM layer - the
                # member's stale, wider restriction must not over-permit.
                denied_b = await client.post(
                    "/v1/chat/completions",
                    json={"model": model_b, "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": f"Bearer {secret}"},
                )
                assert denied_b.status_code == 403, denied_b.text
                assert denied_b.json()["error"]["code"] == "model_denied"

                # model_a still works.
                still_ok_a = await client.post(
                    "/v1/chat/completions",
                    json={"model": model_a, "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": f"Bearer {secret}"},
                )
                assert still_ok_a.status_code == 200, still_ok_a.text
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)


async def test_self_service_model_access_view_reflects_member_layer(
    client: httpx.AsyncClient, auth_headers: dict[str, str], sf
) -> None:
    """`GET /v1/model-access` (the end-user self-service "why is this model
    blocked for me" screen) must show the SAME member-layer narrowing the
    gateway actually enforces - not just org/team - or a member would see
    "allowed" here and then get a real 403 on the same model at `/v1/chat/
    completions`. See `api/v1/model_access.py`'s module docstring for why
    this screen must stay in lockstep with `resolve_model_access()`."""
    model_a, model_b = list(MODEL_REGISTRY)[:2]
    lead_id = await make_user(sf, "mmr-selfview-lead")
    team_id = await make_team(sf, "mmr-selfview-team")
    await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)

    await client.put(
        f"/v1/teams/{team_id}/model-restrictions",
        json={"models": [model_a, model_b]},
        headers=await session_cookie_headers(sf, lead_id),
    )
    await client.put(
        f"/v1/teams/{team_id}/members/{lead_id}/model-restrictions",
        json={"models": [model_a]},
        headers=await session_cookie_headers(sf, lead_id),
    )

    resp = await client.get(
        "/v1/model-access",
        params={"team_id": team_id},
        headers=await session_cookie_headers(sf, lead_id),
    )
    assert resp.status_code == 200, resp.text
    entries = {e["model"]: e for e in resp.json()["models"]}
    assert entries[model_a]["allowed"] is True
    assert entries[model_a]["blocking_layer"] is None
    assert entries[model_b]["allowed"] is False
    assert entries[model_b]["blocking_layer"] == "member"


async def test_gateway_request_blocked_by_member_layer_not_team_layer(
    app: FastAPI, auth_headers: dict[str, str], sf
) -> None:
    """Real end-to-end proof of the third governance layer: a member overlay
    that excludes ONE of the team's two allowed models blocks a real
    `/v1/chat/completions` request for exactly that model (403
    `model_denied`, with the member-layer-specific message naming "your
    team lead" - `blocking_layer` itself is a Python-side attribute on
    `ModelDeniedError`, not a separate JSON field, so the message text is
    the externally-observable proof it was the MEMBER layer, not org/team),
    while the OTHER model - still within both the org baseline and the
    team's own restriction - keeps working normally through the same
    personal key. Mirrors `test_personal_key_gateway_end_to_end_charges_
    membership_counter`'s real-gateway-request pattern (stubbed provider
    transport only)."""
    model_a = "openrouter/openai/gpt-4o-mini"
    model_b = "openrouter/meta/muse-spark-1.2"
    owner_id = await make_user(sf, "mmr-gw-owner")
    team_id = await make_team(sf, "mmr-gw-team")
    # team_lead, not the default member role: this user configures their
    # OWN team- and member-level restrictions below via the same session -
    # a team lead can narrow their own member overlay just like anyone
    # else's, `require_team_role("team_lead")` doesn't exclude self-target.
    await add_membership(sf, team_id, owner_id, role=TeamRole.TEAM_LEAD, budget=None)
    cookie = await session_cookie_headers(sf, owner_id)

    def handler(request: httpx.Request) -> httpx.Response:
        import json as json_module

        requested_model = json_module.loads(request.content)["model"]
        return httpx.Response(200, json=canned_chat_response(requested_model))

    app.dependency_overrides[get_provider_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                put_key = await client.put(
                    "/v1/admin/providers/openrouter/key",
                    json={"api_key": "sk-or-mmr-gw"},
                    headers=auth_headers,
                )
                assert put_key.status_code == 200, put_key.text

                org_resp = await client.put(
                    "/v1/admin/model-policy",
                    json={"mode": "allowlist", "models": [model_a, model_b]},
                    headers=auth_headers,
                )
                assert org_resp.status_code == 200, org_resp.text

                team_resp = await client.put(
                    f"/v1/teams/{team_id}/model-restrictions",
                    json={"models": [model_a, model_b]},
                    headers=cookie,
                )
                assert team_resp.status_code == 200, team_resp.text

                member_resp = await client.put(
                    f"/v1/teams/{team_id}/members/{owner_id}/model-restrictions",
                    json={"models": [model_a]},
                    headers=cookie,
                )
                assert member_resp.status_code == 200, member_resp.text

                created = await client.post(
                    "/v1/keys",
                    json={"name": "mmr-gw-key", "team_id": str(team_id)},
                    headers=cookie,
                )
                assert created.status_code == 201, created.text
                secret = created.json()["secret"]

                allowed_chat = await client.post(
                    "/v1/chat/completions",
                    json={"model": model_a, "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": f"Bearer {secret}"},
                )
                assert allowed_chat.status_code == 200, allowed_chat.text

                denied_chat = await client.post(
                    "/v1/chat/completions",
                    json={"model": model_b, "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": f"Bearer {secret}"},
                )
                assert denied_chat.status_code == 403, denied_chat.text
                denied_body = denied_chat.json()
                assert denied_body["error"]["code"] == "model_denied"
                # Proves it was specifically the MEMBER layer that blocked
                # this (not org/team, both of which allow model_b) - see
                # `errors.ModelDeniedError`'s member-layer message branch.
                assert "team lead" in denied_body["error"]["message"]
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)


def _verified_custom_entry(**overrides) -> CustomModelCacheEntry:
    kwargs = dict(
        id=uuid.uuid4(),
        provider="openai",
        capability=ModelCapability.CHAT,
        native_model_id="mmr-baseline-native-id",
        input_price_per_million_usd=Decimal("2.00"),
        output_price_per_million_usd=Decimal("8.00"),
    )
    kwargs.update(overrides)
    return CustomModelCacheEntry(**kwargs)


async def test_org_baseline_includes_a_custom_model_not_just_static_registry(
    app: FastAPI, client: httpx.AsyncClient, sf, auth_headers: dict[str, str]
) -> None:
    """Regression test for a real bug: `GET /v1/teams/{team_id}/model-
    restrictions`'s `org_baseline` used to enumerate `MODEL_REGISTRY` alone,
    so an org whose ENTIRE allowlist was a custom (or self-hosted/
    OpenRouter-discovered) model got back an EMPTY `org_baseline` - an
    unusable, checkbox-less Team Model Restrictions screen even though the
    org had exactly one model allowed. Reproduced live: an org that had
    enabled a single OpenRouter-discovered custom model via the Model
    Enable Picker showed a completely empty checklist on the Teams page."""
    cache = CustomModelRouteCache()
    cache.set_all({"mmr-verified-custom-model": _verified_custom_entry()})
    app.dependency_overrides[get_custom_model_route_cache] = lambda: cache
    try:
        lead_id = await make_user(sf, "mmr-baseline-lead")
        team_id = await make_team(sf, "mmr-baseline-team")
        await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD)
        lead_cookie = await session_cookie_headers(sf, lead_id)

        policy_resp = await client.put(
            "/v1/admin/model-policy",
            json={"mode": "allowlist", "models": ["mmr-verified-custom-model"]},
            headers=auth_headers,
        )
        assert policy_resp.status_code == 200, policy_resp.text

        resp = await client.get(f"/v1/teams/{team_id}/model-restrictions", headers=lead_cookie)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Before the fix this was `[]` - the checklist a team lead sees
        # would have had nothing to select at all.
        assert body["org_baseline"] == ["mmr-verified-custom-model"]
    finally:
        app.dependency_overrides.pop(get_custom_model_route_cache, None)
