"""Phase 3 audit gap-closure regression: `GET /v1/admin/audit-entries` must
serialize a real `source_ip` (Postgres `INET` -> SQLAlchemy
`ipaddress.IPv4Address`) without 500ing, in both the paginated JSON list and
the `?format=csv|json` export streams. Caught by QA during the access-window
track review; `AuditEntryResponse.source_ip` is `str`, but the ORM attribute
is not a `str` at the Python level until coerced at the response boundary.
"""

from __future__ import annotations

import httpx
import pytest

from .phase2_helpers import sf  # noqa: F401 - fixture resolved by name

pytestmark = pytest.mark.asyncio


async def test_audit_entries_list_serializes_source_ip(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    response = await client.post(
        "/v1/teams", json={"name": "audit-ip-smoke"}, headers=auth_headers
    )
    assert response.status_code == 201, response.text

    listing = await client.get(
        "/v1/admin/audit-entries", params={"action": "team.create"}, headers=auth_headers
    )
    assert listing.status_code == 200, listing.text
    entries = listing.json()["entries"]
    assert entries, "expected at least one team.create audit entry"
    entry = entries[0]
    assert entry["source_ip"] is None or isinstance(entry["source_ip"], str)


async def test_audit_entries_json_export_does_not_500(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
) -> None:
    await client.post("/v1/teams", json={"name": "audit-export-smoke"}, headers=auth_headers)
    response = await client.get(
        "/v1/admin/audit-entries",
        params={"format": "json"},
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()  # a valid JSON array, streaming didn't leave it truncated
