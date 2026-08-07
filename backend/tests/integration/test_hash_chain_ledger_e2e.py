"""Integration tests for Phase 5 (Differentiators, 5.2 Hash-Chained Audit
Ledger) against a real Postgres instance - AC5.2.2/AC5.2.3/AC5.2.4/AC5.2.6/
AC5.2.7/AC5.2.8, and the design doc's own NFR acceptance tests (section 10):

  - Verify before the chain is ever enabled reports `not_enabled`.
  - Enabling the chain backfills a pre-existing `audit_entries` history
    (AC5.2.6, true historical genesis) and `verify` reports it intact.
  - Mutual exclusivity (AC5.2.7) is enforced by the API, not just
    documented - both directions rejected with a structured 422.
  - Directly mutating one historical row via raw SQL (bypassing the service
    layer entirely) is detected by `verify`, naming the exact entry.
  - CSV/JSON export gains chain columns only once chaining is enabled.
  - Two concurrent audit-writing requests never fork the chain (P0 per the
    design doc's own testing-strategy table).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import uuid

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.audit_entry import AuditEntry

from .conftest import to_asyncpg_dsn

pytestmark = pytest.mark.asyncio

_TRUNCATE_SQL = "TRUNCATE TABLE audit_entries, compliance_settings, teams CASCADE"


@pytest_asyncio.fixture(autouse=True)
async def _clean_ledger_tables(migrated_database_url: str):
    conn = await asyncpg.connect(to_asyncpg_dsn(migrated_database_url))
    try:
        await conn.execute(_TRUNCATE_SQL)
    finally:
        await conn.close()
    yield


@pytest_asyncio.fixture
async def sf(migrated_database_url: str):
    engine = create_async_engine(migrated_database_url, pool_size=10, max_overflow=20)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()


async def _seed_unchained_audit_entries(sf: async_sessionmaker, count: int) -> list[uuid.UUID]:
    """Directly INSERTs `count` `audit_entries` rows with distinct
    `created_at` timestamps, bypassing `write_audit_entry` entirely -
    simulates pre-existing (Phase 2/3/4-era) history with `chain_hash =
    NULL` for AC5.2.6's true-historical-genesis backfill test."""
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows: list[AuditEntry] = []
    async with sf() as session:
        for i in range(count):
            row = AuditEntry(
                org_id=DEFAULT_ORG_ID,
                actor_user_id=None,
                actor_label="system:admin_token",
                action="team.create",
                target_type="team",
                target_id=f"pre-existing-{i}",
                old_value=None,
                new_value={"name": f"pre-existing-team-{i}"},
                created_at=base + timedelta(minutes=i),
            )
            session.add(row)
            rows.append(row)
        await session.commit()
        # `id` has a Python-side default (`uuid.uuid4`) evaluated at flush
        # time (inside `commit()` above) - only safe to read `row.id` AFTER
        # the commit, not immediately after `session.add()`.
        return [row.id for row in rows]


async def _fetch_row(database_url: str, query: str, *args):
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        return await conn.fetchrow(query, *args)
    finally:
        await conn.close()


async def _execute(database_url: str, query: str, *args) -> None:
    conn = await asyncpg.connect(to_asyncpg_dsn(database_url))
    try:
        await conn.execute(query, *args)
    finally:
        await conn.close()


async def _enable_chain(client, auth_headers: dict[str, str]) -> None:
    response = await client.put(
        "/v1/admin/compliance-settings",
        json={
            "audit_retention_days": None,
            "log_prompt_retention_days": 30,
            "access_schedule_timezone": "UTC",
            "chain_enabled": True,
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["chain_enabled"] is True


# --- not_enabled ---------------------------------------------------------


async def test_verify_reports_not_enabled_before_chain_is_ever_turned_on(
    client, auth_headers: dict[str, str]
) -> None:
    response = await client.get("/v1/admin/audit/verify", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "not_enabled"}


# --- AC5.2.6 backfill ------------------------------------------------------


async def test_enabling_chain_backfills_pre_existing_history_and_verify_is_intact(
    client, auth_headers: dict[str, str], sf: async_sessionmaker
) -> None:
    pre_existing_ids = await _seed_unchained_audit_entries(sf, 5)

    await _enable_chain(client, auth_headers)

    verify_response = await client.get("/v1/admin/audit/verify", headers=auth_headers)
    assert verify_response.status_code == 200, verify_response.text
    body = verify_response.json()
    assert body["status"] == "intact"
    # The 5 pre-existing rows PLUS the "compliance_settings.update" audit
    # entry the enable-toggle PUT itself wrote (flushed before the backfill
    # runs, in the same transaction - see services/audit.py's Phase 5 note)
    # both get swept into the historical chain.
    assert body["entries_verified"] == 6


async def test_backfill_assigns_chain_seq_by_created_at_order_not_insertion_order(
    client, auth_headers: dict[str, str], sf: async_sessionmaker, migrated_database_url: str
) -> None:
    pre_existing_ids = await _seed_unchained_audit_entries(sf, 3)

    await _enable_chain(client, auth_headers)

    rows = []
    for entry_id in pre_existing_ids:
        row = await _fetch_row(
            migrated_database_url,
            "SELECT chain_seq, prev_hash, chain_hash FROM audit_entries WHERE id = $1",
            entry_id,
        )
        rows.append(row)

    # Seeded in ascending created_at order, so chain_seq must also ascend
    # 1, 2, 3 in that same order (AC5.2.6's deterministic (created_at, id)
    # ordering).
    assert [row["chain_seq"] for row in rows] == [1, 2, 3]
    assert rows[0]["prev_hash"] is None  # true genesis
    assert rows[1]["prev_hash"] == rows[0]["chain_hash"]
    assert rows[2]["prev_hash"] == rows[1]["chain_hash"]
    assert all(row["chain_hash"] is not None for row in rows)


# --- AC5.2.7 mutual exclusivity --------------------------------------------


async def test_enabling_chain_with_finite_retention_in_same_request_is_rejected(
    client, auth_headers: dict[str, str]
) -> None:
    response = await client.put(
        "/v1/admin/compliance-settings",
        json={
            "audit_retention_days": 30,
            "log_prompt_retention_days": 30,
            "access_schedule_timezone": "UTC",
            "chain_enabled": True,
        },
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "chain_purge_mutually_exclusive"


async def test_setting_finite_retention_while_chain_already_enabled_is_rejected(
    client, auth_headers: dict[str, str]
) -> None:
    await _enable_chain(client, auth_headers)

    response = await client.put(
        "/v1/admin/compliance-settings",
        json={
            "audit_retention_days": 30,
            "log_prompt_retention_days": 30,
            "access_schedule_timezone": "UTC",
            "chain_enabled": True,
        },
        headers=auth_headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "chain_purge_mutually_exclusive"

    # The rejected request must not have silently applied anything.
    get_response = await client.get("/v1/admin/compliance-settings", headers=auth_headers)
    assert get_response.json()["audit_retention_days"] is None
    assert get_response.json()["chain_enabled"] is True


async def test_disabling_chain_and_setting_finite_retention_in_one_request_succeeds(
    client, auth_headers: dict[str, str]
) -> None:
    """The "vice versa" valid transition: turning chaining OFF while
    simultaneously setting a finite retention window, in the SAME request,
    must succeed atomically - the mutual-exclusivity guard only rejects
    `chain_enabled=True` combined with a non-null retention, never the
    other three combinations."""
    await _enable_chain(client, auth_headers)

    response = await client.put(
        "/v1/admin/compliance-settings",
        json={
            "audit_retention_days": 14,
            "log_prompt_retention_days": 30,
            "access_schedule_timezone": "UTC",
            "chain_enabled": False,
        },
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["chain_enabled"] is False
    assert body["audit_retention_days"] == 14


# --- tamper detection (design doc section 10 NFR) --------------------------


async def test_tamper_via_raw_sql_is_detected_and_names_the_exact_entry(
    client, auth_headers: dict[str, str], sf: async_sessionmaker, migrated_database_url: str
) -> None:
    await _enable_chain(client, auth_headers)

    # Write 3 real chained entries via the real API (team.create).
    team_ids = []
    for i in range(3):
        response = await client.post(
            "/v1/teams", json={"name": f"chain-tamper-team-{i}"}, headers=auth_headers
        )
        assert response.status_code == 201, response.text
        team_ids.append(response.json()["id"])

    # Precondition: fully intact before tampering.
    pre_tamper = await client.get("/v1/admin/audit/verify", headers=auth_headers)
    assert pre_tamper.json()["status"] == "intact"

    # Pick the middle team.create entry and directly UPDATE its old_value -
    # simulating a raw SQL tamper, bypassing the service layer entirely.
    target_row = await _fetch_row(
        migrated_database_url,
        "SELECT id, chain_seq FROM audit_entries WHERE target_id = $1",
        team_ids[1],
    )
    await _execute(
        migrated_database_url,
        "UPDATE audit_entries SET old_value = $1 WHERE id = $2",
        json.dumps({"tampered": True}),
        target_row["id"],
    )

    verify_response = await client.get("/v1/admin/audit/verify", headers=auth_headers)
    assert verify_response.status_code == 200, verify_response.text
    body = verify_response.json()
    assert body["status"] == "broken"
    assert body["broken_at_entry_id"] == str(target_row["id"])
    assert body["broken_at_chain_seq"] == target_row["chain_seq"]


# --- AC5.2.8 export ----------------------------------------------------


async def test_export_omits_chain_columns_when_chain_disabled(
    client, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/v1/teams", json={"name": "export-unchained-team"}, headers=auth_headers
    )
    assert response.status_code == 201, response.text

    export_response = await client.get(
        "/v1/admin/audit-entries", params={"format": "csv"}, headers=auth_headers
    )
    assert export_response.status_code == 200, export_response.text
    reader = csv.reader(io.StringIO(export_response.text))
    header = next(reader)
    assert "chain_hash" not in header
    assert "chain_seq" not in header


async def test_export_includes_chain_columns_when_chain_enabled(
    client, auth_headers: dict[str, str]
) -> None:
    await _enable_chain(client, auth_headers)
    response = await client.post(
        "/v1/teams", json={"name": "export-chained-team"}, headers=auth_headers
    )
    assert response.status_code == 201, response.text

    csv_response = await client.get(
        "/v1/admin/audit-entries", params={"format": "csv"}, headers=auth_headers
    )
    assert csv_response.status_code == 200, csv_response.text
    rows = list(csv.reader(io.StringIO(csv_response.text)))
    header, data_rows = rows[0], rows[1:]
    assert "chain_seq" in header
    assert "prev_hash" in header
    assert "chain_hash" in header
    chain_hash_idx = header.index("chain_hash")
    assert all(row[chain_hash_idx] for row in data_rows), "every chained row must export a chain_hash"

    json_response = await client.get(
        "/v1/admin/audit-entries", params={"format": "json"}, headers=auth_headers
    )
    assert json_response.status_code == 200, json_response.text
    exported = json_response.json()
    assert exported, "expected at least one exported row"
    assert all("chain_hash" in row and "chain_seq" in row and "prev_hash" in row for row in exported)


# --- concurrency (design doc section 9.1 P0 test) ---------------------------


async def test_concurrent_audit_writes_never_fork_the_chain(
    client, auth_headers: dict[str, str]
) -> None:
    await _enable_chain(client, auth_headers)

    concurrency = 8
    responses = await asyncio.gather(
        *[
            client.post(
                "/v1/teams", json={"name": f"concurrent-chain-team-{i}"}, headers=auth_headers
            )
            for i in range(concurrency)
        ]
    )
    for response in responses:
        assert response.status_code == 201, response.text

    verify_response = await client.get("/v1/admin/audit/verify", headers=auth_headers)
    assert verify_response.status_code == 200, verify_response.text
    body = verify_response.json()
    assert body["status"] == "intact"
    # The enable-toggle's own audit entry (1) + one team.create per
    # concurrent request - every chain_seq is unique and sequential (DB
    # partial-unique-index would have rejected a fork outright; verify()
    # additionally proves the linkage is a real, unforked, single chain).
    assert body["entries_verified"] == 1 + concurrency
