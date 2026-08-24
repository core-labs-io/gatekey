"""Provider Drift Detector admin endpoints (Phase 5 - Differentiators, 5.4).
See `gatekey/phase-5-product-spec.md` AC5.4.7/AC5.4.11 and
`gatekey/phase-5-technical-design.md` section 3.1.

RBAC per AC5.4.11: Org Admin configures per-model canary enable/disable
(`PUT /v1/admin/drift-detector/models/{model}`, `require_role("org_admin")`);
Org Admin + Auditor view alerts/history/status/export
(`require_admin_or_auditor` - compliance-relevant, same posture as the
Audit Log's Auditor read access).

Read endpoints query `services.drift_detector`'s tables directly (a thin,
read-only admin surface) - `services/drift_detector.py` itself is imported
ONLY here and from `services/scheduler.py`'s tick, per the design doc's
wiring checklist "5.2 (Drift Detector, 5.4)" row 3.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import AdminContext, get_source_ip, require_admin_or_auditor, require_role
from gatekey.db.models.canary_baseline import CanaryBaseline
from gatekey.db.models.canary_model_setting import CanaryModelSetting
from gatekey.db.models.canary_prompt import CanaryPrompt
from gatekey.db.models.canary_run import CanaryRun
from gatekey.db.models.drift_alert import DriftAlert
from gatekey.db.session import get_db_session
from gatekey.errors import NotFoundError
from gatekey.services.audit import write_audit_entry
from gatekey.services.sessions import SessionContext

router = APIRouter(prefix="/v1/admin/drift-detector", tags=["admin", "drift-detector"])

_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000


# ---------------------------------------------------------------------------
# Canary prompts - read-only (AC5.4.1: fixed, code/migration-seeded, no
# admin-editable authoring in v1).
# ---------------------------------------------------------------------------


class CanaryPromptResponse(BaseModel):
    id: uuid.UUID
    prompt_text: str
    label: str
    max_tokens: int
    enabled: bool


@router.get("/canary-prompts", response_model=list[CanaryPromptResponse])
async def list_canary_prompts_endpoint(
    ctx: AdminContext = Depends(require_admin_or_auditor),
    session: AsyncSession = Depends(get_db_session),
) -> list[CanaryPromptResponse]:
    rows = (await session.execute(select(CanaryPrompt).order_by(CanaryPrompt.label))).scalars().all()
    return [
        CanaryPromptResponse(
            id=row.id,
            prompt_text=row.prompt_text,
            label=row.label,
            max_tokens=row.max_tokens,
            enabled=row.enabled,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Per-model status/trend table (AC5.4.11).
# ---------------------------------------------------------------------------


class DriftModelStatusResponse(BaseModel):
    model: str
    canary_enabled: bool
    last_run_at: datetime | None
    baselines_established: int
    open_alerts_count: int


@router.get("/status", response_model=list[DriftModelStatusResponse])
async def get_drift_status_endpoint(
    ctx: AdminContext = Depends(require_admin_or_auditor),
    session: AsyncSession = Depends(get_db_session),
) -> list[DriftModelStatusResponse]:
    tested_models = set(
        (await session.execute(select(CanaryRun.model).distinct())).scalars().all()
    )
    configured_models = set(
        (await session.execute(select(CanaryModelSetting.model))).scalars().all()
    )
    all_models = sorted(tested_models | configured_models)

    settings_by_model = {
        row.model: row.enabled
        for row in (await session.execute(select(CanaryModelSetting))).scalars().all()
    }
    last_run_by_model: dict[str, datetime] = {
        model: run_at
        for model, run_at in (
            await session.execute(
                select(CanaryRun.model, func.max(CanaryRun.run_at)).group_by(CanaryRun.model)
            )
        ).all()
    }
    baseline_counts_by_model: dict[str, int] = {
        model: count
        for model, count in (
            await session.execute(
                select(CanaryBaseline.model, func.count()).group_by(CanaryBaseline.model)
            )
        ).all()
    }
    open_alert_counts_by_model: dict[str, int] = {
        model: count
        for model, count in (
            await session.execute(
                select(DriftAlert.model, func.count())
                .where(DriftAlert.status == "open")
                .group_by(DriftAlert.model)
            )
        ).all()
    }

    return [
        DriftModelStatusResponse(
            model=model,
            # Absence of a row means enabled - same convention
            # `services.drift_detector._filter_canary_enabled_models` uses.
            canary_enabled=settings_by_model.get(model, True),
            last_run_at=last_run_by_model.get(model),
            baselines_established=baseline_counts_by_model.get(model, 0),
            open_alerts_count=open_alert_counts_by_model.get(model, 0),
        )
        for model in all_models
    ]


# ---------------------------------------------------------------------------
# Alerts - list + export-to-audit-log.
# ---------------------------------------------------------------------------


class DriftAlertResponse(BaseModel):
    id: uuid.UUID
    model: str
    metric: str
    baseline_value: Decimal
    observed_value: Decimal
    delta_pct: Decimal
    detected_at: datetime
    status: str
    message: str


def _alert_message(row: DriftAlert) -> str:
    """AC5.4.7/ui doc section 12.2: plain-language text naming the metric
    and the percentage delta - never a bare number with no context."""
    direction = "increased" if row.delta_pct >= 0 else "decreased"
    return (
        f"{row.model}'s {row.metric.replace('_', ' ')} {direction} by "
        f"{abs(row.delta_pct):.2f}% versus its established baseline "
        f"({row.baseline_value} -> {row.observed_value})."
    )


@router.get("/alerts", response_model=list[DriftAlertResponse])
async def list_drift_alerts_endpoint(
    model: str | None = Query(default=None),
    status: str | None = Query(default=None),
    metric: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    ctx: AdminContext = Depends(require_admin_or_auditor),
    session: AsyncSession = Depends(get_db_session),
) -> list[DriftAlertResponse]:
    filters = []
    if model is not None:
        filters.append(DriftAlert.model == model)
    if status is not None:
        filters.append(DriftAlert.status == status)
    if metric is not None:
        filters.append(DriftAlert.metric == metric)

    rows = (
        (
            await session.execute(
                select(DriftAlert)
                .where(*filters)
                .order_by(DriftAlert.detected_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        DriftAlertResponse(
            id=row.id,
            model=row.model,
            metric=row.metric,
            baseline_value=row.baseline_value,
            observed_value=row.observed_value,
            delta_pct=row.delta_pct,
            detected_at=row.detected_at,
            status=row.status,
            message=_alert_message(row),
        )
        for row in rows
    ]


@router.post("/alerts/{alert_id}/export", response_model=DriftAlertResponse)
async def export_drift_alert_endpoint(
    alert_id: uuid.UUID,
    ctx: AdminContext = Depends(require_admin_or_auditor),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> DriftAlertResponse:
    """AC5.2.10/AC5.4.11: writes a real `AuditEntry`
    (`action="drift.alert_exported"`) and sets `drift_alerts.status =
    "exported_to_audit"` - design doc wiring checklist "5.2 (Drift Detector,
    5.4)" row 5. Idempotent-safe to call again on an already-exported alert
    (writes another audit entry, same as re-saving any other admin config -
    no special-cased no-op)."""
    row = (
        await session.execute(select(DriftAlert).where(DriftAlert.id == alert_id))
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError("Drift alert not found.")

    await write_audit_entry(
        session,
        actor=ctx,
        action="drift.alert_exported",
        target_type="drift_alert",
        target_id=str(row.id),
        old_value={"status": row.status},
        new_value={"status": "exported_to_audit"},
        source_ip=source_ip,
    )
    row.status = "exported_to_audit"
    await session.commit()
    return DriftAlertResponse(
        id=row.id,
        model=row.model,
        metric=row.metric,
        baseline_value=row.baseline_value,
        observed_value=row.observed_value,
        delta_pct=row.delta_pct,
        detected_at=row.detected_at,
        status=row.status,
        message=_alert_message(row),
    )


# ---------------------------------------------------------------------------
# Canary run history (AC5.4.11: "View canary history").
# ---------------------------------------------------------------------------


class CanaryRunResponse(BaseModel):
    id: uuid.UUID
    model: str
    prompt_id: uuid.UUID
    run_at: datetime
    output_text: str
    latency_ms: int
    refusal_detected: bool
    similarity_score_vs_baseline: Decimal | None
    cost_usd: Decimal


@router.get("/canary-history", response_model=list[CanaryRunResponse])
async def list_canary_history_endpoint(
    model: str | None = Query(default=None),
    prompt_id: uuid.UUID | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    ctx: AdminContext = Depends(require_admin_or_auditor),
    session: AsyncSession = Depends(get_db_session),
) -> list[CanaryRunResponse]:
    filters = []
    if model is not None:
        filters.append(CanaryRun.model == model)
    if prompt_id is not None:
        filters.append(CanaryRun.prompt_id == prompt_id)

    rows = (
        (
            await session.execute(
                select(CanaryRun).where(*filters).order_by(CanaryRun.run_at.desc()).limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        CanaryRunResponse(
            id=row.id,
            model=row.model,
            prompt_id=row.prompt_id,
            run_at=row.run_at,
            output_text=row.output_text,
            latency_ms=row.latency_ms,
            refusal_detected=row.refusal_detected,
            similarity_score_vs_baseline=row.similarity_score_vs_baseline,
            cost_usd=row.cost_usd,
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Per-model canary enable/disable (AC5.4.11 - Org Admin only; thresholds
# stay global/fixed, see `services/drift_detector.py`'s module docstring).
# ---------------------------------------------------------------------------


class CanaryModelSettingPutRequest(BaseModel):
    enabled: bool


class CanaryModelSettingResponse(BaseModel):
    model: str
    enabled: bool


@router.put("/models/{model}", response_model=CanaryModelSettingResponse)
async def set_canary_model_setting_endpoint(
    model: str,
    payload: CanaryModelSettingPutRequest,
    ctx: SessionContext = Depends(require_role("org_admin")),
    session: AsyncSession = Depends(get_db_session),
    source_ip: str | None = Depends(get_source_ip),
) -> CanaryModelSettingResponse:
    existing = (
        await session.execute(select(CanaryModelSetting).where(CanaryModelSetting.model == model))
    ).scalar_one_or_none()
    old_enabled = existing.enabled if existing is not None else True

    await write_audit_entry(
        session,
        actor=ctx,
        action="drift_detector.canary_model_setting.update",
        target_type="canary_model_setting",
        target_id=model,
        old_value={"enabled": old_enabled},
        new_value={"enabled": payload.enabled},
        source_ip=source_ip,
    )

    insert_stmt = postgresql.insert(CanaryModelSetting).values(model=model, enabled=payload.enabled)
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[CanaryModelSetting.model],
        set_={"enabled": insert_stmt.excluded.enabled},
    ).returning(CanaryModelSetting)
    row = (await session.execute(upsert_stmt)).scalar_one()
    await session.commit()
    return CanaryModelSettingResponse(model=row.model, enabled=row.enabled)
