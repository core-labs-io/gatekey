"use client";

/**
 * Org Usage (Phase 2 FE-9, non-admin UI doc section 8.1) - read-only,
 * org-wide usage for Auditor and Org Admin sessions (and the break-glass
 * token). Same layout as the admin Dashboard via the shared
 * UsageSummaryPanels, with zero config affordances - the only controls are
 * the time range and an optional team filter (Phase 2's team_id extension
 * to GET /v1/admin/usage/summary).
 *
 * The team filter list comes from GET /v1/teams (org-wide roles can list
 * all teams); if that call fails the filter is simply hidden - the org-wide
 * summary still renders.
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { RangeSelect, UsageSummaryPanels } from "@/components/usage-summary";
import {
  ApiError,
  getUsageSummary,
  listTeams,
  type TeamResponse,
  type UsageRange,
  type UsageSummaryResponse,
} from "@/lib/api";

export default function OrgUsagePage() {
  const [range, setRange] = useState<UsageRange>("7d");
  const [teamId, setTeamId] = useState("");
  const [teams, setTeams] = useState<TeamResponse[] | null>(null);
  const [summary, setSummary] = useState<UsageSummaryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listTeams()
      .then(setTeams)
      .catch(() => setTeams(null)); // No team filter, org-wide view still works.
  }, []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getUsageSummary(range, teamId ? { teamId } : undefined)
      .then((data) => {
        if (!cancelled) setSummary(data);
      })
      .catch((err) => {
        if (!cancelled)
          setError(
            err instanceof ApiError && (err.status === 401 || err.status === 403)
              ? "You do not have access to org-wide usage. This screen is for Auditors and Org Admins."
              : err instanceof ApiError
                ? err.message
                : "Failed to load org usage."
          );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [range, teamId]);

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-header-row">
          <div>
            <div className="page-title">Org Usage</div>
            <div className="page-subtitle">Read-only, org-wide.</div>
          </div>
          <span style={{ display: "flex", gap: 8 }}>
            {teams ? (
              <select value={teamId} onChange={(e) => setTeamId(e.target.value)}>
                <option value="">All teams</option>
                {teams.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
            ) : null}
            <RangeSelect value={range} onChange={setRange} />
          </span>
        </div>

        {error ? (
          <div className="banner banner-error">{error}</div>
        ) : (
          <UsageSummaryPanels summary={summary} loading={loading} />
        )}
      </div>
    </ConsoleShell>
  );
}
