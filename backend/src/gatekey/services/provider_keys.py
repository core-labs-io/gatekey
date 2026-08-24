"""DB-backed service for managing encrypted provider API keys.

Phase 1.1 scope note: this module deliberately exposes no "get decrypted
key" function. Decryption capability lives in `services.encryption` and is
wired up here only implicitly (via `add_or_replace_key`'s encrypt path);
there is no caller for a decrypt path in this slice (the actual proxy call
that would need the raw secret is Phase 1.2, not built yet), so adding one
now would be dead, unused attack surface. Add it when 1.2 lands and needs
it, guarded by whatever narrow internal seam that phase's design calls for.

Every function in this module operates against `constants.DEFAULT_ORG_ID`
only - see that module's docstring for why no `org_id` parameter is
accepted here.

Phase 4 (Reliability & Cost Efficiency, multi-key/failover) additions
------------------------------------------------------------------------
`add_or_replace_key` now upserts by `(org_id, provider, label)` (relaxed
from `(org_id, provider)`) and auto-assigns `is_primary=true` to the first
key ever added for a provider - see `db/models/provider_key.py`'s module
docstring and design doc section 1.2. `get_key` is now `get_primary_key`
under the hood (an alias is kept for the many pre-existing call sites that
still just want "the" key for a provider - that has always meant, and still
means, the one serving normal traffic). `get_key_by_id`/`list_keys_for_
provider`/`set_primary`/`set_failover_config`/`delete_key_by_id` are the new
multi-key CRUD surface backing `api/v1/admin/providers.py`'s new routes
(design doc section 9.1).

Phase 4 backup group support
----------------------------
`create_backup_group`, `list_backup_groups`, `get_backup_group`, `delete_backup_group`,
and `set_backup_group` functions for managing provider key failover groups.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.backup_group import BackupGroup
from gatekey.db.models.provider_key import ProviderKey
from gatekey.errors import GatekeyError
from gatekey.providers.base import ProviderValidator, ValidationStatus
from gatekey.providers.registry import get_validator
from gatekey.services.encryption import KeyProvider, build_aad, encrypt_secret


class ProviderKeyServiceError(Exception):
    """Base class for provider-key service errors.

    `message` must be safe to surface to an API caller and to log - it must
    never contain raw secret material (this mirrors the constraint on
    `ValidationResult.detail` in `providers/base.py`, since these messages
    are typically threaded straight through from there).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidProviderKeyError(ProviderKeyServiceError):
    """The provider rejected the submitted credential (bad/expired key)."""


class ProviderUnreachableError(ProviderKeyServiceError):
    """Could not reach the provider (network error, timeout, 5xx) to validate."""


class ProviderValidationUnknownError(ProviderKeyServiceError):
    """Validation could not be completed for a reason that isn't the above two."""


def _serialize_secret_payload(provider: str, secret_payload: dict[str, Any]) -> bytes:
    """Extract and JSON-serialize only the actual secret material.

    - openai/anthropic/openrouter: the `api_key` string.
    - vertex_ai: the `service_account_json` object. `project_id`/`location`
      are not secret material (they're routing config a future proxy call
      needs, not credentials) and are stored in the non-secret
      `key_metadata` column instead - see `_build_key_metadata`.
    - ollama: the optional `bearer_token` string (may be blank/absent -
      see below).
    """
    if provider in ("openai", "anthropic", "openrouter"):
        return json.dumps(secret_payload["api_key"]).encode("utf-8")
    if provider == "vertex_ai":
        return json.dumps(secret_payload["service_account_json"]).encode("utf-8")
    if provider == "ollama":
        return json.dumps(secret_payload.get("bearer_token") or "").encode("utf-8")
    raise ValueError(f"Unknown provider: {provider!r}")


def _build_key_metadata(provider: str, secret_payload: dict[str, Any]) -> dict[str, Any]:
    """Non-secret metadata to store alongside the encrypted key.

    Never includes key material - see `ProviderKey.key_metadata` docstring.
    """
    if provider == "vertex_ai":
        return {
            "project_id": secret_payload["project_id"],
            "location": secret_payload["location"],
        }
    if provider == "ollama":
        metadata: dict[str, Any] = {"base_url": secret_payload["base_url"]}
        region = secret_payload.get("region")
        if region is not None:
            metadata["region"] = region
        return metadata
    if provider == "openrouter":
        # See `schemas.provider_key.OpenRouterKeyRequest`'s docstring - both
        # fields are already schema-validated as set-together-or-not-at-all;
        # this `and` is a redundant, cheap re-check (defense in depth,
        # matching this module's existing convention), not the real guard.
        slugs = secret_payload.get("trusted_provider_slugs") or []
        region = secret_payload.get("trusted_provider_region")
        if slugs and region:
            return {"trusted_provider_slugs": slugs, "trusted_provider_region": region}
        return {}
    return {}


async def add_or_replace_key(
    session: AsyncSession,
    provider: str,
    secret_payload: dict[str, Any],
    *,
    label: str = "Default",
    backup_group_id: uuid.UUID | None = None,
    is_primary: bool | None = None,
    validator_registry: dict[str, ProviderValidator],
    key_provider: KeyProvider,
) -> ProviderKey:
    """Validate then atomically upsert an encrypted provider key.

    Only writes to the database if validation returns `VALID`. The upsert
    itself is a single `INSERT ... ON CONFLICT (org_id, provider, label) DO
    UPDATE` statement (not a read-then-write), so two concurrent calls for
    the same `(provider, label)` cannot race into a partially-mixed row
    (e.g. ciphertext from one call paired with the nonce from another) -
    Postgres serializes the two statements and one fully wins.

    Phase 4 (design doc section 1.2): `label` defaults to `"Default"` - the
    same value migration `0023` backfilled onto every pre-existing row - so
    every call site that never adds a second key for a provider (the common
    case) keeps upserting the same single row it always has. `is_primary` is
    computed once, before the insert (`True` iff no key exists yet for this
    `(org, provider)`) and is never touched on an update - the first key
    ever added for a provider becomes primary automatically; every
    subsequent key for that provider starts out non-primary (design doc
    section 1.2/3.4).

    ponytail: the `is_primary` pre-check is a plain `SELECT COUNT`, not a
    row lock - a genuinely concurrent first-add race between two DIFFERENT
    labels for the same brand-new provider could both compute
    `is_primary=True` and one loses to the DB's partial unique index
    (`uq_provider_keys_one_primary_per_provider`), surfacing as a raw
    IntegrityError. Acceptable for a low-frequency, single-admin-console
    action; upgrade path is a `SELECT ... FOR UPDATE` on the provider if
    concurrent admin key-setup ever becomes a real scenario.

    Raises `InvalidProviderKeyError` / `ProviderUnreachableError` /
    `ProviderValidationUnknownError` on the corresponding non-VALID
    `ValidationStatus`; no DB write happens in any of those cases.
    """
    validator = get_validator(provider, validator_registry)
    result = await validator.validate(secret_payload)

    if result.status is ValidationStatus.INVALID_KEY:
        raise InvalidProviderKeyError(result.detail or "Provider rejected the submitted key.")
    if result.status is ValidationStatus.PROVIDER_UNREACHABLE:
        raise ProviderUnreachableError(result.detail or "Provider was unreachable.")
    if result.status is ValidationStatus.UNKNOWN_ERROR:
        raise ProviderValidationUnknownError(
            result.detail or "Provider key validation failed for an unknown reason."
        )

    plaintext = _serialize_secret_payload(provider, secret_payload)
    key_metadata = _build_key_metadata(provider, secret_payload)
    aad = build_aad(str(DEFAULT_ORG_ID), provider)
    encrypted = encrypt_secret(plaintext, aad=aad, key_provider=key_provider)

    existing_count = await session.scalar(
        select(func.count())
        .select_from(ProviderKey)
        .where(ProviderKey.org_id == DEFAULT_ORG_ID, ProviderKey.provider == provider)
    )
    is_primary_for_insert = not existing_count

    insert_stmt = postgresql.insert(ProviderKey).values(
        org_id=DEFAULT_ORG_ID,
        provider=provider,
        label=label,
        is_primary=is_primary_for_insert,
        backup_group_id=backup_group_id,
        ciphertext=encrypted.ciphertext,
        nonce=encrypted.nonce,
        auth_tag=encrypted.auth_tag,
        key_metadata=key_metadata,
        validated_at=func.now(),
    )
    upsert_stmt = insert_stmt.on_conflict_do_update(
        index_elements=[ProviderKey.org_id, ProviderKey.provider, ProviderKey.label],
        set_={
            "ciphertext": insert_stmt.excluded.ciphertext,
            "nonce": insert_stmt.excluded.nonce,
            "auth_tag": insert_stmt.excluded.auth_tag,
            "metadata": insert_stmt.excluded.metadata,
            "validated_at": insert_stmt.excluded.validated_at,
            "backup_group_id": insert_stmt.excluded.backup_group_id,
            "updated_at": func.now(),
        },
    ).returning(ProviderKey)

    execute_result = await session.execute(upsert_stmt)
    provider_key = execute_result.scalar_one()
    await session.commit()
    return provider_key


async def rotate_provider_key(
    session: AsyncSession,
    provider: str,
    secret_payload: dict[str, Any],
    *,
    overlap_buffer_minutes: int,
    validator_registry: dict[str, ProviderValidator],
    key_provider: KeyProvider,
) -> ProviderKey | None:
    """Guided provider-key rotation (Phase 3, design doc sections 4.1/4.3,
    AC7.7): admin pastes a new key, Gatekey validates it live against the
    provider (identical three structured error states as `add_or_replace_
    key` - reuses the exact same validator call), then atomically swaps it
    in while keeping the OLD ciphertext readable (admin-console display
    only, see `ProviderKey` module docstring) for a fixed short overlap.

    Unlike `rotate_service_account_key`, this is NOT load-bearing for any
    live auth lookup - Gatekey is both the only writer and only reader of a
    provider credential, so the moment this commits, every subsequent
    outbound call to the provider uses the NEW key. The `previous_*`
    columns exist purely so the admin console can show "previous key,
    retiring in N minutes" and give a human operator a grace window to
    deactivate the old key at the provider's own console.

    Returns `None` if no key is currently configured for `provider` -
    guided ROTATION presupposes an existing key; the caller should direct
    the admin to `PUT /v1/admin/providers/{provider}/key` (first-time setup)
    instead. Raises `InvalidProviderKeyError`/`ProviderUnreachableError`/
    `ProviderValidationUnknownError` exactly like `add_or_replace_key` - no
    DB write happens in any of those cases.
    """
    existing = await get_key(session, provider)
    if existing is None:
        return None

    validator = get_validator(provider, validator_registry)
    result = await validator.validate(secret_payload)

    if result.status is ValidationStatus.INVALID_KEY:
        raise InvalidProviderKeyError(result.detail or "Provider rejected the submitted key.")
    if result.status is ValidationStatus.PROVIDER_UNREACHABLE:
        raise ProviderUnreachableError(result.detail or "Provider was unreachable.")
    if result.status is ValidationStatus.UNKNOWN_ERROR:
        raise ProviderValidationUnknownError(
            result.detail or "Provider key validation failed for an unknown reason."
        )

    plaintext = _serialize_secret_payload(provider, secret_payload)
    key_metadata = _build_key_metadata(provider, secret_payload)
    aad = build_aad(str(DEFAULT_ORG_ID), provider)
    encrypted = encrypt_secret(plaintext, aad=aad, key_provider=key_provider)
    valid_until = datetime.now(timezone.utc) + timedelta(minutes=overlap_buffer_minutes)

    stmt = (
        update(ProviderKey)
        .where(ProviderKey.org_id == DEFAULT_ORG_ID, ProviderKey.provider == provider)
        .values(
            previous_ciphertext=ProviderKey.ciphertext,
            previous_nonce=ProviderKey.nonce,
            previous_auth_tag=ProviderKey.auth_tag,
            previous_valid_until=valid_until,
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            auth_tag=encrypted.auth_tag,
            key_metadata=key_metadata,
            validated_at=func.now(),
        )
        .returning(ProviderKey)
    )
    execute_result = await session.execute(stmt)
    provider_key = execute_result.scalar_one()
    await session.commit()
    return provider_key


async def list_keys(session: AsyncSession) -> list[ProviderKey]:
    """Return every configured provider key for the default org."""
    stmt = (
        select(ProviderKey)
        .where(ProviderKey.org_id == DEFAULT_ORG_ID)
        .order_by(ProviderKey.provider)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_primary_key(session: AsyncSession, provider: str) -> ProviderKey | None:
    """Return the PRIMARY key row for `provider` (the one serving normal,
    non-failover traffic - design doc section 1.2), or `None` if no key is
    configured at all."""
    stmt = select(ProviderKey).where(
        ProviderKey.org_id == DEFAULT_ORG_ID,
        ProviderKey.provider == provider,
        ProviderKey.is_primary.is_(True),
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


get_key = get_primary_key


async def get_key_by_id(session: AsyncSession, key_id: uuid.UUID) -> ProviderKey | None:
    """Return a specific key row by id, or `None` if it doesn't exist (or
    belongs to a different org)."""
    stmt = select(ProviderKey).where(ProviderKey.org_id == DEFAULT_ORG_ID, ProviderKey.id == key_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_keys_for_provider(session: AsyncSession, provider: str) -> list[ProviderKey]:
    """Every configured key (every label) for one provider - backs `GET
    /v1/admin/providers/{provider}/keys` (design doc section 9.1)."""
    stmt = (
        select(ProviderKey)
        .where(ProviderKey.org_id == DEFAULT_ORG_ID, ProviderKey.provider == provider)
        .order_by(ProviderKey.label)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


class ProviderKeyNotFoundError(GatekeyError):
    """No provider key exists with the given id (or it belongs to a
    different provider than the URL path names)."""

    status_code = 404
    code = "not_found"

    def __init__(self, key_id: uuid.UUID) -> None:
        super().__init__(f"No provider key found with id '{key_id}'.")


class FailoverTargetInvalidError(GatekeyError):
    """`failover_target_id` doesn't reference a real key for the SAME
    provider, or references the key itself (design doc section 3.3/9.1:
    same-provider-constrained at the app layer, not the schema layer)."""

    status_code = 422
    code = "failover_target_invalid"


async def set_primary(session: AsyncSession, provider: str, key_id: uuid.UUID) -> ProviderKey:
    """Promote `key_id` to primary for `provider` (design doc section 9.1).

    Two `UPDATE`s in one transaction - unset whichever key is currently
    primary, THEN set the new one - so the partial unique index
    (`uq_provider_keys_one_primary_per_provider`) never sees two rows both
    `true` at once within this transaction's own statement ordering.
    """
    target = await get_key_by_id(session, key_id)
    if target is None or target.provider != provider:
        raise ProviderKeyNotFoundError(key_id)
    await session.execute(
        update(ProviderKey)
        .where(
            ProviderKey.org_id == DEFAULT_ORG_ID,
            ProviderKey.provider == provider,
            ProviderKey.is_primary.is_(True),
        )
        .values(is_primary=False)
    )
    await session.execute(update(ProviderKey).where(ProviderKey.id == key_id).values(is_primary=True))
    await session.commit()
    refreshed = await get_key_by_id(session, key_id)
    assert refreshed is not None
    return refreshed


async def set_failover_config(
    session: AsyncSession,
    provider: str,
    key_id: uuid.UUID,
    *,
    failover_enabled: bool,
    failover_target_id: uuid.UUID | None,
) -> ProviderKey:
    """Set `failover_enabled`/`failover_target_id` on one key (design doc
    section 9.1). `failover_target_id`, if given, must reference a
    different, existing key for the SAME provider - app-level validation
    (the FK itself would happily accept a different provider's key)."""
    target = await get_key_by_id(session, key_id)
    if target is None or target.provider != provider:
        raise ProviderKeyNotFoundError(key_id)
    if failover_target_id is not None:
        if failover_target_id == key_id:
            raise FailoverTargetInvalidError("A key cannot be its own failover target.")
        backup = await get_key_by_id(session, failover_target_id)
        if backup is None or backup.provider != provider:
            raise FailoverTargetInvalidError(
                "failover_target_id must reference an existing key for the same provider."
            )
    await session.execute(
        update(ProviderKey)
        .where(ProviderKey.id == key_id)
        .values(failover_enabled=failover_enabled, failover_target_id=failover_target_id)
    )
    await session.commit()
    refreshed = await get_key_by_id(session, key_id)
    assert refreshed is not None
    return refreshed


async def delete_key_by_id(session: AsyncSession, provider: str, key_id: uuid.UUID) -> bool:
    """Delete one specific key (by id) for `provider` - the multi-key
    replacement for `delete_key`'s provider-scoped delete (design doc
    section 9.1). Returns True if a row was deleted."""
    stmt = delete(ProviderKey).where(
        ProviderKey.org_id == DEFAULT_ORG_ID, ProviderKey.provider == provider, ProviderKey.id == key_id
    )
    result = cast(CursorResult, await session.execute(stmt))
    await session.commit()
    return result.rowcount > 0


async def delete_key(session: AsyncSession, provider: str) -> bool:
    """Delete every configured key for `provider` (every label).

    Returns True if at least one row was deleted. Unchanged single-row
    behavior for any org that never added a second key; for a multi-key org
    this is a "remove this provider entirely" operation - `DELETE
    /v1/admin/providers/{provider}/keys/{key_id}` (`delete_key_by_id` above)
    is the surgical, one-key-at-a-time alternative multi-key orgs should use
    instead (design doc section 9.1).
    """
    stmt = delete(ProviderKey).where(
        ProviderKey.org_id == DEFAULT_ORG_ID, ProviderKey.provider == provider
    )
    result = cast(CursorResult, await session.execute(stmt))
    await session.commit()
    return result.rowcount > 0


# ============================================================================
# Phase 4: Backup Groups
# ============================================================================


class BackupGroupNotFoundError(GatekeyError):
    """No backup group exists with the given id."""

    status_code = 404
    code = "not_found"


class BackupGroupOrgMismatchError(GatekeyError):
    """The backup group belongs to a different org than the provided one."""

    status_code = 403
    code = "forbidden"


async def create_backup_group(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    name: str,
    description: str | None = None,
) -> BackupGroup:
    """Create a new backup group for provider key failover configuration.

    Backup groups are org-scoped to prevent cross-org key mixing. Keys in the
    same group can serve as backups for each other.
    """
    # NOTE: a previous version of this function used `INSERT ... ON
    # CONFLICT (org_id, name) DO UPDATE`, but neither the `backup_groups`
    # table (migration `0030`, the actual source of truth - see that
    # migration's own docstring correcting the model's stale `0025`
    # reference) nor the `BackupGroup` ORM model declares a unique
    # constraint/index on `(org_id, name)` - Postgres raised
    # `InvalidColumnReferenceError` ("no unique or exclusion constraint
    # matching the ON CONFLICT specification") on every real call. Fixed
    # here as a plain `INSERT` (schema is frozen this task, so "add the
    # missing unique constraint" isn't an available fix) - two backup
    # groups CAN share a name for the same org at the DB level; callers
    # that want create-or-update-by-name semantics should look the group up
    # by name first (see `list_backup_groups`) rather than relying on an
    # upsert this schema doesn't actually support.
    group = BackupGroup(org_id=org_id, name=name, description=description)
    session.add(group)
    await session.commit()
    await session.refresh(group)
    return group


async def list_backup_groups(session: AsyncSession, org_id: uuid.UUID) -> list[BackupGroup]:
    """List all backup groups for an org."""
    stmt = (
        select(BackupGroup)
        .where(BackupGroup.org_id == org_id)
        .order_by(BackupGroup.name)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_backup_group(session: AsyncSession, group_id: uuid.UUID) -> BackupGroup | None:
    """Get a specific backup group by id."""
    stmt = select(BackupGroup).where(BackupGroup.id == group_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def delete_backup_group(session: AsyncSession, group_id: uuid.UUID) -> bool:
    """Delete a backup group. Returns True if a row was deleted."""
    stmt = delete(BackupGroup).where(BackupGroup.id == group_id)
    result = cast(CursorResult, await session.execute(stmt))
    await session.commit()
    return result.rowcount > 0


async def set_backup_group_for_key(
    session: AsyncSession,
    key_id: uuid.UUID,
    backup_group_id: uuid.UUID | None,
) -> ProviderKey:
    """Assign a provider key to a backup group (or remove it from a group).

    A key can only belong to one group at a time. Setting `backup_group_id=None`
    removes the key from any group.
    """
    stmt = update(ProviderKey).where(ProviderKey.id == key_id).values(backup_group_id=backup_group_id)
    await session.execute(stmt)
    await session.commit()

    refreshed = await get_key_by_id(session, key_id)
    if refreshed is None:
        raise ProviderKeyNotFoundError(key_id)
    return refreshed


async def get_keys_in_backup_group(
    session: AsyncSession, group_id: uuid.UUID
) -> list[ProviderKey]:
    """Get all provider keys in a backup group, ordered by is_primary then label."""
    stmt = (
        select(ProviderKey)
        .where(ProviderKey.backup_group_id == group_id)
        .order_by(ProviderKey.is_primary.desc(), ProviderKey.label)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def get_primary_key_with_fallback(
    session: AsyncSession, provider: str
) -> tuple[ProviderKey | None, list[ProviderKey]]:
    """Get the primary key for a provider and all backup keys in its group.

    Returns (primary, [backup_keys]) where backup_keys are other keys in the
    same backup group. This is used for failover routing.

    If no primary exists, returns (None, []).
    If primary exists but has no backup group, returns (primary, []).
    """
    primary = await get_primary_key(session, provider)
    if primary is None:
        return None, []

    if primary.backup_group_id is None:
        return primary, []

    # Get all keys in the same backup group, excluding the primary
    stmt = (
        select(ProviderKey)
        .where(
            ProviderKey.backup_group_id == primary.backup_group_id,
            ProviderKey.id != primary.id,
        )
        .order_by(ProviderKey.availability_24h.desc(), ProviderKey.label)
    )
    result = await session.execute(stmt)
    backup_keys = list(result.scalars().all())
    return primary, backup_keys
