"""Rate limiting service with Redis-backed sliding window counters.

Phase 4 (Reliability & Cost Efficiency, design doc section 1.5).

This module implements distributed rate limiting using Redis sliding window
counters. The key design principles are:

1. **Sliding window counters**: Uses Redis `INCRBY` + `EXPIREAT` to implement
   accurate rate limiting without the "burst at boundary" problem of fixed
   windows.

2. **Distributed-safe**: All state is stored in Redis, so multiple Gateway
   instances can accurately share rate limits without coordination.

3. **Graceful degradation**: If Redis is unavailable, requests are allowed
   through (fail-open) to maintain availability. Logs a warning but doesn't
   fail the request.

4. **Two counter types**: Tracks both requests and tokens (for models with
   token-based pricing).

5. **Queue and retry**: When `on_limit = queue_retry`, requests are queued
   in Redis and retried periodically until the limit clears or TTL expires.

Rate limit key format: `rate_limit:v1:{team_id}:{user_id}:{provider}:{model}:{counter_type}:{window_minutes}`
Queue key format: `rate_limit_queue:v1:{team_id}:{user_id}:{provider}:{model}:{key_id}`

The actual rate limit configuration is stored in the database (`RateLimitRule`)
and loaded into a cache at startup (see `load_rate_limit_cache`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gatekey.constants import DEFAULT_ORG_ID
from gatekey.db.models.rate_limit_rejection_event import (
    RateLimitRejectionEvent,
    RateLimitRejectionOutcome,
)
from gatekey.db.models.rate_limit_rule import RateLimitOnLimit, RateLimitRule, RateLimitScopeType
from gatekey.services.shared_state import SharedStateStore

logger = logging.getLogger("gatekey")


# ============================================================================
# Rate Limit Cache
# ============================================================================


@dataclass(frozen=True)
class RateLimitRuleSnapshot:
    """Immutable snapshot of a rate limit rule configuration.

    Fix 6 (NFR gap): `id`/`scope_type` are carried alongside the limit
    fields (not present in the original snapshot shape) so a snapshot is a
    drop-in, duck-typed substitute for a real `RateLimitRule` ORM row at
    every existing call site that reads `.id`/`.scope_type` off whatever
    rule object it was handed - `check_and_consume_rate_limit()`'s own
    `_pool_rate_limit_key()` (needs `.scope_type` to pick the right Redis
    key shape - see that function's docstring) and `log_rate_limit_
    rejection()` (needs `.id` for the `RateLimitRejectionEvent.rule_id`
    foreign key) neither know nor care whether they were handed this or a
    real `RateLimitRule` row.
    """

    id: uuid.UUID
    scope_type: RateLimitScopeType
    requests_per_min: int | None
    tokens_per_min: int | None
    on_limit: RateLimitOnLimit
    max_queue_wait_seconds: int


# Fix 6: every function below that consumes an already-resolved rule
# (`_pool_rate_limit_key`/`check_and_consume_rate_limit`/`log_rate_limit_
# rejection`) reads only `.id`/`.scope_type`/`.requests_per_min`/`.
# tokens_per_min`/`.on_limit`/`.max_queue_wait_seconds` off it - it never
# needs a real ORM row otherwise (no relationship traversal, no session
# attachment) - so a `RateLimitRuleSnapshot` (cache-backed, Fix 6) is a
# valid, duck-typed substitute for a real `RateLimitRule` (DB-backed,
# pre-Fix-6 tests that still construct one directly) anywhere this alias
# appears.
RateLimitRuleLike = RateLimitRule | RateLimitRuleSnapshot


def snapshot_from_rule(row: RateLimitRule) -> RateLimitRuleSnapshot:
    """Build the `RateLimitCache`-shaped snapshot for one committed
    `RateLimitRule` row. Public (not `_`-prefixed): used both by
    `load_rate_limit_cache_snapshot()`'s startup warm below AND by
    `api.v1.admin.rate_limits.py`'s write endpoints (Fix 6) to refresh the
    cache immediately after a commit - the exact same write-then-refresh-
    cache pattern `services.model_policy.set_team_model_policy()` already
    established."""
    return RateLimitRuleSnapshot(
        id=row.id,
        scope_type=row.scope_type,
        requests_per_min=row.requests_per_min,
        tokens_per_min=row.tokens_per_min,
        on_limit=row.on_limit,
        max_queue_wait_seconds=row.max_queue_wait_seconds,
    )


class RateLimitCache:
    """Process-local cache of rate limit rules.

    Same lock-free, GIL-atomic "replace the whole snapshot, never mutate in
    place" contract as other caches in this codebase.

    Fix 6 (NFR gap - AC4.3.4/cache-lookup-overhead investigation surfaced
    that this cache, unlike `ModelPolicyCache`/`ResidencyRuleCache`, was
    never actually wired into `main.py`'s lifespan or read by the gateway
    hot path): now also holds per-USER rules (`_user_rules`), not just the
    org-default-per-user and team pool rules - `check_rate_limit()`'s live
    per-request read (`load_effective_rate_limit_rules()`) resolves all
    three scopes in one query, so the cache-backed replacement
    (`resolve_effective_rate_limit_rules()` below) needs all three too.
    """

    def __init__(
        self,
        org_rules: dict[uuid.UUID, RateLimitRuleSnapshot] | None = None,
        team_rules: dict[uuid.UUID, RateLimitRuleSnapshot] | None = None,
        user_rules: dict[uuid.UUID, RateLimitRuleSnapshot] | None = None,
    ) -> None:
        self._org_rules: dict[uuid.UUID, RateLimitRuleSnapshot] = dict(org_rules or {})
        self._team_rules: dict[uuid.UUID, RateLimitRuleSnapshot] = dict(team_rules or {})
        self._user_rules: dict[uuid.UUID, RateLimitRuleSnapshot] = dict(user_rules or {})

    def get_org_rule(self, org_id: uuid.UUID) -> RateLimitRuleSnapshot | None:
        return self._org_rules.get(org_id)

    def get_team_rule(self, team_id: uuid.UUID) -> RateLimitRuleSnapshot | None:
        return self._team_rules.get(team_id)

    def get_user_rule(self, user_id: uuid.UUID) -> RateLimitRuleSnapshot | None:
        return self._user_rules.get(user_id)

    def set_all(
        self,
        org_rules: dict[uuid.UUID, RateLimitRuleSnapshot],
        team_rules: dict[uuid.UUID, RateLimitRuleSnapshot],
        user_rules: dict[uuid.UUID, RateLimitRuleSnapshot] | None = None,
    ) -> None:
        """Full replace - the startup-warm write."""
        self._org_rules = dict(org_rules)
        self._team_rules = dict(team_rules)
        self._user_rules = dict(user_rules or {})

    def set_org_rule(self, org_id: uuid.UUID, rule: RateLimitRuleSnapshot | None) -> None:
        replacement = dict(self._org_rules)
        if rule is None:
            replacement.pop(org_id, None)
        else:
            replacement[org_id] = rule
        self._org_rules = replacement

    def set_team_rule(self, team_id: uuid.UUID, rule: RateLimitRuleSnapshot | None) -> None:
        replacement = dict(self._team_rules)
        if rule is None:
            replacement.pop(team_id, None)
        else:
            replacement[team_id] = rule
        self._team_rules = replacement

    def set_user_rule(self, user_id: uuid.UUID, rule: RateLimitRuleSnapshot | None) -> None:
        replacement = dict(self._user_rules)
        if rule is None:
            replacement.pop(user_id, None)
        else:
            replacement[user_id] = rule
        self._user_rules = replacement


async def load_rate_limit_cache_snapshot(
    session: AsyncSession,
) -> tuple[
    dict[uuid.UUID, RateLimitRuleSnapshot],
    dict[uuid.UUID, RateLimitRuleSnapshot],
    dict[uuid.UUID, RateLimitRuleSnapshot],
]:
    """Query every rate limit rule row - used at process startup only.

    Returns (org_rules, team_rules, user_rules) where each is a dict keyed
    by the scope_id (Fix 6: `user_rules` added alongside the pre-existing
    org/team dicts - see `RateLimitCache`'s docstring).
    """
    rows = (await session.execute(select(RateLimitRule))).scalars().all()

    org_rules: dict[uuid.UUID, RateLimitRuleSnapshot] = {}
    team_rules: dict[uuid.UUID, RateLimitRuleSnapshot] = {}
    user_rules: dict[uuid.UUID, RateLimitRuleSnapshot] = {}

    for row in rows:
        rule_snapshot = snapshot_from_rule(row)
        if row.scope_type == RateLimitScopeType.ORG_DEFAULT_PER_USER:
            org_rules[row.org_id] = rule_snapshot
        elif row.scope_type == RateLimitScopeType.TEAM and row.scope_team_id is not None:
            team_rules[row.scope_team_id] = rule_snapshot
        elif row.scope_type == RateLimitScopeType.USER and row.scope_user_id is not None:
            user_rules[row.scope_user_id] = rule_snapshot

    return org_rules, team_rules, user_rules


def resolve_effective_rate_limit_rules(
    cache: RateLimitCache, *, org_id: uuid.UUID, team_id: uuid.UUID | None, user_id: uuid.UUID
) -> tuple[RateLimitRuleSnapshot | None, RateLimitRuleSnapshot | None, RateLimitRuleSnapshot | None]:
    """Cache-backed, zero-I/O replacement for `load_effective_rate_limit_
    rules()`'s live per-request DB read (Fix 6 - see `RateLimitCache`'s
    docstring). Returns `(org_rule, team_rule, user_rule)`, the identical
    shape `check_and_consume_rate_limit()` already accepts - byte-for-byte
    the same resolution `load_effective_rate_limit_rules()` performs, just
    read from the process-local, admin-write-refreshed cache instead of
    Postgres.
    """
    org_rule = cache.get_org_rule(org_id)
    team_rule = cache.get_team_rule(team_id) if team_id is not None else None
    user_rule = cache.get_user_rule(user_id)
    return org_rule, team_rule, user_rule


# ============================================================================
# Rate Limit Key Generation
# ============================================================================


def _rate_limit_key(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    provider: str,
    model: str,
    counter_type: str,
    window_minutes: int = 1,
) -> str:
    """Generate Redis key for a rate limit counter.

    Key format: rate_limit:v1:{team_id}:{user_id}:{provider}:{model}:{counter_type}:{window_minutes}

    The counter_type is either "requests" or "tokens" to track separate limits.
    """
    # Hash the model to keep key lengths reasonable
    model_hash = hashlib.sha256(model.encode("utf-8")).hexdigest()[:16]
    return f"rate_limit:v1:{team_id}:{user_id}:{provider}:{model_hash}:{counter_type}:{window_minutes}m"


def _queue_key(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    provider: str,
    model: str,
    key_id: str,
) -> str:
    """Generate Redis key for a rate limit queue entry.

    Key format: rate_limit_queue:v1:{team_id}:{user_id}:{provider}:{model}:{key_id}
    """
    model_hash = hashlib.sha256(model.encode("utf-8")).hexdigest()[:16]
    return f"rate_limit_queue:v1:{team_id}:{user_id}:{provider}:{model_hash}:{key_id}"


def _queue_state_key(
    team_id: uuid.UUID,
    user_id: uuid.UUID,
    provider: str,
    model: str,
) -> str:
    """Generate Redis key for queue state metadata."""
    model_hash = hashlib.sha256(model.encode("utf-8")).hexdigest()[:16]
    return f"rate_limit_queue_state:v1:{team_id}:{user_id}:{provider}:{model_hash}"


# ============================================================================
# Rate Limiter Core
# ============================================================================


@dataclass(frozen=True)
class RateLimitResult:
    """Result of a rate limit check."""

    allowed: bool
    remaining: int
    limit: int
    reset_at: datetime  # When the window resets (Unix timestamp)
    retry_after_seconds: float | None = None  # How long to wait before retrying (for queue)


class RateLimiter:
    """Redis-backed rate limiter with sliding window counters.

    This class provides the core rate limiting logic. It uses Redis
    `INCRBY` + `EXPIREAT` to implement accurate sliding window rate
    limiting that works correctly across multiple Gateway instances.

    The sliding window is implemented by keeping a counter for each window
    period (e.g., per minute). When checking a rate limit:
    1. Get the current counter value
    2. If below limit, increment and allow
    3. If at/above limit, reject (or queue based on policy)

    Design doc section 1.5: Sliding window vs. fixed window
    --------------------------------------------------------
    Fixed window counters fail under burst traffic at the window boundary.
    If 100 requests come at the very end of window N, and 100 more come at
    the very start of window N+1, the counter resets and allows another 100
    - effectively doubling the rate during the transition.

    Sliding window fixes this by keeping track of requests across the
    boundary. The `INCRBY` + `EXPIREAT` pattern effectively creates a
    rolling window where old requests naturally expire as new ones arrive.
    """

    def __init__(
        self,
        store: SharedStateStore,
        cache: RateLimitCache,
    ) -> None:
        self._store = store
        self._cache = cache

    async def check_and_increment(
        self,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        provider: str,
        model: str,
        *,
        requests: int = 1,
        tokens: int = 0,
    ) -> RateLimitResult:
        """Check rate limits and increment counters if allowed.

        This is the main entry point for rate limiting. It checks both
        request and token limits (if configured) and returns whether the
        request is allowed.

        If Redis is unavailable, this returns `RateLimitResult(allowed=True)`
        to fail open and maintain availability.
        """
        # Try to get the rate limit rule (fail open if cache miss)
        team_rule = self._cache.get_team_rule(team_id)
        org_rule = self._cache.get_org_rule(DEFAULT_ORG_ID)

        # Use team rule if available, otherwise org rule
        rule = team_rule or org_rule

        if rule is None:
            # No rate limit configured - allow everything
            return RateLimitResult(
                allowed=True,
                remaining=0,  # Unknown since no limit
                limit=0,
                reset_at=datetime.now(timezone.utc),
            )

        # Check both request and token limits
        request_result = await self._check_counter(
            team_id,
            user_id,
            provider,
            model,
            counter_type="requests",
            limit=rule.requests_per_min,
            increment=requests,
            window_minutes=1,
        )

        if not request_result.allowed:
            return request_result

        # Check token limit if configured
        if rule.tokens_per_min is not None:
            token_result = await self._check_counter(
                team_id,
                user_id,
                provider,
                model,
                counter_type="tokens",
                limit=rule.tokens_per_min,
                increment=tokens,
                window_minutes=1,
            )
            if not token_result.allowed:
                return token_result

        # Both limits passed
        return RateLimitResult(
            allowed=True,
            remaining=min(
                request_result.remaining,
                rule.tokens_per_min - await self._store.get_int(
                    _rate_limit_key(team_id, user_id, provider, model, "tokens", 1)
                ) if rule.tokens_per_min else request_result.remaining,
            ),
            limit=rule.requests_per_min or 0,
            reset_at=datetime.now(timezone.utc),
        )

    async def _check_counter(
        self,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        provider: str,
        model: str,
        *,
        counter_type: str,
        limit: int | None,
        increment: int,
        window_minutes: int,
    ) -> RateLimitResult:
        """Check a single counter type (requests or tokens).

        Returns RateLimitResult with allowed status and limit info.
        """
        if limit is None:
            # No limit configured for this counter type
            return RateLimitResult(
                allowed=True,
                remaining=0,
                limit=0,
                reset_at=datetime.now(timezone.utc),
            )

        key = _rate_limit_key(team_id, user_id, provider, model, counter_type, window_minutes)
        now = time.time()
        window_seconds = window_minutes * 60

        try:
            # Use try_consume which is atomic: if current < limit, increment and allow
            allowed, new_count = await self._store.try_consume(
                key,
                window_seconds=int(window_seconds),
                limit=limit,
            )

            if allowed:
                # Calculate remaining
                remaining = max(0, limit - new_count)
                reset_at = datetime.fromtimestamp(now + window_seconds, tz=timezone.utc)

                return RateLimitResult(
                    allowed=True,
                    remaining=remaining,
                    limit=limit,
                    reset_at=reset_at,
                )
            else:
                # Rate limit exceeded
                # Get current count for X-RateLimit-Remaining header
                current_count = await self._store.get_int(key)
                remaining = max(0, limit - current_count)

                # Calculate reset time (when the window expires)
                reset_at = datetime.fromtimestamp(now + window_seconds, tz=timezone.utc)

                return RateLimitResult(
                    allowed=False,
                    remaining=remaining,
                    limit=limit,
                    reset_at=reset_at,
                    retry_after_seconds=window_seconds,
                )

        except Exception as exc:
            logger.warning("rate_limit_check_error", extra={"error": str(exc)})
            # Fail open if Redis is unavailable
            return RateLimitResult(
                allowed=True,
                remaining=0,
                limit=limit,
                reset_at=datetime.now(timezone.utc),
            )

    async def queue_request(
        self,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        provider: str,
        model: str,
        key_id: str,
        payload: dict[str, Any],
        max_wait_seconds: int,
    ) -> bool:
        """Queue a request for later retry when the rate limit clears.

        Returns True if queued, False if queue is full or TTL would exceed max_wait.
        """
        queue_key = _queue_key(team_id, user_id, provider, model, key_id)
        now = time.time()

        try:
            # Check current queue size (max 100 pending requests per user)
            current_queue = await self._store.get_json(queue_key)
            if current_queue is None:
                current_queue = []
            if len(current_queue) >= 100:
                return False

            # Calculate TTL (max_wait_seconds from now)
            ttl = int(max_wait_seconds)
            if ttl <= 0:
                return False

            # Add to queue with timestamp
            entry = {
                "id": key_id,
                "timestamp": now,
                "payload": payload,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            current_queue.append(entry)

            # Store queue with TTL
            await self._store.set_json(queue_key, current_queue, ttl_seconds=ttl)

            # Update queue state for monitoring
            state_key = _queue_state_key(team_id, user_id, provider, model)
            state = {
                "queue_size": len(current_queue),
                "last_added": now,
                "user_id": str(user_id),
            }
            await self._store.set_json(state_key, state, ttl_seconds=ttl)

            return True

        except Exception as exc:
            logger.warning("rate_limit_queue_error", extra={"error": str(exc)})
            return False

    async def process_queue(
        self,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        provider: str,
        model: str,
    ) -> list[dict[str, Any]]:
        """Process the rate limit queue and return items that can be retried.

        This is called by the queue processor background worker to retry
        queued requests when the limit has cleared.
        """
        queue_key = _queue_key(team_id, user_id, provider, model)
        now = time.time()

        try:
            raw = await self._store.get_json(queue_key)
            if raw is None:
                return []

            # Filter out expired entries
            valid_entries = [e for e in raw if now - e.get("timestamp", 0) < 300]  # 5 min max age

            # Sort by timestamp (oldest first)
            valid_entries.sort(key=lambda e: e.get("timestamp", 0))

            return valid_entries

        except Exception as exc:
            logger.warning("rate_limit_process_queue_error", extra={"error": str(exc)})
            return []

    async def clear_queue(
        self,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        provider: str,
        model: str,
    ) -> bool:
        """Clear the rate limit queue for a user.

        Called after a queued request is successfully processed or times out.
        """
        queue_key = _queue_key(team_id, user_id, provider, model)

        try:
            await self._store.set_json(queue_key, [], ttl_seconds=0)
            return True
        except Exception as exc:
            logger.warning("rate_limit_clear_queue_error", extra={"error": str(exc)})
            return False

    # ============================================================================
    # Admin API helpers
    # ============================================================================

    async def get_rate_limit_state(
        self,
        team_id: uuid.UUID,
        user_id: uuid.UUID,
        provider: str,
        model: str,
    ) -> dict[str, Any]:
        """Get current rate limit state for monitoring/debugging."""
        requests_key = _rate_limit_key(team_id, user_id, provider, model, "requests", 1)
        tokens_key = _rate_limit_key(team_id, user_id, provider, model, "tokens", 1)

        try:
            requests_count = await self._store.get_int(requests_key)
            tokens_count = await self._store.get_int(tokens_key)
        except Exception as exc:
            logger.warning("rate_limit_state_error", extra={"error": str(exc)})
            requests_count = 0
            tokens_count = 0

        return {
            "team_id": str(team_id),
            "user_id": str(user_id),
            "provider": provider,
            "model": model,
            "requests_count": requests_count,
            "tokens_count": tokens_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }


# ============================================================================
# Queue Processor Background Worker
# ============================================================================


class RateLimitQueueProcessor:
    """Background worker to process queued rate limit requests.

    This runs periodically to check the rate limit queue and retry requests
    that have been waiting for the limit to clear.
    """

    def __init__(
        self,
        store: SharedStateStore,
        cache: RateLimitCache,
        on_request_ready: Awaitable[None],
    ) -> None:
        self._store = store
        self._cache = cache
        self._on_request_ready = on_request_ready
        self._running = False

    async def start(self) -> None:
        """Start the background worker loop."""
        self._running = True
        # In production, this would run as a background task
        # For now, the implementation is a placeholder for the logic

    async def stop(self) -> None:
        """Stop the background worker."""
        self._running = False

    async def process_all_queues(self) -> int:
        """Process all queued requests across all teams.

        Returns the number of requests retried.
        """
        # This is a placeholder - in production, this would:
        # 1. Scan all rate_limit_queue:* keys
        # 2. For each queue, check if rate limit has cleared
        # 3. If yes, process the next request and remove from queue
        return 0


# ============================================================================
# Live per-request enforcement (Phase 4 gateway-pipeline wiring)
# ============================================================================
#
# `RateLimitCache`/`load_rate_limit_cache_snapshot()` above are built for a
# process-wide cache warmed from `main.py`'s startup lifespan (the same
# pattern `ModelPolicyCache`/`ResidencyRuleCache` use) - but wiring a new
# cache into `main.py`'s lifespan was out of scope for the task that added
# this section (see the gateway-pipeline-wiring change notes). The functions
# below instead read `RateLimitRule` through to the database on every call -
# the same "not cacheable this phase, always read through" tradeoff
# `services.budget.get_budget_state`/`check_budget_available` already accept
# on this exact hot path. Flagged to the architect as a latency/read-count
# tradeoff worth revisiting if a future task wires the process-cache path
# into `main.py`.
#
# Key design note: `RateLimitRule` (see `db/models/rate_limit_rule.py`) has
# NO `provider`/`model` columns - a rule is scoped to org-default-per-user,
# one team, or one user, uniformly across every provider/model that caller
# uses. The Redis key formats below therefore deliberately do NOT include
# provider/model (unlike `_rate_limit_key`/`_queue_key` above, which do and
# are reserved for a possible future finer-grained rule shape) - keying
# finer than the actual configured scope would let a caller evade a limit
# simply by rotating models. AC4.2.9's additive "team pool AND personal
# user limit" is a Redis key per side.
#
# Fix 2 (QA/security review finding, fixed here): a TEAM-scoped pool rule is
# a genuinely SHARED, team-wide ceiling (AC4.2.6's own worked example - "the
# Redis key ... holds a GLOBAL count"; AC4.2.9's "100 shared team pool") -
# its Redis key must therefore be `team_id`-only, incremented by every user
# on the team against the SAME counter. An ORG_DEFAULT_PER_USER pool rule is
# a different thing entirely - it is explicitly a PER-USER default (the name
# says so), not a shared org-wide pool, so its key correctly stays keyed by
# `user_id` alone, exactly as before. `_pool_rate_limit_key` was previously
# keyed by `(team_id or NIL, user_id)` for BOTH cases - collapsing the
# genuinely-shared TEAM case down to a per-user copy of the limit, so a team
# configured with a 100rpm "shared" limit never actually capped the team's
# aggregate traffic at 100rpm; each distinct user got their own independent
# 100rpm allowance. Fixed by branching on the rule's own `scope_type`.

_WINDOW_SECONDS = 60


def _pool_rate_limit_key(
    pool_rule: RateLimitRuleLike,
    team_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    *,
    counter_type: str = "requests",
) -> str:
    """Resolve the Redis key for the (already-resolved) pool rule - see
    module note above for why this branches on `pool_rule.scope_type`
    rather than using one shape for every pool rule. `user_id` is only
    actually used (and must be non-`None`) for the ORG_DEFAULT_PER_USER
    branch - the TEAM branch ignores it entirely (the whole point of Fix 2).

    Hardening pass item 4: `counter_type` (`"requests"` default, or
    `"tokens"`) selects which of the two independently-tracked counters this
    key addresses - same key-format convention as `_rate_limit_key()`'s own
    `counter_type` segment above, just applied to the pool/personal key
    shapes `check_and_consume_rate_limit()` actually uses on the live path.
    """
    if pool_rule.scope_type == RateLimitScopeType.TEAM:
        assert team_id is not None  # a TEAM-scoped rule is only ever resolved when team_id is set
        return f"rate_limit:v1:pool:team:{team_id}:{counter_type}:1m"
    # ORG_DEFAULT_PER_USER - explicitly a per-user default, not a shared
    # org-wide pool (only the TEAM scope claims to be shared - see above).
    assert user_id is not None
    return f"rate_limit:v1:pool:org:{user_id}:{counter_type}:1m"


def _personal_rate_limit_key(user_id: uuid.UUID, *, counter_type: str = "requests") -> str:
    return f"rate_limit:v1:user:{user_id}:{counter_type}:1m"


async def load_effective_rate_limit_rules(
    session: AsyncSession, *, org_id: uuid.UUID, team_id: uuid.UUID | None, user_id: uuid.UUID
) -> tuple[RateLimitRule | None, RateLimitRule | None, RateLimitRule | None]:
    """Live per-request lookup of the org-default, team, and per-user
    `RateLimitRule` rows applicable to this caller. Returns
    `(org_rule, team_rule, user_rule)` - any may be `None` (unconfigured).
    One query (three partial-unique-indexed point lookups, OR'd together).
    """
    conditions = [RateLimitRule.scope_type == RateLimitScopeType.ORG_DEFAULT_PER_USER]
    if team_id is not None:
        conditions.append(
            (RateLimitRule.scope_type == RateLimitScopeType.TEAM)
            & (RateLimitRule.scope_team_id == team_id)
        )
    conditions.append(
        (RateLimitRule.scope_type == RateLimitScopeType.USER)
        & (RateLimitRule.scope_user_id == user_id)
    )
    stmt = select(RateLimitRule).where(RateLimitRule.org_id == org_id, or_(*conditions))
    rows = (await session.execute(stmt)).scalars().all()

    org_rule: RateLimitRule | None = None
    team_rule: RateLimitRule | None = None
    user_rule: RateLimitRule | None = None
    for row in rows:
        if row.scope_type == RateLimitScopeType.ORG_DEFAULT_PER_USER:
            org_rule = row
        elif row.scope_type == RateLimitScopeType.TEAM:
            team_rule = row
        elif row.scope_type == RateLimitScopeType.USER:
            user_rule = row
    return org_rule, team_rule, user_rule


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of `check_and_consume_rate_limit()` - both the "allowed,
    attach headers" case and the "rejected" case share this one shape
    (AC4.2.7: headers are attached on every request, limited or not)."""

    configured: bool  # False = no rule applies at all; caller attaches no headers.
    allowed: bool
    remaining: int
    limit: int
    reset_at: datetime
    retry_after_seconds: int
    on_limit: RateLimitOnLimit | None
    max_queue_wait_seconds: int
    rule: RateLimitRuleLike | None  # the rule that produced/failed this decision, for rejection logging


_UNCONFIGURED_DECISION = RateLimitDecision(
    configured=False,
    allowed=True,
    remaining=0,
    limit=0,
    reset_at=datetime.now(timezone.utc),
    retry_after_seconds=0,
    on_limit=None,
    max_queue_wait_seconds=0,
    rule=None,
)


def _headroom_fraction(decision: RateLimitDecision) -> float:
    """`remaining / limit` - used ONLY to pick which of several simultaneously-
    PASSING decisions (Hardening pass item 4: now potentially a mix of
    requests-axis and tokens-axis decisions, whose raw `limit`/`remaining`
    magnitudes are not comparable - a `tokens_per_min` limit is typically
    orders of magnitude larger than a `requests_per_min` one) is "closer to
    its limit" for `X-RateLimit-*` header-reporting purposes (this module's
    `RateLimitDecision`/`build_rate_limit_headers()` in `api.v1.gateway.
    common` have always been a single generic remaining/limit pair, not
    per-counter-type fields - see that function's docstring for the current
    header contract this preserves). Never used to decide whether a request
    is ALLOWED - that is decided purely by each individual check's own
    pass/fail outcome, unaffected by this comparison. `limit <= 0` (a
    configured zero-allowance rule) sorts as maximally "at its limit"."""
    if decision.limit <= 0:
        return 0.0
    return decision.remaining / decision.limit


async def check_and_consume_rate_limit(
    store: SharedStateStore,
    *,
    org_rule: RateLimitRuleLike | None,
    team_rule: RateLimitRuleLike | None,
    user_rule: RateLimitRuleLike | None,
    team_id: uuid.UUID | None,
    user_id: uuid.UUID,
) -> RateLimitDecision:
    """Additively enforce the resolved pool rule (team rule, falling back to
    the org default-per-user rule) AND the personal user-scope rule
    (AC4.2.2/AC4.2.9) - exceeding EITHER trips the limit. Each configured
    side is checked+incremented via its own independent sliding-window
    counter (see module note above for why); a request that fails a LATER
    check after an EARLIER one already incremented is accepted as a minor,
    documented over-count against the earlier counter, the same "fail toward
    availability, not toward perfect accounting" posture `RateLimiter.
    _check_counter`'s Redis-unavailable fail-open path already takes
    elsewhere in this module.

    Hardening pass item 4 (AC4.2.1's `tokens_per_min` was previously
    validated/stored but never enforced here - see this module's git-blame-
    adjacent design doc section 4.3's own pseudocode, which this now
    implements literally): for EACH configured rule (pool and/or personal),
    BOTH axes are checked when configured -
    - `requests_per_min`: pre-emptive, exactly as before - `store.
      try_consume()` atomically increments-and-gates in one step (AC2.3).
    - `tokens_per_min`: retrospective/gate-only (AC2.4's explicit "never
      estimate/pre-charge tokens against the limit" - mirrors `services.
      budget.check_budget_available()`'s own accepted "can only check
      whether already over budget from PRIOR requests" semantics). This is
      a pure `store.get_int()` READ, never incremented here - trips only
      when tokens already consumed by prior requests in the current rolling
      window are already at/over the limit. The actual increment happens
      AFTER a successful provider response, via `record_token_usage()`
      below (called from `api.v1.gateway.common.record_usage_charge()`,
      the one shared choke point every gateway charge flows through) - this
      function never touches the tokens counter's value, only reads it.

    A rule can trip on EITHER axis independently; the first axis that trips
    (checked in requests-then-tokens order per rule, pool rule before
    personal rule - same rule-iteration order as before this change) ends
    the loop immediately and returns that decision, exactly like the
    pre-existing pool-vs-personal short-circuit. Every remaining passing
    decision (whichever axis, whichever rule) competes for `best_passing`
    via `_headroom_fraction()` (see its own docstring for why a raw
    `remaining` comparison, correct when every check was request-count-only,
    stops being meaningful once token-count decisions - typically far
    larger raw numbers - are mixed in).
    """
    pool_rule = team_rule or org_rule
    # Each entry: (rule, requests_key, tokens_key) - a rule participates at
    # all only when it configures at least one of the two axes.
    checks: list[tuple[RateLimitRuleLike, str, str]] = []
    if pool_rule is not None and (
        pool_rule.requests_per_min is not None or pool_rule.tokens_per_min is not None
    ):
        checks.append(
            (
                pool_rule,
                _pool_rate_limit_key(pool_rule, team_id, user_id, counter_type="requests"),
                _pool_rate_limit_key(pool_rule, team_id, user_id, counter_type="tokens"),
            )
        )
    if user_rule is not None and (
        user_rule.requests_per_min is not None or user_rule.tokens_per_min is not None
    ):
        checks.append(
            (
                user_rule,
                _personal_rate_limit_key(user_id, counter_type="requests"),
                _personal_rate_limit_key(user_id, counter_type="tokens"),
            )
        )

    if not checks:
        return _UNCONFIGURED_DECISION

    reset_at = datetime.now(timezone.utc) + timedelta(seconds=_WINDOW_SECONDS)
    best_passing: RateLimitDecision | None = None

    for rule, requests_key, tokens_key in checks:
        if rule.requests_per_min is not None:
            limit = rule.requests_per_min
            try:
                allowed, count = await store.try_consume(
                    requests_key, window_seconds=_WINDOW_SECONDS, limit=limit
                )
            except Exception as exc:  # pragma: no cover - defensive, mirrors RateLimiter's own fail-open
                logger.warning("rate_limit_enforce_error", extra={"error": str(exc)})
                allowed, count = True, 0
            decision = RateLimitDecision(
                configured=True,
                allowed=allowed,
                remaining=max(0, limit - count),
                limit=limit,
                reset_at=reset_at,
                retry_after_seconds=0 if allowed else _WINDOW_SECONDS,
                on_limit=rule.on_limit,
                max_queue_wait_seconds=rule.max_queue_wait_seconds,
                rule=rule,
            )
            if not allowed:
                return decision
            if best_passing is None or _headroom_fraction(decision) < _headroom_fraction(best_passing):
                best_passing = decision

        if rule.tokens_per_min is not None:
            token_limit = rule.tokens_per_min
            try:
                tokens_used = await store.get_int(tokens_key)
            except Exception as exc:  # pragma: no cover - defensive, same fail-open posture
                logger.warning("rate_limit_enforce_error", extra={"error": str(exc)})
                tokens_used = 0
            token_allowed = tokens_used < token_limit
            decision = RateLimitDecision(
                configured=True,
                allowed=token_allowed,
                remaining=max(0, token_limit - tokens_used),
                limit=token_limit,
                reset_at=reset_at,
                retry_after_seconds=0 if token_allowed else _WINDOW_SECONDS,
                on_limit=rule.on_limit,
                max_queue_wait_seconds=rule.max_queue_wait_seconds,
                rule=rule,
            )
            if not token_allowed:
                return decision
            if best_passing is None or _headroom_fraction(decision) < _headroom_fraction(best_passing):
                best_passing = decision

    assert best_passing is not None
    return best_passing


async def record_token_usage(
    store: SharedStateStore,
    cache: RateLimitCache,
    *,
    org_id: uuid.UUID,
    team_id: uuid.UUID | None,
    user_id: uuid.UUID,
    total_tokens: int,
) -> None:
    """Post-response token-count accounting (Hardening pass item 4, design
    doc section 4.4's exact mechanic) - the ONLY place this module's token
    counters are ever incremented (`check_and_consume_rate_limit()` above
    only ever READS them - AC2.4). Call this exactly once per successfully
    charged request, AFTER the provider response's real token usage is
    known - `api.v1.gateway.common.record_usage_charge()` is the single
    shared choke point every gateway charge (all three routes, streaming and
    non-streaming) flows through, so this is wired there, mirroring how
    BD-18's threshold-alert check is wired exactly once in that same
    function.

    Increments the SAME pool/personal token keys `check_and_consume_rate_
    limit()` reads, using the identical rule-resolution (`resolve_effective_
    rate_limit_rules()`, cache-backed/zero-I/O) - a rule with no `tokens_
    per_min` configured is silently skipped (nothing to accumulate against),
    matching that function's own per-axis gating. `total_tokens <= 0` is a
    no-op (an embeddings/chat request always reports a real prompt-token
    count, but this guards defensively against a theoretical zero-usage
    response rather than issuing a pointless `INCRBY 0`). Best-effort/fail-
    open (catches and logs, never raises) - same posture as every other
    Redis-backed operation in this module; a failure here must never turn an
    already-successful, already-charged provider response into an error.
    """
    if total_tokens <= 0:
        return
    org_rule, team_rule, user_rule = resolve_effective_rate_limit_rules(
        cache, org_id=org_id, team_id=team_id, user_id=user_id
    )
    pool_rule = team_rule or org_rule
    try:
        if pool_rule is not None and pool_rule.tokens_per_min is not None:
            await store.incr_by(
                _pool_rate_limit_key(pool_rule, team_id, user_id, counter_type="tokens"),
                window_seconds=_WINDOW_SECONDS,
                amount=total_tokens,
            )
        if user_rule is not None and user_rule.tokens_per_min is not None:
            await store.incr_by(
                _personal_rate_limit_key(user_id, counter_type="tokens"),
                window_seconds=_WINDOW_SECONDS,
                amount=total_tokens,
            )
    except Exception as exc:  # pragma: no cover - defensive, mirrors this module's other fail-open paths
        logger.warning("rate_limit_token_accounting_error", extra={"error": str(exc)})


async def log_rate_limit_rejection(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    rule: RateLimitRuleLike | None,
    team_id: uuid.UUID | None,
    user_id: uuid.UUID,
    outcome: RateLimitRejectionOutcome,
) -> None:
    """Persist one `RateLimitRejectionEvent` row (design doc section 7.1's
    per-rule `rejection_count` dashboard column) - written synchronously,
    before the caller raises `errors.RateLimitExceededError`, mirroring
    Phase 3's established residency/DLP/schedule-block convention exactly
    (a raised exception has no live response for `BackgroundTasks` to run
    after)."""
    scope_type = rule.scope_type if rule is not None else RateLimitScopeType.ORG_DEFAULT_PER_USER
    event = RateLimitRejectionEvent(
        org_id=org_id,
        rule_id=rule.id if rule is not None else None,
        scope_type=scope_type,
        scope_team_id=team_id,
        user_id=user_id,
        outcome=outcome,
    )
    session.add(event)
    await session.commit()


# ============================================================================
# Admin "current utilization" read-only status (AC4.2.8)
# ============================================================================


@dataclass(frozen=True)
class RateLimitRuleStatus:
    """Result of `get_rule_current_status()` - AC4.2.8's "current
    utilization" admin read. `available=False` means the number could not be
    read from the real store this request (Redis unreachable, or - for a
    team/org-scoped rule - no `user_id` was given to resolve the actual
    per-user Redis key the live pipeline uses); `reason` explains why. This
    is deliberately never estimated/computed a different way than what
    actually gates requests (see docstring on `get_rule_current_status`) -
    "not available" beats a fabricated number.
    """

    available: bool
    reason: str | None
    requests_limit: int | None
    requests_used_last_60s: int | None
    requests_remaining: int | None
    queue_depth: int | None
    queue_depth_tracked: bool


async def get_rule_current_status(
    store: SharedStateStore,
    *,
    rule: RateLimitRule,
    user_id: uuid.UUID | None,
) -> RateLimitRuleStatus:
    """Read (never increment) the SAME Redis counter `check_and_consume_
    rate_limit()` above actually gates live requests against, for one rule.

    Key resolution mirrors `check_and_consume_rate_limit()` exactly (Fix 2 -
    see `_pool_rate_limit_key`'s module note):
    - `scope_type == user`: the rule's own `scope_user_id` fixes the key
      (`_personal_rate_limit_key`) - no `user_id` param needed.
    - `scope_type == team`: a genuinely team-wide SHARED counter, keyed by
      `team_id` alone - no `user_id` needed to resolve it either, since
      there is exactly one counter for the whole team now (not one per
      member).
    - `scope_type == org_default_per_user`: still explicitly a PER-USER
      default (the name says so), so a specific `user_id` must be supplied
      to resolve which underlying counter to read; without one, this
      returns `available=False` rather than inventing an aggregate number
      the live pipeline doesn't itself compute anywhere.

    `queue_depth` is always `None`/`queue_depth_tracked=False`: the live
    `queue_and_retry` path (`api.v1.gateway.common.check_rate_limit`) polls
    `check_and_consume_rate_limit()` in-process and never writes to
    `RateLimiter.queue_request`'s Redis-backed queue (that class exists but
    is not wired into any live call site) - there is no real persisted
    queue-depth number to read. Flagged to the architect (design doc
    AC4.2.5/AC4.2.8): either wire `queue_and_retry` through an actual
    Redis-backed queue, or drop the "queue depth" admin-console requirement
    for the in-process-poll implementation actually shipped.
    """
    if rule.scope_type == RateLimitScopeType.USER:
        key = _personal_rate_limit_key(rule.scope_user_id)  # type: ignore[arg-type]
    elif rule.scope_type == RateLimitScopeType.TEAM:
        key = _pool_rate_limit_key(rule, rule.scope_team_id, None)
    else:
        if user_id is None:
            return RateLimitRuleStatus(
                available=False,
                reason=(
                    "user_id query parameter is required to read current utilization for "
                    "an org-default-per-user rule - the live rate limiter tracks this "
                    "counter per user, not as one org-wide aggregate."
                ),
                requests_limit=rule.requests_per_min,
                requests_used_last_60s=None,
                requests_remaining=None,
                queue_depth=None,
                queue_depth_tracked=False,
            )
        key = _pool_rate_limit_key(rule, None, user_id)

    if rule.requests_per_min is None:
        return RateLimitRuleStatus(
            available=True,
            reason=None,
            requests_limit=None,
            requests_used_last_60s=None,
            requests_remaining=None,
            queue_depth=None,
            queue_depth_tracked=False,
        )

    try:
        used = await store.get_int(key)
    except Exception as exc:
        logger.warning("rate_limit_status_read_error", extra={"error": str(exc)})
        return RateLimitRuleStatus(
            available=False,
            reason="Rate limit store (Redis) is not reachable.",
            requests_limit=rule.requests_per_min,
            requests_used_last_60s=None,
            requests_remaining=None,
            queue_depth=None,
            queue_depth_tracked=False,
        )

    return RateLimitRuleStatus(
        available=True,
        reason=None,
        requests_limit=rule.requests_per_min,
        requests_used_last_60s=used,
        requests_remaining=max(0, rule.requests_per_min - used),
        queue_depth=None,
        queue_depth_tracked=False,
    )
