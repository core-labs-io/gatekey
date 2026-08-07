"use client";

/**
 * Degradation Policy admin console (Phase 4, Reliability & Cost Efficiency).
 * Org Admin only - manages the org-wide policy plus a read-only view of the
 * degradation-events log (AC4.4.5/AC4.4.8). The per-team policy lives on
 * each team's own page (Team Lead: /team/reliability).
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { DataTable, useToast } from "@/components/ui";
import {
  ApiError,
  getDegradationPolicy,
  listDegradationEvents,
  listTeams,
  updateDegradationPolicy,
  type DegradationEventResponse,
  type DegradationPolicyResponse,
  type TeamResponse,
} from "@/lib/api";

export default function DegradationPolicyPage() {
  const toast = useToast();
  const [policy, setPolicy] = useState<DegradationPolicyResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

  const [teams, setTeams] = useState<TeamResponse[]>([]);
  const [teamFilter, setTeamFilter] = useState("");
  const [events, setEvents] = useState<DegradationEventResponse[]>([]);
  const [eventsLoading, setEventsLoading] = useState(true);
  const [eventsError, setEventsError] = useState<string | null>(null);

  useEffect(() => {
    loadPolicy();
    listTeams()
      .then(setTeams)
      .catch(() => setTeams([]));
  }, []);

  useEffect(loadEvents, [teamFilter]);

  async function loadPolicy() {
    setLoading(true);
    setError(null);
    try {
      const data = await getDegradationPolicy();
      setPolicy(data);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load degradation policy.");
    } finally {
      setLoading(false);
    }
  }

  function loadEvents() {
    setEventsLoading(true);
    setEventsError(null);
    listDegradationEvents(teamFilter ? { teamId: teamFilter } : undefined)
      .then(setEvents)
      .catch((err) => setEventsError(err instanceof ApiError ? err.message : "Failed to load degradation events."))
      .finally(() => setEventsLoading(false));
  }

  async function handleSave() {
    if (!policy) return;

    setSaving(true);
    setError(null);
    try {
      const result = await updateDegradationPolicy({
        enabled: policy.enabled,
        threshold_pct_of_budget: Number(policy.threshold_pct_of_budget),
        downgrade_target_model: policy.downgrade_target_model,
      });
      setPolicy(result);
      setDirty(false);
      toast.push("success", "Degradation policy saved.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save policy.");
    } finally {
      setSaving(false);
    }
  }

  const teamsById = new Map(teams.map((t) => [t.id, t.name]));

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Graceful Degradation</div>
        <div className="page-subtitle">
          Automatically downgrade to cheaper models when approaching budget limits.
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}

        {loading ? (
          <div className="skeleton skeleton-text" style={{ height: 200 }} />
        ) : policy ? (
          <div className="panel">
            <div className="panel-title">Org-wide Degradation Policy</div>

            <div className="banner banner-info" style={{ marginBottom: 20 }}>
              When a team&apos;s remaining budget falls below this threshold, new chat-completion
              requests are rerouted to the fallback model (embeddings and non-chat completions
              are never downgraded - they still hit the hard budget block, AC4.4.7). A team can
              configure its own policy instead (Team Lead &rarr; Reliability &amp; Cost);
              per-team overrides otherwise fall back to this org default.
            </div>

            <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <input
                type="checkbox"
                id="degradation-enabled"
                style={{ width: "auto" }}
                checked={policy.enabled}
                onChange={(e) => {
                  setPolicy({ ...policy, enabled: e.target.checked });
                  setDirty(true);
                }}
              />
              <label htmlFor="degradation-enabled" style={{ margin: 0 }}>
                Enable graceful degradation org-wide
              </label>
            </div>

            <div className="field">
              <label>Budget threshold (%)</label>
              <input
                type="number"
                value={policy.threshold_pct_of_budget}
                onChange={(e) => {
                  setPolicy({ ...policy, threshold_pct_of_budget: e.target.value });
                  setDirty(true);
                }}
                min={1}
                max={99}
              />
              <div className="field-hint">
                Trigger degradation when remaining budget falls below this percentage of the
                ceiling. Range: 1-99%. Default: 10%.
              </div>
            </div>

            <div className="field">
              <label>Downgrade target model</label>
              <input
                type="text"
                value={policy.downgrade_target_model}
                onChange={(e) => {
                  setPolicy({ ...policy, downgrade_target_model: e.target.value });
                  setDirty(true);
                }}
                placeholder="e.g., gpt-4o-mini"
              />
              <div className="field-hint">
                Must be a model from the team&apos;s allowed model list (enforced server-side).
              </div>
            </div>

            <div className="page-header-row">
              <span className="text-muted">
                {policy.enabled ? "Degradation is active" : "Degradation is disabled"}
              </span>
              <button className="btn btn-primary" onClick={handleSave} disabled={saving || !dirty}>
                {saving ? "Saving..." : "Save Policy"}
              </button>
            </div>
          </div>
        ) : null}

        <div className="panel" style={{ marginTop: 20 }}>
          <div className="page-header-row">
            <div className="panel-title">Degradation events</div>
            <select value={teamFilter} onChange={(e) => setTeamFilter(e.target.value)}>
              <option value="">All teams</option>
              {teams.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
          </div>
          <p className="text-muted">
            Every automatic model substitution (AC4.4.5) - both the original and downgraded model
            are logged, along with the cost saved.
          </p>
          {eventsError ? <div className="banner banner-error">{eventsError}</div> : null}
          <DataTable
            loading={eventsLoading}
            rows={events}
            rowKey={(e) => e.id}
            emptyState="No degradation events recorded yet."
            columns={[
              { key: "time", header: "Time", render: (e) => new Date(e.created_at).toLocaleString() },
              { key: "team", header: "Team", render: (e) => teamsById.get(e.team_id) ?? e.team_id.slice(0, 8) },
              { key: "from", header: "Original model", render: (e) => <span className="mono">{e.original_model}</span> },
              { key: "to", header: "Downgraded to", render: (e) => <span className="mono">{e.degraded_model}</span> },
              { key: "saved", header: "Cost saved", align: "right", render: (e) => `$${Number(e.cost_saved).toFixed(4)}` },
            ]}
          />
        </div>
      </div>
    </ConsoleShell>
  );
}
