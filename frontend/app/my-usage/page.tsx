"use client";

/**
 * My Usage (Phase 2, non-admin UI doc section 4) - personal landing page:
 * own spend, requests, and per-model breakdown over the standard 24h/7d/30d
 * ranges, via session-only GET /v1/me/usage. Nothing here is editable - a
 * Member cannot change their own budget.
 *
 * The budget tile uses spend_by_user (which contains at most the caller
 * themselves on this endpoint); null budget renders as "Unmetered - no
 * spend cutoff", matching the shared budget-bar rule.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { ConsoleShell } from "@/components/ConsoleShell";
import { BudgetBar } from "@/components/ui";
import { RangeSelect, UsageSummaryPanels } from "@/components/usage-summary";
import {
  ApiError,
  getMyUsage,
  getStoredToken,
  type UsageRange,
  type UsageSummaryResponse,
} from "@/lib/api";

export default function MyUsagePage() {
  const [tokenMode] = useState(() => Boolean(getStoredToken()));
  const [range, setRange] = useState<UsageRange>("7d");
  const [summary, setSummary] = useState<UsageSummaryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (tokenMode) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getMyUsage(range)
      .then((data) => {
        if (!cancelled) setSummary(data);
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof ApiError ? err.message : "Failed to load your usage.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [range, tokenMode]);

  const self = summary?.spend_by_user[0] ?? null;

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-header-row">
          <div className="page-title">My Usage</div>
          {tokenMode ? null : <RangeSelect value={range} onChange={setRange} />}
        </div>

        {tokenMode ? (
          <div className="banner banner-info">
            This is a personal screen. The admin token has no personal identity - the
            org-wide view lives on the Dashboard.
          </div>
        ) : (
          <>
            {error ? <div className="banner banner-error">{error}</div> : null}

            <div className="panel" style={{ marginBottom: 16 }}>
              <div className="panel-title">My budget</div>
              {loading ? (
                <div className="skeleton skeleton-text" />
              ) : self && self.budget_usd !== null ? (
                <>
                  <p style={{ margin: "4px 0" }}>
                    ${Number(self.spend_usd).toFixed(2)} spent of $
                    {Number(self.budget_usd).toFixed(2)}
                  </p>
                  <BudgetBar spend={Number(self.spend_usd)} budget={Number(self.budget_usd)} />
                </>
              ) : (
                <p className="text-muted">Unmetered - no spend cutoff.</p>
              )}
            </div>

            <UsageSummaryPanels summary={summary} loading={loading} showUsers={false} />

            <p className="text-muted" style={{ marginTop: 16 }}>
              Your API keys are managed on <Link href="/my-keys">My API Keys</Link>; what each
              key can reach is on <Link href="/model-access">Model Access</Link>.
            </p>
          </>
        )}
      </div>
    </ConsoleShell>
  );
}
