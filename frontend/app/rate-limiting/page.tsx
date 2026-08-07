"use client";

/**
 * Rate Limiting admin console (Phase 4, Reliability & Cost Efficiency).
 * Org Admin only - this screen manages `/v1/admin/rate-limit-rules`, which
 * can create org-default, team-scoped, or user-scoped rules (migration
 * 0034 added user scope). A Team Lead's own-team equivalent lives at
 * /team/reliability (team-scoped endpoints, no scope picker needed there
 * since the team is implicit).
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { ConfirmDialog, DataTable, Modal, useToast } from "@/components/ui";
import {
  ApiError,
  createRateLimitRule,
  deleteRateLimitRule,
  getRateLimitRules,
  listTeams,
  listUsers,
  updateRateLimitRule,
  type RateLimitOnLimit,
  type RateLimitRuleCreate,
  type RateLimitRuleResponse,
  type RateLimitScopeType,
  type TeamResponse,
  type UserResponse,
} from "@/lib/api";

const SCOPE_LABELS: Record<RateLimitScopeType, string> = {
  org_default_per_user: "Org-wide (per user)",
  team: "Team-specific",
  user: "User-specific",
};

const ON_LIMIT_LABELS: Record<RateLimitOnLimit, string> = {
  reject: "Reject immediately",
  queue_and_retry: "Queue and retry",
};

interface FormState {
  editingId: string | null;
  scope_type: RateLimitScopeType;
  scope_team_id: string;
  scope_user_id: string;
  requests_per_minute: string;
  tokens_per_minute: string;
  on_limit: RateLimitOnLimit;
  max_queue_wait_seconds: number;
}

function emptyForm(): FormState {
  return {
    editingId: null,
    scope_type: "org_default_per_user",
    scope_team_id: "",
    scope_user_id: "",
    requests_per_minute: "",
    tokens_per_minute: "",
    on_limit: "reject",
    max_queue_wait_seconds: 30,
  };
}

function toEditForm(rule: RateLimitRuleResponse): FormState {
  return {
    editingId: rule.id,
    scope_type: rule.scope_type,
    scope_team_id: rule.scope_team_id ?? "",
    scope_user_id: rule.scope_user_id ?? "",
    requests_per_minute: rule.requests_per_minute?.toString() ?? "",
    tokens_per_minute: rule.tokens_per_minute?.toString() ?? "",
    on_limit: rule.on_limit,
    max_queue_wait_seconds: rule.max_queue_wait_seconds,
  };
}

function labelFor(rule: RateLimitRuleResponse, teams: TeamResponse[], users: UserResponse[]): string {
  if (rule.scope_type === "team") {
    const team = teams.find((t) => t.id === rule.scope_team_id);
    return `Team: ${team?.name ?? rule.scope_team_id?.slice(0, 8)}`;
  }
  if (rule.scope_type === "user") {
    const user = users.find((u) => u.id === rule.scope_user_id);
    return `User: ${user?.name ?? rule.scope_user_id?.slice(0, 8)}`;
  }
  return SCOPE_LABELS.org_default_per_user;
}

export default function RateLimitingPage() {
  const toast = useToast();
  const [rules, setRules] = useState<RateLimitRuleResponse[]>([]);
  const [teams, setTeams] = useState<TeamResponse[]>([]);
  const [users, setUsers] = useState<UserResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [form, setForm] = useState<FormState | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<RateLimitRuleResponse | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  function refresh() {
    setLoading(true);
    setError(null);
    Promise.all([
      getRateLimitRules(),
      listTeams().catch(() => []),
      listUsers().catch(() => []),
    ])
      .then(([rulesData, teamsData, usersData]) => {
        setRules(rulesData.rules || []);
        setTeams(teamsData);
        setUsers(usersData);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load rate limit rules."))
      .finally(() => setLoading(false));
  }

  useEffect(refresh, []);

  async function handleSave() {
    if (!form) return;
    setFormError(null);

    const requests_per_minute = form.requests_per_minute.trim() ? parseInt(form.requests_per_minute, 10) : null;
    const tokens_per_minute = form.tokens_per_minute.trim() ? parseInt(form.tokens_per_minute, 10) : null;
    if (requests_per_minute === null && tokens_per_minute === null) {
      setFormError("At least one of requests/minute or tokens/minute must be set.");
      return;
    }
    if (form.scope_type === "team" && !form.scope_team_id) {
      setFormError("Select a team for a team-specific rule.");
      return;
    }
    if (form.scope_type === "user" && !form.scope_user_id) {
      setFormError("Select a user for a user-specific rule.");
      return;
    }

    setSaving(true);
    try {
      const body: RateLimitRuleCreate = {
        requests_per_minute,
        tokens_per_minute,
        on_limit: form.on_limit,
        max_queue_wait_seconds: form.max_queue_wait_seconds,
        scope_team_id: form.scope_type === "team" ? form.scope_team_id : null,
        scope_user_id: form.scope_type === "user" ? form.scope_user_id : null,
      };
      const result = form.editingId
        ? await updateRateLimitRule(form.editingId, body)
        : await createRateLimitRule(body);
      setRules((prev) => {
        const filtered = prev.filter((r) => r.id !== result.id);
        return [...filtered, result];
      });
      setForm(null);
      toast.push("success", "Rate limit rule saved.");
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Failed to save rule.");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(rule: RateLimitRuleResponse) {
    setDeleteBusy(true);
    try {
      await deleteRateLimitRule(rule.id);
      setRules((prev) => prev.filter((r) => r.id !== rule.id));
      toast.push("success", "Rate limit rule deleted.");
      setDeleting(null);
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to delete rule.");
    } finally {
      setDeleteBusy(false);
    }
  }

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Rate Limiting</div>
        <div className="page-subtitle">
          Configure request and token rate limits, org-wide, per-team, or per-user, with
          queue-and-retry or immediate-reject behavior on a limit hit.
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}

        <div className="banner banner-info">
          A user&apos;s personal limit is additive to their team&apos;s pool (AC4.2.9): both are
          enforced, and either one being exceeded triggers the configured behavior.
        </div>
        <div className="banner banner-warning">
          Current queue depth / live requests-per-minute utilization is not shown here - the
          backend does not yet expose a monitoring endpoint for in-flight Redis rate-limit
          counters or queue state (AC4.2.8). Flagged as a backend gap, not faked client-side.
        </div>

        <div className="page-header-row">
          <div />
          <button className="btn btn-primary" onClick={() => setForm(emptyForm())}>
            + Create Rule
          </button>
        </div>

        <DataTable
          loading={loading}
          rows={rules}
          rowKey={(r) => r.id}
          emptyState="No rate limit rules configured. Create a rule to start enforcing limits."
          columns={[
            { key: "scope", header: "Scope", render: (r) => labelFor(r, teams, users) },
            {
              key: "requests",
              header: "Requests / min",
              align: "right",
              render: (r) => r.requests_per_minute ?? <span className="text-muted">&mdash;</span>,
            },
            {
              key: "tokens",
              header: "Tokens / min",
              align: "right",
              render: (r) => r.tokens_per_minute ?? <span className="text-muted">&mdash;</span>,
            },
            { key: "on_limit", header: "On Limit", render: (r) => ON_LIMIT_LABELS[r.on_limit] },
            { key: "queue_wait", header: "Queue Wait (max)", align: "right", render: (r) => `${r.max_queue_wait_seconds}s` },
            {
              key: "actions",
              header: "Actions",
              align: "right",
              render: (r) => (
                <>
                  <button className="btn-link" onClick={() => setForm(toEditForm(r))}>
                    Edit
                  </button>{" "}
                  <button className="btn-link" style={{ color: "var(--red)" }} onClick={() => setDeleting(r)}>
                    Delete
                  </button>
                </>
              ),
            },
          ]}
        />

        {form ? (
          <Modal title={form.editingId ? "Edit Rate Limit Rule" : "Create Rate Limit Rule"} onClose={() => setForm(null)}>
            <div className="field">
              <label>Scope</label>
              <select
                value={form.scope_type}
                disabled={!!form.editingId}
                onChange={(e) => setForm({ ...form, scope_type: e.target.value as RateLimitScopeType })}
              >
                {(Object.keys(SCOPE_LABELS) as RateLimitScopeType[]).map((s) => (
                  <option key={s} value={s}>
                    {SCOPE_LABELS[s]}
                  </option>
                ))}
              </select>
              {form.editingId ? (
                <div className="field-hint">Scope cannot be changed on an existing rule - delete and re-create instead.</div>
              ) : null}
            </div>

            {form.scope_type === "team" ? (
              <div className="field">
                <label>Team</label>
                <select
                  value={form.scope_team_id}
                  disabled={!!form.editingId}
                  onChange={(e) => setForm({ ...form, scope_team_id: e.target.value })}
                >
                  <option value="">Select a team...</option>
                  {teams.map((t) => (
                    <option key={t.id} value={t.id}>
                      {t.name}
                    </option>
                  ))}
                </select>
              </div>
            ) : null}

            {form.scope_type === "user" ? (
              <div className="field">
                <label>User</label>
                <select
                  value={form.scope_user_id}
                  disabled={!!form.editingId}
                  onChange={(e) => setForm({ ...form, scope_user_id: e.target.value })}
                >
                  <option value="">Select a user...</option>
                  {users.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.name}
                    </option>
                  ))}
                </select>
              </div>
            ) : null}

            <div className="field">
              <label>Requests per minute</label>
              <input
                type="number"
                value={form.requests_per_minute}
                onChange={(e) => setForm({ ...form, requests_per_minute: e.target.value })}
                placeholder="Unlimited"
              />
            </div>

            <div className="field">
              <label>Tokens per minute</label>
              <input
                type="number"
                value={form.tokens_per_minute}
                onChange={(e) => setForm({ ...form, tokens_per_minute: e.target.value })}
                placeholder="Unlimited"
              />
              <div className="field-hint">At least one of requests/min or tokens/min must be set.</div>
            </div>

            <div className="field">
              <label>On limit</label>
              <select
                value={form.on_limit}
                onChange={(e) => setForm({ ...form, on_limit: e.target.value as RateLimitOnLimit })}
              >
                {(Object.keys(ON_LIMIT_LABELS) as RateLimitOnLimit[]).map((o) => (
                  <option key={o} value={o}>
                    {ON_LIMIT_LABELS[o]}
                  </option>
                ))}
              </select>
            </div>

            <div className="field">
              <label>Max queue wait (seconds)</label>
              <input
                type="number"
                min={10}
                max={300}
                value={form.max_queue_wait_seconds}
                onChange={(e) => setForm({ ...form, max_queue_wait_seconds: parseInt(e.target.value, 10) || 0 })}
              />
              <div className="field-hint">10-300 seconds. Only applies when &quot;Queue and retry&quot; is selected.</div>
            </div>

            {formError ? <div className="field-error">{formError}</div> : null}
            <div className="modal-actions">
              <button className="btn btn-secondary" onClick={() => setForm(null)} disabled={saving}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={handleSave} disabled={saving}>
                {saving ? "Saving..." : "Save Rule"}
              </button>
            </div>
          </Modal>
        ) : null}

        {deleting ? (
          <ConfirmDialog
            title="Delete this rate limit rule?"
            consequence={`Traffic matching ${labelFor(deleting, teams, users)} will no longer be rate-limited by this rule.`}
            confirmLabel="Delete rule"
            busy={deleteBusy}
            onCancel={() => setDeleting(null)}
            onConfirm={() => handleDelete(deleting)}
          />
        ) : null}
      </div>
    </ConsoleShell>
  );
}
