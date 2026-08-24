/**
 * Gatekey admin console - API client (Phase 1).
 *
 * Wired directly to the real backend endpoints documented in
 * gatekey/phase-1-admin-console-ui-requirements.md section 11 - field names
 * match the backend's Pydantic schemas exactly, no renaming.
 *
 * Auth model note: Phase 1 has exactly one admin credential - a shared
 * bearer token that the operator provisions via the backend's
 * GATEKEY_ADMIN_TOKEN environment variable (see backend
 * src/gatekey/api/deps.py - require_admin). There is no backend endpoint
 * that *sets* this token (an unauthenticated "set admin token" endpoint
 * would itself be a privilege-escalation hole) - the "first-run wizard" in
 * this app deliberately does not attempt to persist a new admin credential,
 * only to sign in with the one already configured via env var and then
 * guide first-provider setup. The token entered at /login is stored
 * client-side only (localStorage) and sent as `Authorization: Bearer
 * <token>` on every admin API call - never persisted server-side by this
 * app.
 *
 * Phase 2 adds a second, parallel auth path: SSO login issues an httpOnly
 * session cookie set by the backend (/v1/auth/sso/callback). Session-
 * authenticated calls pass `session: true` to `request`, which sends
 * `credentials: "include"` and never a bearer header. The Phase 1 bearer
 * pattern is unchanged for admin-token flows - the two paths coexist.
 */

const API_BASE_URL = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000").replace(/\/$/, "");

const TOKEN_STORAGE_KEY = "gatekey_admin_token";

export function getStoredToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_STORAGE_KEY);
}

export function setStoredToken(token: string): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(TOKEN_STORAGE_KEY, token);
}

export function clearStoredToken(): void {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(TOKEN_STORAGE_KEY);
}

export class ApiError extends Error {
  code: string;
  status: number;
  /** Parsed from the `Retry-After` response header (seconds), when present
   * - e.g. rate-limit (429) and the custom-model verify cooldown (429) both
   * set this header. `undefined` when the response carried no such header,
   * which is the overwhelming majority of errors. */
  retryAfterSeconds?: number;

  constructor(status: number, code: string, message: string, retryAfterSeconds?: number) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

async function request<T>(
  path: string,
  options: { method?: string; body?: unknown; token?: string | null; session?: boolean } = {}
): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  let credentials: RequestCredentials | undefined;
  if (options.session) {
    // Session-cookie auth (Phase 2 SSO): cookie travels via CORS
    // credentials, no bearer header ever attached on this path.
    credentials = "include";
  } else {
    const token = options.token !== undefined ? options.token : getStoredToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: options.method || "GET",
    headers,
    credentials,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });

  if (response.status === 204) {
    return undefined as unknown as T;
  }

  let payload: unknown = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }

  if (!response.ok) {
    const errorPayload = (payload as { error?: { code?: string; message?: string } } | null)?.error;
    const retryAfterHeader = response.headers.get("Retry-After");
    const retryAfterSeconds =
      retryAfterHeader !== null && !Number.isNaN(Number(retryAfterHeader))
        ? Number(retryAfterHeader)
        : undefined;
    throw new ApiError(
      response.status,
      errorPayload?.code || "unknown_error",
      errorPayload?.message || `Request failed with status ${response.status}.`,
      retryAfterSeconds
    );
  }

  return payload as T;
}

/**
 * Auth-mode selector for admin/team-scoped endpoints: the backend accepts
 * the Phase 1 break-glass bearer token as org_admin-equivalent on every
 * require_role/require_team_role surface (teams, join queues, org settings,
 * identity, audit entries, admin keys) and on GET /v1/teams(+detail).
 * Precedence: stored admin token -> bearer header; else session cookie.
 * Personal routes (/v1/keys self, /v1/me/usage, /v1/model-access,
 * onboarding) are session-only and never use this helper.
 *
 * Returns the value for `request`'s `session` option: false when a stored
 * token exists (request then attaches the bearer header itself).
 */
function adminAuth(): boolean {
  return !getStoredToken();
}

// --- Auth & session (Phase 2, design doc section 5.1) -------------------------

/** Plain link target for the SSO button - the backend 302s to the IdP. */
export const SSO_LOGIN_URL = `${API_BASE_URL}/v1/auth/sso/login`;

/**
 * Probe whether SSO is configured backend-side. GET /v1/auth/sso/login is
 * 302 (opaqueredirect under redirect:"manual") when configured, 404 when
 * the OIDC env vars are unset. Any failure = treat as not configured and
 * hide the SSO button.
 */
export async function probeSsoConfigured(): Promise<boolean> {
  try {
    const response = await fetch(SSO_LOGIN_URL, { redirect: "manual" });
    return response.type === "opaqueredirect" || response.ok;
  } catch {
    return false;
  }
}

export type OrgRole = "org_admin" | "auditor";
export type TeamRole = "team_lead" | "member";
export type OnboardingStatus =
  | "resolved"
  | "pending_profile"
  | "pending_approval"
  | "pending_alert_email";

export interface MeTeam {
  team_id: string;
  team_name: string;
  role: TeamRole;
}

export interface MeResponse {
  user_id: string;
  name: string;
  email: string | null;
  org_role: OrgRole | null;
  teams: MeTeam[];
  onboarding_status: OnboardingStatus;
}

export function getMe(): Promise<MeResponse> {
  return request<MeResponse>("/v1/auth/me", { session: true });
}

export function logoutSession(): Promise<void> {
  return request<void>("/v1/auth/logout", { method: "POST", session: true });
}

// --- Onboarding (Phase 2, design doc section 5.2) -----------------------------

export interface OnboardingTeam {
  id: string;
  name: string;
}

export type JoinRequestStatus = "pending" | "approved" | "rejected";

export interface JoinRequestResponse {
  id: string;
  team_id: string;
  team_name: string | null;
  requester_user_id: string;
  requester_name: string;
  status: JoinRequestStatus;
  routed_to: "team_lead" | "org_admin";
  requested_at: string;
  resolved_at: string | null;
  resolved_by_user_id: string | null;
  approved_budget_usd: string | null;
  rejection_reason: string | null;
}

export function listOnboardingTeams(): Promise<OnboardingTeam[]> {
  return request<OnboardingTeam[]>("/v1/onboarding/teams", { session: true });
}

/** 409 `join_request_already_pending` if the caller already has one (AC6.4). */
export function submitJoinRequest(body: {
  full_name: string;
  team_id: string;
}): Promise<JoinRequestResponse> {
  return request<JoinRequestResponse>("/v1/onboarding/join-requests", {
    method: "POST",
    body,
    session: true,
  });
}

/** Caller's current/most-recent join request - 404 if none ever submitted. */
export function getOnboardingStatus(): Promise<JoinRequestResponse> {
  return request<JoinRequestResponse>("/v1/onboarding/status", { session: true });
}

// --- Providers (Phase 1.1) ---------------------------------------------------

export type ProviderName = "openai" | "anthropic" | "vertex_ai" | "ollama" | "openrouter";

export interface ProviderKeyResponse {
  provider: ProviderName;
  configured: true;
  validated_at: string;
  created_at: string;
  updated_at: string;
  metadata: Record<string, unknown>;
}

export const PROVIDER_LABELS: Record<ProviderName, string> = {
  openai: "OpenAI",
  anthropic: "Anthropic",
  vertex_ai: "Google Vertex AI",
  ollama: "Ollama",
  openrouter: "OpenRouter",
};

export function listProviders(token?: string | null): Promise<ProviderKeyResponse[]> {
  return request<ProviderKeyResponse[]>("/v1/admin/providers", { token });
}

/** Phase 4 (AC4.1.1/AC4.1.2): `label` is optional on every variant - omitted
 * (the overwhelming majority of existing call sites, unchanged) upserts the
 * single `"Default"`-labeled row exactly as before; a distinct label
 * creates/overwrites a genuinely separate `ProviderKey` row for the same
 * provider instead (backend upserts by `(org_id, provider, label)`). */
export function putProviderKey(
  provider: ProviderName,
  body:
    | {
        api_key: string;
        label?: string;
        /** openrouter only - see `OpenRouterKeyForm`'s doc comment. Must
         * be omitted (never sent, not even as empty/null) for openai/
         * anthropic - their backend schemas `extra="forbid"` any field
         * they don't define. */
        trusted_provider_slugs?: string[];
        trusted_provider_region?: string | null;
      }
    | {
        service_account_json: Record<string, unknown>;
        project_id: string;
        location: string;
        label?: string;
      }
    | { base_url: string; bearer_token?: string | null; label?: string },
  token?: string | null
): Promise<ProviderKeyResponse> {
  return request<ProviderKeyResponse>(`/v1/admin/providers/${provider}/key`, {
    method: "PUT",
    body,
    token,
  });
}

/** Deletes EVERY key/label configured for `provider` - unchanged Phase 1
 * behavior. For a multi-key provider, prefer `deleteProviderKeyById` below
 * to remove one specific key instead of the whole provider. */
export function deleteProviderKey(provider: ProviderName): Promise<void> {
  return request<void>(`/v1/admin/providers/${provider}`, { method: "DELETE" });
}

/** Phase 4 (AC4.1.6): deletes exactly one key (by id) for `provider`,
 * leaving any other labeled key for that provider untouched. 404 if no such
 * key exists (bad id, or an id belonging to a different provider). */
export function deleteProviderKeyById(provider: ProviderName, keyId: string): Promise<void> {
  return request<void>(`/v1/admin/providers/${provider}/keys/${keyId}`, {
    method: "DELETE",
    session: adminAuth(),
  });
}

// --- Users (Phase 1.4/1.6) ----------------------------------------------------

export interface UserTeamMembership {
  team_id: string;
  team_name: string;
  budget_usd: string | null;
  current_spend_usd: string;
}

export interface UserResponse {
  id: string;
  name: string;
  budget_usd: string | null;
  current_spend_usd: string;
  /** Phase 2: org-wide role; null = ordinary member/team_lead-only user. */
  org_role: OrgRole | null;
  created_at: string;
  updated_at: string;
  /** Active team memberships. Non-empty means `budget_usd`/`current_spend_usd`
   * above are dead - real enforcement uses each membership's own budget
   * instead (see `db/models/user.py`'s docstring). */
  team_memberships: UserTeamMembership[];
}

export function listUsers(): Promise<UserResponse[]> {
  return request<UserResponse[]>("/v1/admin/users");
}

export function createUser(body: { name: string; budget_usd: string | null }): Promise<UserResponse> {
  return request<UserResponse>("/v1/admin/users", { method: "POST", body });
}

export function updateUser(
  id: string,
  body: { name?: string; budget_usd?: string | null }
): Promise<UserResponse> {
  return request<UserResponse>(`/v1/admin/users/${id}`, { method: "PATCH", body });
}

export function deleteUser(id: string): Promise<void> {
  return request<void>(`/v1/admin/users/${id}`, { method: "DELETE" });
}

export interface OrgRoleUpdateResponse {
  id: string;
  name: string;
  org_role: OrgRole | null;
}

/** Phase 2 (AC1.5): org-wide roles are granted ONLY here - `null` clears the
 * role. require_role surface: org_admin session or break-glass token (also
 * the documented bootstrap path for the first org admin), hence adminAuth.
 * Audited as user.org_role.update. */
export function patchUserOrgRole(
  id: string,
  org_role: OrgRole | null
): Promise<OrgRoleUpdateResponse> {
  return request<OrgRoleUpdateResponse>(`/v1/admin/users/${id}/org-role`, {
    method: "PATCH",
    body: { org_role },
    session: adminAuth(),
  });
}

// --- Service accounts (Phase 1.2/1.4) -----------------------------------------

export interface ServiceAccountKeyResponse {
  id: string;
  name: string;
  user_id: string;
  /** null only on legacy pre-Phase-2 keys (created before team binding). */
  team_id: string | null;
  key_prefix: string;
  created_at: string;
  revoked_at: string | null;
  active: boolean;
}

export interface ServiceAccountKeyCreateResponse extends ServiceAccountKeyResponse {
  secret: string;
}

export function listServiceAccounts(): Promise<ServiceAccountKeyResponse[]> {
  return request<ServiceAccountKeyResponse[]>("/v1/admin/service-accounts");
}

/** `team_id` required (H-1): the target user must hold a TeamMembership on
 * that team - 404 "team membership not found" otherwise, surfaced verbatim. */
export function createServiceAccount(body: {
  name: string;
  user_id: string;
  team_id: string;
}): Promise<ServiceAccountKeyCreateResponse> {
  return request<ServiceAccountKeyCreateResponse>("/v1/admin/service-accounts", {
    method: "POST",
    body,
  });
}

export function revokeServiceAccount(id: string): Promise<void> {
  return request<void>(`/v1/admin/service-accounts/${id}`, { method: "DELETE" });
}

// --- Model policy (Phase 1.3) -------------------------------------------------

export type ModelPolicyMode = "unconfigured" | "allowlist" | "denylist";

export interface ModelPolicyResponse {
  mode: ModelPolicyMode;
  models: string[];
}

export function getModelPolicy(): Promise<ModelPolicyResponse> {
  return request<ModelPolicyResponse>("/v1/admin/model-policy", { session: adminAuth() });
}

export function putModelPolicy(body: {
  mode: "allowlist" | "denylist";
  models: string[];
}): Promise<ModelPolicyResponse> {
  return request<ModelPolicyResponse>("/v1/admin/model-policy", { method: "PUT", body });
}

// Known models grouped by provider, mirroring backend MODEL_REGISTRY (Phase
// 1's fixed, hand-curated pilot list - see
// backend/src/gatekey/providers/model_registry.py). No endpoint exposes this
// list separately in Phase 1, so it is mirrored here per the UI spec's own
// documented mock-data seam (section 11) - keep in lockstep with the backend
// registry if it ever changes.
export const MODELS_BY_PROVIDER: Record<ProviderName, string[]> = {
  openai: ["gpt-4o", "gpt-4o-mini", "text-embedding-3-small", "text-embedding-3-large"],
  anthropic: ["claude-haiku-4-5-20251001", "claude-sonnet-5", "claude-opus-5"],
  vertex_ai: ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-embedding-001"],
  ollama: ["ollama/llama3.1", "ollama/mistral", "ollama/qwen2.5"],
  openrouter: ["openrouter/openai/gpt-4o-mini"],
};

// --- Usage dashboard (Phase 1.5/1.6) ------------------------------------------

export type UsageRange = "24h" | "7d" | "30d" | "90d" | "custom";

export interface UsageSummaryResponse {
  total_spend_usd: string;
  request_count: number;
  avg_latency_ms: number;
  error_rate: number;
  spend_by_day: { date: string; spend_usd: string }[];
  spend_by_model: { model: string; spend_usd: string }[];
  spend_by_user: { user: string; requests: number; spend_usd: string; budget_usd: string | null }[];
  // Phase 4 (AC4.5.1) - additive fields, existing fields unchanged.
  cache_hit_rate: number;
  cache_hits: number;
  cache_misses: number;
  failover_events_count: number;
  degraded_requests_count: number;
  cost_saved_caching_usd: string;
  cost_saved_degradation_usd: string;
  cost_saved_total_usd: string;
}

export interface UsageSummaryFilters {
  teamId?: string;
  /** AC4.5.2's third filter dimension - narrows every aggregate by provider. */
  provider?: string;
  /** Required together when `range === "custom"`. */
  start?: string;
  end?: string;
}

/** Phase 2: optional `teamId` narrows every aggregate to one team; Phase 4
 * (AC4.5.2) adds `provider` and `90d`/`custom` range support. Auth is
 * require_admin_or_auditor (break-glass token, org_admin or auditor session). */
export function getUsageSummary(
  range: UsageRange,
  filters: UsageSummaryFilters = {}
): Promise<UsageSummaryResponse> {
  const qs = new URLSearchParams({ range });
  if (filters.teamId) qs.set("team_id", filters.teamId);
  if (filters.provider) qs.set("provider", filters.provider);
  if (range === "custom") {
    if (filters.start) qs.set("start", filters.start);
    if (filters.end) qs.set("end", filters.end);
  }
  return request<UsageSummaryResponse>(`/v1/admin/usage/summary?${qs.toString()}`, {
    session: adminAuth(),
  });
}

// --- Phase 4: Usage export (AC4.5.3/AC4.5.6) ----------------------------------
//
// `StreamingResponse` on the backend - not a JSON-envelope error/body the
// shared `request<T>` helper expects, so this hits `fetch` directly and
// returns a `Blob` + suggested filename for the caller to save via an
// anchor-tag download (no dedicated download component exists yet in this
// codebase - this is the first file-download surface).

export interface UsageExportFilters extends UsageSummaryFilters {
  format?: "csv" | "json";
  /** AC4.5.6 one-click shortcut - forces org-wide + last 30 days server-side
   * regardless of any other filter also passed (provider is still honored). */
  reportCostEfficiency?: boolean;
}

function usageExportUrl(range: UsageRange, filters: UsageExportFilters): string {
  const qs = new URLSearchParams({ range, format: filters.format ?? "csv" });
  if (filters.teamId) qs.set("team_id", filters.teamId);
  if (filters.provider) qs.set("provider", filters.provider);
  if (range === "custom") {
    if (filters.start) qs.set("start", filters.start);
    if (filters.end) qs.set("end", filters.end);
  }
  if (filters.reportCostEfficiency) qs.set("report", "cost_efficiency");
  return `${API_BASE_URL}/v1/admin/usage/export?${qs.toString()}`;
}

/** Fetches the export as a `Blob` plus the filename the backend suggests via
 * `Content-Disposition`, for the caller to trigger a browser download. */
export async function exportUsageSummary(
  range: UsageRange,
  filters: UsageExportFilters = {}
): Promise<{ blob: Blob; filename: string }> {
  const headers: Record<string, string> = {};
  const token = getStoredToken();
  let credentials: RequestCredentials | undefined;
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  } else {
    credentials = "include";
  }
  const response = await fetch(usageExportUrl(range, filters), { headers, credentials });
  if (!response.ok) {
    throw new ApiError(response.status, "export_failed", "Failed to export usage data.");
  }
  const disposition = response.headers.get("Content-Disposition") || "";
  const match = /filename=([^;]+)/.exec(disposition);
  const fallback = filters.format === "json" ? "usage-export.json" : "usage-export.csv";
  const filename = match ? match[1].trim().replace(/^"|"$/g, "") : fallback;
  const blob = await response.blob();
  return { blob, filename };
}

/** Triggers a browser save-as for a Blob previously fetched via
 * `exportUsageSummary` - no download component exists elsewhere in this
 * codebase to reuse, so this is a small standalone helper. */
export function downloadBlob(blob: Blob, filename: string): void {
  if (typeof window === "undefined") return;
  const url = window.URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.URL.revokeObjectURL(url);
}

// --- Teams (Phase 2, design doc section 5.4) ---------------------------------
//
// Every Phase 2 admin/team route below passes `session: adminAuth()`: the
// backend accepts the break-glass bearer token as org_admin-equivalent on
// all require_role/require_team_role surfaces, so a stored admin token
// takes precedence and the session cookie is the fallback (see adminAuth's
// doc comment). Decimal fields serialize as strings.

export interface TeamResponse {
  id: string;
  name: string;
  budget_ceiling_usd: string | null;
  current_spend_usd: string;
  period_type: "monthly" | "quarterly";
  on_period_end: "rollover" | "reset";
  current_period_started_at: string;
  created_at: string;
  updated_at: string;
}

export interface TeamMemberResponse {
  user_id: string;
  name: string;
  role: TeamRole;
  budget_usd: string | null;
  current_spend_usd: string;
  created_at: string;
}

export interface TeamAlertConfigResponse {
  threshold_80_enabled: boolean;
  threshold_100_enabled: boolean;
  webhook_enabled: boolean;
  /** The webhook URL itself is never returned in any form - this boolean is
   * the only signal (backend secret-hygiene rule). */
  webhook_configured: boolean;
  email_enabled: boolean;
}

export interface TeamDetailResponse extends TeamResponse {
  members: TeamMemberResponse[];
  team_restriction: string[] | null;
  alert_config: TeamAlertConfigResponse;
}

export interface TeamModelRestrictionsResponse {
  org_baseline: string[];
  team_restriction: string[] | null;
}

/** `team_baseline` = every model this member's TEAM can currently use (org
 * baseline intersected with the team's own restriction, if any) -
 * `member_restriction` = this member's own narrowing, or null = the team
 * baseline applies to them unchanged. */
export interface TeamMemberModelRestrictionsResponse {
  team_baseline: string[];
  member_restriction: string[] | null;
}

export interface ReassignBudgetResponse {
  from_user_id: string;
  to_user_id: string;
  amount_usd: string;
  from_new_budget_usd: string;
  to_new_budget_usd: string;
}

export interface TeamUsageResponse {
  total_spend_usd: string;
  request_count: number;
  spend_by_day: { date: string; spend_usd: string }[];
  spend_by_model: { model: string; spend_usd: string }[];
  spend_by_member: {
    user_id: string;
    name: string;
    requests: number;
    spend_usd: string;
    budget_usd: string | null;
    current_spend_usd: string;
  }[];
}

export function listTeams(): Promise<TeamResponse[]> {
  return request<TeamResponse[]>("/v1/teams", { session: adminAuth() });
}

export function createTeam(body: {
  name: string;
  budget_ceiling_usd?: string | null;
}): Promise<TeamResponse> {
  return request<TeamResponse>("/v1/teams", { method: "POST", body, session: adminAuth() });
}

export function getTeam(teamId: string): Promise<TeamDetailResponse> {
  return request<TeamDetailResponse>(`/v1/teams/${teamId}`, { session: adminAuth() });
}

/** 422 budget_ceiling_below_current_allocation on a retroactive reduction. */
export function updateTeam(
  teamId: string,
  body: { name?: string; budget_ceiling_usd?: string | null }
): Promise<TeamResponse> {
  return request<TeamResponse>(`/v1/teams/${teamId}`, { method: "PATCH", body, session: adminAuth() });
}

export function updateTeamPeriodConfig(
  teamId: string,
  body: { period_type?: "monthly" | "quarterly"; on_period_end?: "rollover" | "reset" }
): Promise<TeamResponse> {
  return request<TeamResponse>(`/v1/teams/${teamId}/period-config`, {
    method: "PATCH",
    body,
    session: adminAuth(),
  });
}

/** 409 team_has_members / team_has_join_requests / team_in_use if not empty. */
export function deleteTeam(teamId: string): Promise<void> {
  return request<void>(`/v1/teams/${teamId}`, { method: "DELETE", session: adminAuth() });
}

/** 422 budget_ceiling_exceeded (message carries live headroom). */
export function addTeamMember(
  teamId: string,
  body: { user_id: string; role: TeamRole; budget_usd: string | null }
): Promise<TeamMemberResponse> {
  return request<TeamMemberResponse>(`/v1/teams/${teamId}/members`, {
    method: "POST",
    body,
    session: adminAuth(),
  });
}

export function updateTeamMember(
  teamId: string,
  userId: string,
  body: { role?: TeamRole; budget_usd?: string | null }
): Promise<TeamMemberResponse> {
  return request<TeamMemberResponse>(`/v1/teams/${teamId}/members/${userId}`, {
    method: "PATCH",
    body,
    session: adminAuth(),
  });
}

/** Soft delete (added by `0049`) - takes effect immediately (the member's
 * keys stop working), reversible via `restoreTeamMember`. No longer 409s
 * on active keys existing. */
export function removeTeamMember(teamId: string, userId: string): Promise<void> {
  return request<void>(`/v1/teams/${teamId}/members/${userId}`, {
    method: "DELETE",
    session: adminAuth(),
  });
}

/** Undo `removeTeamMember` (added by `0049`) - same role/budget/spend
 * history, no key re-issuance needed. 404 if this user was never removed
 * from this team. */
export function restoreTeamMember(teamId: string, userId: string): Promise<TeamMemberResponse> {
  return request<TeamMemberResponse>(`/v1/teams/${teamId}/members/${userId}/restore`, {
    method: "POST",
    session: adminAuth(),
  });
}

export interface RemovedTeamMemberResponse extends TeamMemberResponse {
  removed_at: string;
}

/** Restore-UI counterpart to the member list (added by `0049`). */
export function listRemovedTeamMembers(teamId: string): Promise<RemovedTeamMemberResponse[]> {
  return request<RemovedTeamMemberResponse[]>(`/v1/teams/${teamId}/members/removed`, {
    session: adminAuth(),
  });
}

/** 422 budget_ceiling_exceeded with live headroom in the message. */
export function reassignTeamBudget(
  teamId: string,
  body: { from_user_id: string; to_user_id: string; amount_usd: string }
): Promise<ReassignBudgetResponse> {
  return request<ReassignBudgetResponse>(`/v1/teams/${teamId}/reassign-budget`, {
    method: "POST",
    body,
    session: adminAuth(),
  });
}

export function getTeamModelRestrictions(teamId: string): Promise<TeamModelRestrictionsResponse> {
  return request<TeamModelRestrictionsResponse>(`/v1/teams/${teamId}/model-restrictions`, {
    session: adminAuth(),
  });
}

/** 422 team_model_restricts_org_denied_model if the list tries to widen. */
export function putTeamModelRestrictions(
  teamId: string,
  body: { models: string[] }
): Promise<TeamModelRestrictionsResponse> {
  return request<TeamModelRestrictionsResponse>(`/v1/teams/${teamId}/model-restrictions`, {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

/** Third layer, one below the team-wide restriction above: a team lead's
 * per-member narrowing. 403 if a plain-member session requests anyone
 * other than their own `userId` (self-view only - a team lead, or the
 * org_admin bypass, can view any member's). */
export function getMemberModelRestrictions(
  teamId: string,
  userId: string
): Promise<TeamMemberModelRestrictionsResponse> {
  return request<TeamMemberModelRestrictionsResponse>(
    `/v1/teams/${teamId}/members/${userId}/model-restrictions`,
    { session: adminAuth() }
  );
}

/** Team-lead-only. 404 member_not_on_team / 422
 * member_model_restricts_team_denied_model if the list references a
 * non-member or tries to widen beyond the team's own effective set. */
export function putMemberModelRestrictions(
  teamId: string,
  userId: string,
  body: { models: string[] }
): Promise<TeamMemberModelRestrictionsResponse> {
  return request<TeamMemberModelRestrictionsResponse>(
    `/v1/teams/${teamId}/members/${userId}/model-restrictions`,
    { method: "PUT", body, session: adminAuth() }
  );
}

/** `webhook_url` semantics: omitted = keep stored URL, string = replace,
 * explicit null = clear. JSON.stringify drops undefined keys, so callers
 * simply leave the property out to keep. Org-admin only (ADR-fork 8). */
export function putTeamAlertConfig(
  teamId: string,
  body: {
    threshold_80_enabled: boolean;
    threshold_100_enabled: boolean;
    webhook_enabled: boolean;
    webhook_url?: string | null;
    email_enabled: boolean;
  }
): Promise<TeamAlertConfigResponse> {
  return request<TeamAlertConfigResponse>(`/v1/teams/${teamId}/alert-config`, {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

export function getTeamUsage(teamId: string, range: UsageRange): Promise<TeamUsageResponse> {
  return request<TeamUsageResponse>(`/v1/teams/${teamId}/usage?range=${range}`, {
    session: adminAuth(),
  });
}

// --- Join-request queues (Phase 2, design doc section 5.3) --------------------

export function listTeamJoinRequests(
  teamId: string,
  status?: JoinRequestStatus
): Promise<JoinRequestResponse[]> {
  const qs = status ? `?status=${status}` : "";
  return request<JoinRequestResponse[]>(`/v1/teams/${teamId}/join-requests${qs}`, {
    session: adminAuth(),
  });
}

/** 422 budget_ceiling_exceeded with live headroom (AC6.7). Returns the new
 * membership. `budget_usd: null` = unmetered (a deliberate approver choice). */
export function approveJoinRequest(
  teamId: string,
  requestId: string,
  body: { budget_usd: string | null }
): Promise<TeamMemberResponse> {
  return request<TeamMemberResponse>(
    `/v1/teams/${teamId}/join-requests/${requestId}/approve`,
    { method: "POST", body, session: adminAuth() }
  );
}

export function rejectJoinRequest(
  teamId: string,
  requestId: string,
  body: { reason?: string | null }
): Promise<JoinRequestResponse> {
  return request<JoinRequestResponse>(
    `/v1/teams/${teamId}/join-requests/${requestId}/reject`,
    { method: "POST", body, session: adminAuth() }
  );
}

export interface AdminJoinRequestQueueEntry extends JoinRequestResponse {
  escalation_reason: "no_team_lead" | "pending_over_5_business_days";
}

export function getAdminJoinRequestQueue(): Promise<AdminJoinRequestQueueEntry[]> {
  return request<AdminJoinRequestQueueEntry[]>("/v1/admin/join-requests/queue", {
    session: adminAuth(),
  });
}

// --- Org settings (Phase 2, design doc section 5.5) ---------------------------

export interface OrgSettingsResponse {
  budget_ceiling_usd: string | null;
  currency: string;
  max_self_serve_key_expiration_days: number | null;
  personal_key_soft_cap: number;
  auto_provision_personal_key_on_approval: boolean;
  /** Org-wide budget safeguard (added by `0045`) - live spend, read-only. */
  current_spend_usd: string;
  /** Dedicated alert-recipient email (added by `0048`) - read-only here,
   * written only via `setOrgAlertEmail`. `null` = the first-SSO-login
   * org_admin onboarding prompt hasn't been satisfied. */
  alert_recipient_email: string | null;
}

export function getOrgSettings(): Promise<OrgSettingsResponse> {
  return request<OrgSettingsResponse>("/v1/admin/org-settings", { session: adminAuth() });
}

/** Full-replace PUT. 422 budget_ceiling_below_current_allocation if the org
 * ceiling would drop below the current sum of team ceilings. */
export function putOrgSettings(body: {
  budget_ceiling_usd: string | null;
  currency: "USD";
  max_self_serve_key_expiration_days: number | null;
  personal_key_soft_cap: number;
  auto_provision_personal_key_on_approval: boolean;
}): Promise<OrgSettingsResponse> {
  return request<OrgSettingsResponse>("/v1/admin/org-settings", {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

/** The first-SSO-login org_admin onboarding action (added by `0048`) -
 * also freely re-editable afterward from Org Settings like any other
 * field. */
export function setOrgAlertEmail(email: string): Promise<{ alert_recipient_email: string }> {
  return request<{ alert_recipient_email: string }>("/v1/admin/org-settings/alert-email", {
    method: "POST",
    body: { email },
    session: adminAuth(),
  });
}

// --- Audit log (Phase 2, design doc section 5.8) ------------------------------

export interface AuditEntryResponse {
  id: string;
  actor_user_id: string | null;
  actor_label: string;
  action: string;
  target_type: string;
  target_id: string;
  old_value: Record<string, unknown> | null;
  new_value: Record<string, unknown> | null;
  created_at: string;
}

export interface AuditEntriesPageResponse {
  entries: AuditEntryResponse[];
  page: number;
  page_size: number;
  total: number;
}

/** Fixed action vocabulary (backend services/audit.py docstring) - populates
 * the Audit Log filter dropdown. Keep in lockstep with the backend list. */
export const AUDIT_ACTIONS = [
  "team.create",
  "team.update",
  "team.delete",
  "team.period_config.update",
  "team.model_restrictions.update",
  "team.alert_config.update",
  "team.member.add",
  "team.member.update",
  "team.member.remove",
  "team.budget.reassign",
  "join_request.submit",
  "join_request.approve",
  "join_request.reject",
  "user.org_role.update",
  "personal_key.create",
  "personal_key.regenerate",
  "personal_key.revoke",
  "service_account_key.create",
  "service_account_key.revoke",
  "org_settings.update",
  "compliance_settings.update",
  // Phase 5 (Differentiators)
  "drift.alert_exported",
  "drift_detector.canary_model_setting.update",
  "self_hosted_provider.register",
  "self_hosted_provider.update",
  "self_hosted_provider.remove",
  "self_hosted_provider.reverify",
  "sensitivity_label_mapping.create",
  "sensitivity_label_mapping.update",
  "sensitivity_label_mapping.delete",
  "shadow_ai_config.update",
  "shadow_ai_config.rotate_token",
  "known_ai_tool_hostname.create",
  "known_ai_tool_hostname.update",
  "known_ai_tool_hostname.delete",
] as const;

export function listAuditEntries(params: {
  action?: string;
  actor?: string;
  from?: string;
  to?: string;
  page?: number;
}): Promise<AuditEntriesPageResponse> {
  const qs = new URLSearchParams();
  if (params.action) qs.set("action", params.action);
  if (params.actor) qs.set("actor", params.actor);
  if (params.from) qs.set("from", params.from);
  if (params.to) qs.set("to", params.to);
  if (params.page) qs.set("page", String(params.page));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<AuditEntriesPageResponse>(`/v1/admin/audit-entries${suffix}`, {
    session: adminAuth(),
  });
}

/** Phase 5 (5.2, AC5.2.8): CSV/JSON export of the same filtered audit-entries
 * query - `StreamingResponse` on the backend, same "hit fetch directly,
 * return a Blob + filename" shape as `exportUsageSummary` (no JSON-envelope
 * error body to parse via the shared `request<T>` helper). Chain columns
 * (`chain_seq`/`prev_hash`/`chain_hash`) are included by the backend
 * automatically whenever `chain_enabled = true` for this org - nothing to
 * pass here, the caller doesn't need to know the chain state up front. */
export async function exportAuditEntries(params: {
  action?: string;
  actor?: string;
  from?: string;
  to?: string;
  format: "csv" | "json";
}): Promise<{ blob: Blob; filename: string }> {
  const qs = new URLSearchParams();
  if (params.action) qs.set("action", params.action);
  if (params.actor) qs.set("actor", params.actor);
  if (params.from) qs.set("from", params.from);
  if (params.to) qs.set("to", params.to);
  qs.set("format", params.format);
  const headers: Record<string, string> = {};
  const token = getStoredToken();
  let credentials: RequestCredentials | undefined;
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  } else {
    credentials = "include";
  }
  const response = await fetch(`${API_BASE_URL}/v1/admin/audit-entries?${qs.toString()}`, {
    headers,
    credentials,
  });
  if (!response.ok) {
    throw new ApiError(response.status, "export_failed", "Failed to export audit entries.");
  }
  const disposition = response.headers.get("Content-Disposition") || "";
  const match = /filename=([^;]+)/.exec(disposition);
  const fallback = params.format === "json" ? "audit-entries.json" : "audit-entries.csv";
  const filename = match ? match[1].trim().replace(/^"|"$/g, "") : fallback;
  const blob = await response.blob();
  return { blob, filename };
}

// --- Phase 5: Hash-chained audit ledger (5.2, AC5.2.4/AC5.2.5) ----------------

export interface AuditVerifyResponse {
  status: "intact" | "broken" | "not_enabled";
  entries_verified?: number;
  broken_at_entry_id?: string;
  broken_at_chain_seq?: number;
  expected_prev_hash?: string;
  actual_prev_hash?: string;
}

export function verifyAuditChain(): Promise<AuditVerifyResponse> {
  return request<AuditVerifyResponse>("/v1/admin/audit/verify", { session: adminAuth() });
}

// --- Identity & Access (Phase 2, design doc section 5.9, read-only ADR-8) -----

export interface SsoConfigResponse {
  enabled: boolean;
  issuer_url: string | null;
  client_id: string | null;
  redirect_uri: string | null;
  /** The secret value never appears in any response - configured flag only. */
  client_secret: { configured: boolean };
}

export interface SsoTestConnectionResponse {
  status: "ok" | "unreachable" | "invalid_response" | "not_configured";
  detail: string;
}

export function getSsoConfig(): Promise<SsoConfigResponse> {
  return request<SsoConfigResponse>("/v1/admin/identity/sso-config", { session: adminAuth() });
}

export function testSsoConnection(): Promise<SsoTestConnectionResponse> {
  return request<SsoTestConnectionResponse>("/v1/admin/identity/sso-config/test-connection", {
    method: "POST",
    session: adminAuth(),
  });
}

// --- Personal API keys (Phase 2 FE-6, design doc section 5.6) -----------------

/** Safe list/get view - no secret material exists on this shape. */
export interface PersonalApiKeyResponse {
  id: string;
  name: string;
  owner_user_id: string;
  created_by_user_id: string;
  team_id: string;
  key_prefix: string;
  expires_at: string | null;
  created_at: string;
  revoked_at: string | null;
  active: boolean;
}

/** The ONLY personal-key shape with a `secret` - returned exactly once by
 * create/regenerate, shown once, never stored by this app. */
export interface PersonalApiKeyCreateResponse {
  id: string;
  name: string;
  owner_user_id: string;
  team_id: string;
  key_prefix: string;
  secret: string;
  expires_at: string | null;
  created_at: string;
}

// Self-serve routes: session-only by contract - the break-glass token has no
// personal identity, so these never use adminAuth().

export function listMyKeys(): Promise<PersonalApiKeyResponse[]> {
  return request<PersonalApiKeyResponse[]>("/v1/keys", { session: true });
}

/** 422s: personal_key soft cap / max expiry (org settings) pass through
 * verbatim in the error message. `team_id` is always required. */
export function createMyKey(body: {
  name: string;
  team_id: string;
  expires_at: string | null;
}): Promise<PersonalApiKeyCreateResponse> {
  return request<PersonalApiKeyCreateResponse>("/v1/keys", {
    method: "POST",
    body,
    session: true,
  });
}

export function regenerateMyKey(keyId: string): Promise<PersonalApiKeyCreateResponse> {
  return request<PersonalApiKeyCreateResponse>(`/v1/keys/${keyId}/regenerate`, {
    method: "POST",
    session: true,
  });
}

export function revokeMyKey(keyId: string): Promise<void> {
  return request<void>(`/v1/keys/${keyId}`, { method: "DELETE", session: true });
}

// Delegated routes (team lead on own team; org_admin session or break-glass
// token also accepted - require_team_role surface, hence adminAuth()).

export function listMemberKeys(teamId: string, userId: string): Promise<PersonalApiKeyResponse[]> {
  return request<PersonalApiKeyResponse[]>(`/v1/teams/${teamId}/members/${userId}/keys`, {
    session: adminAuth(),
  });
}

export function createMemberKey(
  teamId: string,
  userId: string,
  body: { name: string; expires_at: string | null }
): Promise<PersonalApiKeyCreateResponse> {
  return request<PersonalApiKeyCreateResponse>(`/v1/teams/${teamId}/members/${userId}/keys`, {
    method: "POST",
    body,
    session: adminAuth(),
  });
}

export function regenerateMemberKey(
  teamId: string,
  userId: string,
  keyId: string
): Promise<PersonalApiKeyCreateResponse> {
  return request<PersonalApiKeyCreateResponse>(
    `/v1/teams/${teamId}/members/${userId}/keys/${keyId}/regenerate`,
    { method: "POST", session: adminAuth() }
  );
}

export function revokeMemberKey(teamId: string, userId: string, keyId: string): Promise<void> {
  return request<void>(`/v1/teams/${teamId}/members/${userId}/keys/${keyId}`, {
    method: "DELETE",
    session: adminAuth(),
  });
}

// Admin org-wide oversight: unified app + personal listing.

export type AdminKeyType = "app" | "personal";

export interface AdminKeyResponse {
  id: string;
  key_type: AdminKeyType;
  name: string;
  key_prefix: string;
  owner_user_id: string;
  owner_name: string;
  team_id: string | null;
  expires_at: string | null;
  created_at: string;
  revoked_at: string | null;
  active: boolean;
}

export interface AdminKeyRegenerateResponse {
  id: string;
  key_type: AdminKeyType;
  name: string;
  key_prefix: string;
  secret: string;
}

export function listAdminKeys(type: AdminKeyType | "all"): Promise<AdminKeyResponse[]> {
  return request<AdminKeyResponse[]>(`/v1/admin/keys?type=${type}`, { session: adminAuth() });
}

/** Works on either key type; revoked keys cannot be regenerated (404). */
export function adminRegenerateKey(keyId: string): Promise<AdminKeyRegenerateResponse> {
  return request<AdminKeyRegenerateResponse>(`/v1/admin/keys/${keyId}/regenerate`, {
    method: "POST",
    session: adminAuth(),
  });
}

export function adminRevokeKey(keyId: string): Promise<void> {
  return request<void>(`/v1/admin/keys/${keyId}`, { method: "DELETE", session: adminAuth() });
}

// --- Phase 3: Compliance & DLP (design doc section 9.2) -----------------------

export type DlpAction = "log" | "redact" | "block";

export interface DlpPolicyResponse {
  ssn_detector_enabled: boolean;
  credit_card_detector_enabled: boolean;
  email_detector_enabled: boolean;
  phone_detector_enabled: boolean;
  default_action: DlpAction;
  store_raw_flagged_content: boolean;
  scan_inbound_responses: boolean;
}

export function getDlpPolicy(): Promise<DlpPolicyResponse> {
  return request<DlpPolicyResponse>("/v1/admin/dlp-policy", { session: adminAuth() });
}

export function putDlpPolicy(body: DlpPolicyResponse): Promise<DlpPolicyResponse> {
  return request<DlpPolicyResponse>("/v1/admin/dlp-policy", { method: "PUT", body, session: adminAuth() });
}

export interface DlpCustomPatternResponse {
  id: string;
  name: string;
  pattern: string;
  action: DlpAction;
}

export function listDlpCustomPatterns(): Promise<DlpCustomPatternResponse[]> {
  return request<DlpCustomPatternResponse[]>("/v1/admin/dlp-policy/custom-patterns", {
    session: adminAuth(),
  });
}

export function createDlpCustomPattern(body: {
  name: string;
  pattern: string;
  action: DlpAction;
}): Promise<DlpCustomPatternResponse> {
  return request<DlpCustomPatternResponse>("/v1/admin/dlp-policy/custom-patterns", {
    method: "POST",
    body,
    session: adminAuth(),
  });
}

export function updateDlpCustomPattern(
  id: string,
  body: { name: string; pattern: string; action: DlpAction }
): Promise<DlpCustomPatternResponse> {
  return request<DlpCustomPatternResponse>(`/v1/admin/dlp-policy/custom-patterns/${id}`, {
    method: "PATCH",
    body,
    session: adminAuth(),
  });
}

export function deleteDlpCustomPattern(id: string): Promise<void> {
  return request<void>(`/v1/admin/dlp-policy/custom-patterns/${id}`, {
    method: "DELETE",
    session: adminAuth(),
  });
}

/** `action: null` = no team override, org default applies. */
export interface TeamDlpOverrideResponse {
  action: DlpAction | null;
}

export function getTeamDlpOverride(teamId: string): Promise<TeamDlpOverrideResponse> {
  return request<TeamDlpOverrideResponse>(`/v1/teams/${teamId}/dlp-override`, { session: adminAuth() });
}

export function putTeamDlpOverride(teamId: string, action: DlpAction): Promise<TeamDlpOverrideResponse> {
  return request<TeamDlpOverrideResponse>(`/v1/teams/${teamId}/dlp-override`, {
    method: "PUT",
    body: { action },
    session: adminAuth(),
  });
}

// --- Phase 3: Residency rules (design doc section 9.3) ------------------------

export type ResidencyViolationBehavior = "hard_block" | "warn";

export interface ResidencyRuleResponse {
  allowed_regions: string[];
  violation_behavior: ResidencyViolationBehavior;
}

export function getOrgResidencyRule(): Promise<ResidencyRuleResponse | null> {
  return request<ResidencyRuleResponse | null>("/v1/admin/residency-rules", { session: adminAuth() });
}

export function putOrgResidencyRule(body: ResidencyRuleResponse): Promise<ResidencyRuleResponse> {
  return request<ResidencyRuleResponse>("/v1/admin/residency-rules", {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

export function deleteOrgResidencyRule(): Promise<void> {
  return request<void>("/v1/admin/residency-rules", { method: "DELETE", session: adminAuth() });
}

export function getTeamResidencyRule(teamId: string): Promise<ResidencyRuleResponse | null> {
  return request<ResidencyRuleResponse | null>(`/v1/teams/${teamId}/residency-rule`, {
    session: adminAuth(),
  });
}

/** 422 `residency_rule_widens_org_rule` passes through verbatim on a non-subset write. */
export function putTeamResidencyRule(
  teamId: string,
  body: ResidencyRuleResponse
): Promise<ResidencyRuleResponse> {
  return request<ResidencyRuleResponse>(`/v1/teams/${teamId}/residency-rule`, {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

export function deleteTeamResidencyRule(teamId: string): Promise<void> {
  return request<void>(`/v1/teams/${teamId}/residency-rule`, { method: "DELETE", session: adminAuth() });
}

// --- Phase 3: Content-aware routing rules (design doc section 9.4) ------------

/** Fixed category set - all four are functionally equivalent as of Phase 5
 * (5.3, AC5.3.1/AC5.3.4): each is wired to a real classifier signal
 * (`services.dlp.py`'s `category_findings`). "legal" is new in Phase 5 and
 * has no pre-existing schema-scaffolding precedent - validated identically
 * to the other three at the API layer. */
export const CONTENT_AWARE_CATEGORIES = ["pii", "source_code", "financial_data", "legal"] as const;
export type ContentAwareCategory = (typeof CONTENT_AWARE_CATEGORIES)[number];

export interface ContentAwareRuleResponse {
  category: string;
  enabled: boolean;
  allowed_models: string[];
}

export function getContentAwareRules(): Promise<ContentAwareRuleResponse[]> {
  return request<ContentAwareRuleResponse[]>("/v1/admin/content-aware-rules", { session: adminAuth() });
}

export function putContentAwareRules(
  rules: { category: string; enabled: boolean; allowed_models: string[] }[]
): Promise<ContentAwareRuleResponse[]> {
  return request<ContentAwareRuleResponse[]>("/v1/admin/content-aware-rules", {
    method: "PUT",
    body: { rules },
    session: adminAuth(),
  });
}

// --- Phase 3: Compliance settings (design doc section 9.1) --------------------

export interface ComplianceSettingsResponse {
  audit_retention_days: number | null;
  log_prompt_retention_days: number;
  access_schedule_timezone: string;
  /** Phase 5 (5.2, AC5.2.2/AC5.2.7): mutually exclusive with a non-null
   * `audit_retention_days` - the backend rejects a PUT that sets both. */
  chain_enabled: boolean;
}

export function getComplianceSettings(): Promise<ComplianceSettingsResponse> {
  return request<ComplianceSettingsResponse>("/v1/admin/compliance-settings", { session: adminAuth() });
}

export function putComplianceSettings(
  body: ComplianceSettingsResponse
): Promise<ComplianceSettingsResponse> {
  return request<ComplianceSettingsResponse>("/v1/admin/compliance-settings", {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

// --- Phase 3: Rotation policy (design doc section 9.6) ------------------------

export type RotationMode = "automatic" | "manual_guided";

export interface RotationPolicyResponse {
  enabled: boolean;
  interval_days: number | null;
  rotate_at_local_time: string | null;
  overlap_buffer_minutes: number;
  next_rotation_at: string | null;
  last_rotated_at: string | null;
  mode: RotationMode;
}

export interface RotationPolicyPutBody {
  enabled: boolean;
  interval_days: number | null;
  rotate_at_local_time: string | null;
  overlap_buffer_minutes: number;
}

export function getOrgRotationPolicy(): Promise<RotationPolicyResponse> {
  return request<RotationPolicyResponse>("/v1/admin/rotation-policy", { session: adminAuth() });
}

export function putOrgRotationPolicy(body: RotationPolicyPutBody): Promise<RotationPolicyResponse> {
  return request<RotationPolicyResponse>("/v1/admin/rotation-policy", {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

export function getKeyRotationPolicy(keyId: string): Promise<RotationPolicyResponse> {
  return request<RotationPolicyResponse>(`/v1/admin/keys/${keyId}/rotation-policy`, {
    session: adminAuth(),
  });
}

export function putKeyRotationPolicy(
  keyId: string,
  body: RotationPolicyPutBody
): Promise<RotationPolicyResponse> {
  return request<RotationPolicyResponse>(`/v1/admin/keys/${keyId}/rotation-policy`, {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

/** Short-overlap rotation (AC7.6) - distinct from `adminRevokeKey`'s
 * immediate, zero-overlap revoke. One-time-reveal of the new secret. */
export interface RotateNowResponse {
  id: string;
  key_prefix: string;
  secret: string;
  overlap_expires_at: string;
}

export function rotateKeyNow(keyId: string): Promise<RotateNowResponse> {
  return request<RotateNowResponse>(`/v1/admin/keys/${keyId}/rotate-now`, {
    method: "POST",
    session: adminAuth(),
  });
}

export function getProviderKeyRotationPolicy(provider: ProviderName): Promise<RotationPolicyResponse> {
  return request<RotationPolicyResponse>(`/v1/admin/provider-keys/${provider}/rotation-policy`, {
    session: adminAuth(),
  });
}

export function putProviderKeyRotationPolicy(
  provider: ProviderName,
  body: RotationPolicyPutBody
): Promise<RotationPolicyResponse> {
  return request<RotationPolicyResponse>(`/v1/admin/provider-keys/${provider}/rotation-policy`, {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

/** AC7.7 guided flow: validated live against the provider, then an
 * overlap-swap (same three structured error states as `putProviderKey`). */
export function rotateProviderKeyGuided(
  provider: ProviderName,
  payload: Record<string, unknown>
): Promise<RotationPolicyResponse> {
  return request<RotationPolicyResponse>(`/v1/admin/provider-keys/${provider}/rotate`, {
    method: "POST",
    body: { payload },
    session: adminAuth(),
  });
}

// --- Phase 3: Scheduled access windows (design doc section 9.7) ---------------

export interface AccessScheduleResponse {
  enabled: boolean;
  allowed_days: number[];
  allowed_hours_start: string | null;
  allowed_hours_end: string | null;
}

export interface AccessSchedulePutBody {
  enabled: boolean;
  allowed_days: number[];
  allowed_hours_start: string | null;
  allowed_hours_end: string | null;
}

export function getOrgAccessSchedule(): Promise<AccessScheduleResponse | null> {
  return request<AccessScheduleResponse | null>("/v1/admin/access-schedule", { session: adminAuth() });
}

export function putOrgAccessSchedule(body: AccessSchedulePutBody): Promise<AccessScheduleResponse> {
  return request<AccessScheduleResponse>("/v1/admin/access-schedule", {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

export function deleteOrgAccessSchedule(): Promise<void> {
  return request<void>("/v1/admin/access-schedule", { method: "DELETE", session: adminAuth() });
}

export function getTeamAccessSchedule(teamId: string): Promise<AccessScheduleResponse | null> {
  return request<AccessScheduleResponse | null>(`/v1/teams/${teamId}/access-schedule`, {
    session: adminAuth(),
  });
}

/** 422 `access_schedule_widens_parent` passes through verbatim on a widening write. */
export function putTeamAccessSchedule(
  teamId: string,
  body: AccessSchedulePutBody
): Promise<AccessScheduleResponse> {
  return request<AccessScheduleResponse>(`/v1/teams/${teamId}/access-schedule`, {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

export function deleteTeamAccessSchedule(teamId: string): Promise<void> {
  return request<void>(`/v1/teams/${teamId}/access-schedule`, {
    method: "DELETE",
    session: adminAuth(),
  });
}

export function getKeyAccessSchedule(keyId: string): Promise<AccessScheduleResponse | null> {
  return request<AccessScheduleResponse | null>(`/v1/admin/keys/${keyId}/access-schedule`, {
    session: adminAuth(),
  });
}

export function putKeyAccessSchedule(
  keyId: string,
  body: AccessSchedulePutBody
): Promise<AccessScheduleResponse> {
  return request<AccessScheduleResponse>(`/v1/admin/keys/${keyId}/access-schedule`, {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

export function deleteKeyAccessSchedule(keyId: string): Promise<void> {
  return request<void>(`/v1/admin/keys/${keyId}/access-schedule`, {
    method: "DELETE",
    session: adminAuth(),
  });
}

/** AC9.10 - fully-resolved effective schedule per service-account key
 * ("Mon-Fri 9:00-18:00" / "Always"), not merely has-an-override:yes/no. */
export interface EffectiveScheduleEntry {
  service_account_id: string;
  name: string;
  team_id: string | null;
  effective: string;
}

export function listKeySchedules(): Promise<EffectiveScheduleEntry[]> {
  return request<EffectiveScheduleEntry[]>("/v1/admin/keys/schedules", { session: adminAuth() });
}

export interface HolidayDateResponse {
  id: string;
  holiday_date: string;
  label: string | null;
}

export function listHolidayDates(): Promise<HolidayDateResponse[]> {
  return request<HolidayDateResponse[]>("/v1/admin/holiday-dates", { session: adminAuth() });
}

export function createHolidayDate(body: {
  holiday_date: string;
  label: string | null;
}): Promise<HolidayDateResponse> {
  return request<HolidayDateResponse>("/v1/admin/holiday-dates", {
    method: "POST",
    body,
    session: adminAuth(),
  });
}

export function deleteHolidayDate(id: string): Promise<void> {
  return request<void>(`/v1/admin/holiday-dates/${id}`, { method: "DELETE", session: adminAuth() });
}

/** Emergency, time-boxed bypass of a service-account key's resolved access
 * schedule (AC9.6-AC9.9). `require_team_role(team_lead)` server-side - an
 * org_admin session/token acts on any team (the bypass baked into that
 * dependency), a Team Lead only on their own (out of scope for this admin
 * console build - see ConsoleShell's org_admin-only nav). */
export interface EmergencyOverrideResponse {
  id: string;
  service_account_id: string;
  granted_by_user_id: string;
  reason: string;
  granted_at: string;
  expires_at: string;
  revoked_at: string | null;
}

export function grantEmergencyOverride(
  teamId: string,
  keyId: string,
  body: { reason: string; expires_at: string }
): Promise<EmergencyOverrideResponse> {
  return request<EmergencyOverrideResponse>(
    `/v1/teams/${teamId}/service-account-keys/${keyId}/emergency-override`,
    { method: "POST", body, session: adminAuth() }
  );
}

export function revokeEmergencyOverride(
  teamId: string,
  keyId: string,
  overrideId: string
): Promise<void> {
  return request<void>(
    `/v1/teams/${teamId}/service-account-keys/${keyId}/emergency-override/${overrideId}`,
    { method: "DELETE", session: adminAuth() }
  );
}

// --- Phase 3: SCIM provisioning (design doc section 9.5) ----------------------

export interface ScimConfigResponse {
  enabled: boolean;
  token_created_at: string | null;
  base_url: string;
}

export function getScimConfig(): Promise<ScimConfigResponse> {
  return request<ScimConfigResponse>("/v1/admin/scim-config", { session: adminAuth() });
}

export function putScimConfig(enabled: boolean): Promise<ScimConfigResponse> {
  return request<ScimConfigResponse>("/v1/admin/scim-config", {
    method: "PUT",
    body: { enabled },
    session: adminAuth(),
  });
}

/** One-time-reveal, same discipline as key secrets - immediately invalidates
 * the prior token (no overlap, unlike scheduled key rotation). */
export interface ScimTokenRotateResponse {
  token: string;
  token_created_at: string;
}

export function rotateScimToken(): Promise<ScimTokenRotateResponse> {
  return request<ScimTokenRotateResponse>("/v1/admin/scim-config/rotate-token", {
    method: "POST",
    session: adminAuth(),
  });
}

// --- Model access self-view (Phase 2 FE-7, design doc section 5.7) ------------

export interface ModelAccessEntry {
  model: string;
  allowed: boolean;
  /** null only when allowed. "org" = org baseline denies it; "team" = the
   * team's restriction narrows it out. */
  blocking_layer: "org" | "team" | null;
}

export interface ModelAccessResponse {
  team_id: string | null;
  models: ModelAccessEntry[];
}

/** Session-only. `teamId` required (400 team_id_required) when the caller
 * holds 2+ memberships; auto-selected server-side for exactly one. */
export function getModelAccess(teamId?: string): Promise<ModelAccessResponse> {
  const qs = teamId ? `?team_id=${teamId}` : "";
  return request<ModelAccessResponse>(`/v1/model-access${qs}`, { session: true });
}

// --- My Usage (Phase 2, design doc section 5.8) -------------------------------

/** Session-only self-view; same response shape as the admin usage summary
 * (spend_by_user contains at most the caller themselves). */
export function getMyUsage(range: UsageRange): Promise<UsageSummaryResponse> {
  return request<UsageSummaryResponse>(`/v1/me/usage?range=${range}`, { session: true });
}

// --- Phase 4: Rate Limiting (design doc section 4.2) -------------------------
//
// Org-level surface (`/v1/admin/rate-limit-rules[/{id}]`, Org Admin only -
// `require_admin` router-level). Scope is selected by which of
// `scope_team_id`/`scope_user_id` is set on the request body: neither ->
// `org_default_per_user`, `scope_team_id` only -> team-scoped, `scope_
// user_id` only -> user-scoped (migration 0034 added user scope; setting
// both is rejected 422 by the backend). `PUT` only changes the limit/
// behavior fields, never the scope - re-create (delete+create) to re-scope
// a rule, matching the backend's documented behavior.
//
// Team-Lead-accessible counterpart lives at `/v1/admin/teams/{team_id}/
// rate-limit-rules[/{id}]` (below) - deliberately a THIN body shape with no
// scope fields at all (the team is always the URL's `{team_id}`, never
// body-controlled - see `listTeamRateLimitRules` etc.).

export type RateLimitOnLimit = "reject" | "queue_and_retry";
export type RateLimitScopeType = "org_default_per_user" | "team" | "user";

export interface RateLimitRuleResponse {
  id: string;
  scope_type: RateLimitScopeType;
  scope_team_id: string | null;
  scope_user_id: string | null;
  requests_per_minute: number | null;
  tokens_per_minute: number | null;
  on_limit: RateLimitOnLimit;
  max_queue_wait_seconds: number;
}

export interface RateLimitRuleCreate {
  requests_per_minute?: number | null;
  tokens_per_minute?: number | null;
  on_limit: RateLimitOnLimit;
  max_queue_wait_seconds: number;
  /** Set exactly one of these (or neither, for the org default) - never both. */
  scope_team_id?: string | null;
  scope_user_id?: string | null;
}

export interface RateLimitRulesResponse {
  rules: RateLimitRuleResponse[];
}

export function getRateLimitRules(): Promise<RateLimitRulesResponse> {
  return request<RateLimitRulesResponse>("/v1/admin/rate-limit-rules", { session: adminAuth() });
}

export function createRateLimitRule(body: RateLimitRuleCreate): Promise<RateLimitRuleResponse> {
  return request<RateLimitRuleResponse>("/v1/admin/rate-limit-rules", {
    method: "POST",
    body,
    session: adminAuth(),
  });
}

export function updateRateLimitRule(
  ruleId: string,
  body: RateLimitRuleCreate
): Promise<RateLimitRuleResponse> {
  return request<RateLimitRuleResponse>(`/v1/admin/rate-limit-rules/${ruleId}`, {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

export function deleteRateLimitRule(ruleId: string): Promise<void> {
  return request<void>(`/v1/admin/rate-limit-rules/${ruleId}`, {
    method: "DELETE",
    session: adminAuth(),
  });
}

/** Team-scoped body: no `scope_team_id`/`scope_user_id` fields - the target
 * team is always the URL's `{team_id}` (see module note above). */
export interface TeamRateLimitRuleCreate {
  requests_per_minute?: number | null;
  tokens_per_minute?: number | null;
  on_limit: RateLimitOnLimit;
  max_queue_wait_seconds: number;
}

/** Org Admin or that team's own Team Lead (`require_team_role`). Returns
 * only this team's own rule(s) - never the org default or another team's. */
export function listTeamRateLimitRules(teamId: string): Promise<RateLimitRulesResponse> {
  return request<RateLimitRulesResponse>(`/v1/admin/teams/${teamId}/rate-limit-rules`, {
    session: adminAuth(),
  });
}

export function createTeamRateLimitRule(
  teamId: string,
  body: TeamRateLimitRuleCreate
): Promise<RateLimitRuleResponse> {
  return request<RateLimitRuleResponse>(`/v1/admin/teams/${teamId}/rate-limit-rules`, {
    method: "POST",
    body,
    session: adminAuth(),
  });
}

export function updateTeamRateLimitRule(
  teamId: string,
  ruleId: string,
  body: TeamRateLimitRuleCreate
): Promise<RateLimitRuleResponse> {
  return request<RateLimitRuleResponse>(`/v1/admin/teams/${teamId}/rate-limit-rules/${ruleId}`, {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

export function deleteTeamRateLimitRule(teamId: string, ruleId: string): Promise<void> {
  return request<void>(`/v1/admin/teams/${teamId}/rate-limit-rules/${ruleId}`, {
    method: "DELETE",
    session: adminAuth(),
  });
}

// --- Phase 4: Caching Settings (design doc section 4.3) ----------------------

export interface CachingSettingsResponse {
  org_id: string;
  enabled: boolean;
  ttl_seconds: number;
}

export interface CachingSettingsCreate {
  enabled: boolean;
  ttl_seconds: number;
}

export function getCachingSettings(): Promise<CachingSettingsResponse> {
  return request<CachingSettingsResponse>("/v1/admin/caching-settings", { session: adminAuth() });
}

export function updateCachingSettings(body: CachingSettingsCreate): Promise<CachingSettingsResponse> {
  return request<CachingSettingsResponse>("/v1/admin/caching-settings", {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

export function clearCachingSettings(): Promise<void> {
  return request<void>("/v1/admin/caching-settings/clear", {
    method: "POST",
    session: adminAuth(),
  });
}

// --- Phase 4: Team-level cache opt-in (AC4.3.2/AC4.3.3) -----------------------
//
// `caching-settings` above is an ORG-WIDE KILL SWITCH (org disabled always
// wins) - this is the real per-team opt-in gate AC4.3.2 requires.
// `cache_enabled` defaults false; `cache_ttl_minutes` 1-1440 (default 5).

export interface TeamCacheSettingsResponse {
  team_id: string;
  cache_enabled: boolean;
  cache_ttl_minutes: number;
}

export interface TeamCacheSettingsUpdate {
  cache_enabled: boolean;
  cache_ttl_minutes: number;
}

export function getTeamCacheSettings(teamId: string): Promise<TeamCacheSettingsResponse> {
  return request<TeamCacheSettingsResponse>(`/v1/admin/teams/${teamId}/cache-settings`, {
    session: adminAuth(),
  });
}

export function putTeamCacheSettings(
  teamId: string,
  body: TeamCacheSettingsUpdate
): Promise<TeamCacheSettingsResponse> {
  return request<TeamCacheSettingsResponse>(`/v1/admin/teams/${teamId}/cache-settings`, {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

// --- Phase 4: Cache entries browsing & clearing (AC4.3.8/AC4.3.9) ------------
//
// `CacheEntryTeaser` is deliberately teaser-only - no prompt/response body
// field exists on the wire at all (AC4.3.9), never render one even if a
// future backend change adds one.

export interface CacheEntryTeaser {
  key_preview: string;
  team_id: string | null;
  user_id: string | null;
  provider: string | null;
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  created_at: string | null;
  expires_at: string | null;
}

/** `teamId` omitted = org-wide (Org Admin only server-side); a specific
 * team is also reachable by that team's own Team Lead. */
export function listCacheEntries(teamId?: string): Promise<CacheEntryTeaser[]> {
  const qs = teamId ? `?team_id=${teamId}` : "";
  return request<CacheEntryTeaser[]>(`/v1/admin/cache/entries${qs}`, { session: adminAuth() });
}

export interface CacheClearResponse {
  team_id: string | null;
  entries_cleared: number;
}

/** `teamId` omitted = org-wide clear (Org Admin only); a specific team is
 * also reachable by that team's own Team Lead (soft clear, AC4.3.8). */
export function clearCache(teamId?: string | null): Promise<CacheClearResponse> {
  return request<CacheClearResponse>("/v1/admin/cache/clear", {
    method: "POST",
    body: { team_id: teamId ?? null },
    session: adminAuth(),
  });
}

// --- Phase 4: Degradation Policy (design doc section 4.4) --------------------

export interface DegradationPolicyResponse {
  id: string | null;
  scope_type: "org" | "team";
  scope_team_id: string | null;
  enabled: boolean;
  threshold_pct_of_budget: string;
  downgrade_target_model: string;
}

// Helper to get numeric value for UI
export function getDegradationThreshold(policy: DegradationPolicyResponse): number {
  return parseFloat(policy.threshold_pct_of_budget);
}

export interface DegradationPolicyCreate {
  enabled: boolean;
  threshold_pct_of_budget: number;
  downgrade_target_model: string;
}

export function getDegradationPolicy(): Promise<DegradationPolicyResponse> {
  return request<DegradationPolicyResponse>("/v1/admin/degradation-policy", { session: adminAuth() });
}

export function updateDegradationPolicy(body: DegradationPolicyCreate): Promise<DegradationPolicyResponse> {
  return request<DegradationPolicyResponse>("/v1/admin/degradation-policy", {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

export interface TeamDegradationPolicyResponse {
  team_id: string;
  enabled: boolean;
  threshold_pct_of_budget: string;
  downgrade_target_model: string;
}

export interface TeamDegradationPolicyCreate {
  enabled: boolean;
  threshold_pct_of_budget: number;
  downgrade_target_model: string;
}

export function getTeamDegradationPolicy(teamId: string): Promise<TeamDegradationPolicyResponse> {
  return request<TeamDegradationPolicyResponse>(`/v1/admin/teams/${teamId}/degradation-policy`, {
    session: adminAuth(),
  });
}

export function updateTeamDegradationPolicy(
  teamId: string,
  body: TeamDegradationPolicyCreate
): Promise<TeamDegradationPolicyResponse> {
  return request<TeamDegradationPolicyResponse>(`/v1/admin/teams/${teamId}/degradation-policy`, {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

// --- Phase 4: Degradation events log (AC4.4.5/AC4.4.8) -----------------------

export interface DegradationEventResponse {
  id: string;
  team_id: string;
  user_id: string;
  request_id: string | null;
  original_model: string;
  degraded_model: string;
  original_cost: string;
  degraded_cost: string;
  cost_saved: string;
  created_at: string;
}

/** `require_role(org_admin, auditor)` - accepts the break-glass token too. */
export function listDegradationEvents(params?: {
  teamId?: string;
  from?: string;
  to?: string;
  limit?: number;
}): Promise<DegradationEventResponse[]> {
  const qs = new URLSearchParams();
  if (params?.teamId) qs.set("team_id", params.teamId);
  if (params?.from) qs.set("from", params.from);
  if (params?.to) qs.set("to", params.to);
  if (params?.limit) qs.set("limit", String(params.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<DegradationEventResponse[]>(`/v1/admin/degradation-events${suffix}`, {
    session: adminAuth(),
  });
}

// --- Phase 4: Failover & Backup Groups (design doc section 4.1) --------------
//
// `keys` on `BackupGroupResponse`/`BackupGroupCreate` is a list of
// `ProviderKey.label` values (NOT ids - the backend has no `keys` column on
// `backup_groups`; membership is tracked on each `ProviderKey.
// backup_group_id`, see `api/v1/admin/backup_groups.py`'s module docstring -
// confirmed by re-reading that router, not just the paraphrase). A label
// with no matching key yet is not an error - it's a declared, not-yet-
// realized member. Once real keys exist with that label their actual labels
// are echoed back instead. Confirmed end-to-end against `listProviderKeys()`
// real rows on the Backup Groups screen, which now offers a picker of real
// `(provider, label)` pairs instead of free text - see that screen's notes
// on the one real sharp edge this surfaces: `label` is unique per
// `(org_id, provider)`, NOT globally, so two different providers can share a
// label, and submitting that shared label associates BOTH providers' keys
// with the group at once (intentional backend behavior per AC4.1.2 - a group
// may span providers - not a bug, but worth knowing before typing a label
// rather than picking one).

export interface BackupGroupResponse {
  id: string;
  name: string;
  keys: string[]; // provider key LABELS (declared or actual) - see note above
}

export interface BackupGroupCreate {
  name: string;
  keys: string[];
}

export function listBackupGroups(): Promise<BackupGroupResponse[]> {
  return request<BackupGroupResponse[]>("/v1/admin/backup-groups", { session: adminAuth() });
}

export function createBackupGroup(body: BackupGroupCreate): Promise<BackupGroupResponse> {
  return request<BackupGroupResponse>("/v1/admin/backup-groups", {
    method: "POST",
    body,
    session: adminAuth(),
  });
}

export function deleteBackupGroup(group_id: string): Promise<void> {
  return request<void>(`/v1/admin/backup-groups/${group_id}`, {
    method: "DELETE",
    session: adminAuth(),
  });
}

/** One row per successful failover switch (AC4.5.7: a request retrying
 * across 3 backup keys is still ONE event, not 3 - a property of the
 * writer, not this read surface). `from_provider_key_id`/
 * `to_provider_key_id` are raw key ids; `listProviderKeys()` below now
 * exists to resolve a key id back to its label/provider if a future screen
 * wants to join them, but the Failover Events screen still renders
 * truncated ids for now (out of scope for this pass - see handoff report). */
export interface FailoverEvent {
  id: string;
  from_provider_key_id: string | null;
  to_provider_key_id: string | null;
  request_id: string;
  detected_at: string;
  switched_at: string;
  created_at: string;
}

export function listFailoverEvents(params?: {
  from?: string;
  to?: string;
  limit?: number;
}): Promise<FailoverEvent[]> {
  const qs = new URLSearchParams();
  if (params?.from) qs.set("from", params.from);
  if (params?.to) qs.set("to", params.to);
  if (params?.limit) qs.set("limit", String(params.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<FailoverEvent[]>(`/v1/admin/failover-events${suffix}`, {
    session: adminAuth(),
  });
}

/** AC4.1.7: one row per individual `ProviderKey` (as opposed to
 * `listProviders()`'s one-row-per-PROVIDER aggregate view) - the per-key
 * id/label/health fields the Providers screen's key list and "Check now"
 * button need. Optional `?provider=` filter. Never returns ciphertext/
 * nonce/auth_tag/plaintext - same redaction discipline as every other
 * provider-key surface.
 *
 * Hardening pass item 3: `failover_enabled`/`failover_target_id` are now
 * included here too, closing the gap noted on `ProviderKeyFailoverConfigResponse`
 * below (this endpoint used to be the ONLY way to learn a key's failover
 * state was the PUT's own response, so the Providers screen could only ever
 * show it session-scoped, immediately after a save, and lost it on reload -
 * see that screen's now-updated module doc comment). This is the
 * authoritative, always-fresh source for both fields on page load. */
export interface ProviderKeyListItem {
  id: string;
  provider: ProviderName;
  label: string;
  is_primary: boolean;
  backup_group_id: string | null;
  health_status: string;
  last_health_check: string | null;
  last_error: string | null;
  availability_24h: number | null;
  failover_enabled: boolean;
  failover_target_id: string | null;
}

export function listProviderKeys(provider?: ProviderName): Promise<ProviderKeyListItem[]> {
  const qs = provider ? `?provider=${provider}` : "";
  return request<ProviderKeyListItem[]>(`/v1/admin/provider-keys${qs}`, { session: adminAuth() });
}

/** AC4.1.6/AC4.1.7: trigger an immediate health check for one provider key
 * by its own id (from `listProviderKeys()` above). */
export interface ProviderKeyHealthCheckResponse {
  status: "ok" | "error";
  latency_ms: number;
  error: string | null;
}

export function checkProviderKeyHealth(keyId: string): Promise<ProviderKeyHealthCheckResponse> {
  return request<ProviderKeyHealthCheckResponse>(`/v1/admin/provider-keys/${keyId}/health`, {
    method: "POST",
    session: adminAuth(),
  });
}

/** Phase 4 (security-review fix round, Fix 1): the ONLY admin surface that
 * actually turns on reactive failover for a specific `ProviderKey` row -
 * before this endpoint existed, `services.provider_keys.set_failover_config()`
 * had zero callers, making the whole §4.1 failover feature unreachable
 * through the product despite the retry logic itself being implemented and
 * tested. Org Admin only. 404 if `key_id` doesn't exist. 422
 * `failover_target_invalid` if `failover_target_id` is the key's own id, or
 * isn't an existing key for the SAME provider (AC4.1.9's "must support the
 * same model(s)" guard, enforced as same-provider since a key is
 * provider-scoped, never model-scoped) - `err.message` on that ApiError is
 * already the specific backend reason, safe to render verbatim rather than
 * folding into a generic failure message.
 *
 * Historical note (superseded, hardening pass item 3): `listProviderKeys()`
 * used to NOT include `failover_enabled`/`failover_target_id`, making this
 * PUT's response the only way the console ever learned a key's failover
 * state (session-scoped cache only, lost on reload). `listProviderKeys()`
 * now returns both fields directly (see that interface's doc comment), so
 * that's the authoritative source on page load; this response is still
 * used as an immediate optimistic update right after a save, ahead of the
 * next full list refresh landing. */
export interface ProviderKeyFailoverConfigResponse {
  id: string;
  provider: ProviderName;
  label: string;
  failover_enabled: boolean;
  failover_target_id: string | null;
}

export function updateProviderKeyFailoverConfig(
  keyId: string,
  body: { failover_enabled: boolean; failover_target_id: string | null }
): Promise<ProviderKeyFailoverConfigResponse> {
  return request<ProviderKeyFailoverConfigResponse>(`/v1/admin/provider-keys/${keyId}/failover-config`, {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

// =============================================================================
// Phase 5: Differentiators
// =============================================================================

// --- 5.4 Provider Drift Detector ----------------------------------------------
//
// RBAC per the design doc's API-contract table: Org Admin configures
// per-model enable/disable (`require_role("org_admin")`); Org Admin +
// Auditor view everything else (`require_admin_or_auditor`) - every GET
// below is therefore safe under `adminAuth()`, same as every other
// Org-Admin-or-Auditor read surface in this file.

export interface CanaryPromptResponse {
  id: string;
  prompt_text: string;
  label: string;
  max_tokens: number;
  enabled: boolean;
}

export function listCanaryPrompts(): Promise<CanaryPromptResponse[]> {
  return request<CanaryPromptResponse[]>("/v1/admin/drift-detector/canary-prompts", {
    session: adminAuth(),
  });
}

export interface DriftModelStatusResponse {
  model: string;
  canary_enabled: boolean;
  last_run_at: string | null;
  baselines_established: number;
  open_alerts_count: number;
}

export function getDriftStatus(): Promise<DriftModelStatusResponse[]> {
  return request<DriftModelStatusResponse[]>("/v1/admin/drift-detector/status", {
    session: adminAuth(),
  });
}

export interface DriftAlertResponse {
  id: string;
  model: string;
  metric: "latency" | "refusal_rate" | "output_similarity";
  baseline_value: string;
  observed_value: string;
  delta_pct: string;
  detected_at: string;
  status: "open" | "exported_to_audit";
  /** Plain-language text naming the metric and percentage delta (ui doc
   * section 12.2) - render this directly, never re-derive it client-side. */
  message: string;
}

export function listDriftAlerts(params: {
  model?: string;
  status?: string;
  metric?: string;
  limit?: number;
} = {}): Promise<DriftAlertResponse[]> {
  const qs = new URLSearchParams();
  if (params.model) qs.set("model", params.model);
  if (params.status) qs.set("status", params.status);
  if (params.metric) qs.set("metric", params.metric);
  if (params.limit) qs.set("limit", String(params.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<DriftAlertResponse[]>(`/v1/admin/drift-detector/alerts${suffix}`, {
    session: adminAuth(),
  });
}

/** AC5.2.10/AC5.4.11: writes a real AuditEntry (`drift.alert_exported`) and
 * sets the alert's status to `exported_to_audit`. Idempotent-safe to call
 * again on an already-exported alert. */
export function exportDriftAlert(alertId: string): Promise<DriftAlertResponse> {
  return request<DriftAlertResponse>(`/v1/admin/drift-detector/alerts/${alertId}/export`, {
    method: "POST",
    session: adminAuth(),
  });
}

export interface CanaryRunResponse {
  id: string;
  model: string;
  prompt_id: string;
  run_at: string;
  output_text: string;
  latency_ms: number;
  refusal_detected: boolean;
  similarity_score_vs_baseline: string | null;
  cost_usd: string;
}

export function listCanaryHistory(params: {
  model?: string;
  prompt_id?: string;
  limit?: number;
} = {}): Promise<CanaryRunResponse[]> {
  const qs = new URLSearchParams();
  if (params.model) qs.set("model", params.model);
  if (params.prompt_id) qs.set("prompt_id", params.prompt_id);
  if (params.limit) qs.set("limit", String(params.limit));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<CanaryRunResponse[]>(`/v1/admin/drift-detector/canary-history${suffix}`, {
    session: adminAuth(),
  });
}

export interface CanaryModelSettingResponse {
  model: string;
  enabled: boolean;
}

/** Org Admin only - thresholds themselves stay global/fixed this phase
 * (AC5.4.6/AC5.4.11 tension, resolved by narrowing scope - see design doc
 * section 2.2), only per-model enable/disable is admin-configurable. */
export function setCanaryModelSetting(model: string, enabled: boolean): Promise<CanaryModelSettingResponse> {
  return request<CanaryModelSettingResponse>(`/v1/admin/drift-detector/models/${encodeURIComponent(model)}`, {
    method: "PUT",
    body: { enabled },
    session: adminAuth(),
  });
}

// --- 5.5 Self-Hosted Governance -----------------------------------------------
//
// RBAC: Org Admin registers/edits/removes/re-verifies (`require_role
// ("org_admin")`); Org Admin + Auditor list/read (`require_admin_or_auditor`).

export interface SelfHostedProviderResponse {
  id: string;
  name: string;
  base_url: string;
  cost_basis_per_gpu_hour: string;
  verified: boolean;
  models: string[];
  created_at: string;
  updated_at: string;
}

export function listSelfHostedProviders(): Promise<SelfHostedProviderResponse[]> {
  return request<SelfHostedProviderResponse[]>("/v1/admin/self-hosted-providers", {
    session: adminAuth(),
  });
}

export function registerSelfHostedProvider(body: {
  name: string;
  base_url: string;
  bearer_token?: string | null;
  cost_basis_per_gpu_hour: string;
  models: string[];
}): Promise<SelfHostedProviderResponse> {
  return request<SelfHostedProviderResponse>("/v1/admin/self-hosted-providers", {
    method: "POST",
    body,
    session: adminAuth(),
  });
}

/** Every field optional - omitted means "leave unchanged". `bearer_token`
 * is write-only (never returned by any GET) - omit it entirely to leave the
 * stored credential untouched, or pass a new value to replace it. */
export function editSelfHostedProvider(
  providerId: string,
  body: {
    name?: string;
    base_url?: string;
    bearer_token?: string;
    cost_basis_per_gpu_hour?: string;
    models?: string[];
  }
): Promise<SelfHostedProviderResponse> {
  return request<SelfHostedProviderResponse>(`/v1/admin/self-hosted-providers/${providerId}`, {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

export function removeSelfHostedProvider(providerId: string): Promise<void> {
  return request<void>(`/v1/admin/self-hosted-providers/${providerId}`, {
    method: "DELETE",
    session: adminAuth(),
  });
}

/** AC5.5.3: manual re-verification only - a live `GET {base_url}/v1/models`
 * probe, reusing `OllamaValidator.validate()` as-is. Not wired into any
 * background health-check job this phase. */
export function reverifySelfHostedProvider(providerId: string): Promise<SelfHostedProviderResponse> {
  return request<SelfHostedProviderResponse>(`/v1/admin/self-hosted-providers/${providerId}/verify`, {
    method: "POST",
    session: adminAuth(),
  });
}

/** Hardening pass item 6: the per-endpoint requests/estimated-cost/avg-
 * latency breakdown the Phase 5 technical design's API-contract table named
 * but was never built - only the org-wide `GET /v1/admin/usage/summary?
 * provider=self_hosted` aggregate existed (see the Self-Hosted Governance
 * screen's now-updated module doc comment for the prior gap). Org Admin +
 * Auditor read (`require_admin_or_auditor`, same as `listSelfHostedProviders`
 * above). `range`/`start`/`end` follow the exact same convention as
 * `getUsageSummary()` (reuses `UsageRange`) - `range` selects a rolling
 * window ending now, or `range: "custom"` with explicit `start`/`end` (ISO
 * 8601) for an arbitrary window. `total_estimated_cost_usd` is a `Decimal`
 * on the backend, serialized as a string here - same convention as every
 * other `_usd` field in this file (see `UsageSummaryResponse`). 404 if
 * `providerId` doesn't reference a registered self-hosted provider. */
export interface SelfHostedProviderUsageResponse {
  self_hosted_provider_id: string;
  range_start: string;
  range_end: string;
  total_requests: number;
  total_estimated_cost_usd: string;
  avg_latency_ms: number;
}

export function getSelfHostedProviderUsage(
  providerId: string,
  range: UsageRange,
  filters: { start?: string; end?: string } = {}
): Promise<SelfHostedProviderUsageResponse> {
  const qs = new URLSearchParams({ range });
  if (range === "custom") {
    if (filters.start) qs.set("start", filters.start);
    if (filters.end) qs.set("end", filters.end);
  }
  return request<SelfHostedProviderUsageResponse>(
    `/v1/admin/self-hosted-providers/${providerId}/usage?${qs.toString()}`,
    { session: adminAuth() }
  );
}

// --- Custom Model Registry (Admin-Managed BYOK Models) ------------------------
//
// RBAC: Org Admin registers/edits/removes/verifies (`require_role
// ("org_admin")`); Org Admin + Auditor list/read (`require_admin_or_auditor`)
// - identical posture to 5.5 Self-Hosted Governance above, this feature's
// direct structural precedent. API-client-layer only (CMR-9) - no UI here;
// see `gatekey/custom-model-registry-technical-design.md` section 3.3 for
// the authoritative field list this mirrors (verified directly against
// `backend/src/gatekey/schemas/custom_model.py`, which matches the design
// doc's section 3.3 exactly, no drift found). Decimal fields
// (`input_price_per_million_usd`/`output_price_per_million_usd`) are
// serialized as strings on the wire - same convention as
// `SelfHostedProviderResponse.cost_basis_per_gpu_hour` above.

export type CustomModelProvider = "openai" | "anthropic" | "vertex_ai" | "openrouter";
export type CustomModelCapability = "chat" | "embeddings";

export interface CustomModelResponse {
  id: string;
  name: string;
  provider: string;
  native_model_id: string;
  capability: string;
  input_price_per_million_usd: string;
  output_price_per_million_usd: string | null;
  pricing_source: string | null;
  pricing_as_of: string;
  verified: boolean;
  shadowed_by_registry: boolean;
  // Model Catalog + Cross-Provider Fallback Chains (Part B) - ordered list
  // of other model names (registry / other verified custom models /
  // verified self-hosted model ids) Gatekey automatically tries, in order,
  // if this model's own provider call fails. `[]` = no chain configured
  // (byte-for-byte pre-feature behavior).
  fallback_model_names: string[];
  created_at: string;
  updated_at: string;
}

/** Mirrors `schemas.custom_model.CustomModelCreateRequest` exactly.
 * `output_price_per_million_usd` must be omitted (never sent) for
 * `capability: "embeddings"` - the backend's write-time guard #4/DB CHECK
 * rejects a mismatch either way. */
export interface CustomModelCreateRequest {
  name: string;
  provider: CustomModelProvider;
  native_model_id: string;
  capability: CustomModelCapability;
  input_price_per_million_usd: string;
  output_price_per_million_usd?: string | null;
  pricing_source?: string | null;
  /** Max 5 entries, defaults to `[]` server-side if omitted. */
  fallback_model_names?: string[];
}

/** Mirrors `schemas.custom_model.CustomModelUpdateRequest` - every field
 * optional, omitted means "leave unchanged" (identical discipline to
 * `editSelfHostedProvider`'s body above). Pass
 * `output_price_per_million_usd: null` explicitly (not omitted) to clear a
 * previously-required price when editing `capability` from `"chat"` to
 * `"embeddings"` - the backend distinguishes "omitted" from "explicit
 * null" via `model_fields_set`.
 *
 * `fallback_model_names` has the identical provided-vs-omitted discipline:
 * omit the key entirely to leave the chain unchanged, or send it (even as
 * `[]`) to replace/clear it - never send `null` for this field. */
export interface CustomModelUpdateRequest {
  name?: string;
  provider?: CustomModelProvider;
  native_model_id?: string;
  capability?: CustomModelCapability;
  input_price_per_million_usd?: string;
  output_price_per_million_usd?: string | null;
  pricing_source?: string | null;
  fallback_model_names?: string[];
}

export function listCustomModels(): Promise<CustomModelResponse[]> {
  return request<CustomModelResponse[]>("/v1/admin/custom-models", {
    session: adminAuth(),
  });
}

export function getCustomModel(customModelId: string): Promise<CustomModelResponse> {
  return request<CustomModelResponse>(`/v1/admin/custom-models/${customModelId}`, {
    session: adminAuth(),
  });
}

export function registerCustomModel(body: CustomModelCreateRequest): Promise<CustomModelResponse> {
  return request<CustomModelResponse>("/v1/admin/custom-models", {
    method: "POST",
    body,
    session: adminAuth(),
  });
}

export function editCustomModel(
  customModelId: string,
  body: CustomModelUpdateRequest
): Promise<CustomModelResponse> {
  return request<CustomModelResponse>(`/v1/admin/custom-models/${customModelId}`, {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

export function removeCustomModel(customModelId: string): Promise<void> {
  return request<void>(`/v1/admin/custom-models/${customModelId}`, {
    method: "DELETE",
    session: adminAuth(),
  });
}

/** One live, minimal test call against the provider using the org's
 * existing BYOK credential (technical design doc section 2.3). 30s
 * per-row cooldown enforced backend-side (429 via `ApiError` on repeat
 * calls); a real provider failure surfaces verbatim via `ApiError.message`
 * (502-shaped), never swallowed. */
export function verifyCustomModel(customModelId: string): Promise<CustomModelResponse> {
  return request<CustomModelResponse>(`/v1/admin/custom-models/${customModelId}/verify`, {
    method: "POST",
    session: adminAuth(),
  });
}

// --- Model Catalog + Cross-Provider Fallback Chains (Part A: live listing) ---
//
// See `gatekey/model-catalog-fallback-chains-technical-design.md` section 1.
// RBAC: `require_admin_or_auditor` (read-only, no side effect).

/** One entry in a provider's live "what models does it actually have"
 * catalog. Price fields are non-null whenever the backend has ANY
 * authoritative price for this entry (live OpenRouter figure, or a static
 * registry match for OpenAI/Anthropic) - the caller's rule is simply "if
 * either is non-null, prefill it (still editable); otherwise leave blank". */
export interface AvailableModelEntry {
  native_model_id: string;
  display_name: string;
  input_price_per_million_usd: string | null;
  output_price_per_million_usd: string | null;
  /** The Gatekey-facing model NAME this entry is already routable/priced
   * under (a registry key, or a verified Custom Model's `name`) - `null`
   * means the live provider offers it but Gatekey has never priced it, so
   * it must be registered as a Custom Model before it can be added to org
   * model policy. */
  routable_as: string | null;
}

/** `GET /v1/admin/custom-models/available/{provider}`. Callers must handle
 * two specific, expected `ApiError`s distinctly from a genuine failure:
 * - `err.code === "provider_not_configured"` (404): no `provider_keys` row
 *   configured for this provider yet - point the admin at the Providers
 *   screen, not a generic error.
 * - `err.code === "custom_model_live_listing_unsupported"` (422, vertex_ai
 *   only): expected/documented, not a bug - fall back to a plain text
 *   native_model_id input, no error styling.
 * Anything else (e.g. `provider_upstream_error`, 502) is a genuine failure -
 * surface `err.message` verbatim. */
export function listAvailableModels(provider: CustomModelProvider): Promise<AvailableModelEntry[]> {
  return request<AvailableModelEntry[]>(`/v1/admin/custom-models/available/${provider}`, {
    session: adminAuth(),
  });
}

/** `GET /v1/admin/custom-models/registry-model-names` - every built-in
 * Gatekey model name, sorted. Zero I/O backend-side. Used, alongside this
 * org's other custom models and its self-hosted providers' model ids, as
 * the candidate source for the fallback-chain picker. */
export function listRegistryModelNames(): Promise<string[]> {
  return request<string[]>("/v1/admin/custom-models/registry-model-names", {
    session: adminAuth(),
  });
}

/** One static `MODEL_REGISTRY` entry, provider-tagged. */
export interface RegistryModelEntry {
  name: string;
  provider: ProviderName;
}

/** `GET /v1/admin/custom-models/registry-models` - every built-in Gatekey
 * model name paired with its provider, sorted. Zero I/O backend-side.
 * Model Policy's source for `vertex_ai` models: that provider has no live
 * catalog listing (`listAvailableModels` rejects it with
 * `custom_model_live_listing_unsupported`), so its checklist is sourced
 * from this always-current registry dump instead of a hand-typed list. */
export function listRegistryModels(): Promise<RegistryModelEntry[]> {
  return request<RegistryModelEntry[]>("/v1/admin/custom-models/registry-models", {
    session: adminAuth(),
  });
}

// --- 5.3 Sensitivity-label mappings (AC5.3.5/AC5.3.6/AC5.3.8) -----------------
//
// Org Admin only for every verb, including GET - no Auditor read on this
// surface (design doc section 3.1's API-contract table).

export interface SensitivityLabelMappingResponse {
  id: string;
  external_label: string;
  gatekey_category: string;
}

export function listSensitivityLabelMappings(): Promise<SensitivityLabelMappingResponse[]> {
  return request<SensitivityLabelMappingResponse[]>(
    "/v1/admin/content-aware-rules/sensitivity-label-mappings",
    { session: adminAuth() }
  );
}

export function createSensitivityLabelMapping(body: {
  external_label: string;
  gatekey_category: string;
}): Promise<SensitivityLabelMappingResponse> {
  return request<SensitivityLabelMappingResponse>(
    "/v1/admin/content-aware-rules/sensitivity-label-mappings",
    { method: "POST", body, session: adminAuth() }
  );
}

export function updateSensitivityLabelMapping(
  mappingId: string,
  body: { external_label: string; gatekey_category: string }
): Promise<SensitivityLabelMappingResponse> {
  return request<SensitivityLabelMappingResponse>(
    `/v1/admin/content-aware-rules/sensitivity-label-mappings/${mappingId}`,
    { method: "PUT", body, session: adminAuth() }
  );
}

export function deleteSensitivityLabelMapping(mappingId: string): Promise<void> {
  return request<void>(`/v1/admin/content-aware-rules/sensitivity-label-mappings/${mappingId}`, {
    method: "DELETE",
    session: adminAuth(),
  });
}

// --- 5.1 Shadow AI Discovery ---------------------------------------------------
//
// RBAC (AC5.1.6): Org Admin - full config/CRUD/token-gen + org-wide report.
// Auditor - full org-wide READ-ONLY. Team Lead - `getShadowAiReport` only,
// server-side forced to their own led team(s). Member - no access.

export interface ShadowAiConfigResponse {
  detection_source: "sase_log" | "proxy_log";
  enforcement_mode: "detect_only" | "notification" | "webhook";
  webhook_configured: boolean;
  shadow_ai_retention_days: number;
  /** True once an Org Admin has generated an ingestion token - the
   * functional opt-in gate (AC5.1.4). Before this, the ingestion endpoint
   * rejects all traffic. */
  ingestion_configured: boolean;
  token_created_at: string | null;
}

export function getShadowAiConfig(): Promise<ShadowAiConfigResponse> {
  return request<ShadowAiConfigResponse>("/v1/admin/shadow-ai/config", { session: adminAuth() });
}

/** `confirm: true` is required (422 otherwise) when TRANSITIONING
 * `enforcement_mode` into `"notification"`/`"webhook"` from a different
 * value - the API-level equivalent of the "this is intrusive - are you
 * sure?" confirm dialog (AC5.1.7). Re-saving the same already-active
 * intrusive mode does not require it again. */
export function putShadowAiConfig(body: {
  detection_source: "sase_log" | "proxy_log";
  enforcement_mode: "detect_only" | "notification" | "webhook";
  webhook_url?: string | null;
  shadow_ai_retention_days: number;
  confirm?: boolean;
}): Promise<ShadowAiConfigResponse> {
  return request<ShadowAiConfigResponse>("/v1/admin/shadow-ai/config", {
    method: "PUT",
    body,
    session: adminAuth(),
  });
}

/** One-time-reveal - same discipline as `rotateScimToken`: the plaintext
 * token is returned exactly once, by this endpoint, never persisted, never
 * returned by any other endpoint. Rotating immediately invalidates the
 * prior token - no overlap window. This is also the functional opt-in gate
 * (AC5.1.4) - the ingestion endpoint rejects all traffic until this has
 * been called at least once. */
export function rotateShadowAiIngestToken(): Promise<{ token: string; token_created_at: string }> {
  return request<{ token: string; token_created_at: string }>("/v1/admin/shadow-ai/ingest-token", {
    method: "POST",
    session: adminAuth(),
  });
}

export interface KnownAiToolHostnameResponse {
  hostname: string;
  tool_label: string;
  enabled: boolean;
}

export function listKnownAiToolHostnames(): Promise<KnownAiToolHostnameResponse[]> {
  return request<KnownAiToolHostnameResponse[]>("/v1/admin/shadow-ai/known-hostnames", {
    session: adminAuth(),
  });
}

export function addKnownAiToolHostname(body: {
  hostname: string;
  tool_label: string;
  enabled?: boolean;
}): Promise<KnownAiToolHostnameResponse> {
  return request<KnownAiToolHostnameResponse>("/v1/admin/shadow-ai/known-hostnames", {
    method: "POST",
    body,
    session: adminAuth(),
  });
}

export function updateKnownAiToolHostname(
  hostname: string,
  body: { tool_label?: string; enabled?: boolean }
): Promise<KnownAiToolHostnameResponse> {
  return request<KnownAiToolHostnameResponse>(
    `/v1/admin/shadow-ai/known-hostnames/${encodeURIComponent(hostname)}`,
    { method: "PUT", body, session: adminAuth() }
  );
}

export function removeKnownAiToolHostname(hostname: string): Promise<void> {
  return request<void>(`/v1/admin/shadow-ai/known-hostnames/${encodeURIComponent(hostname)}`, {
    method: "DELETE",
    session: adminAuth(),
  });
}

export interface ShadowAiReportRowResponse {
  user_identifier: string;
  matched_user_id: string | null;
  linked: boolean;
  tool_label: string;
  destination_host: string;
  frequency_per_week: number;
  last_seen: string;
  repeat_violator: boolean;
}

/** Org Admin/Auditor: org-wide, `teamId` an optional plain filter. Team
 * Lead: the backend FORCES scoping to their own led team(s) regardless of
 * what's passed here - a `teamId` for a team they don't lead is rejected
 * (403), never silently widened. */
export function getShadowAiReport(params: {
  teamId?: string;
  since?: string;
  until?: string;
} = {}): Promise<ShadowAiReportRowResponse[]> {
  const qs = new URLSearchParams();
  if (params.teamId) qs.set("team_id", params.teamId);
  if (params.since) qs.set("since", params.since);
  if (params.until) qs.set("until", params.until);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return request<ShadowAiReportRowResponse[]>(`/v1/admin/shadow-ai/report${suffix}`, {
    session: adminAuth(),
  });
}

// --- Phase 4: Team failover narrowing override (AC4.1.3) ---------------------
//
// Narrowing-only: a team can only turn OFF org/key-level failover for
// itself, never force it on (see backend module docstring).

export interface TeamFailoverOverrideResponse {
  team_id: string;
  failover_disabled: boolean;
}

export function getTeamFailoverOverride(teamId: string): Promise<TeamFailoverOverrideResponse> {
  return request<TeamFailoverOverrideResponse>(`/v1/admin/teams/${teamId}/failover-override`, {
    session: adminAuth(),
  });
}

export function putTeamFailoverOverride(
  teamId: string,
  failover_disabled: boolean
): Promise<TeamFailoverOverrideResponse> {
  return request<TeamFailoverOverrideResponse>(`/v1/admin/teams/${teamId}/failover-override`, {
    method: "PUT",
    body: { failover_disabled },
    session: adminAuth(),
  });
}
