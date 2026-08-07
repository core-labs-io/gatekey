"""BD-21: Phase 2 multi-tenant-governance API integration tests against a
real Postgres - AC1.4 (break-glass regression + session RBAC), AC3.2
(org-denied model rejected server-side), AC6.4 (one pending join request),
AC6.7 (atomic approval), personal `gk_pk_` keys end-to-end (incl. ADR-4),
session auth, and the section-7 audit-write convention.

Sessions are seeded directly via `services.sessions.create_session` (the SSO
callback needs a live IdP; the session layer is the trust boundary every
route actually depends on) - see `phase2_helpers.session_cookie_headers`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from gatekey.api.deps import get_provider_http_client
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.personal_api_key import PersonalApiKey
from gatekey.db.models.team_membership import TeamRole
from gatekey.db.models.user import UserOrgRole
from gatekey.providers.model_registry import MODEL_REGISTRY
from gatekey.services.join_requests import submit_join_request
from gatekey.services.personal_keys import generate_personal_key_secret, hash_secret
from gatekey.services.sessions import create_session, revoke_session

from .phase2_helpers import (  # noqa: F401 - fixtures resolved by name
    _clean_phase2_tables,
    add_membership,
    canned_chat_response,
    fetch_row,
    fetch_val,
    make_team,
    make_user,
    session_cookie_headers,
    sf,
)

pytestmark = pytest.mark.asyncio


# --- AC1.4 / RBAC boundary regression ----------------------------------------


async def test_break_glass_token_still_authenticates_admin_routes(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """AC1.4: GATEKEY_ADMIN_TOKEN keeps working end-to-end as a secondary
    path - a mutation, not just a read."""
    response = await client.post(
        "/v1/admin/users", json={"name": "bg-created"}, headers=auth_headers
    )
    assert response.status_code == 201, response.text
    response = await client.get("/v1/admin/users", headers=auth_headers)
    assert response.status_code == 200


async def test_break_glass_token_drives_phase2_admin_surfaces(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    sf,
    migrated_database_url: str,
) -> None:
    """Locked decision #1 / A4: an operator who never configures SSO can use
    every Phase 2 surface via GATEKEY_ADMIN_TOKEN alone - require_role,
    the org_role-branching listing routes, and require_team_role's
    org-admin bypass - with mutations audited as "system:admin_token"."""
    # require_role(org_admin): team creation, bearer only.
    response = await client.post("/v1/teams", json={"name": "bg-team"}, headers=auth_headers)
    assert response.status_code == 201, response.text
    team_id = response.json()["id"]

    # org_role-branching handler route: sees all teams like an org_admin.
    listing = await client.get("/v1/teams", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    assert any(team["id"] == team_id for team in listing.json())
    detail = await client.get(f"/v1/teams/{team_id}", headers=auth_headers)
    assert detail.status_code == 200, detail.text

    # require_team_role(team_lead) via the org-admin bypass: member add.
    member_id = await make_user(sf, "bg-added-member")
    response = await client.post(
        f"/v1/teams/{team_id}/members",
        json={"user_id": str(member_id), "role": "member", "budget_usd": "10"},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text

    # A4: the mutation is in the audit trail with the sentinel actor.
    row = await fetch_row(
        migrated_database_url,
        "SELECT actor_user_id, actor_label FROM audit_entries WHERE action = 'team.member.add'",
    )
    assert row is not None
    assert row["actor_user_id"] is None
    assert row["actor_label"] == "system:admin_token"

    # Personal-scope routes stay cookie-only - a token is not a person.
    assert (await client.get("/v1/keys", headers=auth_headers)).status_code == 401
    assert (await client.get("/v1/auth/me", headers=auth_headers)).status_code == 401


async def test_org_admin_session_cookie_authenticates_same_admin_route(
    client: httpx.AsyncClient, sf
) -> None:
    admin_id = await make_user(sf, "the-org-admin", org_role=UserOrgRole.ORG_ADMIN)
    cookie = await session_cookie_headers(sf, admin_id)
    response = await client.get("/v1/admin/users", headers=cookie)
    assert response.status_code == 200, response.text
    response = await client.post(
        "/v1/admin/users", json={"name": "sess-created"}, headers=cookie
    )
    assert response.status_code == 201, response.text


async def test_member_session_rejected_on_admin_surfaces(
    client: httpx.AsyncClient, sf
) -> None:
    """A plain member (org_role NULL, even a team_lead) never reaches admin
    surfaces: 401 on the break-glass-OR-org_admin dependency, 403 on the
    session-role-only dependencies."""
    member_id = await make_user(sf, "plain-member")
    team_id = await make_team(sf, "m-team")
    await add_membership(sf, team_id, member_id, role=TeamRole.TEAM_LEAD, budget=None)
    cookie = await session_cookie_headers(sf, member_id)

    assert (await client.get("/v1/admin/users", headers=cookie)).status_code == 401
    assert (await client.get("/v1/admin/keys", headers=cookie)).status_code == 403
    assert (await client.get("/v1/admin/audit-entries", headers=cookie)).status_code == 403
    response = await client.post(
        "/v1/teams", json={"name": "nope"}, headers=cookie
    )
    assert response.status_code == 403  # team_lead is not an org-wide privilege


async def test_auditor_reads_but_cannot_mutate(client: httpx.AsyncClient, sf) -> None:
    auditor_id = await make_user(sf, "the-auditor", org_role=UserOrgRole.AUDITOR)
    cookie = await session_cookie_headers(sf, auditor_id)

    response = await client.get("/v1/admin/audit-entries", headers=cookie)
    assert response.status_code == 200, response.text
    assert (await client.get("/v1/teams", headers=cookie)).status_code == 200

    # Mutations: org-admin-only surfaces reject the auditor.
    assert (
        await client.post("/v1/teams", json={"name": "aud-team"}, headers=cookie)
    ).status_code == 403
    assert (
        await client.delete(f"/v1/admin/keys/{uuid.uuid4()}", headers=cookie)
    ).status_code == 403
    assert (await client.get("/v1/admin/users", headers=cookie)).status_code == 401


# --- AC6.4: one pending join request per user --------------------------------


async def test_second_pending_join_request_is_409_until_resolved(
    client: httpx.AsyncClient, sf
) -> None:
    user_id = await make_user(sf, "joiner")
    team_a = await make_team(sf, "team-a")
    team_b = await make_team(sf, "team-b")
    cookie = await session_cookie_headers(sf, user_id)

    first = await client.post(
        "/v1/onboarding/join-requests",
        json={"full_name": "Joiner One", "team_id": str(team_a)},
        headers=cookie,
    )
    assert first.status_code == 201, first.text
    request_id = first.json()["id"]

    # Second pending request - even for a DIFFERENT team - hits the partial
    # unique index and maps to a clean 409.
    second = await client.post(
        "/v1/onboarding/join-requests",
        json={"full_name": "Joiner One", "team_id": str(team_b)},
        headers=cookie,
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "join_request_already_pending"

    # Resolve (reject via org-admin bypass), then a new submission unblocks
    # immediately (AC6.4's second clause).
    admin_id = await make_user(sf, "jr-admin", org_role=UserOrgRole.ORG_ADMIN)
    admin_cookie = await session_cookie_headers(sf, admin_id)
    rejected = await client.post(
        f"/v1/teams/{team_a}/join-requests/{request_id}/reject",
        json={"reason": "not now"},
        headers=admin_cookie,
    )
    assert rejected.status_code == 200, rejected.text

    third = await client.post(
        "/v1/onboarding/join-requests",
        json={"full_name": "Joiner One", "team_id": str(team_b)},
        headers=cookie,
    )
    assert third.status_code == 201, third.text


# --- AC6.7: approval atomic with budget allocation ---------------------------


async def test_over_ceiling_approval_leaves_request_pending_and_no_membership(
    client: httpx.AsyncClient, sf, migrated_database_url: str
) -> None:
    lead_id = await make_user(sf, "ac67-lead")
    team_id = await make_team(sf, "ac67-team", ceiling=Decimal(100))
    await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD, budget=Decimal(80))
    lead_cookie = await session_cookie_headers(sf, lead_id)

    requester_id = await make_user(sf, "ac67-requester")
    async with sf() as session:
        row = await submit_join_request(
            session,
            requester_user_id=requester_id,
            requester_name="AC67 Requester",
            team_id=team_id,
        )
        await session.commit()
        request_id = row.id

    # $80 already allocated under a $100 ceiling: $50 must 422.
    over = await client.post(
        f"/v1/teams/{team_id}/join-requests/{request_id}/approve",
        json={"budget_usd": "50"},
        headers=lead_cookie,
    )
    assert over.status_code == 422, over.text
    assert over.json()["error"]["code"] == "budget_ceiling_exceeded"

    status = await fetch_val(
        migrated_database_url, "SELECT status FROM join_requests WHERE id = $1", request_id
    )
    assert status == "pending"  # untouched - no intermediate state
    membership_count = await fetch_val(
        migrated_database_url,
        "SELECT COUNT(*) FROM team_memberships WHERE team_id = $1 AND user_id = $2",
        team_id,
        requester_id,
    )
    assert int(membership_count) == 0

    # Within headroom: one atomic transaction creates membership + resolves.
    ok = await client.post(
        f"/v1/teams/{team_id}/join-requests/{request_id}/approve",
        json={"budget_usd": "20"},
        headers=lead_cookie,
    )
    assert ok.status_code == 201, ok.text
    assert Decimal(ok.json()["budget_usd"]) == Decimal(20)
    status = await fetch_val(
        migrated_database_url, "SELECT status FROM join_requests WHERE id = $1", request_id
    )
    assert status == "approved"

    # A resolved request cannot be approved twice (guarded status UPDATE ->
    # 404). Amount kept within headroom: the ceiling check runs BEFORE the
    # pending-status guard, so an over-headroom re-approve surfaces as 422
    # instead - see the BD-21 report's minor-findings note.
    again = await client.post(
        f"/v1/teams/{team_id}/join-requests/{request_id}/approve",
        json={"budget_usd": "0"},
        headers=lead_cookie,
    )
    assert again.status_code == 404
    membership_count = await fetch_val(
        migrated_database_url,
        "SELECT COUNT(*) FROM team_memberships WHERE team_id = $1 AND user_id = $2",
        team_id,
        requester_id,
    )
    assert int(membership_count) == 1  # no duplicate membership


# --- AC3.2: team restriction cannot re-enable an org-denied model ------------


async def test_team_restriction_with_org_denied_model_is_422_and_writes_nothing(
    client: httpx.AsyncClient, auth_headers: dict[str, str], sf, migrated_database_url: str
) -> None:
    denied_model = "gpt-4o-mini"
    allowed_model = next(m for m in MODEL_REGISTRY if m != denied_model)
    response = await client.put(
        "/v1/admin/model-policy",
        json={"mode": "denylist", "models": [denied_model]},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text

    lead_id = await make_user(sf, "ac32-lead")
    team_id = await make_team(sf, "ac32-team")
    await add_membership(sf, team_id, lead_id, role=TeamRole.TEAM_LEAD, budget=None)
    cookie = await session_cookie_headers(sf, lead_id)

    rejected = await client.put(
        f"/v1/teams/{team_id}/model-restrictions",
        json={"models": [allowed_model, denied_model]},
        headers=cookie,
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"]["code"] == "team_model_restricts_org_denied_model"

    # No write: no restriction row, and the pending audit entry rolled back.
    row_count = await fetch_val(
        migrated_database_url,
        "SELECT COUNT(*) FROM team_model_policies WHERE team_id = $1",
        team_id,
    )
    assert int(row_count) == 0
    audit_count = await fetch_val(
        migrated_database_url,
        "SELECT COUNT(*) FROM audit_entries WHERE action = 'team.model_restrictions.update'",
    )
    assert int(audit_count) == 0

    accepted = await client.put(
        f"/v1/teams/{team_id}/model-restrictions",
        json={"models": [allowed_model]},
        headers=cookie,
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["team_restriction"] == [allowed_model]


# --- Personal keys (gk_pk_) --------------------------------------------------


async def test_personal_key_gateway_end_to_end_charges_membership_counter(
    app: FastAPI, auth_headers: dict[str, str], sf, migrated_database_url: str
) -> None:
    """Create a gk_pk_ key over HTTP with a session cookie, spend it through
    the real gateway (stubbed provider transport only), and verify the A6
    counter: membership + team aggregate charged in lockstep, usage log
    attributed to the personal key and team."""
    owner_id = await make_user(sf, "pk-owner")
    team_id = await make_team(sf, "pk-team")
    await add_membership(sf, team_id, owner_id, budget=Decimal(10))
    other_team = await make_team(sf, "pk-other-team")
    cookie = await session_cookie_headers(sf, owner_id)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=canned_chat_response("openai/gpt-4o-mini"))

    app.dependency_overrides[get_provider_http_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                put = await client.put(
                    "/v1/admin/providers/openrouter/key",
                    json={"api_key": "sk-or-bd21"},
                    headers=auth_headers,
                )
                assert put.status_code == 200, put.text

                # Membership required: a team the caller doesn't belong to is
                # a generic 403.
                forbidden = await client.post(
                    "/v1/keys",
                    json={"name": "laptop", "team_id": str(other_team)},
                    headers=cookie,
                )
                assert forbidden.status_code == 403

                created = await client.post(
                    "/v1/keys",
                    json={"name": "laptop", "team_id": str(team_id)},
                    headers=cookie,
                )
                assert created.status_code == 201, created.text
                secret = created.json()["secret"]
                assert secret.startswith("gk_pk_")

                chat = await client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "openrouter/openai/gpt-4o-mini",
                        "messages": [{"role": "user", "content": "hi"}],
                    },
                    headers={"Authorization": f"Bearer {secret}"},
                )
                assert chat.status_code == 200, chat.text
                key_id = created.json()["id"]
    finally:
        app.dependency_overrides.pop(get_provider_http_client, None)

    # 4 prompt + 3 completion tokens at $0.15/$0.60 per million.
    expected_cost = (Decimal(4) * Decimal("0.15") + Decimal(3) * Decimal("0.60")) / Decimal(
        1_000_000
    )
    counters = await fetch_row(
        migrated_database_url,
        "SELECT m.current_spend_usd AS member_spend, t.current_spend_usd AS team_spend "
        "FROM team_memberships m JOIN teams t ON t.id = m.team_id "
        "WHERE m.team_id = $1 AND m.user_id = $2",
        team_id,
        owner_id,
    )
    assert Decimal(counters["member_spend"]) == expected_cost
    assert Decimal(counters["team_spend"]) == expected_cost  # ADR-7 lockstep

    usage = await fetch_row(
        migrated_database_url,
        "SELECT team_id, personal_api_key_id, service_account_key_id, cost_usd "
        "FROM usage_logs ORDER BY created_at DESC LIMIT 1",
    )
    assert usage["team_id"] == team_id
    assert usage["personal_api_key_id"] == uuid.UUID(key_id)
    assert usage["service_account_key_id"] is None
    assert Decimal(usage["cost_usd"]) == expected_cost


async def test_revoked_and_expired_personal_keys_rejected_on_gateway(
    client: httpx.AsyncClient, sf
) -> None:
    owner_id = await make_user(sf, "pk-lifecycle-owner")
    team_id = await make_team(sf, "pk-lifecycle-team")
    await add_membership(sf, team_id, owner_id, budget=None)
    cookie = await session_cookie_headers(sf, owner_id)

    created = await client.post(
        "/v1/keys", json={"name": "to-revoke", "team_id": str(team_id)}, headers=cookie
    )
    assert created.status_code == 201, created.text
    secret = created.json()["secret"]
    key_id = created.json()["id"]

    revoke = await client.delete(f"/v1/keys/{key_id}", headers=cookie)
    assert revoke.status_code == 204
    rejected = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert rejected.status_code == 401  # revoked -> generic 401 before any provider work

    # Expired key: seeded directly (the create API correctly refuses a past
    # expires_at), same generic 401.
    expired_secret, prefix = generate_personal_key_secret()
    async with sf() as session:
        session.add(
            PersonalApiKey(
                org_id=DEFAULT_ORG_ID,
                owner_user_id=owner_id,
                created_by_user_id=owner_id,
                team_id=team_id,
                name="expired",
                key_prefix=prefix,
                secret_hash=hash_secret(expired_secret),
                expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
        )
        await session.commit()
    rejected = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {expired_secret}"},
    )
    assert rejected.status_code == 401


async def test_membership_removal_blocked_while_active_personal_key_exists(
    client: httpx.AsyncClient, sf, migrated_database_url: str
) -> None:
    """ADR-4: removal is blocked (409), not silently auto-revoking the key;
    revoking the key unblocks removal."""
    owner_id = await make_user(sf, "adr4-owner")
    team_id = await make_team(sf, "adr4-team")
    await add_membership(sf, team_id, owner_id, budget=None)
    owner_cookie = await session_cookie_headers(sf, owner_id)
    created = await client.post(
        "/v1/keys", json={"name": "blocker", "team_id": str(team_id)}, headers=owner_cookie
    )
    assert created.status_code == 201, created.text
    key_id = created.json()["id"]

    admin_id = await make_user(sf, "adr4-admin", org_role=UserOrgRole.ORG_ADMIN)
    admin_cookie = await session_cookie_headers(sf, admin_id)

    blocked = await client.delete(f"/v1/teams/{team_id}/members/{owner_id}", headers=admin_cookie)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "member_has_active_keys"

    revoked = await client.delete(f"/v1/admin/keys/{key_id}", headers=admin_cookie)
    assert revoked.status_code == 204

    removed = await client.delete(f"/v1/teams/{team_id}/members/{owner_id}", headers=admin_cookie)
    assert removed.status_code == 204
    remaining = await fetch_val(
        migrated_database_url,
        "SELECT COUNT(*) FROM team_memberships WHERE team_id = $1 AND user_id = $2",
        team_id,
        owner_id,
    )
    assert int(remaining) == 0


# --- Session auth (services/sessions.py via /v1/auth/me) ---------------------


async def test_session_lifecycle_lookup_revoke_expiry_logout(
    client: httpx.AsyncClient, sf
) -> None:
    user_id = await make_user(sf, "sess-user")
    team_id = await make_team(sf, "sess-team")
    await add_membership(sf, team_id, user_id, role=TeamRole.MEMBER, budget=None)

    # Valid session -> /me resolves identity + teams + onboarding state.
    async with sf() as session:
        row, raw = await create_session(
            session, user_id=user_id, org_id=DEFAULT_ORG_ID, ttl_hours=12
        )
        session_id = row.id
    headers = {"Cookie": f"gatekey_session={raw}"}
    me = await client.get("/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    body = me.json()
    assert body["user_id"] == str(user_id)
    assert body["org_role"] is None
    assert body["onboarding_status"] == "resolved"
    assert [(t["team_id"], t["role"]) for t in body["teams"]] == [(str(team_id), "member")]

    # Revocation -> 401.
    async with sf() as session:
        assert await revoke_session(session, session_id) is True
    assert (await client.get("/v1/auth/me", headers=headers)).status_code == 401

    # Expiry honored (ttl 0 -> expires_at == created_at, already stale).
    async with sf() as session:
        _, stale = await create_session(
            session, user_id=user_id, org_id=DEFAULT_ORG_ID, ttl_hours=0
        )
    assert (
        await client.get("/v1/auth/me", headers={"Cookie": f"gatekey_session={stale}"})
    ).status_code == 401

    # Logout revokes its own session.
    fresh_headers = await session_cookie_headers(sf, user_id)
    assert (await client.post("/v1/auth/logout", headers=fresh_headers)).status_code == 204
    assert (await client.get("/v1/auth/me", headers=fresh_headers)).status_code == 401

    # Garbage / missing cookie -> 401.
    assert (
        await client.get("/v1/auth/me", headers={"Cookie": "gatekey_session=not-a-token"})
    ).status_code == 401
    assert (await client.get("/v1/auth/me")).status_code == 401


async def test_me_reports_pending_onboarding_states(client: httpx.AsyncClient, sf) -> None:
    user_id = await make_user(sf, "onb-user")
    cookie = await session_cookie_headers(sf, user_id)
    me = await client.get("/v1/auth/me", headers=cookie)
    assert me.json()["onboarding_status"] == "pending_profile"

    team_id = await make_team(sf, "onb-team")
    submitted = await client.post(
        "/v1/onboarding/join-requests",
        json={"full_name": "Onb User", "team_id": str(team_id)},
        headers=cookie,
    )
    assert submitted.status_code == 201, submitted.text
    me = await client.get("/v1/auth/me", headers=cookie)
    assert me.json()["onboarding_status"] == "pending_approval"


# --- Audit-write convention (design doc section 7, AC2.4/AC4.x) --------------


async def test_team_create_and_budget_reassign_write_exactly_one_audit_entry_each(
    client: httpx.AsyncClient, sf, migrated_database_url: str
) -> None:
    admin_id = await make_user(sf, "audit-admin", org_role=UserOrgRole.ORG_ADMIN)
    cookie = await session_cookie_headers(sf, admin_id)

    created = await client.post(
        "/v1/teams", json={"name": "audited-team", "budget_ceiling_usd": "100"}, headers=cookie
    )
    assert created.status_code == 201, created.text
    team_id = uuid.UUID(created.json()["id"])

    create_entries = await fetch_row(
        migrated_database_url,
        "SELECT COUNT(*) AS n, MIN(actor_user_id::text) AS actor, MIN(target_id) AS target, "
        "MIN(new_value::text) AS new_value FROM audit_entries WHERE action = 'team.create'",
    )
    assert int(create_entries["n"]) == 1  # exactly one
    assert create_entries["actor"] == str(admin_id)
    assert create_entries["target"] == str(team_id)
    assert "audited-team" in create_entries["new_value"]

    from_id = await make_user(sf, "audit-from")
    to_id = await make_user(sf, "audit-to")
    await add_membership(sf, team_id, from_id, budget=Decimal(30))
    await add_membership(sf, team_id, to_id, budget=Decimal(10))

    reassigned = await client.post(
        f"/v1/teams/{team_id}/reassign-budget",
        json={"from_user_id": str(from_id), "to_user_id": str(to_id), "amount_usd": "5"},
        headers=cookie,
    )
    assert reassigned.status_code == 200, reassigned.text
    assert Decimal(reassigned.json()["from_new_budget_usd"]) == Decimal(25)
    assert Decimal(reassigned.json()["to_new_budget_usd"]) == Decimal(15)

    entry = await fetch_row(
        migrated_database_url,
        "SELECT COUNT(*) OVER () AS n, old_value, new_value, actor_user_id "
        "FROM audit_entries WHERE action = 'team.budget.reassign'",
    )
    assert int(entry["n"]) == 1  # AC2.4: ONE entry recording both sides
    assert entry["actor_user_id"] == admin_id
    import json

    old_value = json.loads(entry["old_value"])
    new_value = json.loads(entry["new_value"])
    assert old_value["from"]["user_id"] == str(from_id)
    assert Decimal(old_value["from"]["budget_usd"]) == Decimal(30)
    assert Decimal(old_value["to"]["budget_usd"]) == Decimal(10)
    assert Decimal(new_value["from"]["budget_usd"]) == Decimal(25)
    assert Decimal(new_value["to"]["budget_usd"]) == Decimal(15)
    assert Decimal(new_value["amount_usd"]) == Decimal(5)
