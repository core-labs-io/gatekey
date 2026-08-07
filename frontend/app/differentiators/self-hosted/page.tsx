"use client";

/**
 * Differentiators - Self-Hosted Governance (Phase 5, 5.5, ui doc section
 * 12 - "a thin cross-link... surfaces the cost-normalization audit view").
 * Actual endpoint registration/edit/remove lives on the Providers screen's
 * "Self-Hosted Models" card - this tab is read-only everywhere, for both
 * Org Admin and Auditor sessions.
 *
 * Backend gap (flagged, not silently worked around): there is no
 * `GET /v1/admin/self-hosted-providers/{id}/usage` per-endpoint breakdown
 * endpoint in the current backend, despite the design doc's API-contract
 * table naming one. This screen uses the general
 * `GET /v1/admin/usage/summary?provider=self_hosted` aggregate instead
 * (org-wide totals across every self-hosted endpoint combined, not broken
 * out per endpoint) - labeled accordingly below.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { ConsoleShell } from "@/components/ConsoleShell";
import { Badge, StatTile, StatTileSkeleton } from "@/components/ui";
import {
  ApiError,
  getUsageSummary,
  listSelfHostedProviders,
  type SelfHostedProviderResponse,
  type UsageSummaryResponse,
} from "@/lib/api";

export default function SelfHostedGovernancePage() {
  const [providers, setProviders] = useState<SelfHostedProviderResponse[]>([]);
  const [summary, setSummary] = useState<UsageSummaryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([listSelfHostedProviders(), getUsageSummary("30d", { provider: "self_hosted" })])
      .then(([providerRows, summaryRow]) => {
        setProviders(providerRows);
        setSummary(summaryRow);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load self-hosted governance data."))
      .finally(() => setLoading(false));
  }, []);

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Self-Hosted Governance</div>
        <div className="page-subtitle">
          Cost-normalization audit view for registered self-hosted endpoints. Register, edit, or
          remove endpoints on the <Link href="/providers">Providers</Link> screen&apos;s
          &quot;Self-Hosted Models&quot; card.
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}

        <div className="banner banner-info">
          Every figure below is <strong>estimated</strong> (configured GPU-hour rate x request
          wall-clock latency) - not an invoice figure the way a BYOK provider&apos;s token-based
          pricing is. These totals are org-wide across every self-hosted endpoint combined, over
          the last 30 days.
        </div>

        <div className="stat-grid">
          {loading || !summary ? (
            <>
              <StatTileSkeleton />
              <StatTileSkeleton />
              <StatTileSkeleton />
            </>
          ) : (
            <>
              <StatTile label="Requests (30d)" value={summary.request_count.toLocaleString()} />
              <StatTile
                label="Estimated cost (30d)"
                value={`$${Number(summary.total_spend_usd).toFixed(2)}`}
                hint="Estimated - not invoice-grade"
              />
              <StatTile label="Average latency" value={`${Math.round(summary.avg_latency_ms)}ms`} />
            </>
          )}
        </div>

        <div className="panel">
          <div className="panel-title">Registered endpoints</div>
          {loading ? (
            <div className="skeleton skeleton-text" />
          ) : providers.length === 0 ? (
            <div className="text-muted">
              No self-hosted endpoints registered yet - add one on the{" "}
              <Link href="/providers">Providers</Link> screen.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {providers.map((p) => (
                <div key={p.id} style={{ border: "1px solid var(--border)", borderRadius: 6, padding: 12 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <span className="mono">{p.name}</span>
                    <Badge tone={p.verified ? "green" : "gray"}>{p.verified ? "Verified" : "Not verified"}</Badge>
                  </div>
                  <div className="text-muted" style={{ fontSize: 12, marginTop: 4 }}>
                    {p.base_url} · Cost basis: ${p.cost_basis_per_gpu_hour}/GPU-hour (estimated) ·
                    Models: {p.models.join(", ")}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </ConsoleShell>
  );
}
