"use client";

/**
 * Model Policy screen (UI spec section 7.7). Real endpoints.
 *
 * Redesigned: the old checklist iterated a hand-typed `MODELS_BY_PROVIDER`
 * constant for openai/anthropic/openrouter, which drifts from reality the
 * moment a provider adds, renames, or retires a model. This version drives
 * those three providers from `listAvailableModels()` - the same live
 * "what does this provider actually have right now" lookup the post-key-save
 * `ModelEnablePicker` flow already uses - fetched on demand only for the
 * provider the admin picks from the dropdown below (not all three eagerly on
 * load, since each is a real upstream network call). `vertex_ai` has no live
 * listing (`custom_model_live_listing_unsupported`), so it's sourced from
 * `listRegistryModels()` instead - still zero hand-typed data, just backend-
 * derived rather than live-provider-derived.
 *
 * "Select entire provider" is a SNAPSHOT of what's live right now, not a
 * standing "always allow whatever this provider has" rule - a model this
 * provider adds next month needs one more visit here before it's usable.
 * That's a deliberate choice (see conversation this shipped from): a
 * live-tracking wildcard would need a new rule shape across the org/team/
 * member policy layers and would auto-admit unreviewed models org-wide.
 *
 * Self-Hosted and Custom groups are unchanged from before this redesign -
 * both were already sourced live from this org's own DB rows, never from a
 * hand-typed list, so they never had the staleness problem being fixed here.
 */

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ConsoleShell } from "@/components/ConsoleShell";
import { CustomModelForm } from "@/components/custom-model-form";
import { Badge, useToast } from "@/components/ui";
import {
  ApiError,
  getModelPolicy,
  listAvailableModels,
  listCustomModels,
  listRegistryModelNames,
  listRegistryModels,
  listSelfHostedProviders,
  PROVIDER_LABELS,
  putModelPolicy,
  verifyCustomModel,
  type AvailableModelEntry,
  type CustomModelResponse,
  type ModelPolicyMode,
  type RegistryModelEntry,
  type SelfHostedProviderResponse,
} from "@/lib/api";

/** Providers offered in the dropdown below - the four BYOK-style providers
 * Model Policy governs directly. `ollama` isn't here: it's purely
 * self-hosted, with no provider-wide catalog of its own - its models come
 * entirely from this org's registered Self-Hosted endpoints (own section
 * below, unchanged). */
type PolicyProvider = "openai" | "anthropic" | "openrouter" | "vertex_ai";
const POLICY_PROVIDERS: PolicyProvider[] = ["openai", "anthropic", "openrouter", "vertex_ai"];
/** The subset with a real live-listing endpoint - `vertex_ai` is excluded,
 * see module docstring. */
const LIVE_LISTING_PROVIDERS: PolicyProvider[] = ["openai", "anthropic", "openrouter"];

export default function ModelPolicyPage() {
  const toast = useToast();
  const [mode, setMode] = useState<ModelPolicyMode>("unconfigured");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [selfHostedProviders, setSelfHostedProviders] = useState<SelfHostedProviderResponse[]>([]);
  const [customModels, setCustomModels] = useState<CustomModelResponse[]>([]);
  const [registryModels, setRegistryModels] = useState<RegistryModelEntry[]>([]);
  const [registryNames, setRegistryNames] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);

  // --- Provider dropdown + live catalog -------------------------------------
  const [activeProvider, setActiveProvider] = useState<PolicyProvider | "">("");
  const [liveEntries, setLiveEntries] = useState<AvailableModelEntry[]>([]);
  const [liveLoading, setLiveLoading] = useState(false);
  const [liveError, setLiveError] = useState<{ notConfigured: boolean; message: string } | null>(null);
  const [filterText, setFilterText] = useState("");
  // native_model_id -> the name a just-completed inline registration
  // resolved to, so that entry's checkbox reflects checked immediately
  // without waiting on a full catalog refetch.
  const [resolvedNames, setResolvedNames] = useState<Record<string, string>>({});
  const [registering, setRegistering] = useState<AvailableModelEntry | null>(null);
  const [verifying, setVerifying] = useState<{ entry: AvailableModelEntry; model: CustomModelResponse } | null>(
    null
  );

  useEffect(() => {
    setLoading(true);
    Promise.all([
      getModelPolicy(),
      listSelfHostedProviders(),
      listCustomModels(),
      listRegistryModels(),
      listRegistryModelNames(),
    ])
      .then(([res, providers, custom, registry, names]) => {
        setMode(res.mode);
        setSelected(new Set(res.models));
        setSelfHostedProviders(providers);
        setCustomModels(custom);
        setRegistryModels(registry);
        setRegistryNames(names);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load model policy."))
      .finally(() => setLoading(false));
  }, []);

  // Fetch the live catalog only for the provider currently chosen in the
  // dropdown - never all three eagerly, each is a real upstream call.
  useEffect(() => {
    if (!activeProvider || !LIVE_LISTING_PROVIDERS.includes(activeProvider)) return;
    let cancelled = false;
    setLiveLoading(true);
    setLiveError(null);
    setLiveEntries([]);
    listAvailableModels(activeProvider)
      .then((entries) => {
        if (cancelled) return;
        setLiveEntries(entries);
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.code === "provider_not_configured") {
          setLiveError({ notConfigured: true, message: err.message });
        } else {
          setLiveError({
            notConfigured: false,
            message: err instanceof ApiError ? err.message : "Failed to load live models.",
          });
        }
      })
      .finally(() => {
        if (!cancelled) setLiveLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [activeProvider]);

  // Auto-verify right after a successful inline registration - mirrors
  // `ModelEnablePicker`'s identical step (same reason: only a VERIFIED
  // custom model name is accepted by model policy).
  useEffect(() => {
    if (!verifying) return;
    let cancelled = false;
    verifyCustomModel(verifying.model.id)
      .then((result) => {
        if (cancelled) return;
        if (result.verified) {
          setResolvedNames((prev) => ({ ...prev, [verifying.entry.native_model_id]: result.name }));
          setSelected((prev) => new Set(prev).add(result.name));
          setDirty(true);
          setCustomModels((prev) => [...prev.filter((m) => m.id !== result.id), result]);
          toast.push("success", `"${result.name}" registered and verified.`);
        } else {
          toast.push(
            "error",
            `"${verifying.model.name}" was registered but could not be verified - check the native ` +
              "model id from Model Catalog, then come back and enable it."
          );
        }
        setVerifying(null);
      })
      .catch((err) => {
        if (cancelled) return;
        toast.push(
          "error",
          err instanceof ApiError
            ? `"${verifying.model.name}" was registered but verification failed: ${err.message}`
            : `"${verifying.model.name}" was registered but verification failed.`
        );
        setVerifying(null);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [verifying]);

  function toggleModel(model: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(model)) next.delete(model);
      else next.add(model);
      return next;
    });
    setDirty(true);
  }

  function toggleGroup(models: string[]) {
    const allSelected = models.length > 0 && models.every((model) => selected.has(model));
    setSelected((prev) => {
      const next = new Set(prev);
      for (const model of models) {
        if (allSelected) next.delete(model);
        else next.add(model);
      }
      return next;
    });
    setDirty(true);
  }

  function toggleLiveEntry(entry: AvailableModelEntry) {
    const routableName = entry.routable_as ?? resolvedNames[entry.native_model_id] ?? null;
    if (routableName === null) {
      setRegistering(entry);
      return;
    }
    toggleModel(routableName);
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

  // Fallback-chain candidates for the inline registration sub-flow - best
  // effort only, mirrors `ModelEnablePicker`'s identical union.
  const fallbackCandidates = useMemo(() => {
    const verifiedCustomNames = customModels.filter((m) => m.verified).map((m) => m.name);
    const verifiedSelfHostedIds = Array.from(
      new Set(selfHostedProviders.filter((p) => p.verified).flatMap((p) => p.models))
    );
    return Array.from(new Set([...registryNames, ...verifiedCustomNames, ...verifiedSelfHostedIds]));
  }, [registryNames, customModels, selfHostedProviders]);

  const vertexModels = useMemo(
    () => registryModels.filter((m) => m.provider === "vertex_ai").map((m) => m.name),
    [registryModels]
  );

  const filteredLiveEntries = useMemo(() => {
    const q = filterText.trim().toLowerCase();
    if (!q) return liveEntries;
    return liveEntries.filter(
      (e) => e.native_model_id.toLowerCase().includes(q) || e.display_name.toLowerCase().includes(q)
    );
  }, [liveEntries, filterText]);

  const sortedSelected = useMemo(() => Array.from(selected).sort((a, b) => a.localeCompare(b)), [selected]);

  // --- Inline Custom Model registration (routable_as: null live entry) -----
  if (registering) {
    return (
      <ConsoleShell>
        <div className="page">
          <CustomModelForm
            initial={null}
            prefill={{
              provider: activeProvider as "openai" | "anthropic" | "openrouter",
              native_model_id: registering.native_model_id,
              name: registering.native_model_id,
              input_price_per_million_usd: registering.input_price_per_million_usd,
              output_price_per_million_usd: registering.output_price_per_million_usd,
            }}
            fallbackCandidates={fallbackCandidates}
            onClose={() => setRegistering(null)}
            onSaved={(saved) => {
              setVerifying({ entry: registering, model: saved });
              setRegistering(null);
            }}
          />
        </div>
      </ConsoleShell>
    );
  }

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
            {helperCopy ? <p className="field-hint" style={{ marginTop: -8, marginBottom: 4 }}>{helperCopy}</p> : null}

            {/* Always-visible summary of the current set, regardless of which
                provider tab is open below - this is "what's allowed" (or
                "what's blocked", in denylist mode) at a glance, spanning
                registry, self-hosted, AND custom/Model Catalog models. */}
            {mode !== "unconfigured" ? (
              <div className="model-group" style={{ marginTop: 8 }}>
                <div className="model-group-title">
                  {mode === "allowlist" ? "Currently allowed" : "Currently blocked"} ({sortedSelected.length})
                </div>
                {sortedSelected.length === 0 ? (
                  <div className="text-muted" style={{ fontSize: 13 }}>
                    Nothing selected yet - pick models from a provider below.
                  </div>
                ) : (
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                    {sortedSelected.map((model) => (
                      <button
                        key={model}
                        type="button"
                        className="mono"
                        onClick={() => toggleModel(model)}
                        title="Click to remove"
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 6,
                          border: "1px solid var(--border)",
                          borderRadius: 999,
                          padding: "3px 10px",
                          fontSize: 12,
                          background: "var(--surface)",
                          cursor: "pointer",
                        }}
                      >
                        {model}
                        <span aria-hidden="true" style={{ color: "var(--text-muted)" }}>
                          ×
                        </span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ) : null}

            {/* --- Provider picker: dropdown -> live (or registry) catalog --- */}
            <div className="model-group">
              <div className="model-group-title">Add models from a provider</div>
              <select
                value={activeProvider}
                onChange={(e) => {
                  setActiveProvider(e.target.value as PolicyProvider | "");
                  setFilterText("");
                }}
                style={{ maxWidth: 320, marginBottom: 10 }}
              >
                <option value="">Choose a provider...</option>
                {POLICY_PROVIDERS.map((p) => (
                  <option key={p} value={p}>
                    {PROVIDER_LABELS[p]}
                  </option>
                ))}
              </select>

              {activeProvider === "vertex_ai" ? (
                <>
                  <label className="model-checkbox" style={{ marginBottom: 6 }}>
                    <input
                      type="checkbox"
                      checked={vertexModels.length > 0 && vertexModels.every((m) => selected.has(m))}
                      onChange={() => toggleGroup(vertexModels)}
                      disabled={mode === "unconfigured"}
                      style={{ width: "auto" }}
                    />
                    Select entire provider ({vertexModels.length} models)
                  </label>
                  <div className="model-checkbox-grid">
                    {vertexModels.map((model) => (
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
                </>
              ) : null}

              {activeProvider && LIVE_LISTING_PROVIDERS.includes(activeProvider) ? (
                <>
                  {liveLoading ? (
                    <div className="skeleton skeleton-text" />
                  ) : liveError ? (
                    <div className="banner banner-error">
                      {liveError.notConfigured ? (
                        <>
                          No {PROVIDER_LABELS[activeProvider]} key configured yet - add one on{" "}
                          <Link href="/providers">Providers</Link> to see and manage its live models.
                        </>
                      ) : (
                        liveError.message
                      )}
                    </div>
                  ) : (
                    <>
                      {liveEntries.length > 5 ? (
                        <input
                          type="text"
                          placeholder={`Filter ${PROVIDER_LABELS[activeProvider]} models...`}
                          value={filterText}
                          onChange={(e) => setFilterText(e.target.value)}
                          style={{ maxWidth: 320, marginBottom: 10 }}
                        />
                      ) : null}
                      <label className="model-checkbox" style={{ marginBottom: 6 }}>
                        <input
                          type="checkbox"
                          checked={
                            filteredLiveEntries.some((e) => e.routable_as !== null) &&
                            filteredLiveEntries
                              .filter((e) => e.routable_as !== null)
                              .every((e) => selected.has(e.routable_as as string))
                          }
                          onChange={() =>
                            toggleGroup(
                              filteredLiveEntries
                                .filter((e) => e.routable_as !== null)
                                .map((e) => e.routable_as as string)
                            )
                          }
                          disabled={mode === "unconfigured"}
                          style={{ width: "auto" }}
                        />
                        Select entire provider ({filteredLiveEntries.filter((e) => e.routable_as !== null).length}
                        {filteredLiveEntries.length !== liveEntries.length ? " matching" : ""} models)
                      </label>
                      {liveEntries.length === 0 ? (
                        <div className="text-muted">
                          No models are currently available from {PROVIDER_LABELS[activeProvider]}.
                        </div>
                      ) : (
                        <div style={{ display: "flex", flexDirection: "column", gap: 6, maxHeight: 360, overflowY: "auto" }}>
                          {filteredLiveEntries.map((entry) => {
                            const routableName = entry.routable_as ?? resolvedNames[entry.native_model_id] ?? null;
                            const checked = routableName !== null && selected.has(routableName);
                            return (
                              <label
                                key={entry.native_model_id}
                                style={{
                                  display: "flex",
                                  alignItems: "flex-start",
                                  gap: 10,
                                  border: "1px solid var(--border)",
                                  borderRadius: 6,
                                  padding: 8,
                                  cursor: mode === "unconfigured" ? "default" : "pointer",
                                }}
                              >
                                <input
                                  type="checkbox"
                                  checked={checked}
                                  onChange={() => toggleLiveEntry(entry)}
                                  disabled={mode === "unconfigured"}
                                  style={{ marginTop: 3, width: "auto" }}
                                />
                                <span style={{ flex: 1 }}>
                                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                                    <span className="mono">{entry.native_model_id}</span>
                                    <span className="text-muted">{entry.display_name}</span>
                                    {routableName === null ? (
                                      <Badge tone="amber">Needs registration &amp; pricing</Badge>
                                    ) : null}
                                  </div>
                                  <div className="text-muted" style={{ fontSize: 12, marginTop: 2 }}>
                                    {entry.input_price_per_million_usd ? (
                                      <>
                                        ${entry.input_price_per_million_usd} in
                                        {entry.output_price_per_million_usd ? (
                                          <> / ${entry.output_price_per_million_usd} out</>
                                        ) : null}{" "}
                                        per million tokens
                                      </>
                                    ) : (
                                      "Pricing unknown"
                                    )}
                                  </div>
                                </span>
                              </label>
                            );
                          })}
                        </div>
                      )}
                    </>
                  )}
                </>
              ) : null}

              {!activeProvider ? (
                <div className="text-muted" style={{ fontSize: 13 }}>
                  Pick a provider above to see its models, live from {PROVIDER_LABELS.openai} /{" "}
                  {PROVIDER_LABELS.anthropic} / {PROVIDER_LABELS.openrouter}, or {PROVIDER_LABELS.vertex_ai}
                  &apos;s registry.
                </div>
              ) : null}
            </div>

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
                      ) : (
                        <label className="model-checkbox" style={{ display: "inline-flex", marginLeft: 12 }}>
                          <input
                            type="checkbox"
                            checked={p.models.every((model) => selected.has(model))}
                            onChange={() => toggleGroup(p.models)}
                            disabled={mode === "unconfigured"}
                            style={{ width: "auto" }}
                          />
                          select all
                        </label>
                      )}
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

            {/* CMR-11: flat "Custom" group sourced from listCustomModels() -
                unlike Self-Hosted (grouped by endpoint), custom models are each
                individually named/verified, so verification state is shown
                per-checkbox rather than per-group. Only verified: true models
                are selectable (an unverified custom model's name is never
                accepted by PUT /v1/admin/model-policy, same rejection reason
                self-hosted's unverified models get) - shown disabled with an
                explanatory note rather than omitted, matching the Self-Hosted
                group's existing convention above for a consistent pattern. */}
            {customModels.length > 0 ? (
              <div className="model-group">
                <div className="model-group-title">
                  Custom
                  <label className="model-checkbox" style={{ display: "inline-flex", marginLeft: 12, fontWeight: "normal" }}>
                    <input
                      type="checkbox"
                      checked={
                        customModels.some((m) => m.verified) &&
                        customModels.filter((m) => m.verified).every((m) => selected.has(m.name))
                      }
                      onChange={() => toggleGroup(customModels.filter((m) => m.verified).map((m) => m.name))}
                      disabled={mode === "unconfigured" || !customModels.some((m) => m.verified)}
                      style={{ width: "auto" }}
                    />
                    select all
                  </label>
                </div>
                <div className="model-checkbox-grid">
                  {customModels.map((m) => (
                    <label className="model-checkbox" key={m.id}>
                      <input
                        type="checkbox"
                        checked={selected.has(m.name)}
                        onChange={() => toggleModel(m.name)}
                        disabled={mode === "unconfigured" || !m.verified}
                        style={{ width: "auto" }}
                      />
                      {m.name}
                      {!m.verified ? (
                        <span className="text-muted" style={{ fontFamily: "var(--sans)", fontSize: 12, fontWeight: "normal" }}>
                          {" "}
                          - not verified yet, re-verify on <Link href="/providers">Providers</Link>
                        </span>
                      ) : null}
                      {m.shadowed_by_registry ? (
                        <Badge
                          tone="red"
                          title="A newer Gatekey release now defines this same model name - live requests route to that static entry, not this custom mapping."
                        >
                          Shadowed
                        </Badge>
                      ) : null}
                    </label>
                  ))}
                </div>
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
