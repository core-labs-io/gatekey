"use client";

/**
 * Team Lead - Team Dashboard (Phase 2 FE-5, non-admin UI doc section 7.2).
 * GET /v1/teams/{id}/usage?range= scoped to one team - no cross-team
 * comparison is ever rendered here; the switcher only offers teams the
 * session leads (from /v1/auth/me), and the backend 403s anything else.
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { BudgetBar, DataTable, StatTile, StatTileSkeleton } from "@/components/ui";
import { TeamSwitcher, computeHeadroom, fmtUsd, useLeadTeams } from "@/components/team-management";
import {
  ApiError,
  getTeam,
  getTeamUsage,
  type TeamDetailResponse,
  type TeamUsageResponse,
  type UsageRange,
} from "@/lib/api";

// This screen calls GET /v1/teams/{id}/usage (Phase 2), which was not
// extended with 90d/custom range support in Phase 4 - deliberately narrower
// than the shared UsageRange type.
type TeamUsageRange = Extract<UsageRange, "24h" | "7d" | "30d">;

const RANGE_LABELS: Record<TeamUsageRange, string> = {
  "24h": "Last 24 hours",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
};

export default function TeamDashboardPage() {
  const { teams, selected, select, loading: teamsLoading, error: teamsError } = useLeadTeams();
  const [range, setRange] = useState<TeamUsageRange>("7d");
  const [usage, setUsage] = useState<TeamUsageResponse | null>(null);
  const [detail, setDetail] = useState<TeamDetailResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([getTeamUsage(selected, range), getTeam(selected)])
      .then(([usageData, teamDetail]) => {
        if (cancelled) return;
        setUsage(usageData);
        setDetail(teamDetail);
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof ApiError ? err.message : "Failed to load team usage.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected, range]);

  const headroom = detail ? computeHeadroom(detail) : null;
  const allocatedPct =
    headroom && headroom.ceiling !== null && headroom.ceiling > 0
      ? Math.round((headroom.allocated / headroom.ceiling) * 100)
      : null;

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-header-row">
          <div className="page-title">Team Dashboard</div>
          <select value={range} onChange={(e) => setRange(e.target.value as TeamUsageRange)}>
            {(Object.keys(RANGE_LABELS) as TeamUsageRange[]).map((r) => (
              <option key={r} value={r}>
                {RANGE_LABELS[r]}
              </option>
            ))}
          </select>
        </div>
        <TeamSwitcher teams={teams} selected={selected} onSelect={select} />
        {teamsError && !teamsLoading ? (
          <div className="banner banner-error">{teamsError}</div>
        ) : null}
        {error ? <div className="banner banner-error">{error}</div> : null}

        <div className="stat-grid">
          {loading || !usage || !detail ? (
            <>
              <StatTileSkeleton />
              <StatTileSkeleton />
              <StatTileSkeleton />
              <StatTileSkeleton />
            </>
          ) : (
            <>
              <StatTile label="Team spend" value={fmtUsd(usage.total_spend_usd)} hint={RANGE_LABELS[range]} />
              <StatTile label="Requests" value={usage.request_count.toLocaleString()} hint={RANGE_LABELS[range]} />
              <StatTile
                label="Allocated vs ceiling"
                value={allocatedPct === null ? "No ceiling" : `${allocatedPct}%`}
                hint={
                  headroom && headroom.ceiling !== null
                    ? `$${headroom.allocated.toFixed(2)} of $${headroom.ceiling.toFixed(2)}`
                    : undefined
                }
              />
              <StatTile label="Members" value={detail.members.length} />
            </>
          )}
        </div>

        <div className="panel">
          <div className="panel-title">Spend by member</div>
          <DataTable
            loading={loading}
            rows={usage?.spend_by_member ?? []}
            rowKey={(m) => m.user_id}
            emptyState="No member activity in this range."
            columns={[
              { key: "name", header: "Member", render: (m) => m.name },
              { key: "requests", header: "Requests", align: "right", render: (m) => m.requests.toLocaleString() },
              { key: "spend", header: "Spend (range)", align: "right", render: (m) => fmtUsd(m.spend_usd) },
              { key: "budget", header: "Budget", align: "right", render: (m) => fmtUsd(m.budget_usd) },
              {
                key: "bar",
                header: "Budget used",
                render: (m) => (
                  <BudgetBar
                    spend={Number(m.current_spend_usd)}
                    budget={m.budget_usd === null ? null : Number(m.budget_usd)}
                  />
                ),
              },
            ]}
          />
        </div>

        <div className="panel">
          <div className="panel-title">Spend by model</div>
          <DataTable
            loading={loading}
            rows={usage?.spend_by_model ?? []}
            rowKey={(m) => m.model}
            emptyState="No model activity in this range."
            columns={[
              { key: "model", header: "Model", render: (m) => m.model },
              { key: "spend", header: "Spend", align: "right", render: (m) => fmtUsd(m.spend_usd) },
            ]}
          />
        </div>
      </div>
    </ConsoleShell>
  );
}
