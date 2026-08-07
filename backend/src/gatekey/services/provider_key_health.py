"""Passive key-health derivation + failover opt-in resolution + key
selection (Phase 4 - Reliability & Cost Efficiency, design doc section 3).

Health state (`KeyHealthState`) lives in the `SharedStateStore` (see
`services/shared_state.py`), keyed `health:{provider_key_id}` - deliberately
NOT a database table (design doc section 3.1): it is fully re-derivable from
live traffic within a rolling window, so losing it on a process restart is
harmless, and this avoids a write on every single provider call's hot path
against Postgres. Recorded by `api.v1.gateway.common.call_provider_with_
failover` after every outbound provider call, success or failure - never a
separate polling job (AC1.3/ratified #5: passive health checks spend no
provider budget).

`TeamFailoverOverrideCache` is a small, process-local, lock-free,
GIL-atomic full-replace-snapshot cache - same contract as
`services.residency.ResidencyRuleCache` - NOT retrofitted onto
`SharedStateStore` (design doc section 3.2/12: this is process-startup
config, warmed once and refreshed on admin writes, a different shape from
the per-request mutable counters/entries `SharedStateStore` was built for).

`resolve_failover_opt_in` is the cumulative, narrowing-only read this
codebase's `check_access_schedule`/`resolve_residency` pattern established
(Phase 3 security-review fix, see those functions' docstrings): both the
key's own `failover_enabled` AND the team's own `failover_disabled` override
are checked on every read, never validated-narrower-at-write-then-
innermost-only-at-read - closing the identical staleness gap for failover
that Phase 3 closed for residency/access-schedule.

Phase 4 backup group support
----------------------------
`get_backup_group_for_provider` resolves backup keys from the same backup
group, ordered by availability_24h descending for failover selection.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.db.models.backup_group import BackupGroup
from gatekey.db.models.provider_key import ProviderKey
from gatekey.db.models.team_failover_override import TeamFailoverOverride
from gatekey.errors import ProviderNotConfiguredError
from gatekey.services import provider_keys as provider_keys_service
from gatekey.services import proxy_keys as proxy_keys_service
from gatekey.services.encryption import KeyProvider
from gatekey.services.shared_state import SharedStateStore

HealthStatus = Literal["healthy", "degraded", "down", "unavailable"]

logger = logging.getLogger("gatekey")

# Ratified #5 concrete defaults (design doc section 3.1): a rolling 60s
# failure window, >=3 consecutive failures within it trips "down" (failover
# eligibility); 1-2 is "degraded" (admin-visible only, does NOT trigger
# proactive rerouting). The design calls these "admin-configurable" via
# `GET/PUT /v1/admin/reliability-settings` - that endpoint needs its own
# database-admin-owned table/migration (none exists in this schema slice),
# so it is not built this pass; these are fixed module constants until that
# lands. Flagged back to the architect, not silently shipped as if
# configurable.
DEFAULT_FAILURE_WINDOW_SECONDS = 60.0
DEFAULT_DOWN_THRESHOLD = 3


def _health_key(provider_key_id: uuid.UUID) -> str:
    return f"health:{provider_key_id}"


@dataclass(frozen=True)
class KeyHealthState:
    consecutive_failures: int
    window_started_at: float  # time.monotonic() - see design doc section 3.1
    status: HealthStatus
    last_error_summary: str | None


def _status_for(consecutive_failures: int, down_threshold: int) -> HealthStatus:
    if consecutive_failures >= down_threshold:
        return "down"
    if consecutive_failures >= 1:
        return "degraded"
    return "healthy"


async def record_success(store: SharedStateStore, provider_key_id: uuid.UUID) -> None:
    """"1 success immediately recovers it," applied literally (design doc
    section 3.1) - resets to a clean `healthy` state regardless of prior
    failure count."""
    state = KeyHealthState(
        consecutive_failures=0, window_started_at=time.monotonic(), status="healthy", last_error_summary=None
    )
    await store.set_json(_health_key(provider_key_id), state.__dict__, ttl_seconds=None)


async def record_failure(
    store: SharedStateStore,
    provider_key_id: uuid.UUID,
    *,
    error_summary: str | None,
    window_seconds: float = DEFAULT_FAILURE_WINDOW_SECONDS,
    down_threshold: int = DEFAULT_DOWN_THRESHOLD,
) -> HealthStatus:
    """Design doc section 3.1's state machine: if the existing window has
    expired, restart it at `consecutive_failures=1`; else increment.
    Returns the resulting status."""
    now = time.monotonic()
    raw = await store.get_json(_health_key(provider_key_id))
    if raw is not None and (now - raw["window_started_at"]) <= window_seconds:
        consecutive_failures = raw["consecutive_failures"] + 1
        window_started_at = raw["window_started_at"]
    else:
        consecutive_failures = 1
        window_started_at = now
    status = _status_for(consecutive_failures, down_threshold)
    state = KeyHealthState(
        consecutive_failures=consecutive_failures,
        window_started_at=window_started_at,
        status=status,
        last_error_summary=error_summary,
    )
    await store.set_json(_health_key(provider_key_id), state.__dict__, ttl_seconds=None)
    return status


async def get_health(store: SharedStateStore, provider_key_id: uuid.UUID) -> KeyHealthState | None:
    """`None` = no traffic observed yet for this key this process/window -
    treated as healthy by every caller (a key with no recorded failures has
    no reason to be routed around)."""
    raw = await store.get_json(_health_key(provider_key_id))
    return KeyHealthState(**raw) if raw is not None else None


# ---------------------------------------------------------------------------
# Team failover-override cache + cumulative opt-in resolution (design doc
# section 3.2).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TeamFailoverOverrideSnapshot:
    failover_disabled: bool


class TeamFailoverOverrideCache:
    """Process-local, lock-free cache of every team's failover override -
    see module docstring."""

    def __init__(self, overrides: dict[uuid.UUID, TeamFailoverOverrideSnapshot] | None = None) -> None:
        self._overrides: dict[uuid.UUID, TeamFailoverOverrideSnapshot] = dict(overrides or {})

    def get(self, team_id: uuid.UUID) -> TeamFailoverOverrideSnapshot | None:
        return self._overrides.get(team_id)

    def set_all(self, overrides: dict[uuid.UUID, TeamFailoverOverrideSnapshot]) -> None:
        """Full replace - the startup-warm write."""
        self._overrides = dict(overrides)

    def set_one(self, team_id: uuid.UUID, override: TeamFailoverOverrideSnapshot | None) -> None:
        """Refresh one team's entry after a committed write - still a
        whole-dict replace, never an in-place mutation of the live dict.
        `override=None` removes the entry (failover_disabled reset to the
        column default, i.e. no narrowing)."""
        replacement = dict(self._overrides)
        if override is None:
            replacement.pop(team_id, None)
        else:
            replacement[team_id] = override
        self._overrides = replacement


def resolve_failover_opt_in(
    primary_key: ProviderKey,
    *,
    team_id: uuid.UUID | None,
    team_override_cache: TeamFailoverOverrideCache,
) -> bool:
    """Design doc section 3.2, verbatim: both layers checked every time -
    not an innermost-only shortcut. See module docstring for the staleness
    gap this closes (org later disables `failover_enabled` -> a team's
    stale, previously-valid `failover_disabled=false` override can never
    silently re-enable failover the org has since turned off)."""
    if not primary_key.failover_enabled:
        return False
    if team_id is not None:
        override = team_override_cache.get(team_id)
        if override is not None and override.failover_disabled:
            return False
    return True


async def load_team_failover_override_snapshot(
    session: AsyncSession,
) -> dict[uuid.UUID, TeamFailoverOverrideSnapshot]:
    """Query every team failover-override row - used at process startup only
    (to warm `TeamFailoverOverrideCache`, see `main.py`'s lifespan). NEVER
    call this from a gateway route handler."""
    rows = (await session.execute(select(TeamFailoverOverride))).scalars().all()
    return {row.team_id: TeamFailoverOverrideSnapshot(failover_disabled=row.failover_disabled) for row in rows}


# ---------------------------------------------------------------------------
# Health status calculation
# ---------------------------------------------------------------------------


def calculate_health_status(availability_24h: float | None) -> HealthStatus:
    """Determines health status based on 24-hour availability percentage.

    Returns:
        - "healthy" if availability >= 0.99
        - "degraded" if availability >= 0.90
        - "unavailable" if availability < 0.90
        - "unknown" if availability is None (no data yet)
    """
    if availability_24h is None:
        return "unknown"
    if availability_24h >= 0.99:
        return "healthy"
    if availability_24h >= 0.90:
        return "degraded"
    return "unavailable"


# ---------------------------------------------------------------------------
# Backup group resolution (design doc section 1.2)
# ---------------------------------------------------------------------------


async def get_backup_group_for_provider(
    session: AsyncSession, provider: str
) -> tuple[ProviderKey | None, list[ProviderKey]]:
    """Resolve the primary key and all backup keys for a provider.

    Returns (primary, backup_keys) where:
        - primary is the primary key for this provider (or None if not configured)
        - backup_keys are other keys in the same backup group, ordered by
          availability_24h descending (best first), then label

    If the primary key exists but has no backup_group_id, returns (primary, []).
    """
    primary = await provider_keys_service.get_primary_key(session, provider)
    if primary is None:
        return None, []

    if primary.backup_group_id is None:
        return primary, []

    # Get all keys in the same backup group, excluding the primary
    # Order by availability_24h DESC (best first), then label for stability
    stmt = (
        select(ProviderKey)
        .where(
            ProviderKey.backup_group_id == primary.backup_group_id,
            ProviderKey.id != primary.id,
        )
        .order_by(
            ProviderKey.availability_24h.desc().nullslast(),
            ProviderKey.label,
        )
    )
    result = await session.execute(stmt)
    backup_keys = list(result.scalars().all())
    return primary, backup_keys


async def get_backup_group_by_id(
    session: AsyncSession, group_id: uuid.UUID
) -> BackupGroup | None:
    """Get a backup group by its ID."""
    stmt = select(BackupGroup).where(BackupGroup.id == group_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_keys_in_backup_group(
    session: AsyncSession, group_id: uuid.UUID
) -> list[ProviderKey]:
    """List all provider keys in a backup group, ordered by is_primary then label."""
    stmt = (
        select(ProviderKey)
        .where(ProviderKey.backup_group_id == group_id)
        .order_by(ProviderKey.is_primary.desc(), ProviderKey.label)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Key selection (design doc section 3.3's proactive half; the reactive
# one-retry half lives in `api.v1.gateway.common.call_provider_with_
# failover`, per the design's own instruction that it belongs in the
# provider-call wrapper, not here).
# ---------------------------------------------------------------------------


async def select_provider_key(
    session: AsyncSession,
    provider: str,
    *,
    team_id: uuid.UUID | None,
    health_store: SharedStateStore,
    team_override_cache: TeamFailoverOverrideCache,
) -> tuple[ProviderKey, bool]:
    """Proactive half of failover (AC1.9): if the primary is already known
    Down and failover applies, route straight to the backup - never attempt
    a call known to be doomed. Raises `errors.ProviderNotConfiguredError` if
    no key at all is configured for `provider`. Returns `(selected_key,
    failover_applies)` - `failover_applies` is threaded through to the
    reactive retry wrapper so it doesn't need to re-derive it."""
    primary = await provider_keys_service.get_primary_key(session, provider)
    if primary is None:
        raise ProviderNotConfiguredError(f"No key configured for provider '{provider}'.")
    failover_applies = resolve_failover_opt_in(primary, team_id=team_id, team_override_cache=team_override_cache)
    if failover_applies and primary.failover_target_id is not None:
        state = await get_health(health_store, primary.id)
        if state is not None and state.status == "down":
            backup = await provider_keys_service.get_key_by_id(session, primary.failover_target_id)
            if backup is not None:
                return backup, failover_applies
    return primary, failover_applies


# ---------------------------------------------------------------------------
# Health check scheduling (design doc section 6.2)
# ---------------------------------------------------------------------------


async def refresh_single_provider_key_health(
    session: AsyncSession,
    health_store: SharedStateStore,
    key: ProviderKey,
    *,
    key_provider: KeyProvider,
    timeout_seconds: float = 3.0,
) -> tuple[HealthStatus, str | None]:
    """Health-check exactly one `ProviderKey` row.

    Shared by both the scheduled sweep (`refresh_provider_key_health`,
    below) and the single-key admin endpoint
    (`POST /v1/admin/provider-keys/{id}/health`) so the two never drift -
    one health-check implementation, two callers. Updates the DB row
    (`health_status`/`last_health_check`/`last_error`) and the shared-state
    store (for live failover routing decisions, see `select_provider_key`)
    exactly as the pre-extraction inline loop body did. Returns
    `(health_status, error_message)`.

    FIX 1 (previously flagged, fixed here): this used to validate a
    hardcoded literal `{"api_key": "placeholder"}` rather than `key`'s own
    decrypted credential. Now decrypts the REAL credential via
    `services.proxy_keys.get_decrypted_provider_credential_from_row` (the
    same failover-aware decrypt path the gateway's own provider-call
    wrapper uses) and reshapes it into the validator's expected dict via
    `ProviderCredential.to_secret_payload()` (see that method's docstring).
    `key_provider` is threaded in by both callers exactly like
    `timeout_seconds` already was.

    FIX 2 (found alongside fixing #1, by this task's own new test - not
    previously covered): `validator.validate()` RETURNS a `ValidationResult`
    rather than raising for an auth rejection/provider-unreachable/unknown
    outcome (every concrete validator's `except Exception` block already
    catches its own network errors and converts them to a `ValidationResult`
    - see `providers.base.map_httpx_exception`/`map_http_status`). The
    pre-existing code called `validate()` and discarded the return value
    entirely, so the `try` block always ran to completion and
    unconditionally reported `"healthy"` regardless of what the provider
    actually said - meaning fixing #1 alone (passing the real credential)
    would have had NO observable effect: a genuinely revoked/expired key
    would still be reported healthy forever, never tripping proactive
    failover. Fixed by branching on `result.is_valid` and routing the
    non-valid case through the exact same "failure" handling (DB
    `last_error`/`health_status` write, `record_failure`) the `except`
    block below already used for a raised exception.

    Secret hygiene: the decrypted credential/secret_payload is never logged
    or written anywhere by this function - only passed straight to
    `validator.validate()`. Every exception that can be raised on this path
    (`encryption.DecryptionError`, `services.proxy_keys.
    CredentialDecodeError`/`UnsupportedProviderCredentialError`) and every
    `ValidationResult.detail` a validator can return (via `map_httpx_
    exception`/`map_http_status`) is documented, at its own definition, as
    safe to log/persist - none of them ever echo request/response body
    content back, so `error_message` here (persisted to `ProviderKey.
    last_error` and returned to the admin endpoint) can never carry the raw
    key. `timeout_seconds` is threaded into `build_validator_registry()`
    (each validator's own HTTP client timeout) - AC4.1.6's 3s budget is
    therefore enforced as a per-HTTP-call timeout, not a single hard
    `asyncio.wait_for` wrapper around the whole check; flagged as a minor
    gap (a validator that retries internally could still exceed 3s
    wall-clock overall) rather than silently building a stricter guarantee
    than what's actually enforced.
    """
    from gatekey.providers import registry as provider_registry

    validators = provider_registry.build_validator_registry(timeout_seconds=timeout_seconds)
    try:
        credential = await proxy_keys_service.get_decrypted_provider_credential_from_row(
            key, key.provider.value, key_provider=key_provider
        )
        validator = provider_registry.get_validator(key.provider.value, validators)
        result = await validator.validate(credential.to_secret_payload())

        if not result.is_valid:
            # See FIX 2 above - a non-exception, non-VALID outcome is a real
            # health-check failure and must be treated identically to the
            # `except` branch below, not silently reported healthy.
            # `result.detail` is documented safe-to-log (see
            # `providers.base.ValidationResult`'s docstring).
            error_message = result.detail or "Provider rejected the key or was unreachable."
            health_status = "unavailable"
            logger.warning("health_check_error", extra={"key_id": str(key.id), "error": error_message})

            await session.execute(
                update(ProviderKey)
                .where(ProviderKey.id == key.id)
                .values(health_status=health_status, last_health_check=func.now(), last_error=error_message)
            )
            await session.commit()
            await record_failure(health_store, key.id, error_summary=error_message)
            return health_status, error_message

        health_status = "healthy"
        error_message = None

        await session.execute(
            update(ProviderKey)
            .where(ProviderKey.id == key.id)
            .values(health_status=health_status, last_health_check=func.now(), last_error=None)
        )
        await session.commit()
        await record_success(health_store, key.id)

    except Exception as exc:
        # Safe to log/persist `str(exc)` here - see this function's
        # docstring for why every exception reachable on this path (decrypt
        # errors from `services.proxy_keys`) is documented as never
        # containing raw secret material.
        error_message = str(exc)
        health_status = "unavailable"
        logger.warning("health_check_error", extra={"key_id": str(key.id), "error": error_message})

        await session.execute(
            update(ProviderKey)
            .where(ProviderKey.id == key.id)
            .values(health_status=health_status, last_health_check=func.now(), last_error=error_message)
        )
        await session.commit()
        await record_failure(health_store, key.id, error_summary=error_message)

    return health_status, error_message


async def refresh_provider_key_health(
    session: AsyncSession,
    health_store: SharedStateStore,
    *,
    key_provider: KeyProvider,
    timeout_seconds: float = 3.0,
) -> tuple[int, list[str]]:
    """Perform health checks on all provider keys in backup groups.

    This is intended to be called periodically (e.g., every 5 minutes) by
    a background scheduler (see `services.scheduler.run_provider_key_
    health_check_if_due`). Each key's health is checked via
    `refresh_single_provider_key_health` (the shared single-key
    implementation), and the results are stored in both the database
    (health_status, last_health_check, last_error) and the shared state store
    (for runtime failover decisions).

    `key_provider` is passed straight through to `refresh_single_provider_
    key_health` (needed to decrypt each key's real credential - see that
    function's docstring).

    Returns a tuple of (keys_checked, error_messages).
    """
    # Get all keys that are part of a backup group (they need health checks)
    stmt = select(ProviderKey).where(
        ProviderKey.backup_group_id.isnot(None),
    )
    result = await session.execute(stmt)
    keys = result.scalars().all()

    error_messages: list[str] = []
    keys_checked = 0

    for key in keys:
        keys_checked += 1
        _status, error_message = await refresh_single_provider_key_health(
            session, health_store, key, key_provider=key_provider, timeout_seconds=timeout_seconds
        )
        if error_message is not None:
            error_messages.append(f"Health check failed for key {key.id}: {error_message}")

    return keys_checked, error_messages
