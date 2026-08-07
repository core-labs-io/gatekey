"use client";

/**
 * Differentiators - Self-Hosted Governance (Phase 5, 5.5, ui doc section
 * 12 - "a thin cross-link... surfaces the cost-normalization audit view").
 * Actual endpoint registration/edit/remove lives on the Providers screen's
 * "Self-Hosted Models" card - this tab is read-only everywhere, for both
 * Org Admin and Auditor sessions.
 *
 * Hardening pass item 6 (closes a gap flagged, not silently worked around,
 * in the prior pass): `GET /v1/admin/self-hosted-providers/{id}/usage` now
 * exists, giving a real per-endpoint requests/estimated-cost/avg-latency
 * breakdown - one row per registered endpoint below - instead of only the
 * general `GET /v1/admin/usage/summary?provider=self_hosted` org-wide
 * aggregate this screen used to fall back to. The org-wide total is kept as
 * a top-line stat strip (still useful as an at-a-glance figure), now backed
 * by summing the per-endpoint rows fetched alongside it rather than a
 * second, separately-windowed API call - so the top strip and the per-
 * endpoint breakdown below it always agree on the same range/window.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { ConsoleShell } from "@/components/ConsoleShell";
import { RangeSelect } from "@/components/usage-summary";
import { Badge, StatTile, StatTileSkeleton } from "@/components/ui";
import {
  ApiError,
  getSelfHostedProviderUsage,
  listSelfHostedProviders,
  type SelfHostedProviderResponse,
  type SelfHostedProviderUsageResponse,
  type UsageRange,
} from "@/lib/api";

export default function SelfHostedGovernancePage() {
  const [range, setRange] = useState<UsageRange>("30d");
  const [providers, setProviders] = useState<SelfHostedProviderResponse[]>([]);
  // Keyed by self_hosted_provider_id - one usage row per registered
  // endpoint, undefined until that endpoint's own fetch resolves so a slow
  // one doesn't block the others from rendering.
  const [usageByProvider, setUsageByProvider] = useState<Map<string, SelfHostedProviderUsageResponse>>(new Map());
  const [loading, setLoading] = useState(true);
  const [usageError, setUsageError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setUsageError(null);
    listSelfHostedProviders()
      .then(async (providerRows) => {
        if (cancelled) return;
        setProviders(providerRows);
        if (providerRows.length === 0) {
          setUsageByProvider(new Map());
          return;
        }
        const results = await Promise.allSettled(
          providerRows.map((p) => getSelfHostedProviderUsage(p.id, range))
        );
        if (cancelled) return;
        const next = new Map<string, SelfHostedProviderUsageResponse>();
        let anyFailed = false;
        results.forEach((result, i) => {
          if (result.status === "fulfilled") {
            next.set(providerRows[i].id, result.value);
          } else {
            anyFailed = true;
          }
        });
        setUsageByProvider(next);
        if (anyFailed) setUsageError("Usage could not be loaded for one or more endpoints.");
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof ApiError ? err.message : "Failed to load self-hosted governance data.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [range]);

  const orgTotals = Array.from(usageByProvider.values()).reduce(
    (acc, u) => ({
      requests: acc.requests + u.total_requests,
      cost: acc.cost + Number(u.total_estimated_cost_usd),
      latencySum: acc.latencySum + u.avg_latency_ms * u.total_requests,
      requestsForLatency: acc.requestsForLatency + u.total_requests,
    }),
    { requests: 0, cost: 0, latencySum: 0, requestsForLatency: 0 }
  );
  const orgAvgLatency = orgTotals.requestsForLatency > 0 ? orgTotals.latencySum / orgTotals.requestsForLatency : 0;

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-header-row">
          <div>
            <div className="page-title">Self-Hosted Governance</div>
            <div className="page-subtitle">
              Cost-normalization audit view for registered self-hosted endpoints. Register, edit, or
              remove endpoints on the <Link href="/providers">Providers</Link> screen&apos;s
              &quot;Self-Hosted Models&quot; card.
            </div>
          </div>
          <RangeSelect value={range} onChange={setRange} />
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}
        {usageError ? <div className="banner banner-error">{usageError}</div> : null}

        <div className="banner banner-info">
          Every figure below is <strong>estimated</strong> (configured GPU-hour rate x request
          wall-clock latency) - not an invoice figure the way a BYOK provider&apos;s token-based
          pricing is.
        </div>

        <div className="stat-grid">
          {loading ? (
            <>
              <StatTileSkeleton />
              <StatTileSkeleton />
              <StatTileSkeleton />
            </>
          ) : (
            <>
              <StatTile label="Requests" value={orgTotals.requests.toLocaleString()} hint="All endpoints, org-wide" />
              <StatTile
                label="Estimated cost"
                value={`$${orgTotals.cost.toFixed(2)}`}
                hint="Estimated - not invoice-grade"
              />
              <StatTile label="Average latency" value={`${Math.round(orgAvgLatency)}ms`} hint="All endpoints, org-wide" />
            </>
          )}
        </div>

        <div className="panel">
          <div className="panel-title">Per-endpoint breakdown</div>
          {loading ? (
            <div className="skeleton skeleton-text" />
          ) : providers.length === 0 ? (
            <div className="text-muted">
              No self-hosted endpoints registered yet - add one on the{" "}
              <Link href="/providers">Providers</Link> screen.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {providers.map((p) => {
                const usage = usageByProvider.get(p.id);
                return (
                  <div key={p.id} style={{ border: "1px solid var(--border)", borderRadius: 6, padding: 12 }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <span className="mono">{p.name}</span>
                      <Badge tone={p.verified ? "green" : "gray"}>{p.verified ? "Verified" : "Not verified"}</Badge>
                    </div>
                    <div className="text-muted" style={{ fontSize: 12, marginTop: 4 }}>
                      {p.base_url} · Cost basis: ${p.cost_basis_per_gpu_hour}/GPU-hour (estimated) ·
                      Models: {p.models.join(", ")}
                    </div>
                    <div style={{ display: "flex", gap: 24, marginTop: 10 }}>
                      <div>
                        <div className="stat-tile-label">Requests</div>
                        <div className="stat-tile-value">
                          {usage ? usage.total_requests.toLocaleString() : "—"}
                        </div>
                      </div>
                      <div>
                        <div className="stat-tile-label">Estimated cost</div>
                        <div className="stat-tile-value">
                          {usage ? `$${Number(usage.total_estimated_cost_usd).toFixed(2)}` : "—"}
                        </div>
                      </div>
                      <div>
                        <div className="stat-tile-label">Avg latency</div>
                        <div className="stat-tile-value">
                          {usage ? `${Math.round(usage.avg_latency_ms)}ms` : "—"}
                        </div>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </ConsoleShell>
  );
}
