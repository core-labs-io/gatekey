"""Audit-log read + export endpoints (Phase 2 section 5.8; Phase 3 BD-9
extends with `source_ip` and CSV/JSON export - design doc section 7.2).

`GET /v1/admin/audit-entries?action=&actor=&from=&to=&page=` -
`require_role(org_admin, auditor)` per the design contract (session roles
only; the read-only Auditor role's primary surface). No `format` param =
today's unchanged paginated JSON response (AC1.3 - zero regression),
paginated newest-first over the indexed `(org_id, created_at)` columns.

`?format=csv|json` (AC1.3/AC1.4/AC1.5): same role gate, same filters,
`StreamingResponse` over a server-side keyset-paginated query
(`(created_at, id)` cursor, `_EXPORT_BATCH_SIZE` rows per round-trip,
looped) - never a single `SELECT *` materialized in memory, so an org with
an unboundedly large audit table cannot OOM the export endpoint.

Filters: `action` is an exact match against the fixed action vocabulary
(the UI's dropdown); `actor` matches `actor_user_id` when it parses as a
UUID, otherwise a case-insensitive substring of the `actor_label` snapshot
(covers both "filter by picked user" and "search by name/email" without two
parameters); `from`/`to` bound `created_at` (ISO 8601, half-open interval).

Phase 5 (Differentiators, 5.2 Hash-Chained Audit Ledger, AC5.2.8) - export
gains `chain_seq`/`prev_hash`/`chain_hash` columns, but ONLY when
`compliance_settings.chain_enabled = true` (fetched once, at the top of
`list_audit_entries_endpoint`, per the design doc's wiring checklist "5.1
(Ledger, 5.2)" row 5) - an org that never enables chaining gets a
byte-for-byte unchanged export, exactly as before. This makes a downloaded
export independently re-verifiable offline by a third party (the same
`services.audit_chain.compute_chain_hash` formula the admin console's
"Verify now" button uses).
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import require_role
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.audit_entry import AuditEntry
from gatekey.db.session import get_db_session
from gatekey.services.compliance_settings import get_effective_compliance_settings
from gatekey.services.sessions import SessionContext

router = APIRouter(prefix="/v1/admin/audit-entries", tags=["admin", "audit"])

PAGE_SIZE = 50
_EXPORT_BATCH_SIZE = 500

_CSV_COLUMNS = (
    "id",
    "actor_user_id",
    "actor_label",
    "action",
    "target_type",
    "target_id",
    "old_value",
    "new_value",
    "source_ip",
    "created_at",
)

# Phase 5 (AC5.2.8) - appended to `_CSV_COLUMNS` only when
# `compliance_settings.chain_enabled = true` for this export.
_CHAIN_CSV_COLUMNS = ("chain_seq", "prev_hash", "chain_hash")


class AuditEntryResponse(BaseModel):
    id: uuid.UUID
    actor_user_id: uuid.UUID | None
    actor_label: str
    action: str
    target_type: str
    target_id: str
    old_value: dict[str, Any] | None
    new_value: dict[str, Any] | None
    source_ip: str | None
    created_at: datetime


class AuditEntriesPageResponse(BaseModel):
    entries: list[AuditEntryResponse]
    page: int
    page_size: int
    total: int


def _build_filters(
    *, action: str | None, actor: str | None, from_: datetime | None, to: datetime | None
) -> list[Any]:
    filters: list[Any] = [AuditEntry.org_id == DEFAULT_ORG_ID]
    if action is not None:
        filters.append(AuditEntry.action == action)
    if actor is not None:
        try:
            filters.append(AuditEntry.actor_user_id == uuid.UUID(actor))
        except ValueError:
            filters.append(AuditEntry.actor_label.ilike(f"%{actor}%"))
    if from_ is not None:
        filters.append(AuditEntry.created_at >= from_)
    if to is not None:
        filters.append(AuditEntry.created_at < to)
    return filters


async def _iter_export_rows(
    session: AsyncSession, filters: list[Any]
) -> AsyncIterator[AuditEntry]:
    """Keyset-paginated (`created_at`, `id`) descending walk over every
    matching row, `_EXPORT_BATCH_SIZE` at a time - AC1.4's OOM-safety
    requirement. Never a single unbounded `SELECT`."""
    cursor: tuple[datetime, uuid.UUID] | None = None
    while True:
        stmt = select(AuditEntry).where(*filters)
        if cursor is not None:
            cursor_created_at, cursor_id = cursor
            stmt = stmt.where(
                or_(
                    AuditEntry.created_at < cursor_created_at,
                    and_(
                        AuditEntry.created_at == cursor_created_at,
                        AuditEntry.id < cursor_id,
                    ),
                )
            )
        stmt = stmt.order_by(AuditEntry.created_at.desc(), AuditEntry.id.desc()).limit(
            _EXPORT_BATCH_SIZE
        )
        rows = (await session.execute(stmt)).scalars().all()
        if not rows:
            return
        for row in rows:
            yield row
        cursor = (rows[-1].created_at, rows[-1].id)
        if len(rows) < _EXPORT_BATCH_SIZE:
            return


def _export_row_dict(row: AuditEntry, *, include_chain: bool) -> dict[str, Any]:
    exported: dict[str, Any] = {
        "id": str(row.id),
        "actor_user_id": str(row.actor_user_id) if row.actor_user_id else None,
        "actor_label": row.actor_label,
        "action": row.action,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "old_value": row.old_value,
        "new_value": row.new_value,
        "source_ip": str(row.source_ip) if row.source_ip is not None else None,
        "created_at": row.created_at.isoformat(),
    }
    if include_chain:
        # Phase 5 (AC5.2.8) - see module docstring. `row.chain_seq`/
        # `row.prev_hash`/`row.chain_hash` are individually `NULL` for any
        # row written before chaining was enabled (or the org's genesis
        # row, for `prev_hash`) even once `chain_enabled = true` overall -
        # exported as `None`/absent, never a synthesized placeholder.
        exported["chain_seq"] = row.chain_seq
        exported["prev_hash"] = row.prev_hash
        exported["chain_hash"] = row.chain_hash
    return exported


async def _stream_csv(
    session: AsyncSession, filters: list[Any], *, include_chain: bool
) -> AsyncIterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    columns = _CSV_COLUMNS + _CHAIN_CSV_COLUMNS if include_chain else _CSV_COLUMNS
    writer.writerow(columns)
    yield buffer.getvalue()
    async for row in _iter_export_rows(session, filters):
        buffer.seek(0)
        buffer.truncate(0)
        exported = _export_row_dict(row, include_chain=include_chain)
        csv_row = [
            exported["id"],
            exported["actor_user_id"] or "",
            exported["actor_label"],
            exported["action"],
            exported["target_type"],
            exported["target_id"],
            json.dumps(exported["old_value"]) if exported["old_value"] is not None else "",
            json.dumps(exported["new_value"]) if exported["new_value"] is not None else "",
            exported["source_ip"] or "",
            exported["created_at"],
        ]
        if include_chain:
            csv_row.extend(
                [
                    exported["chain_seq"] if exported["chain_seq"] is not None else "",
                    exported["prev_hash"] or "",
                    exported["chain_hash"] or "",
                ]
            )
        writer.writerow(csv_row)
        yield buffer.getvalue()


async def _stream_json(
    session: AsyncSession, filters: list[Any], *, include_chain: bool
) -> AsyncIterator[str]:
    yield "["
    first = True
    async for row in _iter_export_rows(session, filters):
        yield ("" if first else ",") + json.dumps(_export_row_dict(row, include_chain=include_chain))
        first = False
    yield "]"


@router.get("", response_model=None)
async def list_audit_entries_endpoint(
    action: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    format: Literal["csv", "json"] | None = Query(default=None),
    ctx: SessionContext = Depends(require_role("org_admin", "auditor")),
    session: AsyncSession = Depends(get_db_session),
) -> AuditEntriesPageResponse | StreamingResponse:
    filters = _build_filters(action=action, actor=actor, from_=from_, to=to)

    if format in ("csv", "json"):
        # Phase 5 (AC5.2.8) - fetched once, at the top, per the design
        # doc's wiring checklist "5.1 (Ledger, 5.2)" row 5.
        compliance = await get_effective_compliance_settings(session)
        include_chain = compliance.chain_enabled

    if format == "csv":
        return StreamingResponse(
            _stream_csv(session, filters, include_chain=include_chain),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=audit-entries.csv"},
        )
    if format == "json":
        return StreamingResponse(
            _stream_json(session, filters, include_chain=include_chain),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=audit-entries.json"},
        )

    total = (
        await session.execute(select(func.count(AuditEntry.id)).where(*filters))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(AuditEntry)
                .where(*filters)
                .order_by(AuditEntry.created_at.desc(), AuditEntry.id.desc())
                .offset((page - 1) * PAGE_SIZE)
                .limit(PAGE_SIZE)
            )
        )
        .scalars()
        .all()
    )
    return AuditEntriesPageResponse(
        entries=[
            AuditEntryResponse(
                id=row.id,
                actor_user_id=row.actor_user_id,
                actor_label=row.actor_label,
                action=row.action,
                target_type=row.target_type,
                target_id=row.target_id,
                old_value=row.old_value,
                new_value=row.new_value,
                source_ip=str(row.source_ip) if row.source_ip is not None else None,
                created_at=row.created_at,
            )
            for row in rows
        ],
        page=page,
        page_size=PAGE_SIZE,
        total=int(total),
    )
