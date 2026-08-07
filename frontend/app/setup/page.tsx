"use client";

/**
 * First-run setup wizard (UI spec section 7.2), adapted to Phase 1's real
 * auth model: the backend has no endpoint to *set* an admin credential (it
 * is provisioned via the GATEKEY_ADMIN_TOKEN env var before the process
 * ever starts - see api/deps.py), so this app cannot literally implement
 * the spec's "Step 1: set your admin credential" as a persisted action.
 * Signing in at /login already IS "step 1" in practice - by the time a
 * caller reaches this page they have already authenticated. This screen is
 * therefore the spec's step 2 only: connect your first provider. See
 * ../login/page.tsx for where the not-yet-configured redirect happens.
 */

import { useState } from "react";
import { useRouter } from "next/navigation";
import { ProviderKeyForm } from "@/components/ProviderKeyForm";
import { getStoredToken, PROVIDER_LABELS, type ProviderName } from "@/lib/api";
import { useEffect } from "react";

const PROVIDERS: ProviderName[] = ["openai", "anthropic", "vertex_ai"];

export default function SetupPage() {
  const router = useRouter();
  const [provider, setProvider] = useState<ProviderName>("openai");

  useEffect(() => {
    if (!getStoredToken()) router.replace("/login");
  }, [router]);

  return (
    <div className="auth-shell">
      <div className="wizard-card">
        <div className="wizard-header">
          <h2>Welcome to Gatekey</h2>
          <span className="wizard-step-label">Step 2 of 2</span>
        </div>
        <p style={{ marginTop: 0, fontWeight: 600 }}>Connect your first provider</p>
        <p className="field-hint" style={{ marginBottom: 16 }}>
          Add at least one key so requests have somewhere to route to.
        </p>
        <div className="provider-tabs">
          {PROVIDERS.map((p) => (
            <button
              key={p}
              type="button"
              className={`provider-tab ${provider === p ? "active" : ""}`}
              onClick={() => setProvider(p)}
            >
              {PROVIDER_LABELS[p]}
            </button>
          ))}
        </div>
        <ProviderKeyForm
          provider={provider}
          onSaved={() => router.replace("/dashboard")}
          allowSkip={() => router.replace("/dashboard")}
        />
      </div>
    </div>
  );
}
