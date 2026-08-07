"""DB-backed service for managing service-account keys.

Service-account keys are per-app credentials used to authenticate gateway
requests (`/v1/chat/completions`, `/v1/completions`, `/v1/embeddings` - see
`api/deps.require_service_account`, wired up by a later task). This module
is the CRUD/lookup counterpart to `services/provider_keys.py`; see that
module's docstring and `db/models/service_account_key.py` for the broader
rationale (single-org scope, SHA-256-not-a-KDF hashing, no `last_used_at`).

Every function in this module operates against `constants.DEFAULT_ORG_ID`
only, same as `services/provider_keys.py` - see that module's docstring for
why no `org_id` parameter is accepted here. The one exception is
`get_active_service_account_by_hash`, which is deliberately not org-scoped -
see its docstring.

Idempotency note (revoke): `revoke_service_account` itself is idempotent -
calling it twice on the same id returns `True` the first time (state
changed) and `False` the second time (row already revoked, nothing to do),
but never raises just because the key was already revoked. The distinction
between "row doesn't exist at all" (404) and "row exists but was already
revoked" (idempotent 204) is left to the caller (the admin router, BD-4),
since that distinction is about HTTP semantics, not data-layer semantics -
this function only needs to answer "did I just change anything".
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.service_account_key import ServiceAccountKey
from gatekey.db.models.team_membership import TeamMembership
from gatekey.services.users import get_user

# Prefix on every plaintext service-account secret. Lets a reader (and the
# `require_service_account` auth dependency) recognize the credential shape
# before ever touching the database.
SECRET_PREFIX = "gk_sk_"

# `secrets.token_urlsafe(32)` yields 256 bits of entropy encoded as a
# variable-length (~43 char) base64url string - see `secrets` stdlib docs.
# The random material, not its encoded length, is what supplies the 256
# bits; the encoding is just how it's represented as a string.
_TOKEN_ENTROPY_BYTES = 32

# Must match `ServiceAccountKey.key_prefix`'s `String(12)` column limit
# exactly (see `db/models/service_account_key.py`).
KEY_PREFIX_LENGTH = 12


def hash_secret(secret: str) -> bytes:
    """SHA-256 digest of the full plaintext secret.

    See `ServiceAccountKey` module docstring for why this is a fast hash
    and not a slow password KDF (bcrypt/argon2/scrypt): a service-account
    secret is a 256-bit random token, not a guessable human password, so
    that threat model doesn't apply, and a deliberately slow hash would eat
    into the gateway's per-request latency budget on every authenticated
    call. Exposed (not module-private) because `api.deps.
    require_service_account` (BD-5) needs to hash a submitted bearer token
    before calling `get_active_service_account_by_hash`.
    """
    return hashlib.sha256(secret.encode("utf-8")).digest()


class UserNotFoundError(Exception):
    """Raised by `create_service_account()` when `user_id` doesn't
    reference an existing user (Phase 1.4)."""


class TeamMembershipNotFoundError(Exception):
    """Raised by `create_service_account()` when `team_id` is given but the
    target user holds no `TeamMembership` on that team (Phase 2, design doc
    section 1.7 / security review H-1)."""


async def create_service_account(
    session: AsyncSession, name: str, user_id: uuid.UUID, team_id: uuid.UUID | None = None
) -> tuple[ServiceAccountKey, str]:
    """Generate and persist a new service-account key, attributed to `user_id`.

    Returns `(row, plaintext_secret)`. `plaintext_secret` exists only in
    this return value - it is never written to the database (only its
    `secret_hash` is) and the caller (the admin router) is responsible for
    returning it to the API caller exactly once and never logging or
    otherwise persisting it.

    Raises `UserNotFoundError` if `user_id` doesn't reference an existing
    user (Phase 1.4) - pre-checked via a `SELECT` so no row is written in
    that case, rather than relying on a bare FK-violation `IntegrityError`.

    Phase 2 (design doc 1.7): `team_id` is required at the API-schema layer
    for every new key; it stays optional HERE (None = legacy flat-budget
    row) so pre-Phase-2-shaped rows remain constructible (tests, legacy
    behavior - byte-for-byte unchanged). When given, the target user must
    hold a `TeamMembership` on that team; the membership row is locked
    (`SELECT ... FOR UPDATE`) through this transaction's commit so a
    concurrent membership removal serializes against this create (same
    discipline as `create_personal_key` / security review M-2) - removal
    then sees the new key and 409s per ADR-4, never orphaning it.
    Raises `TeamMembershipNotFoundError` if no such membership exists.
    """
    existing_user = await get_user(session, user_id)
    if existing_user is None:
        raise UserNotFoundError(f"No user found with id '{user_id}'.")

    if team_id is not None:
        membership = (
            await session.execute(
                select(TeamMembership)
                .where(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if membership is None:
            raise TeamMembershipNotFoundError(
                f"User '{user_id}' holds no membership on team '{team_id}'."
            )

    secret = SECRET_PREFIX + secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
    token_part = secret[len(SECRET_PREFIX) :]
    key_prefix = token_part[:KEY_PREFIX_LENGTH]

    row = ServiceAccountKey(
        org_id=DEFAULT_ORG_ID,
        name=name,
        user_id=user_id,
        team_id=team_id,
        key_prefix=key_prefix,
        secret_hash=hash_secret(secret),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row, secret


async def list_service_accounts(session: AsyncSession) -> list[ServiceAccountKey]:
    """Return every service-account key (active and revoked) for the default org."""
    stmt = (
        select(ServiceAccountKey)
        .where(ServiceAccountKey.org_id == DEFAULT_ORG_ID)
        .order_by(ServiceAccountKey.created_at)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_service_account(
    session: AsyncSession, service_account_id: uuid.UUID
) -> ServiceAccountKey | None:
    """Return the service-account key row with `service_account_id`, or `None`."""
    stmt = select(ServiceAccountKey).where(
        ServiceAccountKey.org_id == DEFAULT_ORG_ID,
        ServiceAccountKey.id == service_account_id,
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def revoke_service_account(session: AsyncSession, service_account_id: uuid.UUID) -> bool:
    """Revoke the service-account key with `service_account_id`, if it exists and is active.

    Returns `True` if this call changed the row's state (was active, is now
    revoked), `False` if the id doesn't exist OR the row was already
    revoked (idempotent no-op in both cases - see module docstring for why
    "doesn't exist" vs "already revoked" is disambiguated by the caller,
    not here). Uses a single `UPDATE ... WHERE revoked_at IS NULL ...
    RETURNING id` so this is atomic under concurrency: two concurrent
    revokes of the same key cannot both observe `True` (Postgres serializes
    the row-level update, only the first one finds a matching, still-active
    row).
    """
    stmt = (
        update(ServiceAccountKey)
        .where(
            ServiceAccountKey.org_id == DEFAULT_ORG_ID,
            ServiceAccountKey.id == service_account_id,
            ServiceAccountKey.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
        .returning(ServiceAccountKey.id)
    )
    result = await session.execute(stmt)
    changed = result.scalar_one_or_none() is not None
    await session.commit()
    return changed


async def regenerate_service_account_key(
    session: AsyncSession, row: ServiceAccountKey
) -> tuple[ServiceAccountKey, str]:
    """Replace an active key's secret in place (Phase 2, BD-16's unified
    admin keys surface). Returns `(row, plaintext_secret)` - plaintext never
    persisted. FLUSHES, does not commit (unlike this module's Phase 1
    functions): the admin route writes its `AuditEntry` on the same session
    and commits, per Phase 2's same-transaction audit rule.
    """
    if row.revoked_at is not None:
        raise ValueError("Cannot regenerate a revoked service-account key.")
    secret = SECRET_PREFIX + secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
    row.key_prefix = secret[len(SECRET_PREFIX) :][:KEY_PREFIX_LENGTH]
    row.secret_hash = hash_secret(secret)
    await session.flush()
    return row, secret


async def rotate_service_account_key(
    session: AsyncSession, *, key_id: uuid.UUID, overlap_buffer_minutes: int
) -> tuple[ServiceAccountKey, str] | None:
    """Dual-secret overlap rotation (Phase 3, design doc sections 1.11/4.3) -
    used by both the "Rotate now" admin route and the automatic scheduler
    (`services.rotation`/`services.scheduler`).

    Single `UPDATE ... RETURNING`: mints a fresh secret, moves the CURRENT
    `secret_hash` into `previous_secret_hash` (RHS references are evaluated
    against the pre-update row, standard SQL `UPDATE` semantics - so this is
    atomic, not a read-then-write race), sets `previous_secret_valid_until =
    now() + overlap_buffer_minutes`, and writes the new secret as current.

    Returns `(row, new_plaintext_secret)` - the plaintext is never
    persisted (see `ServiceAccountKey` module docstring) and exists only in
    this return value; the caller is responsible for what happens to it
    (the manual "Rotate now" admin route returns it in the response body,
    one-time-reveal, same as `create_service_account`). Returns `None` if
    the key doesn't exist or is already revoked (nothing to rotate) -
    caller decides what that means (404 vs. skip-silently).

    Flushes, does not commit - same "caller audits + commits in the same
    transaction" discipline as `regenerate_service_account_key`.
    """
    secret = SECRET_PREFIX + secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)
    new_hash = hash_secret(secret)
    new_prefix = secret[len(SECRET_PREFIX) :][:KEY_PREFIX_LENGTH]
    valid_until = datetime.now(timezone.utc) + timedelta(minutes=overlap_buffer_minutes)

    stmt = (
        update(ServiceAccountKey)
        .where(
            ServiceAccountKey.id == key_id,
            ServiceAccountKey.revoked_at.is_(None),
        )
        .values(
            previous_secret_hash=ServiceAccountKey.secret_hash,
            previous_secret_valid_until=valid_until,
            secret_hash=new_hash,
            key_prefix=new_prefix,
        )
        .returning(ServiceAccountKey)
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        return None
    await session.flush()
    return row, secret


async def revoke_service_account_row(session: AsyncSession, row: ServiceAccountKey) -> bool:
    """Flush-not-commit revoke of an already-fetched row (Phase 2, BD-16's
    unified admin keys surface - the caller audits and commits in the same
    transaction). Returns True iff this call changed state. The Phase 1
    `revoke_service_account` (commit-inside, id-addressed) stays unchanged
    for its existing callers.
    """
    if row.revoked_at is not None:
        return False
    row.revoked_at = datetime.now(timezone.utc)
    await session.flush()
    return True


async def get_active_service_account_by_hash(
    session: AsyncSession, secret_hash: bytes
) -> ServiceAccountKey | None:
    """Look up an active (non-revoked) service-account key by `secret_hash`.

    Hot-path lookup for the `require_service_account` auth dependency
    (BD-5). Filters on `secret_hash` (backed by the unique index
    `ix_service_account_keys_secret_hash`) and `revoked_at IS NULL` in a
    single indexed-equality query - no full table scan.

    Phase 3 (design doc sections 1.11/4.3): also matches
    `previous_secret_hash` while `previous_secret_valid_until > now()` - the
    dual-secret rotation overlap window. Both columns are indexed (the
    unique partial index on `previous_secret_hash`), so this stays a single
    indexed-equality-shaped lookup, not a table scan. The `now()` comparison
    is evaluated by Postgres inside this one query, not by the app server's
    local clock, so the overlap holds even across clock skew between
    gateway instances (AC7.4).

    Deliberately not scoped by `DEFAULT_ORG_ID`: `secret_hash` is already a
    unique lookup key (the whole row, and therefore its `org_id`, is what
    this call is trying to discover), so requiring the caller to supply an
    `org_id` up front would be circular. This is also why the returned
    row's real `org_id` - not the `DEFAULT_ORG_ID` constant - must be used
    to build `ServiceAccountContext` in `require_service_account`; see that
    function's docstring.
    """
    stmt = select(ServiceAccountKey).where(
        or_(
            ServiceAccountKey.secret_hash == secret_hash,
            (ServiceAccountKey.previous_secret_hash == secret_hash)
            & (ServiceAccountKey.previous_secret_valid_until > func.now()),
        ),
        ServiceAccountKey.revoked_at.is_(None),
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()
