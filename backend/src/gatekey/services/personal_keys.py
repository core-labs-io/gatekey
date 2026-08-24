"""Personal-API-key credential plumbing + CRUD service (Phase 2, BD-6/BD-16).

Credential pieces (BD-6): the `gk_pk_` prefix, secret generation, and the
active-key hash lookup consumed by `api.deps.require_gateway_credential`.
CRUD pieces (BD-16): create/list/regenerate/revoke used by the self-serve,
delegated, and admin routes in `api/v1/keys.py`.

Every credential convention is deliberately identical to
`services/service_accounts.py` (same entropy, same SHA-256 `hash_secret`,
same anti-enumeration lookup shape) - see that module's docstrings for the
shared rationale.

Transaction contract (BD-16 functions): mutating functions FLUSH but never
COMMIT - the route handler writes its `AuditEntry` on the same session and
commits (design doc section 7's same-transaction rule), matching
`services/teams.py`'s contract, not `services/service_accounts.py`'s legacy
commit-inside style.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.personal_api_key import PersonalApiKey
from gatekey.db.models.team_membership import TeamMembership
from gatekey.errors import GatekeyError, NotFoundError
from gatekey.services.org_settings import get_effective_org_settings
from gatekey.services.service_accounts import KEY_PREFIX_LENGTH, hash_secret

__all__ = [
    "PERSONAL_SECRET_PREFIX",
    "create_personal_key",
    "generate_personal_key_secret",
    "get_active_personal_key_by_hash",
    "get_personal_key",
    "hash_secret",
    "list_personal_keys",
    "list_personal_keys_for_owner",
    "regenerate_personal_key",
    "revoke_personal_key",
    "validate_expiry",
]

# Prefix on every plaintext personal-key secret - distinct from service
# accounts' `gk_sk_` so `require_gateway_credential` routes by prefix with a
# single table lookup, never a try-both-tables fallback (design doc 1.6/2.5).
PERSONAL_SECRET_PREFIX = "gk_pk_"

# Same 256-bit entropy as service-account secrets - see
# `services/service_accounts.py`.
_TOKEN_ENTROPY_BYTES = 32


def generate_personal_key_secret() -> tuple[str, str]:
    """Generate a fresh plaintext personal-key secret.

    Returns `(secret, key_prefix)` - `secret` is the one-time plaintext
    (`gk_pk_...`, never persisted; only `hash_secret(secret)` is stored) and
    `key_prefix` is the list-view identification snippet, sized to the
    `PersonalApiKey.key_prefix` column exactly like service accounts.
    """
    secret = PERSONAL_SECRET_PREFIX + secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
    key_prefix = secret[len(PERSONAL_SECRET_PREFIX) :][:KEY_PREFIX_LENGTH]
    return secret, key_prefix


async def get_active_personal_key_by_hash(
    session: AsyncSession, secret_hash: bytes
) -> PersonalApiKey | None:
    """Look up an active personal key by `secret_hash`.

    Same hot-path shape as `get_active_service_account_by_hash` (single
    indexed-equality query, not org-scoped - see that function's docstring),
    PLUS the `expires_at` freshness check personal keys need (design doc
    2.5): expired keys are filtered SQL-side against the DB clock, so
    "expired", "revoked", and "never existed" are all indistinguishable to
    the caller - one generic 401, anti-enumeration preserved.
    """
    stmt = select(PersonalApiKey).where(
        PersonalApiKey.secret_hash == secret_hash,
        PersonalApiKey.revoked_at.is_(None),
        or_(PersonalApiKey.expires_at.is_(None), PersonalApiKey.expires_at > func.now()),
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


# --- BD-16: CRUD service functions (flush-not-commit - module docstring) -----


def validate_expiry(
    expires_at: datetime | None,
    *,
    max_expiration_days: int | None,
    now: datetime | None = None,
) -> None:
    """Enforce org_settings' key-expiry rules (design doc 1.1/5.6).

    Pure so it's cheaply unit-testable. Rules: a past `expires_at` is always
    rejected; when the org sets `max_self_serve_key_expiration_days`, a key
    must expire (a no-expiry key would exceed any max) and must expire within
    that many days from now. `max_expiration_days=None` = no constraint
    beyond "not in the past". Raises 422 `GatekeyError`s.
    """
    now = now or datetime.now(timezone.utc)
    if expires_at is not None and expires_at <= now:
        raise GatekeyError(
            "expires_at must be in the future.",
            code="personal_key_expiry_invalid",
            status_code=422,
        )
    if max_expiration_days is None:
        return
    if expires_at is None:
        raise GatekeyError(
            "This organization requires personal keys to expire within "
            f"{max_expiration_days} days - expires_at is required.",
            code="personal_key_expiry_required",
            status_code=422,
        )
    if expires_at > now + timedelta(days=max_expiration_days):
        raise GatekeyError(
            "expires_at exceeds this organization's maximum key lifetime of "
            f"{max_expiration_days} days.",
            code="personal_key_expiry_too_long",
            status_code=422,
        )


async def create_personal_key(
    session: AsyncSession,
    *,
    owner_user_id: uuid.UUID,
    created_by_user_id: uuid.UUID,
    team_id: uuid.UUID,
    name: str,
    expires_at: datetime | None,
    device_label: str | None = None,
) -> tuple[PersonalApiKey, str]:
    """Create a personal key. Returns `(row, plaintext_secret)` - the
    plaintext exists only in this return value, never persisted or logged.
    `device_label` (added by `0047`) is only ever passed by the CLI-sync
    device-code approval route - every other caller (self-service portal,
    team-lead-assisted create) leaves it `None`.

    Enforces org_settings' `personal_key_soft_cap` (active keys per owner)
    and `max_self_serve_key_expiration_days` (applied to delegated creates
    too - one create path, one rule set). The route layer owns the
    *authorization* checks; the membership *existence* check below is this
    function's own (security review M-2): the owner's `TeamMembership` row
    is locked (`SELECT ... FOR UPDATE`) through the caller's commit, so a
    concurrent removal serializes against this create - whichever
    transaction wins the lock, the loser sees a consistent state (a
    removal that lands first makes this 404; a create that lands first
    just succeeds - `0049`'s soft delete no longer blocks removal on
    active keys existing, so there's no ADR-4 409 case left here). Mirrors
    `team_budget.py`'s locking discipline. Flushes, does not commit.
    """
    # `removed_at IS NULL` (added by `0049`) - a removed member must not be
    # able to mint a NEW key for a team they no longer belong to.
    membership_id = (
        await session.execute(
            select(TeamMembership.id)
            .where(
                TeamMembership.team_id == team_id,
                TeamMembership.user_id == owner_user_id,
                TeamMembership.removed_at.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if membership_id is None:
        raise NotFoundError("Team membership not found.")

    org = await get_effective_org_settings(session)
    validate_expiry(expires_at, max_expiration_days=org.max_self_serve_key_expiration_days)

    active_count = (
        await session.execute(
            select(func.count(PersonalApiKey.id)).where(
                PersonalApiKey.owner_user_id == owner_user_id,
                PersonalApiKey.revoked_at.is_(None),
                or_(
                    PersonalApiKey.expires_at.is_(None),
                    PersonalApiKey.expires_at > func.now(),
                ),
            )
        )
    ).scalar_one()
    if active_count >= org.personal_key_soft_cap:
        raise GatekeyError(
            f"This user already holds {active_count} active personal keys - "
            f"the organization's cap is {org.personal_key_soft_cap}. Revoke "
            "an existing key first.",
            code="personal_key_soft_cap_exceeded",
            status_code=422,
        )

    secret, key_prefix = generate_personal_key_secret()
    row = PersonalApiKey(
        org_id=DEFAULT_ORG_ID,
        owner_user_id=owner_user_id,
        created_by_user_id=created_by_user_id,
        team_id=team_id,
        name=name,
        key_prefix=key_prefix,
        secret_hash=hash_secret(secret),
        expires_at=expires_at,
        device_label=device_label,
    )
    session.add(row)
    await session.flush()
    return row, secret


async def get_personal_key(
    session: AsyncSession,
    key_id: uuid.UUID,
    *,
    owner_user_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
) -> PersonalApiKey | None:
    """Fetch one personal key by id, optionally scoped to an owner and/or
    team. The optional scoping is what gives the routes their
    anti-enumeration shape: "exists but not yours" and "never existed" both
    come back None -> one generic 404."""
    stmt = select(PersonalApiKey).where(
        PersonalApiKey.org_id == DEFAULT_ORG_ID, PersonalApiKey.id == key_id
    )
    if owner_user_id is not None:
        stmt = stmt.where(PersonalApiKey.owner_user_id == owner_user_id)
    if team_id is not None:
        stmt = stmt.where(PersonalApiKey.team_id == team_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_personal_keys_for_owner(
    session: AsyncSession, owner_user_id: uuid.UUID, *, team_id: uuid.UUID | None = None
) -> list[PersonalApiKey]:
    """Every key (active and revoked) owned by `owner_user_id`, optionally
    scoped to one team (the delegated team-lead view, AC5.8)."""
    stmt = select(PersonalApiKey).where(
        PersonalApiKey.org_id == DEFAULT_ORG_ID,
        PersonalApiKey.owner_user_id == owner_user_id,
    )
    if team_id is not None:
        stmt = stmt.where(PersonalApiKey.team_id == team_id)
    stmt = stmt.order_by(PersonalApiKey.created_at)
    return list((await session.execute(stmt)).scalars().all())


async def list_personal_keys(session: AsyncSession) -> list[PersonalApiKey]:
    """Every personal key in the org (admin oversight listing)."""
    stmt = (
        select(PersonalApiKey)
        .where(PersonalApiKey.org_id == DEFAULT_ORG_ID)
        .order_by(PersonalApiKey.created_at)
    )
    return list((await session.execute(stmt)).scalars().all())


async def regenerate_personal_key(
    session: AsyncSession, row: PersonalApiKey
) -> tuple[PersonalApiKey, str]:
    """Replace the key's secret in place (same id/name/team/expiry). Only an
    active (non-revoked) key may be regenerated - callers 404 revoked rows
    before getting here. Flushes, does not commit."""
    if row.revoked_at is not None:
        raise GatekeyError(
            "Cannot regenerate a revoked key.",
            code="personal_key_revoked",
            status_code=409,
        )
    secret, key_prefix = generate_personal_key_secret()
    row.key_prefix = key_prefix
    row.secret_hash = hash_secret(secret)
    await session.flush()
    return row, secret


async def revoke_personal_key(session: AsyncSession, row: PersonalApiKey) -> bool:
    """Set `revoked_at` if still active. Returns True iff this call changed
    state (already-revoked = idempotent no-op False, mirroring
    `revoke_service_account`'s semantics). Flushes, does not commit."""
    if row.revoked_at is not None:
        return False
    row.revoked_at = datetime.now(timezone.utc)
    await session.flush()
    return True
