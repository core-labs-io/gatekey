"""Integration tests for Phase 5 (Differentiators, 5.1 Shadow AI Discovery)
against a real Postgres instance - `gatekey/phase-5-product-spec.md`
AC5.1.1/AC5.1.3-AC5.1.10 and `gatekey/phase-5-technical-design.md` section
9.1's mandatory test scenarios.

Covers: the full ingest -> report flow, the AC5.1.1 data-minimization gate
(mixed matched/unmatched batch persists only matched rows), the ingest-token
trust-boundary proof (P0, "Shadow-AI ingestion token cannot authenticate any
other endpoint, and vice versa"), the Team-Lead-scoped report access
(server-side-forced `team_id`, not client-trusted), the AC5.1.7
confirm-required gate, and the retention purge job.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.shadow_ai_ingest_event import ShadowAiIngestEvent
from gatekey.db.models.team_membership import TeamRole
from gatekey.db.models.user import User
from gatekey.services.scheduler import run_shadow_ai_purge_if_due

from .conftest import to_asyncpg_dsn
from .phase2_helpers import (  # noqa: F401 - fixtures resolved by name
    _clean_phase2_tables,
    add_membership,
    make_team,
    session_cookie_headers,
    sf,
)

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _truncate_shadow_ai_tables(migrated_database_url: str):
    """Every test starts with an empty Shadow AI slate - including
    `known_ai_tool_hostnames`, whose migration-seeded starter rows would
    otherwise leak into (and make non-deterministic) the exact-match
    assertions below. Tests that need a known hostname add one explicitly."""
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute(
            "TRUNCATE TABLE shadow_ai_ingest_events, shadow_ai_ingest_config, "
            "known_ai_tool_hostnames CASCADE"
        )
    finally:
        await conn.close()
    yield


async def make_user_with_email(sf: async_sessionmaker, name: str, email: str) -> uuid.UUID:
    async with sf() as session:
        user = User(org_id=DEFAULT_ORG_ID, name=name, sso_email=email)
        session.add(user)
        await session.commit()
        return user.id


async def _add_known_hostname(
    client: httpx.AsyncClient, auth_headers: dict[str, str], hostname: str, tool_label: str = "Test Tool"
) -> None:
    response = await client.post(
        "/v1/admin/shadow-ai/known-hostnames",
        json={"hostname": hostname, "tool_label": tool_label, "enabled": True},
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text


async def _rotate_ingest_token(client: httpx.AsyncClient, auth_headers: dict[str, str]) -> str:
    response = await client.post("/v1/admin/shadow-ai/ingest-token", headers=auth_headers)
    assert response.status_code == 200, response.text
    token = response.json()["token"]
    assert token.startswith("gk_sai_")
    return token


def _ingest_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# AC5.1.1 data-minimization gate: mixed batch -> only matched rows persist.
# ---------------------------------------------------------------------------


async def test_ingest_batch_persists_only_matched_hostname_rows(
    client: httpx.AsyncClient, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    await _add_known_hostname(client, auth_headers, "chat.openai.com")
    token = await _rotate_ingest_token(client, auth_headers)

    response = await client.post(
        "/v1/admin/shadow-ai/ingest",
        json={
            "events": [
                {
                    "user_identifier": "alice@example.com",
                    "destination_host": "chat.openai.com",
                    "occurred_at": "2026-08-01T12:00:00Z",
                    "source": "sase_log",
                },
                {
                    "user_identifier": "alice@example.com",
                    "destination_host": "totally-unrelated.example.com",
                    "occurred_at": "2026-08-01T12:05:00Z",
                    "source": "sase_log",
                },
                {
                    "user_identifier": "bob@example.com",
                    "destination_host": "also-not-an-ai-tool.example.net",
                    "occurred_at": "2026-08-01T12:10:00Z",
                    "source": "proxy_log",
                },
            ]
        },
        headers=_ingest_headers(token),
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body == {"received": 3, "persisted": 1, "dropped": 2}

    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        rows = await conn.fetch("SELECT destination_host, user_identifier FROM shadow_ai_ingest_events")
    finally:
        await conn.close()
    assert len(rows) == 1
    assert rows[0]["destination_host"] == "chat.openai.com"
    assert rows[0]["user_identifier"] == "alice@example.com"


# ---------------------------------------------------------------------------
# Hardening pass item 7: `raw_metadata` size cap (AC5.1.9's "connection
# metadata only" claim, now enforced at the schema level, not just documented
# convention - see `docs/policy/shadow-ai-data-handling.md` §2).
# ---------------------------------------------------------------------------


async def test_ingest_rejects_oversized_raw_metadata_with_structured_422(
    client: httpx.AsyncClient, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    await _add_known_hostname(client, auth_headers, "chat.openai.com")
    token = await _rotate_ingest_token(client, auth_headers)

    # Comfortably over the 4096-byte serialized cap.
    oversized_metadata = {"padding": "x" * 5000}
    response = await client.post(
        "/v1/admin/shadow-ai/ingest",
        json={
            "events": [
                {
                    "user_identifier": "alice@example.com",
                    "destination_host": "chat.openai.com",
                    "occurred_at": "2026-08-01T12:00:00Z",
                    "source": "sase_log",
                    "raw_metadata": oversized_metadata,
                }
            ]
        },
        headers=_ingest_headers(token),
    )
    assert response.status_code == 422, response.text

    # A clean rejection, not a silent truncation or a partial persist - the
    # whole batch (Pydantic validates the full request body before the
    # handler ever runs) is refused, nothing is written.
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        rows = await conn.fetch("SELECT id FROM shadow_ai_ingest_events")
    finally:
        await conn.close()
    assert rows == []


async def test_ingest_accepts_raw_metadata_within_the_size_cap(
    client: httpx.AsyncClient, auth_headers: dict[str, str], migrated_database_url: str
) -> None:
    await _add_known_hostname(client, auth_headers, "chat.openai.com")
    token = await _rotate_ingest_token(client, auth_headers)

    small_metadata = {"connection_type": "vpn", "client_version": "1.2.3"}
    response = await client.post(
        "/v1/admin/shadow-ai/ingest",
        json={
            "events": [
                {
                    "user_identifier": "alice@example.com",
                    "destination_host": "chat.openai.com",
                    "occurred_at": "2026-08-01T12:00:00Z",
                    "source": "sase_log",
                    "raw_metadata": small_metadata,
                }
            ]
        },
        headers=_ingest_headers(token),
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"received": 1, "persisted": 1, "dropped": 0}

    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        rows = await conn.fetch("SELECT raw_metadata FROM shadow_ai_ingest_events")
    finally:
        await conn.close()
    assert len(rows) == 1


async def test_ingestion_rejected_before_setup_no_token_generated(client: httpx.AsyncClient) -> None:
    """AC5.1.4 fail-closed: with no `shadow_ai_ingest_config` row at all
    (nothing rotated yet in this test), the ingest endpoint rejects every
    request regardless of the bearer token presented."""
    response = await client.post(
        "/v1/admin/shadow-ai/ingest",
        json={"events": []},
        headers=_ingest_headers("gk_sai_some-token-nobody-issued"),
    )
    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# Full ingest -> report flow (AC5.1.5).
# ---------------------------------------------------------------------------


async def test_full_ingest_to_report_flow(
    client: httpx.AsyncClient, auth_headers: dict[str, str], sf
) -> None:
    await _add_known_hostname(client, auth_headers, "claude.ai", "Claude")
    token = await _rotate_ingest_token(client, auth_headers)

    matched_user_id = await make_user_with_email(sf, "Alice", "alice@example.com")

    ingest_response = await client.post(
        "/v1/admin/shadow-ai/ingest",
        json={
            "events": [
                {
                    "user_identifier": "alice@example.com",
                    "destination_host": "claude.ai",
                    "occurred_at": "2026-08-01T09:00:00Z",
                    "source": "sase_log",
                },
                {
                    "user_identifier": "unknown-person@example.com",
                    "destination_host": "claude.ai",
                    "occurred_at": "2026-08-02T09:00:00Z",
                    "source": "sase_log",
                },
            ]
        },
        headers=_ingest_headers(token),
    )
    assert ingest_response.status_code == 202, ingest_response.text
    assert ingest_response.json()["persisted"] == 2

    report_response = await client.get(
        "/v1/admin/shadow-ai/report",
        params={"since": "2026-01-01T00:00:00Z", "until": "2026-12-31T00:00:00Z"},
        headers=auth_headers,
    )
    assert report_response.status_code == 200, report_response.text
    rows = report_response.json()
    assert len(rows) == 2

    matched_row = next(r for r in rows if r["user_identifier"] == "alice@example.com")
    assert matched_row["matched_user_id"] == str(matched_user_id)
    assert matched_row["linked"] is True
    assert matched_row["tool_label"] == "Claude"
    assert matched_row["destination_host"] == "claude.ai"

    unmatched_row = next(r for r in rows if r["user_identifier"] == "unknown-person@example.com")
    assert unmatched_row["matched_user_id"] is None
    assert unmatched_row["linked"] is False


async def test_repeat_violator_flag_derived_at_query_time(
    client: httpx.AsyncClient, auth_headers: dict[str, str], sf
) -> None:
    await _add_known_hostname(client, auth_headers, "chat.deepseek.com", "DeepSeek")
    token = await _rotate_ingest_token(client, auth_headers)
    await make_user_with_email(sf, "Carol", "carol@example.com")

    now = datetime.now(timezone.utc)
    events = [
        {
            "user_identifier": "carol@example.com",
            "destination_host": "chat.deepseek.com",
            "occurred_at": (now - timedelta(days=offset)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": "sase_log",
        }
        for offset in (0, 1, 2)
    ]
    ingest_response = await client.post(
        "/v1/admin/shadow-ai/ingest", json={"events": events}, headers=_ingest_headers(token)
    )
    assert ingest_response.json()["persisted"] == 3

    report_response = await client.get("/v1/admin/shadow-ai/report", headers=auth_headers)
    rows = report_response.json()
    row = next(r for r in rows if r["user_identifier"] == "carol@example.com")
    assert row["repeat_violator"] is True


# ---------------------------------------------------------------------------
# P0: ingest-token trust-boundary proof - non-overlap, both directions.
# ---------------------------------------------------------------------------


async def test_ingest_token_cannot_authenticate_admin_gated_endpoint(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    token = await _rotate_ingest_token(client, auth_headers)

    response = await client.get("/v1/admin/shadow-ai/config", headers=_ingest_headers(token))
    assert response.status_code == 401, response.text

    other_admin_response = await client.get("/v1/admin/users", headers=_ingest_headers(token))
    assert other_admin_response.status_code == 401, other_admin_response.text


async def test_admin_break_glass_token_cannot_call_ingest_endpoint(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """The break-glass `GATEKEY_ADMIN_TOKEN` (`auth_headers`) satisfies
    `require_admin` on every other admin route in this codebase - it must
    NOT satisfy `require_shadow_ai_ingest_token`."""
    await _add_known_hostname(client, auth_headers, "chat.openai.com")
    await _rotate_ingest_token(client, auth_headers)

    response = await client.post(
        "/v1/admin/shadow-ai/ingest", json={"events": []}, headers=auth_headers
    )
    assert response.status_code == 401, response.text


async def test_service_account_key_cannot_call_ingest_endpoint(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    default_user_id: str,
    default_team_id: str,
) -> None:
    await _rotate_ingest_token(client, auth_headers)

    create_response = await client.post(
        "/v1/admin/service-accounts",
        json={"name": "gateway-app", "user_id": default_user_id, "team_id": default_team_id},
        headers=auth_headers,
    )
    assert create_response.status_code == 201, create_response.text
    secret = create_response.json()["secret"]
    assert secret.startswith("gk_sk_")

    response = await client.post(
        "/v1/admin/shadow-ai/ingest",
        json={"events": []},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 401, response.text


async def test_ingest_token_cannot_call_gateway_chat_endpoint(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Conversely: an ingestion feed must never be able to make an inference
    call (design doc section 2.5's "an ingestion feed should never be able
    to make inference calls, and vice versa")."""
    token = await _rotate_ingest_token(client, auth_headers)
    response = await client.post(
        "/v1/chat/completions",
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        headers=_ingest_headers(token),
    )
    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# Team-Lead-scoped report access (AC5.1.6, design doc wiring row 6).
# ---------------------------------------------------------------------------


async def test_team_lead_sees_only_own_teams_matched_rows(
    client: httpx.AsyncClient, auth_headers: dict[str, str], sf
) -> None:
    await _add_known_hostname(client, auth_headers, "gemini.google.com", "Gemini")
    token = await _rotate_ingest_token(client, auth_headers)

    team_a = await make_team(sf, "team-a-shadow-ai")
    team_b = await make_team(sf, "team-b-shadow-ai")
    lead_a_id = await make_user_with_email(sf, "Lead A", "lead-a@example.com")
    member_a_id = await make_user_with_email(sf, "Member A", "member-a@example.com")
    member_b_id = await make_user_with_email(sf, "Member B", "member-b@example.com")
    await add_membership(sf, team_a, lead_a_id, role=TeamRole.TEAM_LEAD)
    await add_membership(sf, team_a, member_a_id, role=TeamRole.MEMBER)
    await add_membership(sf, team_b, member_b_id, role=TeamRole.MEMBER)
    lead_a_headers = await session_cookie_headers(sf, lead_a_id)

    ingest_response = await client.post(
        "/v1/admin/shadow-ai/ingest",
        json={
            "events": [
                {
                    "user_identifier": "member-a@example.com",
                    "destination_host": "gemini.google.com",
                    "occurred_at": "2026-08-01T09:00:00Z",
                    "source": "sase_log",
                },
                {
                    "user_identifier": "member-b@example.com",
                    "destination_host": "gemini.google.com",
                    "occurred_at": "2026-08-01T10:00:00Z",
                    "source": "sase_log",
                },
            ]
        },
        headers=_ingest_headers(token),
    )
    assert ingest_response.json()["persisted"] == 2

    # No team_id filter - forced to the Team Lead's own led team(s) only.
    report_response = await client.get("/v1/admin/shadow-ai/report", headers=lead_a_headers)
    assert report_response.status_code == 200, report_response.text
    rows = report_response.json()
    assert [r["user_identifier"] for r in rows] == ["member-a@example.com"]

    # Explicitly requesting another team's id is rejected, not silently
    # widened or silently ignored.
    spoofed_response = await client.get(
        "/v1/admin/shadow-ai/report", params={"team_id": str(team_b)}, headers=lead_a_headers
    )
    assert spoofed_response.status_code == 403, spoofed_response.text


async def test_plain_member_has_no_report_access(
    client: httpx.AsyncClient, auth_headers: dict[str, str], sf
) -> None:
    team = await make_team(sf, "team-member-only")
    member_id = await make_user_with_email(sf, "Plain Member", "plain-member@example.com")
    await add_membership(sf, team, member_id, role=TeamRole.MEMBER)
    member_headers = await session_cookie_headers(sf, member_id)

    response = await client.get("/v1/admin/shadow-ai/report", headers=member_headers)
    assert response.status_code == 403, response.text


async def test_auditor_has_full_org_wide_read_only_access(
    client: httpx.AsyncClient, auth_headers: dict[str, str], sf
) -> None:
    await _add_known_hostname(client, auth_headers, "claude.ai")
    token = await _rotate_ingest_token(client, auth_headers)
    await client.post(
        "/v1/admin/shadow-ai/ingest",
        json={
            "events": [
                {
                    "user_identifier": "someone@example.com",
                    "destination_host": "claude.ai",
                    "occurred_at": "2026-08-01T09:00:00Z",
                    "source": "sase_log",
                }
            ]
        },
        headers=_ingest_headers(token),
    )

    from gatekey.db.models.user import UserOrgRole

    auditor_id = await make_user_with_email(sf, "Auditor", "auditor@example.com")
    async with sf() as session:
        row = (await session.execute(select(User).where(User.id == auditor_id))).scalar_one()
        row.org_role = UserOrgRole.AUDITOR
        await session.commit()
    auditor_headers = await session_cookie_headers(sf, auditor_id)

    read_response = await client.get("/v1/admin/shadow-ai/report", headers=auditor_headers)
    assert read_response.status_code == 200, read_response.text
    assert len(read_response.json()) == 1

    # Read-only: auditor cannot write config.
    write_response = await client.put(
        "/v1/admin/shadow-ai/config",
        json={
            "detection_source": "sase_log",
            "enforcement_mode": "detect_only",
            "webhook_url": None,
            "shadow_ai_retention_days": 30,
            "confirm": False,
        },
        headers=auditor_headers,
    )
    assert write_response.status_code == 403, write_response.text


# ---------------------------------------------------------------------------
# AC5.1.7 confirm-required gate.
# ---------------------------------------------------------------------------


async def test_enabling_notification_enforcement_without_confirm_is_rejected(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.put(
        "/v1/admin/shadow-ai/config",
        json={
            "detection_source": "sase_log",
            "enforcement_mode": "notification",
            "webhook_url": None,
            "shadow_ai_retention_days": 90,
            "confirm": False,
        },
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text


async def test_enabling_notification_enforcement_with_confirm_succeeds(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.put(
        "/v1/admin/shadow-ai/config",
        json={
            "detection_source": "sase_log",
            "enforcement_mode": "notification",
            "webhook_url": None,
            "shadow_ai_retention_days": 90,
            "confirm": True,
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["enforcement_mode"] == "notification"

    # Re-submitting the SAME already-active mode (only changing an unrelated
    # field) does NOT require confirm again.
    second_response = await client.put(
        "/v1/admin/shadow-ai/config",
        json={
            "detection_source": "sase_log",
            "enforcement_mode": "notification",
            "webhook_url": None,
            "shadow_ai_retention_days": 45,
            "confirm": False,
        },
        headers=auth_headers,
    )
    assert second_response.status_code == 200, second_response.text
    assert second_response.json()["shadow_ai_retention_days"] == 45


async def test_enabling_webhook_enforcement_requires_a_webhook_url(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.put(
        "/v1/admin/shadow-ai/config",
        json={
            "detection_source": "sase_log",
            "enforcement_mode": "webhook",
            "webhook_url": None,
            "shadow_ai_retention_days": 90,
            "confirm": True,
        },
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# Retention purge job (AC5.1.10, design doc wiring row 5).
# ---------------------------------------------------------------------------


async def test_purge_deletes_only_events_older_than_retention_window(
    client: httpx.AsyncClient, auth_headers: dict[str, str], sf, migrated_database_url: str
) -> None:
    await _add_known_hostname(client, auth_headers, "chatgpt.com")
    await client.put(
        "/v1/admin/shadow-ai/config",
        json={
            "detection_source": "sase_log",
            "enforcement_mode": "detect_only",
            "webhook_url": None,
            "shadow_ai_retention_days": 30,
            "confirm": False,
        },
        headers=auth_headers,
    )

    now = datetime.now(timezone.utc)
    async with sf() as session:
        old_row = ShadowAiIngestEvent(
            org_id=DEFAULT_ORG_ID,
            user_identifier="old-event@example.com",
            destination_host="chatgpt.com",
            occurred_at=now - timedelta(days=40),
            source="sase_log",
        )
        recent_row = ShadowAiIngestEvent(
            org_id=DEFAULT_ORG_ID,
            user_identifier="recent-event@example.com",
            destination_host="chatgpt.com",
            occurred_at=now - timedelta(days=1),
            source="sase_log",
        )
        session.add_all([old_row, recent_row])
        await session.commit()
        # Backdate `created_at` for the "old" row directly - the purge cutoff
        # is `created_at`-based (ingestion time), not `occurred_at`.
        from sqlalchemy import update

        await session.execute(
            update(ShadowAiIngestEvent)
            .where(ShadowAiIngestEvent.id == old_row.id)
            .values(created_at=now - timedelta(days=40))
        )
        await session.commit()

        deleted = await run_shadow_ai_purge_if_due(session)
        assert deleted == 1

        remaining = (await session.execute(select(ShadowAiIngestEvent))).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].user_identifier == "recent-event@example.com"
