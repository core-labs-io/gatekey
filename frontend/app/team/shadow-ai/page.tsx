"use client";

/**
 * Team Lead - Shadow AI (Phase 5, 5.1, AC5.1.6): read-only, scoped to the
 * caller's own led team(s) only - the backend forces this server-side
 * (`_resolve_report_scope` in `api/v1/admin/shadow_ai.py`), this screen
 * just never offers a control that would ask for another team's data.
 * Reuses the exact same `ShadowAiReportTable`/`ShadowAiPolicyModal` the
 * org-wide Differentiators screen renders - same convention
 * `AuditEntriesView` already established for the Audit Log / Org Logs
 * split.
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { ShadowAiPolicyModal, ShadowAiReportTable } from "@/components/differentiators";
import { TeamSwitcher, useLeadTeams } from "@/components/team-management";
import { ApiError, getShadowAiReport, type ShadowAiReportRowResponse } from "@/lib/api";

export default function TeamShadowAiPage() {
  const { teams, selected, select, loading: teamsLoading, error: teamsError } = useLeadTeams();
  const [rows, setRows] = useState<ShadowAiReportRowResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showPolicy, setShowPolicy] = useState(false);

  useEffect(() => {
    if (!selected) return;
    let cancelled = false;
    setLoading(true);
    getShadowAiReport({ teamId: selected })
      .then((data) => {
        if (!cancelled) setRows(data);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Failed to load the Shadow AI report.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-header-row">
          <div className="page-title">Shadow AI</div>
          <button className="btn-link" onClick={() => setShowPolicy(true)}>
            View policy
          </button>
        </div>
        <div className="page-subtitle">
          Unsanctioned AI-tool usage detected for your own team&apos;s members only. Read-only -
          configuration is an Org Admin surface.
        </div>
        <TeamSwitcher teams={teams} selected={selected} onSelect={select} />
        {teamsError && !teamsLoading ? <div className="banner banner-error">{teamsError}</div> : null}
        {error ? <div className="banner banner-error">{error}</div> : null}
        <ShadowAiReportTable rows={rows} loading={loading} />
        {showPolicy ? <ShadowAiPolicyModal onClose={() => setShowPolicy(false)} /> : null}
      </div>
    </ConsoleShell>
  );
}
