"use client";

/**
 * Dashboard / usage overview (UI spec section 7.3, extended UI spec section
 * 5 for Phase 4). Landing page after login. Wired to the real GET
 * /v1/admin/usage/summary endpoint (Phase 1.5/1.6), extended in Phase 4
 * (AC4.5.1-AC4.5.7) with cache hit rate, failover events, and cost-saved
 * metrics, team/provider filters, CSV/JSON export, and a refresh-interval
 * preference - no mock data anywhere on this screen.
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { StatTile, StatTileSkeleton, DataTable, BudgetBar, Badge, useToast } from "@/components/ui";
import {
  ApiError,
  downloadBlob,
  exportUsageSummary,
  getUsageSummary,
  listTeams,
  PROVIDER_LABELS,
  type ProviderName,
  type TeamResponse,
  type UsageRange,
  type UsageSummaryResponse,
} from "@/lib/api";
import Link from "next/link";

const RANGE_LABELS: Record<Exclude<UsageRange, "custom">, string> = {
  "24h": "Last 24 hours",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
  "90d": "Last 90 days",
};

const PROVIDERS: ProviderName[] = ["openai", "anthropic", "vertex_ai", "ollama", "openrouter"];

type RefreshInterval = "manual" | "15" | "30" | "60";
const REFRESH_STORAGE_KEY = "gatekey_dashboard_refresh_interval";
const REFRESH_LABELS: Record<RefreshInterval, string> = {
  manual: "Manual only",
  "15": "Every 15s",
  "30": "Every 30s",
  "60": "Every 60s",
};

export default function DashboardPage() {
  const toast = useToast();
  const [range, setRange] = useState<UsageRange>("7d");
  const [teamId, setTeamId] = useState("");
  const [provider, setProvider] = useState("");
  const [teams, setTeams] = useState<TeamResponse[]>([]);
  const [summary, setSummary] = useState<UsageSummaryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);

  const [refreshInterval, setRefreshInterval] = useState<RefreshInterval>("manual");

  useEffect(() => {
    const stored = window.localStorage.getItem(REFRESH_STORAGE_KEY) as RefreshInterval | null;
    if (stored && stored in REFRESH_LABELS) setRefreshInterval(stored);
    listTeams()
      .then(setTeams)
      .catch(() => setTeams([]));
  }, []);

  function handleRefreshIntervalChange(value: RefreshInterval) {
    setRefreshInterval(value);
    window.localStorage.setItem(REFRESH_STORAGE_KEY, value);
  }

  function loadSummary() {
    let cancelled = false;
    setLoading(true);
    setError(null);
    getUsageSummary(range, { teamId: teamId || undefined, provider: provider || undefined })
      .then((data) => {
        if (!cancelled) setSummary(data);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Failed to load usage data.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(loadSummary, [range, teamId, provider]);

  // AC4.5.4: configurable refresh interval (15s/30s/60s/manual), a per-user
  // UI-only preference persisted via localStorage - no backend change.
  useEffect(() => {
    if (refreshInterval === "manual") return;
    const ms = Number(refreshInterval) * 1000;
    const id = window.setInterval(() => {
      loadSummary();
    }, ms);
    return () => window.clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshInterval, range, teamId, provider]);

  async function handleExport(format: "csv" | "json") {
    setExporting(true);
    try {
      const { blob, filename } = await exportUsageSummary(range, {
        format,
        teamId: teamId || undefined,
        provider: provider || undefined,
      });
      downloadBlob(blob, filename);
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to export usage data.");
    } finally {
      setExporting(false);
    }
  }

  async function handleCostEfficiencyReport(format: "csv" | "json") {
    setExporting(true);
    try {
      const { blob, filename } = await exportUsageSummary(range, {
        format,
        provider: provider || undefined,
        reportCostEfficiency: true,
      });
      downloadBlob(blob, filename);
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to generate cost efficiency report.");
    } finally {
      setExporting(false);
    }
  }

  const isEmpty = !loading && !error && summary && summary.request_count === 0;
  const maxDaySpend = summary ? Math.max(1e-9, ...summary.spend_by_day.map((d) => Number(d.spend_usd))) : 1;
  const maxModelSpend = summary
    ? Math.max(1e-9, ...summary.spend_by_model.map((m) => Number(m.spend_usd)))
    : 1;

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-header-row">
          <div>
            <div className="page-title">Dashboard</div>
          </div>
          <span style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <select value={teamId} onChange={(e) => setTeamId(e.target.value)}>
              <option value="">All teams</option>
              {teams.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </select>
            <select value={provider} onChange={(e) => setProvider(e.target.value)}>
              <option value="">All providers</option>
              {PROVIDERS.map((p) => (
                <option key={p} value={p}>
                  {PROVIDER_LABELS[p]}
                </option>
              ))}
            </select>
            <select value={range} onChange={(e) => setRange(e.target.value as UsageRange)}>
              {Object.entries(RANGE_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <select
              value={refreshInterval}
              onChange={(e) => handleRefreshIntervalChange(e.target.value as RefreshInterval)}
              title="Auto-refresh interval"
            >
              {(Object.keys(REFRESH_LABELS) as RefreshInterval[]).map((r) => (
                <option key={r} value={r}>
                  {REFRESH_LABELS[r]}
                </option>
              ))}
            </select>
          </span>
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}

        <div className="page-header-row">
          <div className="text-muted" style={{ fontSize: 12 }}>
            Filters apply to every metric below (AC4.5.2).
          </div>
          <span style={{ display: "flex", gap: 8 }}>
            <button className="btn btn-secondary" onClick={() => handleExport("csv")} disabled={exporting}>
              Export CSV
            </button>
            <button className="btn btn-secondary" onClick={() => handleExport("json")} disabled={exporting}>
              Export JSON
            </button>
            <button className="btn btn-primary" onClick={() => handleCostEfficiencyReport("csv")} disabled={exporting}>
              Cost Efficiency Report
            </button>
          </span>
        </div>

        {isEmpty ? (
          <div className="banner banner-info">
            No traffic yet. Point an app at your gateway to see usage here.{" "}
            <Link href="/service-accounts">Create a service account</Link> to get started.
          </div>
        ) : null}

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
              <StatTile label="Total spend" value={`$${Number(summary?.total_spend_usd ?? 0).toFixed(2)}`} />
              <StatTile label="Requests" value={(summary?.request_count ?? 0).toLocaleString()} />
              <StatTile label="Avg latency" value={`${Math.round(summary?.avg_latency_ms ?? 0)}ms`} />
              <StatTile
                label="Errors"
                value={`${((summary?.error_rate ?? 0) * 100).toFixed(1)}%`}
              />
            </>
          )}
        </div>

        <div className="panel-grid">
          <div className="panel">
            <div className="panel-title">Spend by day</div>
            {loading ? (
              <div className="skeleton skeleton-text" style={{ height: 120 }} />
            ) : summary && summary.spend_by_day.length > 0 ? (
              summary.spend_by_day.map((d) => (
                <div className="bar-row" key={d.date}>
                  <span className="bar-label">{d.date}</span>
                  <span className="bar-track">
                    <span
                      className="bar-fill"
                      style={{ width: `${(Number(d.spend_usd) / maxDaySpend) * 100}%` }}
                    />
                  </span>
                  <span className="bar-value">${Number(d.spend_usd).toFixed(2)}</span>
                </div>
              ))
            ) : (
              <p className="text-muted">No spend data for this range.</p>
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

        {/* Phase 4 (AC4.5.1): real values from the extended usage/summary
            response - never placeholders like "Calculating..." */}
        <div className="panel-title" style={{ marginBottom: 12 }}>
          Reliability &amp; Cost Metrics
        </div>
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
                label="Cache Hit Rate"
                value={`${((summary?.cache_hit_rate ?? 0) * 100).toFixed(1)}%`}
                hint={
                  summary
                    ? `${summary.cache_hits} hits / ${summary.cache_misses} misses`
                    : undefined
                }
              />
              <StatTile
                label="Failover Events"
                value={(summary?.failover_events_count ?? 0).toLocaleString()}
                hint={
                  (summary?.failover_events_count ?? 0) > 0
                    ? "Requests that switched to a backup key"
                    : "No failovers in range"
                }
              />
              <StatTile
                label="Degraded Requests"
                value={(summary?.degraded_requests_count ?? 0).toLocaleString()}
                hint="Requests auto-downgraded to a cheaper model"
              />
              <StatTile
                label="Total Cost Saved"
                value={`$${Number(summary?.cost_saved_total_usd ?? 0).toFixed(2)}`}
                hint={
                  summary
                    ? `Caching $${Number(summary.cost_saved_caching_usd).toFixed(2)} + Degradation $${Number(
                        summary.cost_saved_degradation_usd
                      ).toFixed(2)}`
                    : undefined
                }
              />
            </>
          )}
        </div>

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
            { key: "requests", header: "Requests", align: "right", render: (r) => r.requests.toLocaleString() },
            { key: "spend", header: "Spend", align: "right", render: (r) => `$${Number(r.spend_usd).toFixed(2)}` },
            {
              key: "budget",
              header: "Budget",
              align: "right",
              render: (r) => (r.budget_usd === null ? "Unmetered" : `$${Number(r.budget_usd).toFixed(2)}`),
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
                  <span style={{ display: "flex", alignItems: "center", gap: 8, justifyContent: "flex-end" }}>
                    {spend >= budget ? <Badge tone="red">Exhausted</Badge> : null}
                    <BudgetBar spend={spend} budget={budget} />
                  </span>
                );
              },
            },
          ]}
        />
      </div>
    </ConsoleShell>
  );
}
