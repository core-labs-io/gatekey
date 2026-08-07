"use client";

/**
 * Org Logs (Phase 2 FE-9, non-admin UI doc section 8.2) - read-only, for
 * Auditor and Org Admin sessions (and the break-glass token). Two tabs:
 *
 * - "Audit events": the exact same AuditEntriesView the admin Audit Log
 *   renders (require_role(org_admin, auditor) backend-side).
 * - "Request log": Phase 2's backend has NO per-request log listing
 *   endpoint (api/v1/admin/usage.py exposes only the aggregate summary),
 *   so this tab renders the usage-summary equivalent with a note; the
 *   queryable/exportable per-request table is Phase 3 (section 3.1).
 *   CSV/JSON export is likewise Phase 3 - deliberately absent here.
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { AuditEntriesView } from "@/components/audit-entries";
import { RangeSelect, UsageSummaryPanels } from "@/components/usage-summary";
import { ApiError, getUsageSummary, type UsageRange, type UsageSummaryResponse } from "@/lib/api";

function RequestLogTab() {
  const [range, setRange] = useState<UsageRange>("7d");
  const [summary, setSummary] = useState<UsageSummaryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getUsageSummary(range)
      .then((data) => {
        if (!cancelled) setSummary(data);
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof ApiError ? err.message : "Failed to load request data.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [range]);

  return (
    <>
      <div className="banner banner-info">
        Per-request log browsing (individual requests with model, tokens, latency) arrives
        with Phase 3&apos;s queryable log endpoint - until then this tab shows the aggregate
        request/spend summary from the same underlying request log.
      </div>
      <div className="page-header-row">
        <span />
        <RangeSelect value={range} onChange={setRange} />
      </div>
      {error ? (
        <div className="banner banner-error">{error}</div>
      ) : (
        <UsageSummaryPanels summary={summary} loading={loading} />
      )}
    </>
  );
}

export default function OrgLogsPage() {
  const [tab, setTab] = useState<"audit" | "requests">("audit");

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Org Logs</div>
        <div className="page-subtitle">Read-only, org-wide.</div>

        <div className="provider-tabs">
          <button
            className={`provider-tab ${tab === "audit" ? "active" : ""}`}
            onClick={() => setTab("audit")}
          >
            Audit events
          </button>
          <button
            className={`provider-tab ${tab === "requests" ? "active" : ""}`}
            onClick={() => setTab("requests")}
          >
            Request log
          </button>
        </div>

        {tab === "audit" ? <AuditEntriesView /> : <RequestLogTab />}
      </div>
    </ConsoleShell>
  );
}
