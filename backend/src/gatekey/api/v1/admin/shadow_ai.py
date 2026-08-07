"""Admin endpoints for Shadow AI Discovery config/report/token-gen/hostname
CRUD (Phase 5 - Differentiators, 5.1 Shadow AI Discovery). See
`gatekey/phase-5-product-spec.md` AC5.1.2/AC5.1.4-AC5.1.9 and
`gatekey/phase-5-technical-design.md` section 3.1's API-contract table.

**Deliberately a SEPARATE router from `api/v1/shadow_ai_ingest.py`** (design
doc section 2.5 / wiring checklist row 4) - every route here uses this
codebase's normal session-cookie/break-glass-token RBAC dependencies
(`require_role`, `require_admin_or_auditor`, `get_privileged_session`), NEVER
`require_shadow_ai_ingest_token` - an admin session must never be able to
call the ingestion endpoint, and the ingestion token must never be able to
call any route in this router (see `api.deps.require_shadow_ai_ingest_token`'s
docstring for the full non-overlap proof).

RBAC (AC5.1.6, design doc section 3.1's table):
  - Org Admin: full org-wide view + all config/CRUD/token-gen.
  - Auditor: full org-wide READ-ONLY (`require_admin_or_auditor`).
  - Team Lead: read-only, `GET /report` ONLY, scoped to their own led
    team(s) - `require_role`/`require_admin_or_auditor` are both too
    narrow/too-wide for this one endpoint (see `_resolve_report_scope`
    below), so it uses `get_privileged_session` directly and branches.
  - Member: no access to anything in this router.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import (
    get_key_provider,
    get_privileged_session,
    get_source_ip,
    require_admin_or_auditor,
    require_role,
)
from gatekey.db.session import get_db_session
from gatekey.errors import ForbiddenError
from gatekey.schemas.shadow_ai import (
    KnownAiToolHostnameCreateRequest,
    KnownAiToolHostnameResponse,
    KnownAiToolHostnameUpdateRequest,
    ShadowAiConfigPutRequest,
    ShadowAiConfigResponse,
    ShadowAiReportRowResponse,
    ShadowAiTokenRotateResponse,
)
from gatekey.services.audit import write_audit_entry
from gatekey.services.encryption import KeyProvider
from gatekey.services.sessions import SessionContext
from gatekey.services.shadow_ai import (
    KnownAiToolHostnameNotFoundError,
    add_known_ai_tool_hostname,
    get_known_ai_tool_hostname,
    get_shadow_ai_ingest_config,
    get_shadow_ai_report,
    list_known_ai_tool_hostnames,
    remove_known_ai_tool_hostname,
    rotate_shadow_ai_ingest_token,
    set_shadow_ai_config,
    shadow_ai_webhook_configured,
    update_known_ai_tool_hostname,
)
from gatekey.services.teams import list_team_ids_led_by_user

router = APIRouter(prefix="/v1/admin/shadow-ai", tags=["admin", "shadow-ai"])


# ---------------------------------------------------------------------------
# Config (AC5.1.4/AC5.1.7/AC5.1.10).
# ---------------------------------------------------------------------------


def _to_config_response(row) -> ShadowAiConfigResponse:
    if row is None:
        return ShadowAiConfigResponse(
            detection_source="sase_log",
            enforcement_mode="detect_only",
            webhook_configured=False,
            shadow_ai_retention_days=90,
            ingestion_configured=False,
            token_created_at=None,
        )
    return ShadowAiConfigResponse(
        detection_source=row.detection_source,
        enforcement_mode=row.enforcement_mode,
        webhook_configured=shadow_ai_webhook_configured(row),
        shadow_ai_retention_days=row.shadow_ai_retention_days,
        ingestion_configured=row.ingest_token_hash is not None,
        token_created_at=row.token_created_at,
    )


@router.get("/config", response_model=ShadowAiConfigResponse)
async def get_shadow_ai_config_endpoint(
    ctx=Depends(require_admin_or_auditor),
    session: AsyncSession = Depends(get_db_session),
) -> ShadowAiConfigResponse:
    row = await get_shadow_ai_ingest_config(session)
    return _to_config_response(row)


@router.put("/config", response_model=ShadowAiConfigResponse)
async def put_shadow_ai_config_endpoint(
    payload: ShadowAiConfigPutRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
    key_provider: KeyProvider = Depends(get_key_provider),
) -> ShadowAiConfigResponse:
    """AC5.1.7's confirm-required gate / AC5.1.10's retention window are both
    enforced inside `set_shadow_ai_config` - see that function's docstring.
    Audit-before-because-the-service-call-commits (same pattern
    `api/v1/admin/scim_config.py`'s PUT handler uses). The audit record
    carries configured-state booleans only - never the webhook URL in any
    form (design doc section 7 secret hygiene, same discipline `api/v1/
    teams.py`'s alert-config PUT handler already follows)."""
    old_row = await get_shadow_ai_ingest_config(session)
    await write_audit_entry(
        session,
        actor=ctx,
        action="shadow_ai_config.update",
        target_type="shadow_ai_config",
        target_id=str(ctx.org_id),
        old_value={
            "detection_source": old_row.detection_source if old_row else None,
            "enforcement_mode": old_row.enforcement_mode if old_row else "detect_only",
            "webhook_configured": shadow_ai_webhook_configured(old_row),
            "shadow_ai_retention_days": old_row.shadow_ai_retention_days if old_row else 90,
        },
        new_value={
            "detection_source": payload.detection_source,
            "enforcement_mode": payload.enforcement_mode,
            "webhook_configured": payload.webhook_url is not None,
            "shadow_ai_retention_days": payload.shadow_ai_retention_days,
        },
        source_ip=source_ip,
    )
    row = await set_shadow_ai_config(
        session,
        detection_source=payload.detection_source,
        enforcement_mode=payload.enforcement_mode,
        webhook_url=payload.webhook_url,
        shadow_ai_retention_days=payload.shadow_ai_retention_days,
        confirm=payload.confirm,
        key_provider=key_provider,
    )
    return _to_config_response(row)


@router.post("/ingest-token", response_model=ShadowAiTokenRotateResponse)
async def rotate_shadow_ai_ingest_token_endpoint(
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> ShadowAiTokenRotateResponse:
    """AC5.1.3/AC5.1.4: one-time-reveal generation/rotation - the opt-in
    gate. Rotation immediately invalidates the prior token - no overlap
    window (same shape as `services.scim.rotate_scim_token`)."""
    await write_audit_entry(
        session,
        actor=ctx,
        action="shadow_ai_config.rotate_token",
        target_type="shadow_ai_config",
        target_id=str(ctx.org_id),
        old_value=None,
        new_value=None,
        source_ip=source_ip,
    )
    row, token = await rotate_shadow_ai_ingest_token(session)
    return ShadowAiTokenRotateResponse(token=token, token_created_at=row.token_created_at)


# ---------------------------------------------------------------------------
# Curated hostname allowlist CRUD (AC5.1.2).
# ---------------------------------------------------------------------------


@router.get("/known-hostnames", response_model=list[KnownAiToolHostnameResponse])
async def list_known_ai_tool_hostnames_endpoint(
    ctx=Depends(require_admin_or_auditor),
    session: AsyncSession = Depends(get_db_session),
) -> list[KnownAiToolHostnameResponse]:
    rows = await list_known_ai_tool_hostnames(session)
    return [KnownAiToolHostnameResponse.model_validate(row) for row in rows]


@router.post("/known-hostnames", response_model=KnownAiToolHostnameResponse, status_code=201)
async def add_known_ai_tool_hostname_endpoint(
    payload: KnownAiToolHostnameCreateRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> KnownAiToolHostnameResponse:
    await write_audit_entry(
        session,
        actor=ctx,
        action="known_ai_tool_hostname.create",
        target_type="known_ai_tool_hostname",
        target_id=payload.hostname,
        old_value=None,
        new_value={"tool_label": payload.tool_label, "enabled": payload.enabled},
        source_ip=source_ip,
    )
    row = await add_known_ai_tool_hostname(
        session, hostname=payload.hostname, tool_label=payload.tool_label, enabled=payload.enabled
    )
    return KnownAiToolHostnameResponse.model_validate(row)


@router.put("/known-hostnames/{hostname}", response_model=KnownAiToolHostnameResponse)
async def update_known_ai_tool_hostname_endpoint(
    hostname: str,
    payload: KnownAiToolHostnameUpdateRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> KnownAiToolHostnameResponse:
    existing = await get_known_ai_tool_hostname(session, hostname)
    if existing is None:
        raise KnownAiToolHostnameNotFoundError(hostname)
    await write_audit_entry(
        session,
        actor=ctx,
        action="known_ai_tool_hostname.update",
        target_type="known_ai_tool_hostname",
        target_id=hostname,
        old_value={"tool_label": existing.tool_label, "enabled": existing.enabled},
        new_value=payload.model_dump(exclude_unset=True),
        source_ip=source_ip,
    )
    row = await update_known_ai_tool_hostname(
        session, hostname, tool_label=payload.tool_label, enabled=payload.enabled
    )
    return KnownAiToolHostnameResponse.model_validate(row)


@router.delete("/known-hostnames/{hostname}", status_code=204)
async def remove_known_ai_tool_hostname_endpoint(
    hostname: str,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> None:
    existing = await get_known_ai_tool_hostname(session, hostname)
    if existing is None:
        raise KnownAiToolHostnameNotFoundError(hostname)
    await write_audit_entry(
        session,
        actor=ctx,
        action="known_ai_tool_hostname.delete",
        target_type="known_ai_tool_hostname",
        target_id=hostname,
        old_value={"tool_label": existing.tool_label, "enabled": existing.enabled},
        new_value=None,
        source_ip=source_ip,
    )
    await remove_known_ai_tool_hostname(session, hostname)


# ---------------------------------------------------------------------------
# Report (AC5.1.5/AC5.1.6/AC5.1.8).
# ---------------------------------------------------------------------------


async def _resolve_report_scope(
    session: AsyncSession, ctx: SessionContext, requested_team_id: uuid.UUID | None
) -> frozenset[uuid.UUID] | None:
    """RBAC + server-side team-scope resolution for `GET /report` (AC5.1.6,
    design doc wiring checklist "5.5 (Shadow AI, 5.1)" row 6).

    Org Admin / Auditor: org-wide - `requested_team_id` (if any) is honored
    as a plain optional filter, returned as a one-element set.

    Team Lead (`org_role IS NULL` but leads >=1 team): FORCED to their own
    led team(s), never trusting a client-supplied `team_id` beyond validating
    it's actually one of theirs - a `team_id` for a team they do NOT lead is
    rejected (403), not silently ignored or silently widened.

    Everyone else (Member, or a `team_lead`-role-nowhere caller): 403 - no
    access to this report at all (AC5.1.6).
    """
    if ctx.org_role in ("org_admin", "auditor"):
        return frozenset({requested_team_id}) if requested_team_id is not None else None

    if ctx.user_id is None:
        raise ForbiddenError("You do not have access to the Shadow AI report.")
    led_team_ids = await list_team_ids_led_by_user(session, ctx.user_id)
    if not led_team_ids:
        raise ForbiddenError("You do not have access to the Shadow AI report.")
    if requested_team_id is not None:
        if requested_team_id not in led_team_ids:
            raise ForbiddenError("You do not have access to the Shadow AI report.")
        return frozenset({requested_team_id})
    return led_team_ids


@router.get("/report", response_model=list[ShadowAiReportRowResponse])
async def get_shadow_ai_report_endpoint(
    team_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    ctx: SessionContext = Depends(get_privileged_session),
    session: AsyncSession = Depends(get_db_session),
) -> list[ShadowAiReportRowResponse]:
    team_ids = await _resolve_report_scope(session, ctx, team_id)
    rows = await get_shadow_ai_report(session, team_ids=team_ids, since=since, until=until)
    return [
        ShadowAiReportRowResponse(
            user_identifier=row.user_identifier,
            matched_user_id=row.matched_user_id,
            linked=row.matched_user_id is not None,
            tool_label=row.tool_label,
            destination_host=row.destination_host,
            frequency_per_week=row.frequency_per_week,
            last_seen=row.last_seen,
            repeat_violator=row.repeat_violator,
        )
        for row in rows
    ]
