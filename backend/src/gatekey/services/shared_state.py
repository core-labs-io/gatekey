"""`SharedStateStore` - the one shared-state mechanism used by every Phase 4
subsystem that needs mutable, per-request-scale state visible across worker
processes (key health, rate-limit counters, cache entries) - see
`docs/design/phase-4-reliability-cost-efficiency-design.md` section 4.1.

Deliberately a single interface with two implementations, not solved
independently per feature (design doc section 4.1 / section 12's
forward-looking flag, carried over from Phase 2/3's own "not solved three
times" note). This module builds ONLY the interface + both implementations;
individual consumers (key health - `services/provider_key_health.py`; rate
limiting/caching - later tasks) own their own key-naming conventions on top
of it.

Which implementation is selected is a `main.py` lifespan concern
(`GATEKEY_REDIS_URL` set -> `RedisSharedStateStore`; unset, the default ->
`InProcessSharedStateStore`) - this module exposes both, unconditionally
importable, and never decides selection itself.

In-process implementation
--------------------------
A process-local dict, every method body a single dict read+write with no
`await` between them - the identical "CPython GIL makes this atomic, no lock
needed" discipline `services.model_policy.ModelPolicyCache` and
`services.cli_refresh_credentials.DeviceAuthStore` already rely on. Accurate
for this project's actual shipped topology (a single backend
container/process per `docker-compose.yml`) - see the design doc's NFR
accounting (section 2) for the documented in-process-vs-Redis trade-off under
an operator-added horizontally-scaled deployment.

Redis implementation
---------------------
`try_consume`/`incr_by` are each a single Lua script (atomic
GET-then-conditional-INCR-then-EXPIRE-if-new-key - the standard Redis
rate-limiting idiom, run server-side so no other client can interleave
between the check and the increment). `get_int`/`get_json`/`set_json` are
plain `GET`/`GET`+json.loads/`SET`(EX). Only imports `redis.asyncio` - never
constructed unless `GATEKEY_REDIS_URL` is configured.
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol


class SharedStateStore(Protocol):
    """See module docstring. Every method is async even on the in-process
    implementation (where nothing actually awaits) so call sites never need
    to know which backend is live."""

    async def try_consume(self, key: str, *, window_seconds: int, limit: int) -> tuple[bool, int]:
        """Atomically: if the current window's count < limit, increments and
        returns (True, new_count); else leaves the counter untouched and
        returns (False, current_count)."""
        ...

    async def incr_by(self, key: str, *, window_seconds: int, amount: int) -> int:
        """Unconditional atomic add, returns the new total."""
        ...

    async def get_int(self, key: str) -> int:
        """Current window count, 0 if absent/expired - never increments."""
        ...

    async def get_json(self, key: str) -> Any | None:
        ...

    async def set_json(self, key: str, value: Any, *, ttl_seconds: int | None) -> None:
        ...

    async def delete(self, key: str) -> None:
        """Unconditionally remove `key` (a no-op if absent). NOTE: prefer
        this over `set_json(key, {}, ttl_seconds=0)` as a "delete" idiom -
        Redis's `SET ... EX 0` is rejected by the server (`ERR invalid
        expire time`), so that idiom only ever worked against the
        in-process backend; this method is the one actually safe against
        both."""
        ...

    async def scan_prefix(self, prefix: str) -> list[str]:
        """Every live key starting with `prefix`, for admin-console listing
        surfaces (e.g. `GET /v1/admin/cache/entries`, AC4.3.9). Uses Redis
        `SCAN` (cursor-based, non-blocking) on the Redis backend - never
        `KEYS`, which blocks the whole Redis instance on a large keyspace.
        Not used on any gateway request hot path - admin-read-only."""
        ...

    async def aclose(self) -> None:
        """Release any held resources (a Redis connection pool; a no-op for
        the in-process store). Called once, from `main.py`'s lifespan
        shutdown."""
        ...


class InProcessSharedStateStore:
    """Default, zero-configuration `SharedStateStore` - see module docstring.

    `window_seconds` on `try_consume`/`incr_by` starts a fresh expiry clock
    only when a key is first created (or has already expired) - exactly the
    "EXPIRE if new key" idiom the Redis implementation also follows, so both
    backends behave identically from a caller's perspective. Lazy expiry
    only (checked on next access) - no background sweep, matching
    `services.response_cache`'s later `OrderedDict` design's own "lazy TTL
    check on get" note (design doc section 5.4) for the same reason: the
    naturally-bounded key spaces this store's first two consumers (key
    health, rate limits) use need no eviction at all.
    """

    def __init__(self) -> None:
        self._counters: dict[str, tuple[int, float]] = {}  # key -> (count, expires_at)
        self._json_store: dict[str, tuple[Any, float | None]] = {}  # key -> (value, expires_at)

    def _live_counter_entry(self, key: str) -> tuple[int, float] | None:
        entry = self._counters.get(key)
        if entry is None:
            return None
        count, expires_at = entry
        if time.monotonic() >= expires_at:
            del self._counters[key]
            return None
        return count, expires_at

    async def try_consume(self, key: str, *, window_seconds: int, limit: int) -> tuple[bool, int]:
        entry = self._live_counter_entry(key)
        current = entry[0] if entry is not None else 0
        if current >= limit:
            return False, current
        expires_at = entry[1] if entry is not None else time.monotonic() + window_seconds
        new_count = current + 1
        self._counters[key] = (new_count, expires_at)
        return True, new_count

    async def incr_by(self, key: str, *, window_seconds: int, amount: int) -> int:
        entry = self._live_counter_entry(key)
        current = entry[0] if entry is not None else 0
        expires_at = entry[1] if entry is not None else time.monotonic() + window_seconds
        new_count = current + amount
        self._counters[key] = (new_count, expires_at)
        return new_count

    async def get_int(self, key: str) -> int:
        entry = self._live_counter_entry(key)
        return entry[0] if entry is not None else 0

    async def get_json(self, key: str) -> Any | None:
        entry = self._json_store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and time.monotonic() >= expires_at:
            del self._json_store[key]
            return None
        return value

    async def set_json(self, key: str, value: Any, *, ttl_seconds: int | None) -> None:
        expires_at = time.monotonic() + ttl_seconds if ttl_seconds is not None else None
        self._json_store[key] = (value, expires_at)

    async def scan_prefix(self, prefix: str) -> list[str]:
        now = time.monotonic()
        return [
            key
            for key, (_value, expires_at) in list(self._json_store.items())
            if key.startswith(prefix) and (expires_at is None or expires_at > now)
        ]

    async def delete(self, key: str) -> None:
        self._json_store.pop(key, None)
        self._counters.pop(key, None)

    async def aclose(self) -> None:  # pragma: no cover - trivial no-op
        return None


# Lua scripts - see module docstring. `KEYS[1]` is the counter key,
# `ARGV[1]` is `window_seconds` (for `EXPIRE`), `ARGV[2]` is `limit`
# (try_consume) / `amount` (incr_by).
_TRY_CONSUME_SCRIPT = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= tonumber(ARGV[2]) then
  return {0, current}
end
local new_count = redis.call('INCR', KEYS[1])
if new_count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return {1, new_count}
"""

_INCR_BY_SCRIPT = """
local existed = redis.call('EXISTS', KEYS[1])
local new_count = redis.call('INCRBY', KEYS[1], ARGV[2])
if existed == 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return new_count
"""


class RedisSharedStateStore:
    """`--profile cache` `SharedStateStore` - see module docstring. Only
    constructed by `main.py`'s lifespan when `GATEKEY_REDIS_URL` is set."""

    def __init__(self, redis_url: str) -> None:
        # Local import: `redis` is a real installed dependency (see
        # pyproject.toml) but importing `redis.asyncio` at call time rather
        # than module-import time keeps this class constructible without
        # requiring a live connection until first use, matching `httpx.
        # AsyncClient`'s own lazy-connect behavior elsewhere in this codebase.
        import redis.asyncio as redis_asyncio

        self._client = redis_asyncio.from_url(redis_url)
        self._try_consume = self._client.register_script(_TRY_CONSUME_SCRIPT)
        self._incr_by = self._client.register_script(_INCR_BY_SCRIPT)

    async def try_consume(self, key: str, *, window_seconds: int, limit: int) -> tuple[bool, int]:
        allowed, new_count = await self._try_consume(keys=[key], args=[window_seconds, limit])
        return bool(allowed), int(new_count)

    async def incr_by(self, key: str, *, window_seconds: int, amount: int) -> int:
        new_count = await self._incr_by(keys=[key], args=[window_seconds, amount])
        return int(new_count)

    async def get_int(self, key: str) -> int:
        value = await self._client.get(key)
        return int(value) if value is not None else 0

    async def get_json(self, key: str) -> Any | None:
        value = await self._client.get(key)
        return json.loads(value) if value is not None else None

    async def set_json(self, key: str, value: Any, *, ttl_seconds: int | None) -> None:
        serialized = json.dumps(value)
        if ttl_seconds is not None:
            await self._client.set(key, serialized, ex=ttl_seconds)
        else:
            await self._client.set(key, serialized)

    async def scan_prefix(self, prefix: str) -> list[str]:
        keys: list[str] = []
        async for raw_key in self._client.scan_iter(match=f"{prefix}*", count=200):
            keys.append(raw_key.decode("utf-8") if isinstance(raw_key, bytes) else raw_key)
        return keys

    async def delete(self, key: str) -> None:
        await self._client.delete(key)

    async def aclose(self) -> None:
        await self._client.aclose()
