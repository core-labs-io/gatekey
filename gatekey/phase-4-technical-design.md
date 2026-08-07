---
title: Gatekey Phase 4 — Reliability & Cost Efficiency
description: Technical Design Document
status: draft
last_updated: 2026-08-05
authors: architect
---

# Gatekey Phase 4 — Reliability & Cost Efficiency
## Technical Design Document

---

## 1. Overview

Phase 4 delivers reliability (failover, rate limiting) and cost efficiency (caching, graceful degradation) features. This document details the technical implementation within the existing tech stack (Python/FastAPI backend, Next.js/React admin console, PostgreSQL).

### 1.1 Key Constraints Carried Forward

| Constraint | Implication |
|------------|-------------|
| Self-hosted first | All features must work offline; Redis is an optional dependency |
| No plaintext keys at rest | All API keys encrypted; health checks use encrypted key decryption |
| OpenAI-compatible API | Response headers only; body shape unchanged |
| DLP/residency boundaries (Phase 3) | Caching and failover must respect residency zones |

### 1.2 Non-Functional Requirements

| NFR | Target | Enforcement |
|-----|--------|-------------|
| Failover switch time | < 2 seconds | Synchronous retry path; async health checks |
| Cache lookup overhead | < 10ms for miss | Local client library cache; minimal serialization |
| Rate limiter distributed | Accurate under multi-instance | Redis sliding window; no local state |
| Cache DLP respect | Never serve across boundary | Key includes team_id, user_id, residency_zone |

---

## 2. System Architecture

### 2.1 Data Flow: Failover Routing

```
┌─────────────────┐
│   Gateway       │
│   Request       │
│   /v1/chat/...  │
└────────┬────────┘
         │
         │ 1. Resolve team_id, provider_id from service account key
         │ 2. Check team.failover_enabled (default: false)
         │
         v
    ┌─────────────┐
    │  Failover   │
    │   Check     │
    └──────┬──────┘
         │
    ┌──────┴──────────────────────────────────────┐
    │ failover_enabled = false                    │
    │ → Direct request to primary key             │
    │ → On error: return immediately              │
    └─────────────────────────────────────────────┘
                    │
    ┌───────────────┴──────────────────────────────┐
    │ failover_enabled = true                      │
    │ → Get backup group: provider_keys WHERE      │
    │   backup_group_id = (primary_key.backup_group_id)│
    │ → Sort keys by availability_24h DESC         │
    └──────────────────────────────────────────────┘
                    │
         ┌──────────┴──────────┐
         │ Attempt primary key │
         └──────────┬──────────┘
                    │
         ┌──────────┴──────────┐    ┌──────────────────┐
         │ Success?            │    │ Failure?         │
         │ (HTTP < 500)        │    │ (HTTP >= 500,    │
         │ → Return response   │    │ network error,   │
         └─────────────────────┘    │ rate_limit code) │
                                    └────────┬─────────┘
                                             │
                                    ┌────────┴──────────┐
                                    │ Retry against     │
                                    │ next backup key   │
                                    │ (with timeout)    │
                                    └────────┬──────────┘
                                             │
                                    ┌────────┴──────────┐
                                    │ All keys failed?  │
                                    │ → Return error    │
                                    │ (with X-Failover: │
                                    │  attempts count)  │
                                    └───────────────────┘
```

**Key Decision: Synchronous Failover (Not Async)**

*Decision:* Failover uses synchronous retries within the request path, not async queuing.

*Rationale:* OpenAI-compatible APIs expect immediate response codes. Async failover would break compatibility. The 2-second NFR requires keeping the retry window tight.

*Trade-off:* A failed primary key plus a slow backup key could approach the 2-second limit. Mitigation: health checks proactively identify degraded keys.

---

### 2.2 Data Flow: Rate Limiting

```
┌──────────────────────────────────────────────────────────────────────────┐
│  Gateway Request                              ┌──────────────────────────┐ │
│  /v1/chat/completions                                      │             │ │
│  ┌───────────────────────────────────────────────────────▼─────────────┐ │ │
│  │ 1. Parse API key → team_id, user_id, provider_id                   │ │ │
│  │ 2. Get rate limit config: team.rate_limit_requests_per_minute      │ │ │
│  │     + user's personal limit (if configured)                        │ │ │
│  │ 3. Get behavior: team.rate_limit_behavior (immediate_reject/       │ │ │
│  │    queue_and_retry)                                                 │ │ │
│  └─────────────────────────────────────────────────────────────────────┘ │ │
│                                            ┌─────────────────────────────┘ │
│                                            │                              │
│                                            │                              │
│  ┌─────────────────────────────────────────▼───────────────────────────┐  │
│  │ Redis Sliding Window Counter                                        │  │
│  │ Key: rate_limit:v1:{team_id}:{user_id}:{provider}:{model}:{window}│  │
│  │ Counter Types: requests, tokens                                     │  │
│  │ Operation: INCRBY + EXPIREAT (sliding window)                       │  │
│  └─────────────────────────────────────────┬───────────────────────────┘  │
│                                            │                              │
│  ┌─────────────────────────────────────────▼───────────────────────────┐  │
│  │ Check Limit                                                         │  │
│  │ If exceeded:                                                        │  │
│  │   - immediate_reject → HTTP 429 + X-RateLimit-* headers            │  │
│  │   - queue_and_retry → Queue request (Redis LIST + TTL)             │  │
│  │ If under limit:                                                     │  │
│  │   - Increment counter                                               │  │
│  │   - Continue to next phase                                          │  │
│  └─────────────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────────────┘
```

**Key Decision: Sliding Window with Redis**

*Decision:* Use Redis `INCRBY` + `EXPIREAT` for sliding window rate limiting.

*Rationale:*
- Naive fixed window counters fail under burst traffic at boundary
- Sliding window provides accurate rate limiting under distributed load
- Redis `PEXPIREAT` with millisecond precision enables accurate windows

*Key Format:* `rate_limit:v1:{team_id}:{user_id}:{provider_id}:{model}:{counter_type}:{window_minutes}`

---

### 2.3 Data Flow: Caching

```
┌────────────────────────────────────────────────────────────────────────────┐
│  Gateway Request                                                          │
│  ┌──────────────────────────────────────────────────────────────────────┐ │
│  │ 1. Check team.cache_enabled (default: false)                        │ │
│  │    If disabled → skip cache entirely                                │ │
│  │ 2. Build cache key:                                                  │ │
│  │    cache:v1:{team_id}:{user_id}:{model}:{prompt_hash}:{residency}  │ │
│  │    (prompt_hash = SHA-256 of normalized request body)               │ │
│  │ 3. Redis GET                                                        │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                             ┌────────────────────────────┘ │
│                                             │                              │
│  ┌──────────────────────────────────────────▼───────────────────────────┐  │
│  │ Cache Hit?                                                             │  │
│  │ - Return cached response with X-Cache: HIT                            │  │
│  │ - Update X-Cache-Stats: hits={count}, ttl_remaining={seconds}        │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                             ┌──────────────────────────────┘ │
│                                             │                                │
│  ┌──────────────────────────────────────────▼───────────────────────────────┐│
│  │ Cache Miss?                                                               ││
│  │ 1. DLP Scan: if blocked/redacted, do NOT cache                           ││
│  │ 2. Forward to provider                                                   ││
│  │ 3. On success:                                                           ││
│  │    - Redis SET with EX (TTL from team.cache_ttl_minutes)                ││
│  │    - Store prompt_hash, input_tokens, output_tokens, response_body      ││
│  │    - Return response with X-Cache: MISS                                  ││
│  └───────────────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────────────┘
```

**Key Decision: Write-Through Only (No Write-Behind)**

*Decision:* Cache write occurs after successful provider response, not before.

*Rationale:* Phase 4 prioritizes simplicity and correctness. Write-behind risks serving incomplete/stale data on network failures. Write-through ensures cached data is always valid.

---

### 2.4 Data Flow: Graceful Degradation

```
┌────────────────────────────────────────────────────────────────────────────┐
│  Policy Resolution Complete                                               │
│  ┌──────────────────────────────────────────────────────────────────────┐ │
│  │ 1. Check team.degradation_enabled (default: false)                  │ │
│  │    If disabled → skip degradation check                             │ │
│  │ 2. Evaluate budget proximity:                                       │ │
│  │    remaining = team.budget_ceiling - current_spend                 │ │
│  │    threshold = team.budget_ceiling * (team.degradation_threshold_pct/100)│ │
│  │    if remaining < threshold → trigger degradation                  │ │
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                             ┌────────────────────────────┘ │
│                                             │                              │
│  ┌──────────────────────────────────────────▼───────────────────────────┐  │
│  │ Degradation Triggered?                                                 │  │
│  │ - Log: original_model, fallback_model, team_id, user_id             │  │
│  │ - Substitute model: team.degradation_fallback_model                 │  │
│  │ - Add response headers:                                              │  │
│  │   X-Gatekey-Degraded: true                                          │  │
│  │   X-Gatekey-Degraded-From: {original_model}                        │  │
│  │   X-Gatekey-Degraded-To: {fallback_model}                          │  │
│  │ - Proceed with substituted model (charge at fallback rate)         │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                             ┌──────────────────────────────┘ │
│                                             │                                │
│  ┌──────────────────────────────────────────▼───────────────────────────────┐│
│  │ Budget Check Passed (Not Near Limit)                                    ││
│  │ - Continue with original model                                          ││
│  │ - No degradation headers added                                          ││
│  └───────────────────────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────────────────────┘
```

**Key Decision: Check After Policy, Before Provider Routing**

*Decision:* Degradation check runs after model-policy resolution but before provider key selection.

*Rationale:*
- Must know the original model to determine if it's expensive
- Must substitute before routing to ensure correct provider selection
- Budget state is cached (1-minute TTL) to avoid DB thrashing

---

## 3. API Contracts

### 3.1 New Endpoints (Admin Console)

| Endpoint | Method | Description | RBAC |
|----------|--------|-------------|------|
| `/api/v1/provider-keys/{id}/health` | POST | Trigger immediate health check | Org Admin, Team Lead |
| `/api/v1/rate-limits` | GET | List rate limit configs (filtered) | Org Admin, Team Lead |
| `/api/v1/rate-limits` | POST | Create/update rate limit config | Org Admin, Team Lead |
| `/api/v1/rate-limits/{id}` | DELETE | Remove rate limit config | Org Admin, Team Lead |
| `/api/v1/cache/entries` | GET | List cache entries (team-filtered) | Org Admin, Team Lead |
| `/api/v1/cache/clear` | POST | Clear cache (team/org-wide) | Org Admin |
| `/api/v1/degradation-events` | GET | List degradation events | Org Admin, Auditor |

### 3.2 Extended Existing Endpoints

#### `/api/v1/provider-keys` (POST)
**Request Body Extension:**
```json
{
  "provider_id": "uuid",
  "team_id": "uuid",
  "key_name": "string",
  "backup_group_id": "string|null",  // NEW: groups keys for failover
  "is_primary": "boolean"            // NEW: identifies primary in group
}
```

#### `/api/v1/teams/{id}` (PATCH)
**Request Body Extension:**
```json
{
  "failover_enabled": "boolean|null",
  "rate_limit_requests_per_minute": "integer|null",
  "rate_limit_tokens_per_minute": "integer|null",
  "rate_limit_behavior": "immediate_reject|queue_and_retry|null",
  "cache_enabled": "boolean|null",
  "cache_ttl_minutes": "integer|null",
  "degradation_enabled": "boolean|null",
  "degradation_threshold_pct": "integer|null",
  "degradation_fallback_model": "string|null"
}
```

### 3.3 Response Headers (OpenAI-Compatible)

#### Cache Headers (AC4.3.6)
| Header | Value | Description |
|--------|-------|-------------|
| `X-Cache` | `HIT` or `MISS` | Cache status |
| `X-Cache-TTL` | `integer` | Seconds until expiry |

#### Failover Headers (AC4.1.7)
| Header | Value | Description |
|--------|-------|-------------|
| `X-Failover-Attempt` | `0` or `integer` | 0 = primary, >0 = retry count |
| `X-Failover-Used-Key` | `key_id` or null | Backup key used (if any) |

#### Rate Limit Headers (AC4.2.6)
| Header | Value | Description |
|--------|-------|-------------|
| `X-RateLimit-Remaining` | `integer` | Requests remaining in window |
| `X-RateLimit-Limit` | `integer` | Configured limit |
| `X-RateLimit-Reset` | `ISO 8601` | Window reset time |
| `Retry-After` | `integer` | Seconds until retry allowed (429 only) |

#### Degradation Headers (AC4.4.4)
| Header | Value | Description |
|--------|-------|-------------|
| `X-Gatekey-Degraded` | `true` or absent | Whether degradation occurred |
| `X-Gatekey-Degraded-From` | `model_name` | Original model |
| `X-Gatekey-Degraded-To` | `model_name` | Substituted model |

---

## 4. Data Model Changes

### 4.1 New Tables

#### `cache_entries` (New Table)
```sql
CREATE TABLE cache_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id UUID NOT NULL REFERENCES teams(id),
    user_id UUID NOT NULL REFERENCES users(id),
    provider_id UUID NOT NULL REFERENCES providers(id),
    model TEXT NOT NULL,
    residency_zone TEXT NOT NULL,
    prompt_hash CHAR(64) NOT NULL,  -- SHA-256 hex
    response_body JSONB NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    
    CONSTRAINT uq_cache_key UNIQUE(team_id, user_id, provider_id, model, residency_zone, prompt_hash)
);

CREATE INDEX idx_cache_entries_expires ON cache_entries(expires_at);
CREATE INDEX idx_cache_entries_team_created ON cache_entries(team_id, created_at);
```

#### `rate_limit_configs` (New Table)
```sql
CREATE TABLE rate_limit_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id UUID NOT NULL REFERENCES teams(id),
    provider_id UUID REFERENCES providers(id),  -- NULL = all providers
    model TEXT,  -- NULL = all models
    requests_per_minute INTEGER,  -- NULL = no limit
    tokens_per_minute INTEGER,    -- NULL = no limit
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_rate_limit UNIQUE(team_id, provider_id, model),
    CONSTRAINT chk_at_least_one_limit CHECK (
        requests_per_minute IS NOT NULL OR tokens_per_minute IS NOT NULL
    )
);

CREATE INDEX idx_rate_limits_team ON rate_limit_configs(team_id);
```

#### `backup_groups` (New Table)
```sql
CREATE TABLE backup_groups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES orgs(id),
    name TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    CONSTRAINT uq_backup_group_name_org UNIQUE(org_id, name)
);

CREATE INDEX idx_backup_groups_org ON backup_groups(org_id);
```

#### `rate_limit_states` (Redis-backed, PostgreSQL for observability)
*Note: This table is only for monitoring/debugging. The actual state is in Redis.*

```sql
CREATE TABLE rate_limit_states (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id UUID NOT NULL,
    user_id UUID,
    provider_id UUID,
    model TEXT,
    counter_type TEXT NOT NULL CHECK(counter_type IN ('requests', 'tokens')),
    window_start TIMESTAMPTZ NOT NULL,
    current_count INTEGER NOT NULL DEFAULT 0,
    last_updated TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 4.2 Modified Tables

#### `teams` (Extended)
```sql
ALTER TABLE teams ADD COLUMN failover_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE teams ADD COLUMN rate_limit_behavior TEXT NOT NULL DEFAULT 'immediate_reject' CHECK(rate_limit_behavior IN ('immediate_reject', 'queue_and_retry'));
ALTER TABLE teams ADD COLUMN cache_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE teams ADD COLUMN cache_ttl_minutes INTEGER NOT NULL DEFAULT 5 CHECK(cache_ttl_minutes >= 1 AND cache_ttl_minutes <= 1440);
ALTER TABLE teams ADD COLUMN degradation_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE teams ADD COLUMN degradation_threshold_pct INTEGER NOT NULL DEFAULT 90 CHECK(degradation_threshold_pct >= 50 AND degradation_threshold_pct <= 99);
ALTER TABLE teams ADD COLUMN degradation_fallback_model TEXT;
```

#### `provider_keys` (Extended)
```sql
ALTER TABLE provider_keys ADD COLUMN backup_group_id UUID REFERENCES backup_groups(id);
ALTER TABLE provider_keys ADD COLUMN is_primary BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE provider_keys ADD COLUMN health_status TEXT NOT NULL DEFAULT 'unknown' CHECK(health_status IN ('unknown', 'healthy', 'degraded', 'unavailable'));
ALTER TABLE provider_keys ADD COLUMN last_health_check TIMESTAMPTZ;
ALTER TABLE provider_keys ADD COLUMN last_error TEXT;
ALTER TABLE provider_keys ADD COLUMN availability_24h NUMERIC(5,4);  -- 0.0000 to 1.0000
ALTER TABLE provider_keys ADD COLUMN last_degraded_at TIMESTAMPTZ;
```

#### `service_account_keys` (Extended)
```sql
ALTER TABLE service_account_keys ADD COLUMN backup_key_id UUID REFERENCES provider_keys(id);
-- Note: backup_key_id is nullable; resolved via join to backup_group
```

#### `request_logs` (Extended)
```sql
ALTER TABLE request_logs ADD COLUMN failover_attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE request_logs ADD COLUMN failover_key_id UUID REFERENCES provider_keys(id);
ALTER TABLE request_logs ADD COLUMN cache_hit BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE request_logs ADD COLUMN degraded_from_model TEXT;
ALTER TABLE request_logs ADD COLUMN degraded_to_model TEXT;
```

#### `degradation_events` (New table for cost savings calculation)
```sql
CREATE TABLE degradation_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id UUID NOT NULL REFERENCES teams(id),
    user_id UUID NOT NULL REFERENCES users(id),
    request_id UUID REFERENCES request_logs(id),
    original_model TEXT NOT NULL,
    degraded_model TEXT NOT NULL,
    original_cost NUMERIC(12,4) NOT NULL,
    degraded_cost NUMERIC(12,4) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_degradation_events_team ON degradation_events(team_id, created_at);
```

---

## 5. Integration Points

### 5.1 Phase 2: Multi-Tenant Hierarchy

| Feature | Integration Point |
|---------|-------------------|
| Failover routing | `provider_keys.team_id → teams.failover_enabled` |
| Rate limiting | `team_id + user_id` from service account key |
| Degradation | `team_id` from key, uses `teams.degradation_*` fields |
| Backup group scoping | `backup_groups.org_id` prevents cross-org key mixing |

**Design Note:** Backup groups are org-scoped (not team-scoped) to allow org-wide key pooling while respecting team-level failover enablement.

### 5.2 Phase 3: DLP/Residency Policy Engine

| Feature | Integration Point |
|---------|-------------------|
| Caching DLP respect | `cache_entries` requires team DLP policy check BEFORE write |
| Residency boundary | Cache key includes `residency_zone` derived from team/org config |
| Nested policy | Degradation model must pass team model policy |

**Key Constraint:** A cached response must never be served if the current request's DLP policy would block/redact it. The cache read happens *before* DLP check, but a miss leads to a DLP check that may result in no cache write.

**Residency Enforcement:**
```python
# Pseudocode
def get_residency_zone(team_id: UUID) -> str:
    team = db.query(Team).get(team_id)
    if team.residency_config.enabled:
        return team.residency_config.zone  # e.g., "EU", "US"
    org = db.query(Org).get(team.org_id)
    if org.residency_config.enabled:
        return org.residency_config.zone
    return "global"  # No restriction

# Cache key format includes zone
cache_key = f"cache:v1:{team_id}:{user_id}:{model}:{prompt_hash}:{residency_zone}"
```

---

## 6. Deployment Considerations

### 6.1 Redis Configuration

**Docker Compose Profile:**
```yaml
# docker-compose.yml
version: '3.8'

services:
  gateway:
    # ... existing ...
    profiles: ["default"]

  redis:
    image: redis:7-alpine
    profiles: ["redis"]
    ports:
      - "6379:6379"
    volumes:
      - redis-data:/data
    command: redis-server --maxmemory-policy allkeys-lru --maxmemory 256mb

volumes:
  redis-data:
```

**Redis Eviction Policy:**
- `allkeys-lru`: Remove least-recently-used keys when memory full
- Matches "cache hit rate" goal by keeping frequently-accessed entries
- Configurable via env var `REDIS_MAXMEMORY_POLICY`

**Key Prefixes:**
- Cache: `cache:v1:`
- Rate limits: `rate_limit:v1:`
- Rate limit queues: `rate_limit_queue:v1:`

### 6.2 Health Check Scheduling

**Background Worker (Cron-style, every 5 minutes):**
```python
# Scheduler config
HEALTH_CHECK_INTERVAL_MINUTES = 5
HEALTH_CHECK_TIMEOUT_SECONDS = 3  # Per-key timeout

# Job: refresh_provider_key_health.py
async def refresh_all_provider_keys_health():
    keys = db.query(ProviderKey).filter(ProviderKey.backup_group_id.isnot(None)).all()
    for key in keys:
        await health_check_single_key(key)
```

**Health Check Endpoint:**
```python
# POST /api/v1/provider-keys/{id}/health
# Returns: {"status": "ok"|"error", "latency_ms": int, "error": string|null}
```

### 6.3 Rate Limiter State

**Redis Structure:**
```redis
# Counter key
rate_limit:v1:team-123:user-456:openai:gpt-4o:requests:1m
  = "42"  (current count)

# Queue key (for queue_and_retry behavior)
rate_limit_queue:v1:team-123:user-456:openai:gpt-4o
  = [
      {"request_id": "...", "timestamp": 1234567890},
      ...
    ]
```

**Background Worker (Queue Processor):**
```python
# Job: process_rate_limit_queue.py
async def process_queue():
    # Poll queue, check if count dropped below limit
    # If yes: process request and remove from queue
    # If no (TTL expired): return 429 to original caller
```

---

## 7. Error Handling and Edge Cases

### 7.1 Failover Scenarios

| Scenario | Handling |
|----------|----------|
| Primary key returns 503 | Retry with next backup key |
| Primary key returns 429 (rate limit) | Retry with next backup key |
| All backup keys return errors | Return last error, add `X-Failover-Final-Error` header |
| Backup key doesn't support requested model | Return immediate error (no further retries) |
| Failover attempt takes >2 seconds | Cancel and return timeout (logs warning) |
| Partial SSE stream (stream cut short) | Treat as failure only if rate_limit error; otherwise return partial with warning |

### 7.2 Cache Edge Cases

| Scenario | Handling |
|----------|----------|
| Cache lookup error (Redis down) | Fall back to provider; log warning |
| Cache write error | Log warning; request still succeeds |
| Team changes cache TTL during TTL window | Current cache entries expire naturally; new entries use new TTL |
| DLP scan blocks request after cache miss | Do NOT cache the blocked request |

### 7.3 Rate Limiter Edge Cases

| Scenario | Handling |
|----------|----------|
| Redis unavailable | Fall back to allow request (degraded mode); log warning |
| Queue TTL expires before limit clears | Return 429 with `Retry-After: 0` |
| Multiple Gateway instances incrementing counter | Redis atomic INCRBY ensures accuracy |
| High-traffic burst at window boundary | Sliding window provides accurate limiting |

### 7.4 Degradation Edge Cases

| Scenario | Handling |
|----------|----------|
| Degradation triggered but fallback model denied by policy | Skip degradation; hard block at budget |
| Fallback model not from allowed team model list | Config validation prevents this at admin level |
| Budget check race condition (concurrent requests) | Budget cache (1-minute TTL) provides eventual consistency |

---

## 8. Security Considerations

### 8.1 API Key Protection

| Concern | Mitigation |
|---------|------------|
| Keys in health check requests | Use encrypted key decryption; never log full key |
| Keys in Redis cache | Cache stores only `prompt_hash`, not full prompt |
| Health check credentials | Use same key encryption as provider keys |

### 8.2 DLP Boundary Enforcement

```python
# Cache write requires ALL conditions:
# 1. DLP scan passed (no block)
# 2. Not a partial/redacted response that would violate policy
# 3. Residency zone matches current request's zone

async def should_cache_response(request: Request, response: ProviderResponse) -> bool:
    # Check DLP policy for team
    dlp_policy = await get_dlp_policy(request.team_id)
    
    if dlp_policy.action == "block":
        return False  # Request already blocked
    
    # Check if response would violate residency
    if not await check_residency_match(request, response):
        return False
    
    return True
```

### 8.3 Audit Logging

All failover attempts, cache hits/misses, and degradation events are logged to `request_logs`:
```sql
INSERT INTO request_logs (
    team_id, user_id, model, provider_id,
    cache_hit, failover_attempt, failover_key_id,
    degraded_from_model, degraded_to_model
) VALUES (...);
```

---

## 9. Testing Strategy

### 9.1 Integration Test Scenarios

| Test Scenario | Priority | Test Type |
|---------------|----------|-----------|
| Failover to backup key on 503 | P0 | Integration |
| Failover uses degraded key (availability < 0.9) | P0 | Integration |
| Failover switch time < 2 seconds | P0 | Integration |
| Cache hit returns `X-Cache: HIT` | P0 | Integration |
| Cache miss falls back to provider | P0 | Integration |
| DLP-blocked request not cached | P0 | Integration |
| Rate limit `immediate_reject` returns 429 | P0 | Integration |
| Rate limit `queue_and_retry` queues request | P0 | Integration |
| Distributed rate limit (3 instances, 150 req/min limit) | P0 | Integration |
| Degradation adds headers on budget threshold | P0 | Integration |
| Degradation respects team model policy | P0 | Integration |
| Redis offline → graceful degradation | P1 | Integration |

### 9.2 Mocking Strategy

| External Service | Mock Approach |
|------------------|---------------|
| Provider APIs (OpenAI, Anthropic) | `pytest-httpx` + `respx` mock responses |
| Redis | `pytest-redis` fixture with in-memory instance |
| S3/Durable storage | Local file system in test mode |

### 9.3 Test Coverage Targets

| Component | Unit Test | Integration Test | End-to-End |
|-----------|-----------|------------------|------------|
| Failover router | 90% | 80% | 70% |
| Rate limiter | 85% | 95% | 85% |
| Cache layer | 80% | 90% | 80% |
| Degradation policy | 90% | 85% | 75% |
| Admin API endpoints | 95% | 90% | 80% |

---

## 10. Implementation Tasks

### 10.1 Database Tasks (Database Admin)

| Task | Priority | Dependencies |
|------|----------|--------------|
| Create `backup_groups` table | P0 | - |
| Create `cache_entries` table | P0 | - |
| Create `rate_limit_configs` table | P0 | - |
| Create `degradation_events` table | P0 | - |
| Extend `teams` with new columns | P0 | - |
| Extend `provider_keys` with new columns | P0 | - |
| Extend `request_logs` with new columns | P0 | - |
| Create migration script | P0 | All above |
| Test migration on dev DB | P1 | Migration script |

**Parallelism:** All schema changes can run in parallel except migrations need final validation.

### 10.2 Backend Tasks (Backend Developer)

| Task | Priority | Dependencies |
|------|----------|--------------|
| **Failover** | | |
| Implement backup group resolution logic | P0 | Schema |
| Implement health check job | P0 | Schema |
| Implement failover router middleware | P0 | Schema |
| **Rate Limiting** | | |
| Implement Redis rate limiter | P0 | Redis deployment |
| Implement rate limit config API | P0 | Schema |
| Implement queue processor worker | P0 | Schema |
| **Caching** | | |
| Implement cache layer | P0 | Redis deployment |
| Implement cache invalidation API | P0 | Schema |
| **Degradation** | | |
| Implement budget proximity check | P0 | Phase 2 budget schema |
| Implement model substitution logic | P0 | Phase 2 model policy |
| **General** | | |
| Health check endpoint | P0 | - |
| Admin console API endpoints | P0 | All above |

**Parallelism:**
- Failover + Degradation can be developed in parallel (both need schema)
- Rate Limiting + Caching can be developed in parallel (both need Redis)
- Admin API endpoints developed alongside features

### 10.3 Frontend Tasks (Frontend Developer)

| Task | Priority | Dependencies |
|------|----------|--------------|
| **Admin Console** | | |
| Provider Keys health status display | P0 | Backend API |
| Failover configuration panel | P0 | Backend API |
| Rate Limits configuration | P0 | Backend API |
| Caching configuration | P0 | Backend API |
| Degradation configuration | P0 | Backend API |
| **Dashboard** | | |
| Cache hit rate metric | P0 | Backend metrics API |
| Failover events metric | P0 | Backend metrics API |
| Cost saved metric | P0 | Backend metrics API |
| Export functionality | P1 | Dashboard metrics |

**Parallelism:** All admin console features can be developed in parallel with backend APIs.

---

## 11. Non-Compliance Risks

| Risk | Mitigation |
|------|------------|
| Failover exceeds 2 seconds | Timeout per request; health checks identify slow keys |
| Cache serves across residency boundary | Key includes `residency_zone`; read validates boundary |
| Rate limiter inaccurate under distributed load | Redis atomic operations; no local state |
| Degradation bypasses DLP | Degradation model must pass team policy |
| Redis connection leaks | Connection pooling; timeout on cache operations |

---

## 12. Known Limitations

| Limitation | Reason | Future Phase |
|------------|--------|--------------|
| No semantic caching (near-duplicate) | As specified in spec as stretch goal only | Phase 5 (if needed) |
| Smart routing based on latency/cost | Only health status drives failover decisions | Phase 6 |
| Per-user degradation policies | Only team-level config in this phase | Phase 6 |
| Automatic fallback model discovery | Must be explicitly configured | Phase 6 |

---

## 13. Success Criteria Verification

| NFR | Verification Method |
|-----|---------------------|
| Failover switch time < 2 seconds | Chaos test: inject 503 on primary, measure to successful backup response |
| Cache lookup overhead < 10ms | Benchmark: measure p99 latency with cache enabled vs disabled on misses |
| Rate limiter accurate under distributed deployment | Integration test: 3 Gateway instances, 150 requests at 100 rpm limit |
| Cache respects DLP/residency | Test: Team A (US) caches; Team B (EU) identical request does NOT hit cache |

---

## 14. Deployment Checklist

### Pre-Deployment
- [ ] Redis deployed and accessible from all Gateway instances
- [ ] Database migrations applied successfully
- [ ] Health check schedule configured (every 5 minutes)
- [ ] Rate limit configs validated for at least one team
- [ ] Backup groups created with at least 2 keys each

### Post-Deployment
- [ ] Health check job running (verify in logs: `Health check completed for X keys`)
- [ ] Rate limit stats visible in admin console
- [ ] Cache hit/miss headers present on test requests
- [ ] Failover test: primary key blocked, request succeeds via backup
- [ ] Degradation test: budget set to trigger point, model substitution occurs

---

*This design document is reference material for implementation. Questions should be routed to the architect via the gatekey project repository.*
