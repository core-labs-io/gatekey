"use client";

/**
 * Add/edit provider key form (UI spec section 7.4). Shared between the
 * Providers screen's modal and the first-run setup wizard's step 2.
 *
 * Validation is a live provider round-trip on the backend (test call before
 * save) - it can take a couple seconds and can fail three distinct ways,
 * each rendered with distinct copy per the UI spec, never collapsed into
 * one generic error.
 */

import { useState } from "react";
import {
  ApiError,
  PROVIDER_LABELS,
  putProviderKey,
  rotateProviderKeyGuided,
  type ProviderName,
} from "@/lib/api";

function validationErrorMessage(err: unknown, providerLabel: string): string {
  if (err instanceof ApiError) {
    if (err.status === 422 || err.code === "invalid_key") {
      return `This key was rejected by ${providerLabel}. Double-check it and try again.`;
    }
    if (err.status === 502 || err.code === "provider_unreachable") {
      return `Couldn't reach ${providerLabel} to validate this key. Check your network and try again.`;
    }
    return `Something went wrong validating this key with ${providerLabel}. Try again.`;
  }
  return "Something went wrong. Try again.";
}

// Per-provider placeholder text for the generic single-API-key-field branch
// (everything except vertex_ai and ollama, which each have their own
// dedicated field layouts above/below). Providers with no entry here fall
// back to a generic placeholder rather than cosmetically showing another
// provider's key format.
function apiKeyPlaceholder(provider: ProviderName): string {
  if (provider === "openai") return "sk-...";
  if (provider === "anthropic") return "sk-ant-...";
  if (provider === "openrouter") return "Enter your OpenRouter API key";
  return "Enter your API key";
}

export function ProviderKeyForm({
  provider,
  onSaved,
  onCancel,
  allowSkip,
  mode = "save",
  editingLabel,
  hasExistingKeys,
}: {
  provider: ProviderName;
  onSaved: () => void;
  onCancel?: () => void;
  allowSkip?: () => void;
  /** "rotate" (Phase 3, BD-15, AC7.7): validates the new key live, then a
   * fixed-short-overlap swap via POST .../rotate instead of the immediate
   * PUT .../key replace `"save"` uses - same three structured error states
   * either way. Rotation targets the provider's guided-rotation flow (not a
   * specific key id), so no label field is ever shown in this mode. */
  mode?: "save" | "rotate";
  /** Phase 4 (AC4.1.1/AC4.1.2): editing an EXISTING labeled key - the label
   * is fixed (shown read-only) so saving overwrites that exact `ProviderKey`
   * row (backend upserts by `(org_id, provider, label)`) instead of
   * accidentally creating a new one via a typo'd label. */
  editingLabel?: string;
  /** True when this provider already has at least one key configured. When
   * adding a brand-new key (`editingLabel` unset) and this is true, shows an
   * editable, required label input so the new key gets its own distinct
   * row instead of overwriting the existing `"Default"` one. When false
   * (this is the provider's first key), the label input stays hidden and
   * the backend's own `"Default"` default applies - keeps the common
   * single-key-per-provider case exactly as simple as before. */
  hasExistingKeys?: boolean;
}) {
  const label = PROVIDER_LABELS[provider];
  const [apiKey, setApiKey] = useState("");
  const [serviceAccountJson, setServiceAccountJson] = useState("");
  const [projectId, setProjectId] = useState("");
  const [location, setLocation] = useState("us-central1");
  const [baseUrl, setBaseUrl] = useState("");
  const [bearerToken, setBearerToken] = useState("");
  const [keyLabel, setKeyLabel] = useState("");
  const [validating, setValidating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Only "save" mode ever sends a label - "rotate" targets the provider's
  // existing rotation flow, which has no label concept (see mode's doc
  // comment above).
  const showLabelField = mode === "save" && !editingLabel && hasExistingKeys;

  async function handleSave() {
    setError(null);
    if (showLabelField && !keyLabel.trim()) {
      setError("Label is required when adding another key to a provider that already has one.");
      return;
    }
    setValidating(true);
    let parsedServiceAccountJson: Record<string, unknown> | null = null;
    if (provider === "vertex_ai") {
      try {
        parsedServiceAccountJson = JSON.parse(serviceAccountJson);
      } catch {
        setError("Service account JSON is not valid JSON.");
        setValidating(false);
        return;
      }
    }
    // undefined = let the backend apply its own "Default" default (first key
    // for this provider, or editing that same default key unlabeled).
    const labelToSend: string | undefined = editingLabel ?? (showLabelField ? keyLabel.trim() : undefined);
    try {
      if (mode === "rotate") {
        const payload =
          provider === "vertex_ai"
            ? { service_account_json: parsedServiceAccountJson, project_id: projectId, location }
            : provider === "ollama"
              ? { base_url: baseUrl, bearer_token: bearerToken }
              : { api_key: apiKey };
        await rotateProviderKeyGuided(provider, payload);
      } else if (provider === "vertex_ai") {
        await putProviderKey(provider, {
          service_account_json: parsedServiceAccountJson!,
          project_id: projectId,
          location,
          ...(labelToSend !== undefined ? { label: labelToSend } : {}),
        });
      } else if (provider === "ollama") {
        await putProviderKey(provider, {
          base_url: baseUrl,
          bearer_token: bearerToken,
          ...(labelToSend !== undefined ? { label: labelToSend } : {}),
        });
      } else {
        await putProviderKey(provider, {
          api_key: apiKey,
          ...(labelToSend !== undefined ? { label: labelToSend } : {}),
        });
      }
      onSaved();
    } catch (err) {
      setError(validationErrorMessage(err, label));
    } finally {
      setValidating(false);
    }
  }

  return (
    <div>
      {editingLabel ? (
        <div className="field">
          <label htmlFor="pkf-label-locked">Label</label>
          <input id="pkf-label-locked" type="text" value={editingLabel} disabled />
          <div className="field-hint">
            Saving overwrites this specific key - to add a separate key instead, use "Add key"
            rather than editing this one.
          </div>
        </div>
      ) : showLabelField ? (
        <div className="field">
          <label htmlFor="pkf-label">Label</label>
          <input
            id="pkf-label"
            type="text"
            value={keyLabel}
            onChange={(e) => setKeyLabel(e.target.value)}
            placeholder="e.g. Backup"
          />
          <div className="field-hint">
            {label} already has a key configured. Give this one a distinct label so both can be
            used, e.g. in a backup group.
          </div>
        </div>
      ) : null}
      {provider === "vertex_ai" ? (
        <>
          <div className="field">
            <label htmlFor="pkf-sa-json">Service account JSON</label>
            <textarea
              id="pkf-sa-json"
              rows={5}
              value={serviceAccountJson}
              onChange={(e) => setServiceAccountJson(e.target.value)}
              placeholder="Paste the service account JSON key here"
            />
          </div>
          <div className="field">
            <label htmlFor="pkf-project">Project ID</label>
            <input id="pkf-project" type="text" value={projectId} onChange={(e) => setProjectId(e.target.value)} />
          </div>
          <div className="field">
            <label htmlFor="pkf-location">Location</label>
            <input id="pkf-location" type="text" value={location} onChange={(e) => setLocation(e.target.value)} />
          </div>
        </>
      ) : provider === "ollama" ? (
        <>
          <div className="field">
            <label htmlFor="pkf-base-url">Base URL</label>
            <input
              id="pkf-base-url"
              type="text"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="http://localhost:11434"
            />
          </div>
          <div className="field">
            <label htmlFor="pkf-bearer">Bearer token (optional)</label>
            <input
              id="pkf-bearer"
              type="password"
              value={bearerToken}
              onChange={(e) => setBearerToken(e.target.value)}
            />
            <div className="field-hint">
              Only needed if your Ollama instance sits behind an authenticating reverse proxy.
            </div>
          </div>
        </>
      ) : (
        <div className="field">
          <label htmlFor="pkf-api-key">API key</label>
          <input
            id="pkf-api-key"
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={apiKeyPlaceholder(provider)}
          />
        </div>
      )}
      {mode === "rotate" ? (
        <p className="field-hint">
          Paste the new key below. Once it validates, it takes over and the current key stays
          valid for a short overlap window (never an immediate cutover) before it&apos;s retired.
        </p>
      ) : null}
      {validating ? <p className="field-hint">Validating key with {label}...</p> : null}
      {error ? <div className="field-error">{error}</div> : null}
      <div className="modal-actions" style={{ justifyContent: "space-between" }}>
        <div>
          {allowSkip ? (
            <button className="btn btn-link" onClick={allowSkip} disabled={validating}>
              Skip for now, I&apos;ll add this later
            </button>
          ) : null}
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          {onCancel ? (
            <button className="btn btn-secondary" onClick={onCancel} disabled={validating}>
              Cancel
            </button>
          ) : null}
          <button className="btn btn-primary" onClick={handleSave} disabled={validating}>
            {validating
              ? "Validating..."
              : mode === "rotate"
                ? "Validate & rotate"
                : "Validate & save"}
          </button>
        </div>
      </div>
    </div>
  );
}
