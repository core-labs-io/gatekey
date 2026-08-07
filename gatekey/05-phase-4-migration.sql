-- =============================================================================
-- Gatekey Phase 4 Database Migration
-- Reliability & Cost Efficiency Features
-- =============================================================================
-- This migration implements:
--   1. Backup Groups for multi-key failover configuration
--   2. Cache Entries for response caching with TTL
--   3. Rate Limit Configs for distributed rate limiting
--   4. Degradation Events for cost savings tracking
--   5. Rate Limit States for observability (Redis-backed)
--   6. Extended tables: teams, provider_keys, request_logs
--
-- All changes are PostgreSQL 15+ compliant with RLS support.
-- =============================================================================

-- =============================================================================
-- UP MIGRATION (Apply)
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Section 1: New Tables
-- -----------------------------------------------------------------------------

-- -----------------------------------------------------------------------------
-- 1.1 backup_groups: Group provider keys for failover routing
-- -----------------------------------------------------------------------------
-- A backup group contains multiple keys that can serve as backups for each other.
-- Keys in the same group share the same backup_group_id.
-- Groups are org-scoped to prevent cross-org key mixing (multi-tenant isolation).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backup_groups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_backup_group_name_org UNIQUE(org_id, name)
);

COMMENT ON TABLE backup_groups IS 'Groups provider keys for failover routing. Keys in the same group can serve as backups for each other. Org-scoped for multi-tenant isolation.';
COMMENT ON COLUMN backup_groups.org_id IS 'Org ID for multi-tenant isolation. Keys can only reference backup groups within their own org.';
COMMENT ON COLUMN backup_groups.name IS 'Human-readable name for the backup group (unique per org).';
COMMENT ON COLUMN backup_groups.description IS 'Optional description of the backup group''s purpose.';

CREATE INDEX idx_backup_groups_org ON backup_groups(org_id);
CREATE INDEX idx_backup_groups_created ON backup_groups(created_at);

-- -----------------------------------------------------------------------------
-- 1.2 cache_entries: Response caching with TTL
-- -----------------------------------------------------------------------------
-- Cache entries store exact-match responses for identical requests.
-- Cache keys are derived from: team_id, user_id, provider_id, model, prompt_hash, residency_zone
-- Respects DLP/residency boundaries from Phase 3.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS cache_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider_id UUID NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    residency_zone TEXT NOT NULL,
    prompt_hash CHAR(64) NOT NULL,
    response_body JSONB NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,

    CONSTRAINT uq_cache_key UNIQUE(team_id, user_id, provider_id, model, residency_zone, prompt_hash)
);

COMMENT ON TABLE cache_entries IS 'Response cache entries for exact-match requests. Stores provider response bodies with TTL expiry.';

COMMENT ON COLUMN cache_entries.team_id IS 'Team context for the cache entry. Enforces multi-tenant isolation.';
COMMENT ON COLUMN cache_entries.user_id IS 'User context for the cache entry. Personalized caching.';
COMMENT ON COLUMN cache_entries.provider_id IS 'Provider that generated this response.';
COMMENT ON COLUMN cache_entries.model IS 'Model name used for the request.';
COMMENT ON COLUMN cache_entries.residency_zone IS 'Residency zone for DLP compliance. Prevents serving across policy boundaries.';
COMMENT ON COLUMN cache_entries.prompt_hash IS 'SHA-256 hash of normalized request body. Used for exact-match lookup.';
COMMENT ON COLUMN cache_entries.response_body IS 'Cached provider response body (JSONB for flexibility).';
COMMENT ON COLUMN cache_entries.input_tokens IS 'Input token count from original request (for cost tracking).';
COMMENT ON COLUMN cache_entries.output_tokens IS 'Output token count from original request (for cost tracking).';
COMMENT ON COLUMN cache_entries.created_at IS 'Timestamp when this cache entry was created.';
COMMENT ON COLUMN cache_entries.expires_at IS 'Timestamp when this cache entry should be considered expired. TTL-based expiry.';

CREATE INDEX idx_cache_entries_expires ON cache_entries(expires_at);
CREATE INDEX idx_cache_entries_team_created ON cache_entries(team_id, created_at);
CREATE INDEX idx_cache_entries_user_created ON cache_entries(user_id, created_at);
CREATE INDEX idx_cache_entries_residency ON cache_entries(residency_zone);

-- -----------------------------------------------------------------------------
-- 1.3 rate_limit_configs: Per-team, per-provider rate limiting configuration
-- -----------------------------------------------------------------------------
-- Rate limits are configured per team/provider/model combination.
-- At least one limit (requests or tokens) must be configured for the rate limiter to be active.
-- Implements sliding window rate limiting via Redis.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rate_limit_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    provider_id UUID REFERENCES providers(id) ON DELETE SET NULL,
    model TEXT,
    requests_per_minute INTEGER,
    tokens_per_minute INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_rate_limit UNIQUE(team_id, provider_id, model),
    CONSTRAINT chk_at_least_one_limit CHECK (
        requests_per_minute IS NOT NULL OR tokens_per_minute IS NOT NULL
    )
);

COMMENT ON TABLE rate_limit_configs IS 'Rate limit configuration per team/provider/model. At least one limit must be configured.';

COMMENT ON COLUMN rate_limit_configs.team_id IS 'Team this rate limit applies to.';
COMMENT ON COLUMN rate_limit_configs.provider_id IS 'Provider to rate limit. NULL = all providers for this team.';
COMMENT ON COLUMN rate_limit_configs.model IS 'Model to rate limit. NULL = all models for this team/provider.';
COMMENT ON COLUMN rate_limit_configs.requests_per_minute IS 'Maximum requests per minute. NULL = no request limit.';
COMMENT ON COLUMN rate_limit_configs.tokens_per_minute IS 'Maximum tokens per minute. NULL = no token limit.';
COMMENT ON COLUMN rate_limit_configs.created_at IS 'Timestamp when this config was created.';
COMMENT ON COLUMN rate_limit_configs.updated_at IS 'Timestamp when this config was last updated.';

CREATE INDEX idx_rate_limits_team ON rate_limit_configs(team_id);
CREATE INDEX idx_rate_limits_provider ON rate_limit_configs(provider_id);
CREATE INDEX idx_rate_limits_team_provider ON rate_limit_configs(team_id, provider_id);

-- -----------------------------------------------------------------------------
-- 1.4 rate_limit_states: Observability table for Redis-backed rate limiting
-- -----------------------------------------------------------------------------
-- This table tracks Redis rate limit state for observability/monitoring purposes.
-- The actual state is stored in Redis; this PostgreSQL table is for dashboard views.
-- NOT used for actual rate limiting logic (which uses Redis sliding windows).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rate_limit_states (
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

COMMENT ON TABLE rate_limit_states IS 'Observability table for Redis rate limit state. Tracks sliding window counters for dashboard monitoring. Actual state is in Redis.';
COMMENT ON COLUMN rate_limit_states.team_id IS 'Team context for the rate limit state.';
COMMENT ON COLUMN rate_limit_states.user_id IS 'User context for the rate limit state (for per-user limits).';
COMMENT ON COLUMN rate_limit_states.provider_id IS 'Provider context for the rate limit state.';
COMMENT ON COLUMN rate_limit_states.model IS 'Model context for the rate limit state.';
COMMENT ON COLUMN rate_limit_states.counter_type IS 'Type of counter: requests or tokens.';
COMMENT ON COLUMN rate_limit_states.window_start IS 'Start time of the current sliding window.';
COMMENT ON COLUMN rate_limit_states.current_count IS 'Current count in the sliding window.';
COMMENT ON COLUMN rate_limit_states.last_updated IS 'Last time this state was updated.';

CREATE INDEX idx_rate_limit_states_team ON rate_limit_states(team_id);
CREATE INDEX idx_rate_limit_states_window ON rate_limit_states(team_id, window_start);
CREATE INDEX idx_rate_limit_states_counter ON rate_limit_states(counter_type);

-- -----------------------------------------------------------------------------
-- 1.5 degradation_events: Track model downgrades for cost savings calculation
-- -----------------------------------------------------------------------------
-- Records when graceful degradation substitutes an expensive model with a cheaper fallback.
-- Used to calculate cost savings via the dashboard.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS degradation_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    team_id UUID NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    request_id UUID REFERENCES request_logs(id) ON DELETE SET NULL,
    original_model TEXT NOT NULL,
    degraded_model TEXT NOT NULL,
    original_cost NUMERIC(12,4) NOT NULL,
    degraded_cost NUMERIC(12,4) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE degradation_events IS 'Records model downgrades via graceful degradation. Used for cost savings calculation in the dashboard.';

COMMENT ON COLUMN degradation_events.team_id IS 'Team where degradation occurred.';
COMMENT ON COLUMN degradation_events.user_id IS 'User whose request was downgraded.';
COMMENT ON COLUMN degradation_events.request_id IS 'Reference to the original request log entry (nullable for audit trail).';
COMMENT ON COLUMN degradation_events.original_model IS 'Model name that was originally requested.';
COMMENT ON COLUMN degradation_events.degraded_model IS 'Model name that was substituted (cheaper fallback).';
COMMENT ON COLUMN degradation_events.original_cost IS 'Cost that would have been incurred with the original model.';
COMMENT ON COLUMN degradation_events.degraded_cost IS 'Actual cost incurred with the degraded model.';
COMMENT ON COLUMN degradation_events.created_at IS 'Timestamp when degradation occurred.';

CREATE INDEX idx_degradation_events_team ON degradation_events(team_id, created_at);
CREATE INDEX idx_degradation_events_user ON degradation_events(user_id, created_at);
CREATE INDEX idx_degradation_events_request ON degradation_events(request_id);

-- -----------------------------------------------------------------------------
-- Section 2: Extended Tables
-- -----------------------------------------------------------------------------

-- -----------------------------------------------------------------------------
-- 2.1 teams: Extended with failover, rate limiting, caching, degradation settings
-- -----------------------------------------------------------------------------
-- New columns for Phase 4 features:
--   - failover_enabled: Enable automatic failover to backup keys
--   - rate_limit_behavior: immediate_reject vs queue_and_retry
--   - cache_enabled: Enable response caching
--   - cache_ttl_minutes: Cache TTL (1-1440 minutes)
--   - degradation_enabled: Enable graceful degradation
--   - degradation_threshold_pct: Budget proximity threshold (50-99%)
--   - degradation_fallback_model: Model to use when degraded
-- -----------------------------------------------------------------------------
ALTER TABLE teams ADD COLUMN IF NOT EXISTS failover_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE teams ADD COLUMN IF NOT EXISTS rate_limit_behavior TEXT NOT NULL DEFAULT 'immediate_reject' CHECK(rate_limit_behavior IN ('immediate_reject', 'queue_and_retry'));
ALTER TABLE teams ADD COLUMN IF NOT EXISTS cache_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE teams ADD COLUMN IF NOT EXISTS cache_ttl_minutes INTEGER NOT NULL DEFAULT 5 CHECK(cache_ttl_minutes >= 1 AND cache_ttl_minutes <= 1440);
ALTER TABLE teams ADD COLUMN IF NOT EXISTS degradation_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE teams ADD COLUMN IF NOT EXISTS degradation_threshold_pct INTEGER NOT NULL DEFAULT 90 CHECK(degradation_threshold_pct >= 50 AND degradation_threshold_pct <= 99);
ALTER TABLE teams ADD COLUMN IF NOT EXISTS degradation_fallback_model TEXT;

COMMENT ON COLUMN teams.failover_enabled IS 'Enable automatic failover to backup keys on provider failure. Default false (safe default).';
COMMENT ON COLUMN teams.rate_limit_behavior IS 'Behavior when rate limit is hit: immediate_reject (default) or queue_and_retry.';
COMMENT ON COLUMN teams.cache_enabled IS 'Enable response caching for this team. Default false (opt-in feature).';
COMMENT ON COLUMN teams.cache_ttl_minutes IS 'Cache TTL in minutes (1-1440). Default 5 minutes.';
COMMENT ON COLUMN teams.degradation_enabled IS 'Enable graceful degradation when budget threshold is reached. Default false.';
COMMENT ON COLUMN teams.degradation_threshold_pct IS 'Budget proximity threshold (50-99%). When remaining budget < ceiling * (threshold/100), degradation triggers. Default 90%.';
COMMENT ON COLUMN teams.degradation_fallback_model IS 'Model name to use when graceful degradation triggers. Must be from team''s allowed model list.';

-- -----------------------------------------------------------------------------
-- 2.2 provider_keys: Extended with backup group association and health tracking
-- -----------------------------------------------------------------------------
-- New columns for Phase 4 features:
--   - backup_group_id: Links key to a backup group for failover
--   - is_primary: Identifies primary key in group (for health check scheduling)
--   - health_status: Current health status from scheduled checks
--   - last_health_check: Timestamp of last health check
--   - last_error: Last error message from health check
--   - availability_24h: 24-hour rolling availability percentage
--   - last_degraded_at: When key was last marked degraded
-- -----------------------------------------------------------------------------
ALTER TABLE provider_keys ADD COLUMN IF NOT EXISTS backup_group_id UUID REFERENCES backup_groups(id) ON DELETE SET NULL;
ALTER TABLE provider_keys ADD COLUMN IF NOT EXISTS is_primary BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE provider_keys ADD COLUMN IF NOT EXISTS health_status TEXT NOT NULL DEFAULT 'unknown' CHECK(health_status IN ('unknown', 'healthy', 'degraded', 'unavailable'));
ALTER TABLE provider_keys ADD COLUMN IF NOT EXISTS last_health_check TIMESTAMPTZ;
ALTER TABLE provider_keys ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE provider_keys ADD COLUMN IF NOT EXISTS availability_24h NUMERIC(5,4) CHECK(availability_24h IS NULL OR (availability_24h >= 0 AND availability_24h <= 1));
ALTER TABLE provider_keys ADD COLUMN IF NOT EXISTS last_degraded_at TIMESTAMPTZ;

COMMENT ON COLUMN provider_keys.backup_group_id IS 'Reference to backup group for failover. NULL = no backup configuration.';
COMMENT ON COLUMN provider_keys.is_primary IS 'Whether this is the primary key in its backup group. Primary keys are checked first; backups only used on failure.';
COMMENT ON COLUMN provider_keys.health_status IS 'Calculated health status from health checks: unknown (initial), healthy, degraded (availability < 0.9), unavailable.';
COMMENT ON COLUMN provider_keys.last_health_check IS 'Timestamp of the last health check attempt.';
COMMENT ON COLUMN provider_keys.last_error IS 'Last error message from health check (if any).';
COMMENT ON COLUMN provider_keys.availability_24h IS '24-hour rolling availability percentage (0.0000 to 1.0000). Calculated from success/failure ratio.';
COMMENT ON COLUMN provider_keys.last_degraded_at IS 'Timestamp when this key was last marked degraded (availability dropped below 0.9).';

-- Create index for health check scheduling job
CREATE INDEX idx_provider_keys_backup_group ON provider_keys(backup_group_id) WHERE backup_group_id IS NOT NULL;
CREATE INDEX idx_provider_keys_health ON provider_keys(health_status);
CREATE INDEX idx_provider_keys_last_health ON provider_keys(last_health_check);

-- -----------------------------------------------------------------------------
-- 2.3 request_logs: Extended with failover, cache hit, and degradation tracking
-- -----------------------------------------------------------------------------
-- New columns for Phase 4 features:
--   - failover_attempt: 0 = primary, >0 = retry count
--   - failover_key_id: Backup key used (if any)
--   - cache_hit: Whether request was served from cache
--   - degraded_from_model: Original model (when degradation occurred)
--   - degraded_to_model: Substituted model (when degradation occurred)
-- -----------------------------------------------------------------------------
ALTER TABLE request_logs ADD COLUMN IF NOT EXISTS failover_attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE request_logs ADD COLUMN IF NOT EXISTS failover_key_id UUID REFERENCES provider_keys(id) ON DELETE SET NULL;
ALTER TABLE request_logs ADD COLUMN IF NOT EXISTS cache_hit BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE request_logs ADD COLUMN IF NOT EXISTS degraded_from_model TEXT;
ALTER TABLE request_logs ADD COLUMN IF NOT EXISTS degraded_to_model TEXT;

COMMENT ON COLUMN request_logs.failover_attempt IS 'Failover attempt number: 0 = primary key, 1 = first backup retry, etc.';
COMMENT ON COLUMN request_logs.failover_key_id IS 'Backup key used for this request (NULL if primary succeeded or no failover).';
COMMENT ON COLUMN request_logs.cache_hit IS 'True if response was served from cache. Indicates cost savings.';
COMMENT ON COLUMN request_logs.degraded_from_model IS 'Original model requested (when graceful degradation occurred).';
COMMENT ON COLUMN request_logs.degraded_to_model IS 'Substituted model used (when graceful degradation occurred).';

-- Create index for failover event queries
CREATE INDEX idx_request_logs_failover ON request_logs(failover_attempt) WHERE failover_attempt > 0;
CREATE INDEX idx_request_logs_cache_hit ON request_logs(cache_hit);

-- -----------------------------------------------------------------------------
-- Section 3: Row-Level Security (RLS)
-- -----------------------------------------------------------------------------
-- Enable RLS on new tables and existing tables with new columns to ensure
-- multi-tenant isolation for Phase 4 features.
-- -----------------------------------------------------------------------------

-- Enable RLS on new tables
ALTER TABLE backup_groups ENABLE ROW LEVEL SECURITY;
ALTER TABLE cache_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE degradation_events ENABLE ROW LEVEL SECURITY;

-- Policy for backup_groups: Org members can read/write within their org
CREATE POLICY backup_groups_select ON backup_groups FOR SELECT USING (
    org_id IN (SELECT org_id FROM team_members WHERE user_id = current_setting('app.current_user_id', true)::UUID)
);

CREATE POLICY backup_groups_insert ON backup_groups FOR INSERT WITH CHECK (
    org_id IN (SELECT org_id FROM team_members WHERE user_id = current_setting('app.current_user_id', true)::UUID)
);

CREATE POLICY backup_groups_update ON backup_groups FOR UPDATE USING (
    org_id IN (SELECT org_id FROM team_members WHERE user_id = current_setting('app.current_user_id', true)::UUID)
);

-- Policy for cache_entries: Team members can read/write within their team
CREATE POLICY cache_entries_select ON cache_entries FOR SELECT USING (
    team_id IN (SELECT team_id FROM team_members WHERE user_id = current_setting('app.current_user_id', true)::UUID)
);

CREATE POLICY cache_entries_insert ON cache_entries FOR INSERT WITH CHECK (
    team_id IN (SELECT team_id FROM team_members WHERE user_id = current_setting('app.current_user_id', true)::UUID)
);

-- Policy for degradation_events: Team members can read within their team
CREATE POLICY degradation_events_select ON degradation_events FOR SELECT USING (
    team_id IN (SELECT team_id FROM team_members WHERE user_id = current_setting('app.current_user_id', true)::UUID)
);

CREATE POLICY degradation_events_insert ON degradation_events FOR INSERT WITH CHECK (
    team_id IN (SELECT team_id FROM team_members WHERE user_id = current_setting('app.current_user_id', true)::UUID)
);

-- -----------------------------------------------------------------------------
-- Section 4: Additional Indexes (CONCURRENTLY for non-blocking)
-- -----------------------------------------------------------------------------
-- Create indexes that may benefit query performance without blocking writes.

-- Cache expiry queries benefit from partial index
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_cache_entries_expires_active ON cache_entries(expires_at) WHERE expires_at > NOW();

-- Rate limit config lookup for request processing
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_rate_limits_team_provider_model ON rate_limit_configs(team_id, provider_id, model);

--Degradation events dashboard queries
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_degradation_events_team_period ON degradation_events(team_id, created_at);

-- -----------------------------------------------------------------------------
-- Section 5: View for Cache Hit Statistics
-- -----------------------------------------------------------------------------
-- Provides aggregate cache performance metrics per team.

CREATE OR REPLACE VIEW cache_stats AS
SELECT
    c.team_id,
    COUNT(*) FILTER (WHERE c.cache_hit = true) AS total_hits,
    COUNT(*) FILTER (WHERE c.cache_hit = false) AS total_misses,
    ROUND(
        (COUNT(*) FILTER (WHERE c.cache_hit = true)::NUMERIC / NULLIF(COUNT(*), 0)) * 100,
        2
    ) AS hit_rate,
    AVG(EXTRACT(EPOCH FROM (NOW() - c.created_at))) FILTER (WHERE c.cache_hit = true) AS avg_cache_age_seconds
FROM request_logs c
GROUP BY c.team_id;

COMMENT ON VIEW cache_stats IS 'Aggregate cache performance metrics per team: hit count, miss count, hit rate, and average cache age.';

-- -----------------------------------------------------------------------------
-- Section 6: Function for Health Status Calculation
-- -----------------------------------------------------------------------------
-- Calculates health status based on availability_24h value.
-- Used by health check jobs to update provider_keys.

CREATE OR REPLACE FUNCTION calculate_health_status(availability_24h NUMERIC)
RETURNS TEXT AS $$
BEGIN
    IF availability_24h IS NULL THEN
        RETURN 'unknown';
    ELSIF availability_24h >= 0.99 THEN
        RETURN 'healthy';
    ELSIF availability_24h >= 0.9 THEN
        RETURN 'degraded';
    ELSE
        RETURN 'unavailable';
    END IF;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

COMMENT ON FUNCTION calculate_health_status IS 'Determines health status based on 24-hour availability percentage. Returns: healthy (>=99%), degraded (90-98%), unavailable (<90%), unknown (NULL).';

-- -----------------------------------------------------------------------------
-- Section 7: Trigger for updating updated_at timestamp
-- -----------------------------------------------------------------------------
-- Automatically updates the updated_at column on rate_limit_configs.

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_rate_limit_configs_updated_at
    BEFORE UPDATE ON rate_limit_configs
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- =============================================================================
-- DOWN MIGRATION (Revert)
-- =============================================================================
-- This section contains the SQL to undo all changes from the UP migration.
-- Execute in reverse order to maintain referential integrity.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Drop functions and views (must be done before dropping tables)
-- -----------------------------------------------------------------------------

DROP VIEW IF EXISTS cache_stats;
DROP FUNCTION IF EXISTS calculate_health_status(NUMERIC);
DROP TRIGGER IF EXISTS update_rate_limit_configs_updated_at ON rate_limit_configs;

-- -----------------------------------------------------------------------------
-- Remove RLS policies and disable RLS on new tables
-- -----------------------------------------------------------------------------

DROP POLICY IF EXISTS backup_groups_select ON backup_groups;
DROP POLICY IF EXISTS backup_groups_insert ON backup_groups;
DROP POLICY IF EXISTS backup_groups_update ON backup_groups;
DROP POLICY IF EXISTS cache_entries_select ON cache_entries;
DROP POLICY IF EXISTS cache_entries_insert ON cache_entries;
DROP POLICY IF EXISTS degradation_events_select ON degradation_events;
DROP POLICY IF EXISTS degradation_events_insert ON degradation_events;

ALTER TABLE backup_groups DISABLE ROW LEVEL SECURITY;
ALTER TABLE cache_entries DISABLE ROW LEVEL SECURITY;
ALTER TABLE degradation_events DISABLE ROW LEVEL SECURITY;

-- -----------------------------------------------------------------------------
-- Drop indexes (drop constraints that create indexes first)
-- -----------------------------------------------------------------------------

-- Drop indexes on new tables
DROP INDEX IF EXISTS idx_backup_groups_org;
DROP INDEX IF EXISTS idx_backup_groups_created;
DROP INDEX IF EXISTS idx_cache_entries_expires;
DROP INDEX IF EXISTS idx_cache_entries_team_created;
DROP INDEX IF EXISTS idx_cache_entries_user_created;
DROP INDEX IF EXISTS idx_cache_entries_residency;
DROP INDEX IF EXISTS idx_rate_limits_team;
DROP INDEX IF EXISTS idx_rate_limits_provider;
DROP INDEX IF EXISTS idx_rate_limits_team_provider;
DROP INDEX IF EXISTS idx_rate_limit_states_team;
DROP INDEX IF EXISTS idx_rate_limit_states_window;
DROP INDEX IF EXISTS idx_rate_limit_states_counter;
DROP INDEX IF EXISTS idx_degradation_events_team;
DROP INDEX IF EXISTS idx_degradation_events_user;
DROP INDEX IF EXISTS idx_degradation_events_request;
DROP INDEX IF EXISTS idx_degradation_events_team_period;
DROP INDEX IF EXISTS idx_provider_keys_backup_group;
DROP INDEX IF EXISTS idx_provider_keys_health;
DROP INDEX IF EXISTS idx_provider_keys_last_health;
DROP INDEX IF EXISTS idx_request_logs_failover;
DROP INDEX IF EXISTS idx_request_logs_cache_hit;

-- Drop concurrent indexes
DROP INDEX IF EXISTS idx_cache_entries_expires_active;
DROP INDEX IF EXISTS idx_rate_limits_team_provider_model;

-- -----------------------------------------------------------------------------
-- Drop new tables (in dependency order)
-- -----------------------------------------------------------------------------

DROP TABLE IF EXISTS degradation_events;
DROP TABLE IF EXISTS rate_limit_states;
DROP TABLE IF EXISTS rate_limit_configs;
DROP TABLE IF EXISTS cache_entries;
DROP TABLE IF EXISTS backup_groups;

-- -----------------------------------------------------------------------------
-- Remove extended columns from existing tables
-- -----------------------------------------------------------------------------

-- teams: Remove Phase 4 columns
ALTER TABLE teams DROP COLUMN IF EXISTS failover_enabled;
ALTER TABLE teams DROP COLUMN IF EXISTS rate_limit_behavior;
ALTER TABLE teams DROP COLUMN IF EXISTS cache_enabled;
ALTER TABLE teams DROP COLUMN IF EXISTS cache_ttl_minutes;
ALTER TABLE teams DROP COLUMN IF EXISTS degradation_enabled;
ALTER TABLE teams DROP COLUMN IF EXISTS degradation_threshold_pct;
ALTER TABLE teams DROP COLUMN IF EXISTS degradation_fallback_model;

-- provider_keys: Remove Phase 4 columns
ALTER TABLE provider_keys DROP COLUMN IF EXISTS backup_group_id;
ALTER TABLE provider_keys DROP COLUMN IF EXISTS is_primary;
ALTER TABLE provider_keys DROP COLUMN IF EXISTS health_status;
ALTER TABLE provider_keys DROP COLUMN IF EXISTS last_health_check;
ALTER TABLE provider_keys DROP COLUMN IF EXISTS last_error;
ALTER TABLE provider_keys DROP COLUMN IF EXISTS availability_24h;
ALTER TABLE provider_keys DROP COLUMN IF EXISTS last_degraded_at;

-- request_logs: Remove Phase 4 columns
ALTER TABLE request_logs DROP COLUMN IF EXISTS failover_attempt;
ALTER TABLE request_logs DROP COLUMN IF EXISTS failover_key_id;
ALTER TABLE request_logs DROP COLUMN IF EXISTS cache_hit;
ALTER TABLE request_logs DROP COLUMN IF EXISTS degraded_from_model;
ALTER TABLE request_logs DROP COLUMN IF EXISTS degraded_to_model;

-- =============================================================================
-- Migration complete
-- =============================================================================
-- Summary of changes:
--   - 5 new tables created
--   - 3 existing tables extended with new columns
--   - 2 new views/functions for operational insights
--   - RLS policies enabled on new tables
--   - Multiple indexes for performance
--   - All changes are reversible via the DOWN section
-- =============================================================================
