"""Admin endpoints for service-account key management (Phase 1.2, section 4).

All endpoints require `require_admin` (the Phase 1.1 single-shared-admin-
token stub - see `api/deps.py`), i.e. the *human admin* trust boundary.
This is a deliberately separate, non-overlapping trust boundary from
`require_service_account` (the per-app credential these endpoints manage) -
an admin can mint/revoke service-account keys, but a service-account key
itself grants no access to this router. See design doc section 4.

None of these endpoints accept an `org_id` - see `constants.DEFAULT_ORG_ID`
for why this slice only ever operates against the single seeded default
org.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.api.deps import AdminContext, get_source_ip, require_admin
from gatekey.db.session import get_db_session
from gatekey.errors import NotFoundError
from gatekey.schemas.service_account_key import (
    ServiceAccountKeyCreateRequest,
    ServiceAccountKeyCreateResponse,
    ServiceAccountKeyResponse,
)
from gatekey.services.audit import write_audit_entry
from gatekey.services.service_accounts import (
    TeamMembershipNotFoundError,
    UserNotFoundError,
    create_service_account,
    get_service_account,
    list_service_accounts,
    revoke_service_account,
)

router = APIRouter(
    prefix="/v1/admin/service-accounts",
    tags=["admin", "service-accounts"],
    dependencies=[Depends(require_admin)],
)


@router.post("", response_model=ServiceAccountKeyCreateResponse, status_code=201)
async def create_service_account_key(
    payload: ServiceAccountKeyCreateRequest,
    session: AsyncSession = Depends(get_db_session),
    admin_ctx: AdminContext = Depends(require_admin),
    source_ip: str | None = Depends(get_source_ip),
) -> ServiceAccountKeyCreateResponse:
    """Create a new service-account key.

    The plaintext `secret` is returned in this response body only - it is
    never persisted and this is the only endpoint that will ever return it.
    Callers must store it themselves; it cannot be retrieved again.

    Phase 2 (design doc section 7): now also writes a
    `service_account_key.create` audit entry. Known, accepted deviation
    from the same-transaction rule: `create_service_account` commits
    internally (Phase 1 semantics, kept for its other callers), so the
    audit entry rides a second commit immediately after - an audit-write
    failure surfaces as a 500 but cannot roll back the already-created key.
    """
    try:
        row, secret = await create_service_account(
            session, payload.name, payload.user_id, team_id=payload.team_id
        )
    except (UserNotFoundError, TeamMembershipNotFoundError) as exc:
        raise NotFoundError(str(exc)) from None
    await write_audit_entry(
        session,
        actor=admin_ctx,
        action="service_account_key.create",
        target_type="service_account_key",
        target_id=str(row.id),
        old_value=None,
        new_value={
            "name": row.name,
            "user_id": row.user_id,
            "team_id": row.team_id,
            "key_prefix": row.key_prefix,
        },
        source_ip=source_ip,
    )
    await session.commit()
    return ServiceAccountKeyCreateResponse(
        id=row.id,
        name=row.name,
        user_id=row.user_id,
        team_id=row.team_id,
        key_prefix=row.key_prefix,
        secret=secret,
        created_at=row.created_at,
    )


@router.get("", response_model=list[ServiceAccountKeyResponse])
async def list_service_account_keys(
    session: AsyncSession = Depends(get_db_session),
) -> list[ServiceAccountKeyResponse]:
    """List every service-account key (safe fields only, active and revoked) for the default org."""
    rows = await list_service_accounts(session)
    return [ServiceAccountKeyResponse.model_validate(row) for row in rows]


@router.get("/{service_account_id}", response_model=ServiceAccountKeyResponse)
async def get_service_account_key(
    service_account_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
) -> ServiceAccountKeyResponse:
    """Fetch a single service-account key (safe fields only). 404 if not found."""
    row = await get_service_account(session, service_account_id)
    if row is None:
        raise NotFoundError(f"No service account key found with id '{service_account_id}'.")
    return ServiceAccountKeyResponse.model_validate(row)


@router.delete("/{service_account_id}", status_code=204)
async def revoke_service_account_key(
    service_account_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
    admin_ctx: AdminContext = Depends(require_admin),
    source_ip: str | None = Depends(get_source_ip),
) -> Response:
    """Revoke a service-account key.

    404 if the id doesn't exist at all. Idempotent 204 success if the key
    exists but is already revoked - re-issuing a revoke should never error
    just because it already took effect. Distinguishing "doesn't exist"
    from "already revoked" requires a separate existence check here (see
    `services.service_accounts.revoke_service_account`'s docstring for why
    that function alone can't tell the two apart from its boolean return).

    Phase 2 (design doc section 7): a `service_account_key.revoke` audit
    entry is written when (and only when) this call actually changed state
    - same second-commit deviation as the create endpoint above.
    """
    changed = await revoke_service_account(session, service_account_id)
    if not changed:
        row = await get_service_account(session, service_account_id)
        if row is None:
            raise NotFoundError(f"No service account key found with id '{service_account_id}'.")
        # Row exists but was already revoked - idempotent no-op success.
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    await write_audit_entry(
        session,
        actor=admin_ctx,
        action="service_account_key.revoke",
        target_type="service_account_key",
        target_id=str(service_account_id),
        old_value={"active": True},
        new_value={"active": False},
        source_ip=source_ip,
    )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
