"use client";

/**
 * Guided "select models to enable" flow shown right after an admin
 * successfully adds/edits a BYOK provider key (openai/anthropic/openrouter -
 * NEVER vertex_ai, which has no live listing, and NEVER ollama, which has its
 * own separate Self-Hosted Models flow). See `app/providers/page.tsx` for the
 * trigger wiring (fires from `ProviderKeyForm`'s `onSaved`, `mode="save"`
 * only - never `mode="rotate"`).
 *
 * This is a picker UI on top of the ALREADY-BUILT org model-policy allowlist
 * (`GET`/`PUT /v1/admin/model-policy`) - it writes into that existing
 * enforcement surface, it does not add any new one. Today, org model policy
 * defaults to `mode: "unconfigured"` (every known model allowed to every
 * team) until an admin explicitly builds an allowlist - this flow closes the
 * gap between "I just configured a provider" and "which of its models should
 * actually be usable", right at the moment that decision is most relevant.
 *
 * Flow (see `gatekey/` handoff spec for the full product rationale):
 * 1. Fetch the provider's live model catalog (`listAvailableModels`). Any
 *    failure (`custom_model_live_listing_unsupported`, `provider_not_
 *    configured`, or a genuine upstream error) falls back to today's
 *    behavior - silently close, no broken/error picker shown for something
 *    the admin didn't ask for.
 * 2. Render a checklist. An entry with a known `routable_as` is a normal
 *    checkbox that stages that name. An entry with `routable_as: null` has
 *    never been priced by Gatekey - clicking it step-swaps to the shared
 *    `CustomModelForm` (extracted from `/model-catalog`, prefilled with this
 *    entry's provider/native_model_id/default name) instead of toggling;
 *    only a successful registration stages it (cancel leaves it unstaged).
 *    A freshly-registered custom model is `verified: false` by construction
 *    (see `services.model_catalog._routable_as`'s docstring - only a
 *    VERIFIED custom model is ever reported as `routable_as`), and
 *    `services.model_policy.set_policy`'s known-model set only accepts
 *    verified custom model names - so this step auto-runs one live
 *    `verifyCustomModel()` call right after registration, before staging
 *    the name, and surfaces (never silently drops) a verification failure
 *    instead of letting a later `putModelPolicy` 422 on an unknown model.
 * 3. "Enable selected" merges the staged names into the org's CURRENT model
 *    policy if it's already in allowlist mode, or - the first time an org
 *    would transition into allowlist mode from "unconfigured"/"denylist" -
 *    shows an unmissable, `ConfirmDialog`-style warning first (mirrors
 *    `app/users/page.tsx`'s `OrgRoleModal` "Grant Org Admin" confirmation)
 *    since that transition immediately restricts every OTHER model, for
 *    every provider and team, that hasn't been explicitly enabled anywhere.
 * 4. "Skip for now" closes with no policy change at all - mirrors
 *    `ProviderKeyForm`'s own `allowSkip` pattern for tone/visual consistency.
 *
 * RBAC: this flow only ever mounts from `app/providers/page.tsx`'s provider
 * key modal, already only reachable by an org_admin session - no additional
 * client-side role gating is added here (same posture `ProviderKeyForm`
 * itself already takes).
 */

import { useEffect, useState } from "react";
import { CustomModelForm } from "@/components/custom-model-form";
import { Badge, ConfirmDialog, FieldError, Modal, useToast } from "@/components/ui";
import {
  ApiError,
  getModelPolicy,
  listAvailableModels,
  listCustomModels,
  listRegistryModelNames,
  listSelfHostedProviders,
  putModelPolicy,
  verifyCustomModel,
  PROVIDER_LABELS,
  type AvailableModelEntry,
  type CustomModelResponse,
  type SelfHostedProviderResponse,
} from "@/lib/api";

/** The only providers this flow ever triggers for - see module doc comment. */
export type ModelEnablePickerProvider = "openai" | "anthropic" | "openrouter";

export function ModelEnablePicker({
  provider,
  onClose,
}: {
  provider: ModelEnablePickerProvider;
  /** Fires when the whole flow is done - enabled, skipped, or silently
   * fell back because the live catalog wasn't available. The caller (the
   * Providers screen) just needs to know "the flow is over", not why. */
  onClose: () => void;
}) {
  const toast = useToast();
  const [loading, setLoading] = useState(true);
  const [entries, setEntries] = useState<AvailableModelEntry[]>([]);
  const [fallbackCandidates, setFallbackCandidates] = useState<string[]>([]);
  // Gatekey-facing model names currently staged to be enabled - both those
  // resolved directly from an entry's `routable_as` and those resolved by
  // completing an inline Custom Model registration below.
  const [staged, setStaged] = useState<Set<string>>(new Set());
  // native_model_id -> the name a just-completed inline registration
  // resolved to, so a `routable_as: null` entry's checkbox reflects checked
  // immediately without waiting on a full catalog refetch.
  const [resolvedNames, setResolvedNames] = useState<Record<string, string>>({});
  const [registering, setRegistering] = useState<AvailableModelEntry | null>(null);
  // Set right after a successful inline registration, cleared once the
  // follow-up `verifyCustomModel()` call (below) resolves either way - see
  // module doc comment for why this step exists.
  const [verifying, setVerifying] = useState<{ entry: AvailableModelEntry; model: CustomModelResponse } | null>(
    null
  );
  const [pendingFirstTransitionModels, setPendingFirstTransitionModels] = useState<string[] | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      listAvailableModels(provider),
      // Fallback-chain candidates for the inline registration sub-flow -
      // best-effort only (an empty/partial candidate list there is a minor
      // inconvenience, never a reason to abandon the whole picker).
      listRegistryModelNames().catch(() => [] as string[]),
      listCustomModels().catch(() => [] as CustomModelResponse[]),
      listSelfHostedProviders().catch(() => [] as SelfHostedProviderResponse[]),
    ])
      .then(([availableModels, registryNames, customModels, selfHosted]) => {
        if (cancelled) return;
        setEntries(availableModels);
        const verifiedCustomNames = customModels.filter((m) => m.verified).map((m) => m.name);
        const verifiedSelfHostedIds = Array.from(
          new Set(selfHosted.filter((p) => p.verified).flatMap((p) => p.models))
        );
        setFallbackCandidates(Array.from(new Set([...registryNames, ...verifiedCustomNames, ...verifiedSelfHostedIds])));
        setLoading(false);
      })
      .catch(() => {
        if (cancelled) return;
        // custom_model_live_listing_unsupported / provider_not_configured /
        // any genuine upstream failure - fall back to today's behavior: no
        // picker at all, just close (the key-saved toast already fired at
        // the call site before this component ever mounted).
        onClose();
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider]);

  // Auto-verify right after a successful inline registration - see module
  // doc comment. Runs whenever `verifying` is set; a real provider failure
  // (or the rare 429 cooldown, though unreachable in practice for a
  // brand-new row) is surfaced via toast, never silently swallowed, and the
  // entry is simply left unstaged (its registration itself still stands -
  // the admin can verify it later from Model Catalog and enable it from a
  // future run of this flow, or from the Model Policy screen directly).
  useEffect(() => {
    if (!verifying) return;
    let cancelled = false;
    verifyCustomModel(verifying.model.id)
      .then((result) => {
        if (cancelled) return;
        if (result.verified) {
          setResolvedNames((prev) => ({ ...prev, [verifying.entry.native_model_id]: result.name }));
          setStaged((prev) => new Set(prev).add(result.name));
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

  function toggle(entry: AvailableModelEntry) {
    const routableName = entry.routable_as ?? resolvedNames[entry.native_model_id] ?? null;
    if (routableName === null) {
      setRegistering(entry);
      return;
    }
    setStaged((prev) => {
      const next = new Set(prev);
      if (next.has(routableName)) next.delete(routableName);
      else next.add(routableName);
      return next;
    });
  }

  async function commitPolicy(models: string[]) {
    setSubmitting(true);
    setError(null);
    try {
      await putModelPolicy({ mode: "allowlist", models });
      toast.push(
        "success",
        `Enabled ${staged.size} model${staged.size === 1 ? "" : "s"} for ${PROVIDER_LABELS[provider]}.`
      );
      onClose();
    } catch (err) {
      // unknown_model_in_policy (422) shouldn't happen (everything staged
      // here came from a live `routable_as` or a just-completed
      // registration) but is surfaced verbatim, never swallowed, if it
      // somehow does. Falls back to the checklist step so the error is
      // actually visible.
      setError(err instanceof ApiError ? err.message : "Failed to update model policy.");
      setPendingFirstTransitionModels(null);
    } finally {
      setSubmitting(false);
    }
  }

  /** Re-reads the LIVE policy and decides merge-vs-replace against THAT,
   * never a value read earlier - closes a lost-update race a security
   * review flagged: the first-transition confirmation dialog can sit open
   * for an arbitrary, human-timescale amount of time, during which a
   * second org_admin could legitimately change the policy (e.g. move it
   * into allowlist mode themselves). Blindly replacing with whatever was
   * true when the dialog first opened could silently discard that other
   * admin's write with no error to either party. Called both right after
   * the merge-path's own read (no dialog, negligible window, but the same
   * discipline costs nothing extra) and from the confirmation dialog's
   * `onConfirm` (the real risk window). */
  async function finalizeSubmit(stagedNames: string[]) {
    setSubmitting(true);
    setError(null);
    try {
      const latest = await getModelPolicy();
      const models =
        latest.mode === "allowlist"
          ? Array.from(new Set([...latest.models, ...stagedNames]))
          : Array.from(new Set(stagedNames));
      await commitPolicy(models);
    } catch (err) {
      setSubmitting(false);
      setError(err instanceof ApiError ? err.message : "Failed to update model policy.");
      setPendingFirstTransitionModels(null);
    }
  }

  async function handleSubmitClick() {
    setError(null);
    setSubmitting(true);
    try {
      const currentPolicy = await getModelPolicy();
      const stagedNames = Array.from(staged);
      setSubmitting(false);
      if (currentPolicy.mode === "allowlist") {
        // Already in allowlist mode - no restrictive transition, no
        // confirmation needed. Straight to the race-safe re-read-then-write.
        await finalizeSubmit(stagedNames);
      } else {
        // First-ever transition into allowlist mode from "unconfigured" or
        // "denylist" - deliberately NOT pre-seeded with anything currently
        // allowed; only what's picked here becomes usable (unless the
        // policy has since moved to allowlist itself by the time of the
        // actual write - see `finalizeSubmit`). This is a real,
        // potentially-breaking behavior change, so it needs an explicit
        // confirmation before the PUT ever happens.
        setPendingFirstTransitionModels(Array.from(new Set(stagedNames)));
      }
    } catch (err) {
      setSubmitting(false);
      setError(err instanceof ApiError ? err.message : "Failed to load the current model policy.");
    }
  }

  // --- Step: inline Custom Model registration (routable_as: null entry) ------
  if (registering) {
    return (
      <CustomModelForm
        initial={null}
        prefill={{
          provider,
          native_model_id: registering.native_model_id,
          name: registering.native_model_id,
          input_price_per_million_usd: registering.input_price_per_million_usd,
          output_price_per_million_usd: registering.output_price_per_million_usd,
        }}
        fallbackCandidates={fallbackCandidates}
        onClose={() => setRegistering(null)}
        onSaved={(saved) => {
          // Not staged yet - a fresh registration is unverified, and only a
          // VERIFIED custom model name is accepted by model policy. The
          // effect above runs the required live verification next.
          setVerifying({ entry: registering, model: saved });
          setRegistering(null);
        }}
      />
    );
  }

  // --- Step: auto-verifying a just-registered custom model -------------------
  if (verifying) {
    return (
      <Modal title={`Verifying "${verifying.model.name}"...`} onClose={null}>
        <p className="text-muted" style={{ marginTop: 0 }}>
          Running one live test call against {PROVIDER_LABELS[provider]} to confirm this model is
          actually reachable before it can be enabled - only a verified custom model can be added
          to your org&apos;s model policy.
        </p>
        <div className="skeleton skeleton-text" />
      </Modal>
    );
  }

  // --- Step: first-ever allowlist-mode transition confirmation ----------------
  if (pendingFirstTransitionModels) {
    return (
      <ConfirmDialog
        title="Switch the org's model policy to allowlist mode?"
        consequence={
          `This is the org's first switch into allowlist mode. From the moment you confirm, EVERY ` +
          `model - for every provider and every team - is restricted to ONLY what has been ` +
          `explicitly enabled, including providers and models that are working right now and have ` +
          `not been reviewed through this flow. Only the ${pendingFirstTransitionModels.length} ` +
          `model${pendingFirstTransitionModels.length === 1 ? "" : "s"} you just picked for ` +
          `${PROVIDER_LABELS[provider]} will be usable until you enable more from the Model Policy ` +
          `screen.`
        }
        confirmLabel="Switch to allowlist mode"
        busy={submitting}
        onCancel={() => setPendingFirstTransitionModels(null)}
        onConfirm={() => finalizeSubmit(pendingFirstTransitionModels)}
      />
    );
  }

  // --- Step: loading the live catalog -----------------------------------------
  if (loading) {
    return (
      <Modal title={`Select models to enable for ${PROVIDER_LABELS[provider]}`} onClose={onClose}>
        <div className="skeleton skeleton-text" />
        <div className="skeleton skeleton-text" style={{ marginTop: 8 }} />
      </Modal>
    );
  }

  // --- Step: the checklist itself ---------------------------------------------
  return (
    <Modal title={`Select models to enable for ${PROVIDER_LABELS[provider]}`} onClose={onClose} width={640}>
      <p className="text-muted" style={{ marginTop: 0 }}>
        Pick which {PROVIDER_LABELS[provider]} models your org can actually use. This writes into
        the org&apos;s model access policy (Model Policy screen) - anything you don&apos;t select
        here isn&apos;t removed, but if this is your org&apos;s first allowlist you&apos;ll be
        warned before anything is restricted. You can always come back and adjust this later.
      </p>
      {entries.length === 0 ? (
        <div className="text-muted">No models are currently available from {PROVIDER_LABELS[provider]}.</div>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 8, maxHeight: 360, overflowY: "auto" }}>
          {entries.map((entry) => {
            const routableName = entry.routable_as ?? resolvedNames[entry.native_model_id] ?? null;
            const checked = routableName !== null && staged.has(routableName);
            const justRegistered = Boolean(resolvedNames[entry.native_model_id]);
            return (
              <label
                key={entry.native_model_id}
                style={{
                  display: "flex",
                  alignItems: "flex-start",
                  gap: 10,
                  border: "1px solid var(--border)",
                  borderRadius: 6,
                  padding: 10,
                  cursor: "pointer",
                }}
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => toggle(entry)}
                  style={{ marginTop: 3, width: "auto" }}
                />
                <span style={{ flex: 1 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
                    <span className="mono">{entry.native_model_id}</span>
                    <span className="text-muted">{entry.display_name}</span>
                    {routableName === null ? (
                      <Badge tone="amber">Needs registration &amp; pricing</Badge>
                    ) : justRegistered ? (
                      <Badge tone="green">Registered as &quot;{routableName}&quot;</Badge>
                    ) : null}
                  </div>
                  <div className="text-muted" style={{ fontSize: 12, marginTop: 2 }}>
                    {entry.input_price_per_million_usd ? (
                      <>
                        ${entry.input_price_per_million_usd} in
                        {entry.output_price_per_million_usd ? <> / ${entry.output_price_per_million_usd} out</> : null}
                        {" "}per million tokens
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
      <FieldError message={error} />
      <div className="modal-actions" style={{ justifyContent: "space-between" }}>
        <button className="btn btn-link" onClick={onClose} disabled={submitting}>
          Skip for now
        </button>
        <button className="btn btn-primary" onClick={handleSubmitClick} disabled={submitting || staged.size === 0}>
          {submitting ? "Working..." : `Enable selected (${staged.size})`}
        </button>
      </div>
    </Modal>
  );
}
