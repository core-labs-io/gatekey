"""Response caching with Redis-backed exact-match caching.

Phase 4 (Reliability & Cost Efficiency, design doc section 1.6).

This module implements response caching for exact-match requests. When a
request is cached, the gateway returns the cached response without calling
the provider, reducing latency and cost.

Cache key format: `cache:v1:{team_id}:{user_id}:{provider}:{model}:{prompt_hash}:{residency_zone}`
- team_id: for multi-tenant isolation
- user_id: for personalization (per-user caching)
- provider: the provider that generated the response
- model: the model name (normalized to hash for key length)
- prompt_hash: SHA-256 hash of normalized request body
- residency_zone: for DLP/residency boundary enforcement

Cache entries include:
- response_body: the provider's JSON response
- input_tokens: input token count (for cost tracking)
- output_tokens: output token count (for cost tracking)
- expires_at: TTL-based expiry

Key features:
1. **Write-through caching**: Cache is populated after successful provider
   responses, ensuring cached data is always valid.
2. **DLP respect**: A cached response is never served if DLP would block
   the current request.
3. **Residency boundary**: The residency_zone in the cache key ensures
   responses are never served across residency boundaries.
4. **Graceful degradation**: If Redis is unavailable, requests proceed
   normally (fail open).
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.db.models.caching_settings import CachingSettings
from gatekey.db.models.team import Team
from gatekey.providers.model_registry import ModelRoute
from gatekey.services import residency as residency_service
from gatekey.services.shared_state import SharedStateStore

logger = logging.getLogger("gatekey")


def compute_prompt_hash(request_body: dict[str, Any]) -> str:
    """Generate a hash of the normalized request body for exact-match caching.

    Normalization:
    - Sort keys in all dicts
    - Convert to compact JSON

    Returns a 64-character hex string (SHA-256). Public (not `_`-prefixed):
    `api.v1.gateway.common.check_response_cache()`/`write_response_cache()`
    are cross-module callers of this, not just this module's own internal
    helpers.
    """
    normalized = json.dumps(request_body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _cache_key(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    provider: str,
    model: str,
    prompt_hash: str,
    residency_zone: str,
) -> str:
    """Generate Redis cache key.

    Key format: cache:v1:{team_id}:{user_id}:{provider}:{model_hash}:{prompt_hash}:{residency_zone}
    """
    model_hash = hashlib.sha256(model.encode("utf-8")).hexdigest()[:16]
    return f"cache:v1:{team_id}:{user_id}:{provider}:{model_hash}:{prompt_hash}:{residency_zone}"


# ============================================================================
# Caching Settings Cache
# ============================================================================


@dataclass(frozen=True)
class CachingSettingsSnapshot:
    """Immutable snapshot of ORG-level caching settings (the kill switch -
    `caching_settings.enabled`/`.ttl_seconds`)."""

    enabled: bool
    ttl_seconds: int


@dataclass(frozen=True)
class TeamCachingSettingsSnapshot:
    """Immutable snapshot of one TEAM's own opt-in caching columns
    (`teams.cache_enabled`/`teams.cache_ttl_minutes`, migration `0035`) -
    Fix 6 (NFR gap): added alongside `CachingSettingsSnapshot` (org-only)
    above so `CachingSettingsCache` can resolve the full `load_effective_
    caching_config()` decision (org kill switch AND team opt-in) without a
    live DB read - see that function's docstring for why BOTH layers are
    needed, not just the org one."""

    cache_enabled: bool
    cache_ttl_minutes: int


class CachingSettingsCache:
    """Process-local cache of org AND team caching settings.

    Same lock-free, GIL-atomic "replace the whole snapshot, never mutate in
    place" contract as other caches in this codebase. Fix 6: extended to
    also hold every team's own `cache_enabled`/`cache_ttl_minutes` -
    `resolve_effective_caching_config()` below needs both layers to
    reproduce `load_effective_caching_config()`'s live-DB decision from the
    cache alone.
    """

    def __init__(
        self,
        org_settings: dict[uuid.UUID, CachingSettingsSnapshot] | None = None,
        team_settings: dict[uuid.UUID, TeamCachingSettingsSnapshot] | None = None,
    ) -> None:
        self._org_settings: dict[uuid.UUID, CachingSettingsSnapshot] = dict(org_settings or {})
        self._team_settings: dict[uuid.UUID, TeamCachingSettingsSnapshot] = dict(team_settings or {})

    def get_org_settings(self, org_id: uuid.UUID) -> CachingSettingsSnapshot | None:
        return self._org_settings.get(org_id)

    def get_team_settings(self, team_id: uuid.UUID) -> TeamCachingSettingsSnapshot | None:
        return self._team_settings.get(team_id)

    def set_all(
        self,
        org_settings: dict[uuid.UUID, CachingSettingsSnapshot],
        team_settings: dict[uuid.UUID, TeamCachingSettingsSnapshot] | None = None,
    ) -> None:
        """Full replace - the startup-warm write."""
        self._org_settings = dict(org_settings)
        self._team_settings = dict(team_settings or {})

    def set_org_settings(self, org_id: uuid.UUID, settings: CachingSettingsSnapshot | None) -> None:
        replacement = dict(self._org_settings)
        if settings is None:
            replacement.pop(org_id, None)
        else:
            replacement[org_id] = settings
        self._org_settings = replacement

    def set_team_settings(
        self, team_id: uuid.UUID, settings: TeamCachingSettingsSnapshot | None
    ) -> None:
        replacement = dict(self._team_settings)
        if settings is None:
            replacement.pop(team_id, None)
        else:
            replacement[team_id] = settings
        self._team_settings = replacement


async def load_caching_settings_snapshot(
    session: AsyncSession,
) -> tuple[dict[uuid.UUID, CachingSettingsSnapshot], dict[uuid.UUID, TeamCachingSettingsSnapshot]]:
    """Query every caching settings row AND every team's own cache columns -
    used at process startup only.

    Returns `(org_settings, team_settings)`, each a dict keyed by id (Fix 6:
    `team_settings` added alongside the pre-existing `org_settings` dict -
    see `CachingSettingsCache`'s docstring).
    """
    org_rows = (await session.execute(select(CachingSettings))).scalars().all()
    org_settings = {
        row.org_id: CachingSettingsSnapshot(enabled=row.enabled, ttl_seconds=row.ttl_seconds)
        for row in org_rows
    }
    team_rows = (
        await session.execute(select(Team.id, Team.cache_enabled, Team.cache_ttl_minutes))
    ).all()
    team_settings = {
        row.id: TeamCachingSettingsSnapshot(
            cache_enabled=row.cache_enabled, cache_ttl_minutes=row.cache_ttl_minutes
        )
        for row in team_rows
    }
    return org_settings, team_settings


# ============================================================================
# Live per-request gating (Phase 4 gateway-pipeline wiring)
# ============================================================================
#
# Fix 6 (NFR gap - AC4.3.4): `CachingSettingsCache`/`load_caching_settings_
# snapshot()` above are now wired into `main.py`'s startup lifespan (the same
# pattern `ModelPolicyCache`/`ResidencyRuleCache` already use) -
# `resolve_effective_caching_config()` below is the zero-I/O, cache-backed
# replacement for `load_effective_caching_config()`'s live DB read; every
# gateway-pipeline call site (`api.v1.gateway.common.check_response_cache()`)
# now calls the cache-backed version. `load_effective_caching_config()`
# itself is left in place (used by nothing on the hot path anymore) as the
# read-through source of truth `main.py`'s startup warm and the admin write
# endpoints' cache-refresh both still ultimately derive from.


def resolve_effective_caching_config(
    cache: CachingSettingsCache, *, org_id: uuid.UUID, team_id: uuid.UUID | None
) -> tuple[bool, int]:
    """Cache-backed, zero-I/O replacement for `load_effective_caching_
    config()`'s live per-request DB read (Fix 6). Returns `(enabled,
    ttl_seconds)` - byte-for-byte the same org-kill-switch-AND-team-opt-in
    resolution `load_effective_caching_config()` performs (see that
    function's docstring for the exact semantics, including the `team_id
    is None` -> always disabled and "no team row" -> always disabled
    cases), just read from the process-local, admin-write-refreshed cache
    instead of Postgres.
    """
    if team_id is None:
        return False, 0
    team_settings = cache.get_team_settings(team_id)
    if team_settings is None:
        return False, 0
    org_settings = cache.get_org_settings(org_id)
    # Absence of an org cache entry = the documented default state
    # (enabled=true, AC3.5) - same default `load_effective_caching_config()`
    # applies for an absent DB row.
    org_enabled = org_settings.enabled if org_settings is not None else True
    return (org_enabled and team_settings.cache_enabled), team_settings.cache_ttl_minutes * 60


async def load_effective_caching_config(
    session: AsyncSession, *, org_id: uuid.UUID, team_id: uuid.UUID | None
) -> tuple[bool, int]:
    """Resolve whether response caching is active for this request, and its
    TTL in seconds.

    Returns `(enabled, ttl_seconds)`.

    AC4.3.2: `teams.cache_enabled` (default `false`) is the real per-team
    opt-in gate - NOT the legacy `teams.cache_opt_out` column (a
    schema/code-drift artifact from an earlier, inverted-polarity design;
    see `db/models/team.py`'s "cache_enabled / cache_ttl_minutes"
    docstring). The org-level `caching_settings.enabled` remains an
    ADDITIONAL kill switch that wins over a team's own `cache_enabled=true`
    (an org that disables caching org-wide always wins, regardless of any
    team's opt-in) - `enabled = org_enabled AND team.cache_enabled`, never
    `OR`. `team_id=None` (a personal key with no team context) has no team
    row to opt in on, so caching is always disabled on that path - matches
    every other Phase 4 team-gated feature in this codebase.
    """
    if team_id is None:
        return False, 0

    org_row = (
        await session.execute(select(CachingSettings).where(CachingSettings.org_id == org_id))
    ).scalar_one_or_none()
    # Absence of an org row = the documented default state (enabled=true,
    # AC3.5) - see `db/models/caching_settings.py`'s docstring.
    org_enabled = org_row.enabled if org_row is not None else True

    team_row = (
        await session.execute(
            select(Team.cache_enabled, Team.cache_ttl_minutes).where(Team.id == team_id)
        )
    ).one_or_none()
    if team_row is None:
        return False, 0
    team_enabled, team_ttl_minutes = team_row

    return (org_enabled and team_enabled), team_ttl_minutes * 60


def resolve_cache_residency_zone(route: ModelRoute, key_metadata: dict[str, Any] | None) -> str:
    """Resolve the residency-zone component of the cache key (design doc
    section 2.3/5.2, AC3.3's "never serve across a residency boundary").

    Reuses the exact same region resolution `api.v1.gateway.common.
    check_residency()` already performs
    (`services.residency.resolve_model_region`) rather than a separate
    "team's configured residency zone" concept - this codebase's actual
    residency model is a per-request resolved provider/region (openai/
    anthropic static; vertex_ai/ollama from non-secret key metadata), not a
    single stored "team zone" field (the technical design doc's pseudocode
    sketch predates the real schema). `None` (unknown/unconfigured region -
    openrouter, or vertex_ai/ollama with no key metadata yet) normalizes to
    the literal string `"unknown"` so it still partitions the cache key
    away from every known region, rather than colliding with a
    `"global"`/no-restriction bucket.
    """
    region = residency_service.resolve_model_region(route, key_metadata)
    return region if region is not None else "unknown"


# ============================================================================
# Response Cache
# ============================================================================


def _parse_expires_at(value: Any) -> datetime | None:
    """Parse a cache entry's stored `expires_at` field.

    Always written by `ResponseCache.set()` below as an ISO-8601 string
    (`datetime.isoformat()`) - accepts a bare epoch-seconds float/int too as
    defense-in-depth against any pre-existing entry written by an older
    (buggy) build of this module that stored a raw `.timestamp()` float,
    which `datetime > float` would otherwise raise `TypeError` on when
    compared below."""
    if value is None:
        return None
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


@dataclass(frozen=True)
class CacheEntryDetail:
    """Full detail of one cache hit - the cached response body plus the
    metadata needed to attach `X-Cache-TTL` and to charge/log the hit
    without re-deriving anything from the (never re-fetched) provider."""

    response_body: dict[str, Any]
    input_tokens: int
    output_tokens: int
    ttl_remaining_seconds: int


class ResponseCache:
    """Redis-backed response cache for exact-match requests.

    This cache stores provider responses keyed by the normalized request body.
    When a request comes in, we:
    1. Generate a cache key from the request
    2. Try to get from Redis
    3. If hit: return cached response with X-Cache: HIT header
    4. If miss: forward to provider, then cache the response
    """

    def __init__(self, store: SharedStateStore) -> None:
        self._store = store

    async def get_entry(
        self,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        provider: str,
        model: str,
        prompt_hash: str,
        residency_zone: str,
    ) -> CacheEntryDetail | None:
        """Get the full detail of a cached response, or `None` on a miss
        (not found, expired, or a store error - fail open, see module
        docstring point 4)."""
        key = _cache_key(team_id, user_id, provider, model, prompt_hash, residency_zone)

        try:
            raw = await self._store.get_json(key)
            if raw is None:
                return None

            expiry = _parse_expires_at(raw.get("expires_at"))
            if expiry is None:
                return None
            now = datetime.now(timezone.utc)
            if now > expiry:
                # Cache entry expired - delete it (best-effort; a failure
                # here just means it's cleaned up on the next TTL-expired
                # read instead, never re-served as a hit either way).
                await self._store.delete(key)
                return None

            response_body = raw.get("response_body")
            if response_body is None:
                return None
            return CacheEntryDetail(
                response_body=response_body,
                input_tokens=int(raw.get("input_tokens") or 0),
                output_tokens=int(raw.get("output_tokens") or 0),
                ttl_remaining_seconds=max(0, int((expiry - now).total_seconds())),
            )

        except Exception as exc:
            logger.warning("cache_get_error", extra={"key": key, "error": str(exc)})
            return None

    async def get(
        self,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        provider: str,
        model: str,
        prompt_hash: str,
        residency_zone: str,
    ) -> dict[str, Any] | None:
        """Get a cached response body only (back-compat/convenience wrapper
        around `get_entry()`).

        Returns the cached response body if found, None otherwise.
        """
        entry = await self.get_entry(team_id, user_id, provider, model, prompt_hash, residency_zone)
        return entry.response_body if entry is not None else None

    async def set(
        self,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        provider: str,
        model: str,
        prompt_hash: str,
        residency_zone: str,
        response_body: dict[str, Any],
        ttl_seconds: int,
        input_tokens: int = 0,
        output_tokens: int = 0,
    ) -> bool:
        """Store a response in the cache.

        Returns True if the cache was written, False otherwise.
        """
        key = _cache_key(team_id, user_id, provider, model, prompt_hash, residency_zone)

        # Calculate expiry time - always an ISO-8601 string (see
        # `_parse_expires_at()`'s docstring for why `get_entry()` also
        # tolerates a bare epoch-seconds float, defensively).
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()

        cache_entry = {
            "response_body": response_body,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "expires_at": expires_at,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "team_id": str(team_id),
            "user_id": str(user_id),
            "provider": provider,
            "model": model,
        }

        try:
            await self._store.set_json(key, cache_entry, ttl_seconds=ttl_seconds)
            return True
        except Exception as exc:
            logger.warning("cache_set_error", extra={"key": key, "error": str(exc)})
            return False

    async def delete(
        self,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        provider: str,
        model: str,
        prompt_hash: str,
        residency_zone: str,
    ) -> bool:
        """Delete a specific cache entry.

        Returns True if deleted, False if not found or error.
        """
        key = _cache_key(team_id, user_id, provider, model, prompt_hash, residency_zone)

        try:
            await self._store.delete(key)
            return True
        except Exception as exc:
            logger.warning("cache_delete_error", extra={"key": key, "error": str(exc)})
            return False

    async def delete_team(
        self,
        team_id: uuid.UUID,
        residency_zone: str | None = None,
    ) -> int:
        """Delete all cache entries for a team.

        Uses `SharedStateStore.scan_prefix` (Redis `SCAN`, never blocking
        `KEYS` - see that method's docstring) to find every matching key,
        then deletes each one. `residency_zone` is not part of the prefix
        (it's the LAST key segment, after `prompt_hash` - see `_cache_key`)
        so it can't be scanned as a prefix filter; when given, this filters
        the scanned keys client-side by exact suffix match instead. Returns
        the number of entries deleted.
        """
        prefix = f"cache:v1:{team_id}:"
        try:
            keys = await self._store.scan_prefix(prefix)
        except Exception as exc:
            logger.warning("cache_delete_team_error", extra={"team_id": str(team_id), "error": str(exc)})
            return 0

        if residency_zone:
            keys = [key for key in keys if key.endswith(f":{residency_zone}")]

        deleted = 0
        for key in keys:
            try:
                await self._store.delete(key)
                deleted += 1
            except Exception as exc:
                logger.warning("cache_delete_team_error", extra={"team_id": str(team_id), "key": key, "error": str(exc)})
        return deleted

    async def list_entries(self, team_id: uuid.UUID | None, *, limit: int = 200) -> list[dict[str, Any]]:
        """Teaser-only metadata for every live cache entry, optionally
        scoped to one team (AC4.3.9's admin "Caching" screen). Deliberately
        never returns `response_body`/the raw prompt - only routing/cost
        metadata safe to show an Org Admin without re-exposing another
        user's prompt content."""
        prefix = f"cache:v1:{team_id}:" if team_id is not None else "cache:v1:"
        try:
            keys = await self._store.scan_prefix(prefix)
        except Exception as exc:
            logger.warning("cache_list_entries_error", extra={"error": str(exc)})
            return []

        entries: list[dict[str, Any]] = []
        for key in keys[:limit]:
            try:
                raw = await self._store.get_json(key)
            except Exception:
                continue
            if raw is None:
                continue
            entries.append(
                {
                    "key_preview": key,
                    "team_id": raw.get("team_id"),
                    "user_id": raw.get("user_id"),
                    "provider": raw.get("provider"),
                    "model": raw.get("model"),
                    "input_tokens": raw.get("input_tokens", 0),
                    "output_tokens": raw.get("output_tokens", 0),
                    "created_at": raw.get("created_at"),
                    "expires_at": raw.get("expires_at"),
                }
            )
        return entries

    async def get_stats(self, team_id: uuid.UUID) -> dict[str, Any]:
        """Get cache stats for a team.

        Entry count/size are derived live from a `scan_prefix` sweep -
        cheap for an admin-console read (not a gateway hot path). Hit/miss
        counts are NOT tracked here - `cache_lookup_events` (Postgres,
        written by the gateway pipeline) is this codebase's hit/miss audit
        log; `services.cache_stats`/the admin dashboard endpoint compute
        hit rate from that table, not from this method.
        """
        prefix = f"cache:v1:{team_id}:"
        try:
            keys = await self._store.scan_prefix(prefix)
        except Exception as exc:
            logger.warning("cache_get_stats_error", extra={"team_id": str(team_id), "error": str(exc)})
            keys = []

        total_size_bytes = 0
        for key in keys:
            try:
                raw = await self._store.get_json(key)
            except Exception:
                continue
            if raw is not None:
                total_size_bytes += len(json.dumps(raw))

        return {
            "team_id": str(team_id),
            "total_entries": len(keys),
            "total_size_bytes": total_size_bytes,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    async def check_residency_match(
        self,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        provider: str,
        model: str,
        prompt_hash: str,
        requested_residency_zone: str,
    ) -> bool:
        """Check if a cached entry is for the same residency zone.

        This is a security check to ensure we never serve a cached response
        to a request that would violate residency boundaries.
        """
        key = _cache_key(team_id, user_id, provider, model, prompt_hash, requested_residency_zone)

        try:
            raw = await self._store.get_json(key)
            if raw is None:
                return True  # No entry, safe to proceed

            # Check if the cached entry has a residency zone
            cached_residency = raw.get("residency_zone")
            if cached_residency is None:
                return True  # No residency constraint, safe

            return cached_residency == requested_residency_zone

        except Exception as exc:
            logger.warning("cache_residency_check_error", extra={"error": str(exc)})
            return True  # Fail open - allow the request if we can't verify


# ============================================================================
# Cache Invalidation API
# ============================================================================


class CacheInvalidator:
    """API for cache invalidation operations.

    Used by admin endpoints to clear cache entries for debugging or
    compliance reasons.
    """

    def __init__(self, store: SharedStateStore) -> None:
        self._store = store

    async def clear_all(self) -> int:
        """Clear every cache entry, org-wide (use with caution!).

        Bulk invalidation, Org-Admin-only (AC4.3.8's "bulk invalidation
        (all teams, org-wide) available to Org Admin"). `AC4.3.8` describes
        this as a "soft clear (sets a sentinel value, not data deletion)" -
        deviation, flagged: this implementation performs a real Redis `SCAN`
        + `DEL` of every `cache:v1:` key rather than a sentinel/generation
        bump, since `caching_settings`/`response_cache.py` have no existing
        per-team cache-generation counter to bump instead (see design doc
        A1's eviction-policy note - no such mechanism was specified/built).
        Functionally equivalent from a caller's perspective (an old entry
        becomes unreachable either way) but this is real deletion, not
        soft/reversible. Returns the number of entries deleted.
        """
        try:
            keys = await self._store.scan_prefix("cache:v1:")
        except Exception as exc:
            logger.error("cache_clear_all_error", extra={"error": str(exc)})
            return 0
        deleted = 0
        for key in keys:
            try:
                await self._store.delete(key)
                deleted += 1
            except Exception as exc:
                logger.error("cache_clear_all_error", extra={"key": key, "error": str(exc)})
        return deleted

    async def clear_team(self, team_id: uuid.UUID) -> int:
        """Clear all cache entries for a team - see `clear_all`'s docstring
        for the same soft-clear-vs-real-delete deviation note.

        Returns the number of entries deleted.
        """
        try:
            keys = await self._store.scan_prefix(f"cache:v1:{team_id}:")
        except Exception as exc:
            logger.warning("cache_clear_team_error", extra={"team_id": str(team_id), "error": str(exc)})
            return 0
        deleted = 0
        for key in keys:
            try:
                await self._store.delete(key)
                deleted += 1
            except Exception as exc:
                logger.warning(
                    "cache_clear_team_error",
                    extra={"team_id": str(team_id), "key": key, "error": str(exc)},
                )
        return deleted

    async def _clear_team_matching(self, team_id: uuid.UUID, predicate) -> int:  # noqa: ANN001
        """Shared implementation for `clear_provider`/`clear_by_model`:
        `provider`/`model` are not prefix-filterable segments of the cache
        key (they sit after the per-user segment, before the hashed
        `model`/`prompt_hash` components - see `_cache_key`), so this scans
        the team's full prefix and filters by the entry's own stored
        `provider`/`model` fields (written by `ResponseCache.set()`) rather
        than pattern-matching the opaque hashed key itself."""
        try:
            keys = await self._store.scan_prefix(f"cache:v1:{team_id}:")
        except Exception as exc:
            logger.warning("cache_clear_matching_error", extra={"team_id": str(team_id), "error": str(exc)})
            return 0
        deleted = 0
        for key in keys:
            try:
                raw = await self._store.get_json(key)
            except Exception:
                continue
            if raw is None or not predicate(raw):
                continue
            try:
                await self._store.delete(key)
                deleted += 1
            except Exception as exc:
                logger.warning(
                    "cache_clear_matching_error",
                    extra={"team_id": str(team_id), "key": key, "error": str(exc)},
                )
        return deleted

    async def clear_provider(self, team_id: uuid.UUID, provider: str) -> int:
        """Clear all cache entries for a team and provider.

        Returns the number of entries deleted.
        """
        return await self._clear_team_matching(team_id, lambda raw: raw.get("provider") == provider)

    async def clear_by_model(self, team_id: uuid.UUID, model: str) -> int:
        """Clear all cache entries for a team and model.

        Returns the number of entries deleted.
        """
        return await self._clear_team_matching(team_id, lambda raw: raw.get("model") == model)
