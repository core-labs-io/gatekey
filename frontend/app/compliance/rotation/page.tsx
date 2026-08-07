"use client";

/**
 * Rotation Policy - org-wide default (Phase 3, BD-15, design doc section
 * 9.6). Disabled by default (AC7.2). Per-service-account-key overrides live
 * on the Service Accounts screen (a "Rotation" action per app-key row);
 * provider-key rotation (always guided/manual) lives on the Providers
 * screen next to each provider's key.
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { RotationPolicyForm } from "@/components/rotation";
import { ApiError, getOrgRotationPolicy, putOrgRotationPolicy, type RotationPolicyResponse } from "@/lib/api";

export default function RotationPolicyPage() {
  const [policy, setPolicy] = useState<RotationPolicyResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getOrgRotationPolicy()
      .then(setPolicy)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load rotation policy."));
  }, []);

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Rotation Policy</div>
        <div className="page-subtitle">
          Org-wide default for automatic service-account key rotation. Individual keys can
          override this from <Link href="/service-accounts">Service Accounts</Link>; provider
          keys use a separate guided (manual) flow from <Link href="/providers">Providers</Link>.
        </div>
        {error ? <div className="banner banner-error">{error}</div> : null}
        {policy ? (
          <div className="panel">
            <div className="panel-title">Org default</div>
            <RotationPolicyForm policy={policy} onSave={putOrgRotationPolicy} onSaved={setPolicy} />
          </div>
        ) : !error ? (
          <div className="skeleton skeleton-text" style={{ height: 200 }} />
        ) : null}
      </div>
    </ConsoleShell>
  );
}
