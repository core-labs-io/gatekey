"""Append-only audit-entry write helper (Phase 2, BD-17).

See `docs/design/phase-2-multi-tenant-governance-design.md` section 7. Every
mutation this phase introduces writes exactly ONE `AuditEntry`, in the SAME
DB transaction as the mutation itself - `write_audit_entry` flushes but
never commits; the call site (route handler) commits, so a failed mutation
never leaves a stray audit row and a failed audit write rolls the mutation
back with it (an audit entry that silently failed to write would be worse
than not having the feature).

`actor_label`/`actor_user_id` are derived once here from either actor shape
(`AdminContext.actor_label` - including the `"system:admin_token"` break-
glass sentinel, A4 - or `SessionContext.display_label`) so call sites never
re-derive that logic independently.

Fixed action vocabulary (design doc section 7 - populates the Audit Log
filter dropdown; do not invent ad hoc action strings outside this list):

    team.create, team.update, team.delete, team.period_config.update,
    team.model_restrictions.update, team.alert_config.update,
    team.member.add, team.member.update, team.member.remove,
    team.budget.reassign, join_request.submit, join_request.approve,
    join_request.reject, user.org_role.update, personal_key.create,
    personal_key.regenerate, personal_key.revoke,
    service_account_key.create, service_account_key.revoke,
    org_settings.update

Phase 3 (Security & Compliance Hardening, DLP/residency/content-classification
track) additions - see `docs/design/phase-3-security-compliance-design.md`
sections 3.3/7.1 and 9.2-9.4:

    dlp_policy.update, dlp_policy.custom_pattern.create,
    dlp_policy.custom_pattern.update, dlp_policy.custom_pattern.delete,
    team.dlp_override.update, dlp.block, residency_rule.update,
    residency_rule.weakened, residency_rule.delete,
    team_residency_rule.update, team_residency_rule.delete,
    residency.hard_block, residency.warn, content_aware_rules.update

`dlp.block`/`residency.hard_block`/`residency.warn` are the one class of
audit entry written from the GATEWAY request path (not an admin mutation) -
`api/v1/gateway/common.py`'s `check_residency()`/`run_dlp_scan()` construct
a lightweight `api.deps.AdminContext` from the authenticated gateway
caller's identity as the `actor` for these (there is no `SessionContext` on
that path - see those functions' docstrings for why these specific writes
are synchronous, not deferred).

Secret hygiene: `old_value`/`new_value` must never contain secret material
(webhook URLs, key plaintext/hashes, tokens) - call sites record
configured-state booleans instead. Phase 3 additionally never records raw
flagged prompt/response content in an audit entry - DLP finding names
(detector/pattern identifiers) only, never the matched substring itself
(that belongs in `dlp_scan_results`, gated by `store_raw_flagged_content`).

Phase 3 (Security & Compliance Hardening, audit gap-closure track) additions
- see `docs/design/phase-3-security-compliance-design.md` sections 1.8/7.1
and 9.6:

    rotation_policy.update, service_account_key.rotate,
    service_account_key.rotate_now, provider_key.rotate,
    provider_key.rotation_policy.update, compliance_settings.update

Phase 3 (Security & Compliance Hardening, CLI-sync backend contract, BD-25)
additions - see `docs/design/phase-3-security-compliance-design.md` section
8.2 and `api/v1/auth_device.py`:

    cli_refresh_credential.create, cli_refresh_credential.revoke

`GET /v1/me/current-key`'s per-fetch rotation reuses the existing
`personal_key.regenerate` action (not a new one) - it is functionally the
same mutation as `POST /v1/keys/{id}/regenerate`, just invoked by a
refresh-credential-authenticated caller instead of a session; the actor is
a lightweight `api.deps.AdminContext` with `actor_label="system:cli_sync"`
(a new sentinel, same shape as `"system:admin_token"`/`"system:scim"`).

Phase 3 (Security & Compliance Hardening, SCIM track, BD-20..24) additions
- see `docs/design/phase-3-security-compliance-design.md` sections 6.1-6.4
and `services/scim.py`:

    scim_config.update, scim_config.rotate_token, scim_user.create,
    scim_user.update, scim_user.deactivate, scim_group.create,
    scim_group.update, scim_group.delete, scim_group.member.add,
    scim_group.member.remove

The deactivation cascade (`services.scim.revoke_scim_deactivated_user_
credentials`, design doc section 6.4) reuses THREE existing actions -
`personal_key.revoke`/`service_account_key.revoke` (functionally the same
state change `DELETE /v1/keys/{id}`/`DELETE /v1/admin/service-accounts/{id}`
already produce, just SCIM-triggered) and `cli_refresh_credential.revoke`
(introduced above by the CLI-sync track) - plus one genuinely new action:
`session.revoke`. Every entry this cascade writes uses the `"system:scim"`
sentinel actor (`services.scim.build_system_scim_actor`, same shape as
`"system:admin_token"`/`"system:cli_sync"` - no `actor_user_id`, since SCIM
pushes have no human session on the request).

`source_ip` (AC1.1/AC1.2) is resolved by the caller from `api.deps.
get_source_ip` (a `Request`-based FastAPI dependency) and passed through
here as an optional keyword - best-effort, `None` when genuinely
unavailable (an internal call with no request context), never blocks the
write. See `api/deps.py`'s `get_source_ip` docstring for the
`GATEKEY_TRUST_PROXY_HEADERS` off-by-default caveat.

Phase 3 (Security & Compliance Hardening, access windows track, BD-16/17/18)
additions - see `docs/design/phase-3-security-compliance-design.md` sections
5.3/9.7:

    access_schedule.update, access_schedule.delete,
    team_access_schedule.update, team_access_schedule.delete,
    service_account_access_schedule.update,
    service_account_access_schedule.delete, holiday_date.create,
    holiday_date.delete, access_schedule.block, emergency_override.grant,
    emergency_override.revoke

`access_schedule.block` is, like `dlp.block`/`residency.hard_block`, written
from the GATEWAY request path (not an admin mutation) -
`api/v1/gateway/common.py`'s `check_access_schedule()` constructs the same
lightweight `api.deps.AdminContext` shape as those two for the `actor` (AC9.6
- the block itself is the auditable event here, not a mutation).

Phase 5 (Differentiators, 5.4 Provider Drift Detector) additions - see
`gatekey/phase-5-technical-design.md` section 5's wiring checklist ("5.2
(Drift Detector, 5.4)" row 5):

    drift.alert_exported, drift_detector.canary_model_setting.update

`drift.alert_exported` is written by `api/v1/admin/drift_detector.py`'s
export endpoint when an Org Admin/Auditor exports a `DriftAlert` to the
audit log (AC5.2.10/AC5.4.11) - `target_type="drift_alert"`,
`target_id=str(alert.id)`. `drift_detector.canary_model_setting.update` is
written by that same router's per-model canary enable/disable endpoint
(AC5.4.11, Org Admin only) - `target_type="canary_model_setting"`,
`target_id=<model string>`.

Phase 5 (Differentiators, 5.1 Shadow AI Discovery) additions - see
`gatekey/phase-5-technical-design.md` section 5's wiring checklist ("5.5
(Shadow AI, 5.1)") and `services/shadow_ai.py`:

    shadow_ai_config.update, shadow_ai_config.rotate_token,
    known_ai_tool_hostname.create, known_ai_tool_hostname.update,
    known_ai_tool_hostname.delete

Every one of these is written by `api/v1/admin/shadow_ai.py` (Org Admin
only for every mutation this router exposes) - `target_type`/`target_id`
are `"shadow_ai_config"`/`str(org_id)` for the first two,
`"known_ai_tool_hostname"`/`<hostname string>` for the other three.
`shadow_ai_ingest_events` themselves (written by `POST
/v1/admin/shadow-ai/ingest`, the dedicated-ingest-token-authenticated
route) do NOT each get an audit entry - unlike `dlp.block`/
`residency.hard_block`/`access_schedule.block`, an ingested shadow-AI
detection event is not itself an admin mutation or a gateway-path block
decision, and audit-logging every ingested event (a potentially
high-volume, external-tool-driven feed) would make `audit_entries` a
second, redundant copy of `shadow_ai_ingest_events` - the report endpoint
(`GET /v1/admin/shadow-ai/report`) is the intended way to review this
data, not the audit log.

Phase 5 (Differentiators, 5.2 Hash-Chained Audit Ledger) - hash-chain
computation
------------------------------------------------------------------------
See `db/models/audit_entry.py`'s "Hash-chain columns" note and
`gatekey/phase-5-technical-design.md` section 2.1 for the full write-path
design. `write_audit_entry` - still the sole INSERT path this module's own
opening paragraph describes - now branches on
`compliance_settings.chain_enabled` (read via `services.compliance_settings.
get_effective_compliance_settings`, an existing cheap indexed read):

- `chain_enabled = false` (the default, and every pre-Phase-5 org): the
  INSERT is byte-for-byte what it always was - `chain_hash`/`prev_hash`/
  `chain_seq` stay `NULL`.
- `chain_enabled = true`: the tail-read + insert is serialized per `org_id`
  by taking `SELECT ... FOR UPDATE` on the org's (guaranteed-to-exist, see
  `services/compliance_settings.py`) `compliance_settings` row for the rest
  of the CALLER's transaction (released whenever the call site's own
  `session.commit()`/`rollback()` runs - `write_audit_entry` itself still
  only flushes, never commits) - this is the ADR-5-style "lock the parent
  config row before writing a dependent child" pattern already established
  by `services/team_budget.py::_lock_team`, applied here to a new problem
  shape (see the design doc's "Key Decision" for why `compliance_settings`,
  not the `audit_entries` tail row itself, and not a raw
  `pg_advisory_xact_lock`). `services.audit_chain.compute_chain_hash`
  computes the actual hash - this module never reimplements that formula.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.db.models.audit_entry import AuditEntry
from gatekey.db.models.compliance_settings import ComplianceSettings
from gatekey.services.audit_chain import compute_chain_hash
from gatekey.services.compliance_settings import get_effective_compliance_settings
from gatekey.services.sessions import SessionContext

if TYPE_CHECKING:
    from gatekey.api.deps import AdminContext


def _jsonable(value: Any) -> Any:
    """Convert Decimal/UUID/datetime/enum values (the shapes budget/team
    state naturally carries) into JSONB-serializable primitives. Decimals
    become strings, not floats - no precision loss in the durable record."""
    if isinstance(value, dict):
        return {key: _jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (Decimal, uuid.UUID)):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


async def write_audit_entry(
    session: AsyncSession,
    *,
    actor: "AdminContext | SessionContext",
    action: str,
    target_type: str,
    target_id: str,
    old_value: dict[str, Any] | None,
    new_value: dict[str, Any] | None,
    source_ip: str | None = None,
) -> None:
    """INSERT one `AuditEntry` on the caller's session. Flushes, does NOT
    commit - same-transaction discipline (module docstring). Call sites add
    this immediately before their own `session.commit()`.

    `source_ip` (Phase 3, AC1.1/AC1.2) is optional and best-effort - `None`
    on any call site with no request context (e.g. the scheduler loop, or a
    Phase 2 call site not yet threaded through) simply omits it; the write
    itself never fails because of a missing IP.

    Phase 5 (5.2): when the org's hash chain is enabled, this also computes
    and persists `chain_hash`/`prev_hash`/`chain_seq` under a per-org lock -
    see module docstring "Phase 5 ... hash-chain computation"."""
    if isinstance(actor, SessionContext):
        actor_user_id: uuid.UUID | None = actor.user_id
        actor_label = actor.display_label
        org_id = actor.org_id
    else:
        actor_user_id = actor.actor_user_id
        actor_label = actor.actor_label
        org_id = actor.org_id

    old_value_jsonable = _jsonable(old_value) if old_value is not None else None
    new_value_jsonable = _jsonable(new_value) if new_value is not None else None

    compliance = await get_effective_compliance_settings(session)
    if not compliance.chain_enabled:
        session.add(
            AuditEntry(
                org_id=org_id,
                actor_user_id=actor_user_id,
                actor_label=actor_label,
                action=action,
                target_type=target_type,
                target_id=target_id,
                old_value=old_value_jsonable,
                new_value=new_value_jsonable,
                source_ip=source_ip,
            )
        )
        await session.flush()
        return

    # Phase 5 (5.2, AC5.2.3): serialize the tail-read + insert per org -
    # lock the guaranteed-to-exist `compliance_settings` row, not the
    # `audit_entries` tail (design doc section 2.1's "Key Decision" - a
    # tail-row lock can't bootstrap a true chain genesis, and this codebase
    # has no existing advisory-lock precedent).
    await session.execute(
        select(ComplianceSettings.org_id)
        .where(ComplianceSettings.org_id == org_id)
        .with_for_update()
    )
    tail = (
        await session.execute(
            select(AuditEntry.chain_hash, AuditEntry.chain_seq)
            .where(AuditEntry.org_id == org_id, AuditEntry.chain_seq.is_not(None))
            .order_by(AuditEntry.chain_seq.desc())
            .limit(1)
        )
    ).one_or_none()
    prev_hash_for_hash = tail.chain_hash if tail is not None else ""
    prev_hash_to_store = tail.chain_hash if tail is not None else None
    chain_seq = (tail.chain_seq + 1) if tail is not None else 1

    entry_id = uuid.uuid4()
    created_at = datetime.now(timezone.utc)
    chain_hash = compute_chain_hash(
        prev_hash_for_hash,
        entry_id=entry_id,
        org_id=org_id,
        actor_label=actor_label,
        action=action,
        target_type=target_type,
        target_id=target_id,
        old_value=old_value_jsonable,
        new_value=new_value_jsonable,
        source_ip=source_ip,
        created_at=created_at,
    )
    session.add(
        AuditEntry(
            id=entry_id,
            org_id=org_id,
            actor_user_id=actor_user_id,
            actor_label=actor_label,
            action=action,
            target_type=target_type,
            target_id=target_id,
            old_value=old_value_jsonable,
            new_value=new_value_jsonable,
            source_ip=source_ip,
            created_at=created_at,
            chain_hash=chain_hash,
            prev_hash=prev_hash_to_store,
            chain_seq=chain_seq,
        )
    )
    await session.flush()
