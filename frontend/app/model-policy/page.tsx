"use client";

/**
 * Model Policy screen (UI spec section 7.7). Real endpoints.
 *
 * Phase 5 (5.5 Unified Governance, AC5.5.9): the provider-grouped checklist
 * gains a "Self-Hosted" group sourced from registered self-hosted models -
 * only VERIFIED endpoints' models are selectable (an unverified endpoint's
 * models are never in `SelfHostedModelRouteCache`, so the backend would
 * reject them as unknown - AC5.5.6/design doc section 2.3(d)).
 */

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ConsoleShell } from "@/components/ConsoleShell";
import { useToast } from "@/components/ui";
import {
  ApiError,
  getModelPolicy,
  listSelfHostedProviders,
  MODELS_BY_PROVIDER,
  PROVIDER_LABELS,
  putModelPolicy,
  type ModelPolicyMode,
  type ProviderName,
  type SelfHostedProviderResponse,
} from "@/lib/api";

const PROVIDERS: ProviderName[] = ["openai", "anthropic", "vertex_ai"];

export default function ModelPolicyPage() {
  const toast = useToast();
  const [mode, setMode] = useState<ModelPolicyMode>("unconfigured");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [selfHostedProviders, setSelfHostedProviders] = useState<SelfHostedProviderResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    setLoading(true);
    Promise.all([getModelPolicy(), listSelfHostedProviders()])
      .then(([res, providers]) => {
        setMode(res.mode);
        setSelected(new Set(res.models));
        setSelfHostedProviders(providers);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load model policy."))
      .finally(() => setLoading(false));
  }, []);

  function toggleModel(model: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(model)) next.delete(model);
      else next.add(model);
      return next;
    });
    setDirty(true);
  }

  function setModeAndMarkDirty(next: ModelPolicyMode) {
    setMode(next);
    setDirty(true);
  }

  async function handleSave() {
    if (mode === "unconfigured") return;
    setSaving(true);
    setError(null);
    try {
      const result = await putModelPolicy({ mode, models: Array.from(selected) });
      setMode(result.mode);
      setSelected(new Set(result.models));
      setDirty(false);
      toast.push("success", "Model policy saved.");
    } catch (err) {
      if (err instanceof ApiError && err.code === "unknown_model_in_policy") {
        setError("One or more selected models aren't recognized. Refresh and try again.");
      } else {
        setError(err instanceof ApiError ? err.message : "Failed to save policy.");
      }
    } finally {
      setSaving(false);
    }
  }

  const helperCopy = useMemo(() => {
    if (mode === "allowlist") return "checked = allowed to be used";
    if (mode === "denylist") return "checked = blocked from use";
    return null;
  }, [mode]);

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Model Policy</div>
        <div className="page-subtitle">Control which models this org&apos;s traffic is allowed to reach.</div>

        {/* Phase 2 (FE-7): this policy is the ORG BASELINE layer - teams can
            narrow it further (never widen it) from each team's Model
            Restrictions card; users see the resolved result, with the
            blocking layer named, on their Model Access screen. */}
        <div className="banner banner-info">
          This is the org-wide baseline. Individual teams can further narrow it (never
          re-enable a model denied here) via Team Model Restrictions on each team&apos;s page
          under <Link href="/teams">Teams</Link>.
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}
        {!loading && mode === "unconfigured" ? (
          <div className="banner banner-info">
            No policy configured - all models are currently allowed by default.
          </div>
        ) : null}

        {!loading ? (
          <>
            <div className="mode-toggle">
              <label className="mode-option">
                <input
                  type="radio"
                  name="mode"
                  checked={mode === "allowlist"}
                  onChange={() => setModeAndMarkDirty("allowlist")}
                  style={{ width: "auto" }}
                />
                <span>
                  Allowlist
                  <div className="mode-option-copy">Only listed models may be used</div>
                </span>
              </label>
              <label className="mode-option">
                <input
                  type="radio"
                  name="mode"
                  checked={mode === "denylist"}
                  onChange={() => setModeAndMarkDirty("denylist")}
                  style={{ width: "auto" }}
                />
                <span>
                  Denylist
                  <div className="mode-option-copy">All models except those listed are allowed</div>
                </span>
              </label>
            </div>
            {helperCopy ? <p className="field-hint" style={{ marginTop: -8, marginBottom: 12 }}>{helperCopy}</p> : null}

            {PROVIDERS.map((provider) => (
              <div className="model-group" key={provider}>
                <div className="model-group-title">{PROVIDER_LABELS[provider]}</div>
                <div className="model-checkbox-grid">
                  {MODELS_BY_PROVIDER[provider].map((model) => (
                    <label className="model-checkbox" key={model}>
                      <input
                        type="checkbox"
                        checked={selected.has(model)}
                        onChange={() => toggleModel(model)}
                        disabled={mode === "unconfigured"}
                        style={{ width: "auto" }}
                      />
                      {model}
                    </label>
                  ))}
                </div>
              </div>
            ))}

            {selfHostedProviders.length > 0 ? (
              <div className="model-group">
                <div className="model-group-title">Self-Hosted</div>
                {selfHostedProviders.map((p) => (
                  <div key={p.id} style={{ marginBottom: 8 }}>
                    <div className="text-muted" style={{ fontSize: 12, marginBottom: 4 }}>
                      {p.name}{" "}
                      {!p.verified ? (
                        <span>
                          - not verified yet, re-verify on{" "}
                          <Link href="/providers">Providers</Link> before adding these models
                        </span>
                      ) : null}
                    </div>
                    <div className="model-checkbox-grid">
                      {p.models.map((model) => (
                        <label className="model-checkbox" key={model}>
                          <input
                            type="checkbox"
                            checked={selected.has(model)}
                            onChange={() => toggleModel(model)}
                            disabled={mode === "unconfigured" || !p.verified}
                            style={{ width: "auto" }}
                          />
                          {model}
                        </label>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            ) : null}

            <div className="page-header-row">
              <span className="text-muted">{selected.size} models selected</span>
              <button
                className="btn btn-primary"
                onClick={handleSave}
                disabled={saving || !dirty || mode === "unconfigured"}
              >
                {saving ? "Saving..." : "Save policy"}
              </button>
            </div>
          </>
        ) : (
          <div className="skeleton skeleton-text" style={{ height: 200 }} />
        )}
      </div>
    </ConsoleShell>
  );
}
