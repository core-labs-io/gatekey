"""Admin endpoints for provider-key backup groups (Phase 4, Reliability &
Cost Efficiency - AC4.1.1/AC4.1.2).

Structural template: `api/v1/admin/providers.py` (`require_admin` router-
level dependency, `GatekeyError`-based error responses, no `org_id` param -
this slice only ever operates against `constants.DEFAULT_ORG_ID`, see that
module's docstring).

All the actual DB plumbing (`create_backup_group`/`list_backup_groups`/
`get_backup_group`/`delete_backup_group`/`get_keys_in_backup_group`) already
exists in `services/provider_keys.py` (Phase 4 backup-group support) - this
router is purely the HTTP-facing wiring the audit found missing.

`keys` field design (judgment call - flagged, not silently guessed)
---------------------------------------------------------------------
The `backup_groups` table (migration `0025`) has no `keys` column of its
own - membership is inverted, tracked on each `ProviderKey.backup_group_id`
(migration `0030`). `POST /v1/admin/backup-groups`'s `keys` request field is
therefore treated as a list of `ProviderKey.label` values to best-effort
associate: any label that already matches an existing key for this org is
immediately linked (`backup_group_id` set), enabling real failover routing
right away; a label with no matching key yet is not an error (a backup
group may reasonably be named/declared before every member key exists) -
the *submitted* label list is persisted (JSON-encoded in the otherwise-
unused `description` column, since schema is frozen this task and no
dedicated column exists) as the group's declared membership, and is what
`GET`/`POST` responses echo back unless real associated keys exist, in
which case their actual labels take precedence (so the API surfaces ground
truth once it exists, not a stale declared list).
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Body, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import require_admin
from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.backup_group import BackupGroup
from gatekey.db.models.provider_key import ProviderKey
from gatekey.db.session import get_db_session
from gatekey.errors import NotFoundError
from gatekey.services import provider_keys as provider_keys_service

router = APIRouter(
    prefix="/v1/admin",
    tags=["admin", "backup_groups"],
    dependencies=[Depends(require_admin)],
)


class BackupGroupCreate(BaseModel):
    name: str
    keys: list[str] = []


class BackupGroupResponse(BaseModel):
    id: uuid.UUID
    name: str
    keys: list[str]


def _decode_declared_keys(description: str | None) -> list[str]:
    if not description:
        return []
    try:
        decoded = json.loads(description)
    except (ValueError, TypeError):
        return []
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        return []
    return decoded


async def _resolve_keys_field(session: AsyncSession, group: BackupGroup) -> list[str]:
    """Actual associated key labels, if any exist yet; else the declared
    (submitted-at-creation) label list - see module docstring."""
    associated = await provider_keys_service.get_keys_in_backup_group(session, group.id)
    if associated:
        return [key.label for key in associated]
    return _decode_declared_keys(group.description)


async def _to_response(session: AsyncSession, group: BackupGroup) -> BackupGroupResponse:
    return BackupGroupResponse(
        id=group.id, name=group.name, keys=await _resolve_keys_field(session, group)
    )


@router.post("/backup-groups", response_model=BackupGroupResponse, status_code=201)
async def create_backup_group_endpoint(
    payload: BackupGroupCreate = Body(...),
    session: AsyncSession = Depends(get_db_session),
) -> BackupGroupResponse:
    group = await provider_keys_service.create_backup_group(
        session,
        org_id=DEFAULT_ORG_ID,
        name=payload.name,
        description=json.dumps(payload.keys),
    )

    # Best-effort association - see module docstring. Labels with no
    # matching key yet are silently skipped (not an error). `label` is only
    # unique per `(org_id, provider)` (`uq_provider_keys_org_id_provider_
    # label`), NOT globally - two different providers' keys can legitimately
    # share a label (e.g. both openai and anthropic keys labeled "primary"),
    # and AC4.1.2 explicitly allows a backup group to span providers - so
    # every matching row is associated, not just one (`.scalars().all()`,
    # never `.scalar()`, which would raise `MultipleResultsFound` here).
    for label in payload.keys:
        matching_keys = (
            await session.execute(
                select(ProviderKey).where(
                    ProviderKey.org_id == DEFAULT_ORG_ID, ProviderKey.label == label
                )
            )
        ).scalars().all()
        for existing_key in matching_keys:
            if existing_key.backup_group_id != group.id:
                await provider_keys_service.set_backup_group_for_key(session, existing_key.id, group.id)

    return BackupGroupResponse(id=group.id, name=group.name, keys=payload.keys)


@router.get("/backup-groups", response_model=list[BackupGroupResponse])
async def list_backup_groups_endpoint(
    session: AsyncSession = Depends(get_db_session),
) -> list[BackupGroupResponse]:
    groups = await provider_keys_service.list_backup_groups(session, DEFAULT_ORG_ID)
    return [await _to_response(session, group) for group in groups]


@router.delete("/backup-groups/{group_id}", status_code=204)
async def delete_backup_group_endpoint(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    """204 on success. `ON DELETE SET NULL` (migration `0030`) already
    nulls out `backup_group_id` on every member `ProviderKey` row - no app-
    layer cleanup needed for that half."""
    deleted = await provider_keys_service.delete_backup_group(session, group_id)
    if not deleted:
        raise NotFoundError(f"No backup group found with id '{group_id}'.")
    return Response(status_code=204)
