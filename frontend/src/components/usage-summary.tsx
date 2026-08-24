"use client";

/**
 * Shared renderer for the backend's UsageSummaryResponse shape - used by My
 * Usage (personal /v1/me/usage) and the Auditor/Org Admin read-only screens
 * (/v1/admin/usage/summary). Same tiles/bars/table as the Phase 1 admin
 * Dashboard, with the config affordances stripped (read-only by design).
 */

import { SpendOverTimePanel } from "@/components/charts";
import { Badge, BudgetBar, DataTable, StatTile, StatTileSkeleton } from "@/components/ui";
import type { UsageRange, UsageSummaryResponse } from "@/lib/api";

// "custom" is deliberately excluded here - it needs explicit start/end date
// inputs, which this simple shared selector doesn't render (the Dashboard's
// own export/report controls handle "custom" directly where needed).
const RANGE_LABELS: Record<Exclude<UsageRange, "custom">, string> = {
  "24h": "Last 24 hours",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  "90d": "Last 90 days",
};

export function RangeSelect({
  value,
  onChange,
}: {
  value: UsageRange;
  onChange: (range: UsageRange) => void;
}) {
  return (
    <select value={value} onChange={(e) => onChange(e.target.value as UsageRange)}>
      {Object.entries(RANGE_LABELS).map(([v, label]) => (
        <option key={v} value={v}>
          {label}
        </option>
      ))}
    </select>
  );
}

export function UsageSummaryPanels({
  summary,
  loading,
  showUsers = true,
}: {
  summary: UsageSummaryResponse | null;
  loading: boolean;
  showUsers?: boolean;
}) {
  const maxModelSpend = summary
    ? Math.max(1e-9, ...summary.spend_by_model.map((m) => Number(m.spend_usd)))
    : 1;

  return (
    <>
      <div className="stat-grid">
        {loading ? (
          <>
            <StatTileSkeleton />
            <StatTileSkeleton />
            <StatTileSkeleton />
            <StatTileSkeleton />
          </>
        ) : (
          <>
            <StatTile
              label="Total spend"
              value={`$${Number(summary?.total_spend_usd ?? 0).toFixed(2)}`}
            />
            <StatTile label="Requests" value={(summary?.request_count ?? 0).toLocaleString()} />
            <StatTile label="Avg latency" value={`${Math.round(summary?.avg_latency_ms ?? 0)}ms`} />
            <StatTile label="Errors" value={`${((summary?.error_rate ?? 0) * 100).toFixed(1)}%`} />
          </>
        )}
      </div>

      <div className="panel-grid">
        <div className="panel">
          <div className="panel-title">Spend by day</div>
          {loading ? (
            <div className="skeleton skeleton-text" style={{ height: 120 }} />
          ) : (
            <SpendOverTimePanel
              label="Spend by day"
              points={(summary?.spend_by_day ?? []).map((d) => ({
                date: d.date,
                value: Number(d.spend_usd),
              }))}
            />
          )}
        </div>
        <div className="panel">
          <div className="panel-title">Spend by model</div>
          {loading ? (
            <div className="skeleton skeleton-text" style={{ height: 120 }} />
          ) : summary && summary.spend_by_model.length > 0 ? (
            summary.spend_by_model.map((m) => (
              <div className="bar-row" key={m.model}>
                <span className="bar-label mono">{m.model}</span>
                <span className="bar-track">
                  <span
                    className="bar-fill"
                    style={{ width: `${(Number(m.spend_usd) / maxModelSpend) * 100}%` }}
                  />
                </span>
                <span className="bar-value">${Number(m.spend_usd).toFixed(2)}</span>
              </div>
            ))
          ) : (
            <p className="text-muted">No spend data for this range.</p>
          )}
        </div>
      </div>

      {showUsers ? (
        <>
          <div className="panel-title" style={{ marginBottom: 10 }}>
            Spend by user
          </div>
          <DataTable
            loading={loading}
            rows={summary?.spend_by_user ?? []}
            rowKey={(row) => row.user}
            emptyState="No usage recorded for this range yet."
            columns={[
              { key: "user", header: "User", render: (r) => r.user },
              {
                key: "requests",
                header: "Requests",
                align: "right",
                render: (r) => r.requests.toLocaleString(),
              },
              {
                key: "spend",
                header: "Spend",
                align: "right",
                render: (r) => `$${Number(r.spend_usd).toFixed(2)}`,
              },
              {
                key: "budget",
                header: "Budget",
                align: "right",
                render: (r) =>
                  r.budget_usd === null ? "Unmetered" : `$${Number(r.budget_usd).toFixed(2)}`,
              },
              {
                key: "status",
                header: "Status",
                align: "right",
                render: (r) => {
                  if (r.budget_usd === null) return <span className="text-muted">&mdash;</span>;
                  const spend = Number(r.spend_usd);
                  const budget = Number(r.budget_usd);
                  return (
                    <span
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 8,
                        justifyContent: "flex-end",
                      }}
                    >
                      {spend >= budget ? <Badge tone="red">Exhausted</Badge> : null}
                      <BudgetBar spend={spend} budget={budget} />
                    </span>
                  );
                },
              },
            ]}
          />
        </>
      ) : null}
    </>
  );
}
