"use client";

/**
 * Extracted from `app/model-catalog/page.tsx` (Model Catalog + Cross-Provider
 * Fallback Chains technical design doc, section 1.6 "Frontend flow" / section
 * 6 frontend-developer tasks 11-14) so it can be reused by a second caller -
 * the "select models to enable after configuring a provider key" guided flow
 * (`src/components/model-enable-picker.tsx`) - without duplicating the
 * live-catalog-lookup + pricing + fallback-chain registration form.
 *
 * `/model-catalog`'s own usage (`initial`, `fallbackCandidates`, `onClose`,
 * `onSaved`) is byte-for-byte unchanged by this extraction - `onSaved` now
 * receives the saved `CustomModelResponse` as an argument, but every existing
 * caller that ignores it (as `/model-catalog` does) behaves identically.
 *
 * The new optional `prefill` prop is ONLY consulted when `initial` is `null`
 * (a brand-new registration) - it lets a caller open this form pre-populated
 * for a specific live-catalog entry (provider/native_model_id/default name,
 * and its known live price if any) without pretending it's an edit of an
 * existing custom model. `initial` still wins whenever both are supplied
 * (not expected to happen in practice, but keeps the precedence obvious).
 */

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Modal, FieldError } from "@/components/ui";
import {
  ApiError,
  editCustomModel,
  listAvailableModels,
  registerCustomModel,
  PROVIDER_LABELS,
  type AvailableModelEntry,
  type CustomModelCapability,
  type CustomModelProvider,
  type CustomModelResponse,
} from "@/lib/api";

const CUSTOM_MODEL_PROVIDERS: CustomModelProvider[] = ["openai", "anthropic", "vertex_ai", "openrouter"];
const MAX_FALLBACK_CHAIN_LENGTH = 5;

// --- Fallback chain picker (Part B, task 14) ----------------------------------

function FallbackChainPicker({
  value,
  onChange,
  candidates,
}: {
  value: string[];
  onChange: (next: string[]) => void;
  /** Full candidate universe - already excludes this row's own name and
   * anything currently already picked gets filtered out of the "add"
   * dropdown below (but stays visible in the ordered list, obviously). */
  candidates: string[];
}) {
  const [toAdd, setToAdd] = useState("");
  const available = candidates.filter((c) => !value.includes(c)).sort((a, b) => a.localeCompare(b));

  function move(index: number, dir: -1 | 1) {
    const target = index + dir;
    if (target < 0 || target >= value.length) return;
    const next = [...value];
    [next[index], next[target]] = [next[target], next[index]];
    onChange(next);
  }

  function remove(index: number) {
    onChange(value.filter((_, i) => i !== index));
  }

  function add() {
    if (!toAdd || value.length >= MAX_FALLBACK_CHAIN_LENGTH) return;
    onChange([...value, toAdd]);
    setToAdd("");
  }

  return (
    <div className="field">
      <label>Fallback chain (optional, up to {MAX_FALLBACK_CHAIN_LENGTH})</label>
      <div className="field-hint">
        If this model&apos;s own provider call fails, Gatekey automatically tries these, in order,
        until one succeeds - fully automatic, no client-visible syntax change. Each candidate is
        fully re-vetted (policy/residency/budget) at request time; only currently{" "}
        <strong>verified</strong> custom models and self-hosted endpoints are offered below, since an
        unverified one would be rejected by the server anyway.
      </div>
      {value.length > 0 ? (
        <ol style={{ margin: "8px 0", paddingLeft: 20, display: "flex", flexDirection: "column", gap: 6 }}>
          {value.map((name, i) => (
            <li key={name} style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <span className="mono" style={{ flex: 1 }}>
                {name}
              </span>
              <button
                type="button"
                className="btn-link"
                onClick={() => move(i, -1)}
                disabled={i === 0}
                aria-label={`Move ${name} earlier in the chain`}
              >
                &uarr;
              </button>
              <button
                type="button"
                className="btn-link"
                onClick={() => move(i, 1)}
                disabled={i === value.length - 1}
                aria-label={`Move ${name} later in the chain`}
              >
                &darr;
              </button>
              <button
                type="button"
                className="btn-link"
                style={{ color: "var(--red)" }}
                onClick={() => remove(i)}
              >
                Remove
              </button>
            </li>
          ))}
        </ol>
      ) : (
        <div className="text-muted" style={{ margin: "8px 0" }}>
          No fallback chain configured.
        </div>
      )}
      {value.length < MAX_FALLBACK_CHAIN_LENGTH ? (
        <div style={{ display: "flex", gap: 8 }}>
          <select
            value={toAdd}
            onChange={(e) => setToAdd(e.target.value)}
            style={{ flex: 1 }}
            disabled={available.length === 0}
          >
            <option value="">
              {available.length === 0 ? "No more candidates available" : "Select a model to add..."}
            </option>
            {available.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
          <button type="button" className="btn btn-secondary" onClick={add} disabled={!toAdd}>
            Add
          </button>
        </div>
      ) : (
        <div className="field-hint">Maximum of {MAX_FALLBACK_CHAIN_LENGTH} fallback entries reached.</div>
      )}
    </div>
  );
}

// --- Add / edit form (Part A, tasks 12/13) ------------------------------------

type AvailabilityState =
  | { kind: "loading" }
  | { kind: "ready"; entries: AvailableModelEntry[] }
  | { kind: "not_configured" }
  | { kind: "manual_vertex" }
  | { kind: "error"; message: string };

export interface CustomModelFormPrefill {
  provider: CustomModelProvider;
  native_model_id: string;
  /** Defaults to `native_model_id` itself when omitted - still editable. */
  name?: string;
  input_price_per_million_usd?: string | null;
  output_price_per_million_usd?: string | null;
}

export function CustomModelForm({
  initial,
  prefill,
  fallbackCandidates,
  onClose,
  onSaved,
}: {
  initial: CustomModelResponse | null;
  /** Only consulted when `initial` is null - see module doc comment. */
  prefill?: CustomModelFormPrefill;
  /** Union of (a) registry model names, (b) other VERIFIED custom models in
   * this org, (c) VERIFIED self-hosted providers' model ids - already
   * excludes this row's own name. See module docstring / design doc
   * section 6 task 14. */
  fallbackCandidates: string[];
  onClose: () => void;
  onSaved: (saved: CustomModelResponse) => void;
}) {
  const [name, setName] = useState(initial?.name ?? prefill?.name ?? prefill?.native_model_id ?? "");
  const [provider, setProvider] = useState<CustomModelProvider>(
    (initial?.provider as CustomModelProvider) ?? prefill?.provider ?? "openai"
  );
  const [nativeModelId, setNativeModelId] = useState(initial?.native_model_id ?? prefill?.native_model_id ?? "");
  const [capability, setCapability] = useState<CustomModelCapability>(
    (initial?.capability as CustomModelCapability) ?? "chat"
  );
  const [inputPrice, setInputPrice] = useState(
    initial?.input_price_per_million_usd ?? prefill?.input_price_per_million_usd ?? ""
  );
  const [outputPrice, setOutputPrice] = useState(
    initial?.output_price_per_million_usd ?? prefill?.output_price_per_million_usd ?? ""
  );
  const [pricingSource, setPricingSource] = useState(initial?.pricing_source ?? "");
  const [fallbackChain, setFallbackChain] = useState<string[]>(initial?.fallback_model_names ?? []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Part A (1.6): on provider selection (and on initial mount), fetch the
  // live per-provider catalog. Three distinct outcomes, all handled
  // separately below - see `listAvailableModels`'s doc comment.
  const [availability, setAvailability] = useState<AvailabilityState>({ kind: "loading" });
  const [selectedFromList, setSelectedFromList] = useState("");
  const [manualEntry, setManualEntry] = useState(false);

  // Distinguishes "this effect run is the initial mount" from "the admin
  // just switched the provider dropdown" - the two need different field
  // handling below (QA-found gaps: pre-selecting the current value vs.
  // clearing a now-stale one).
  const isInitialProviderRun = useRef(true);

  useEffect(() => {
    let cancelled = false;
    const isInitialRun = isInitialProviderRun.current;
    isInitialProviderRun.current = false;

    setAvailability({ kind: "loading" });
    setSelectedFromList("");
    setManualEntry(false);
    // A genuine provider switch (not the initial mount) invalidates
    // whatever native id/pricing was set for the PREVIOUS provider - e.g. a
    // gpt-4o id and its OpenAI pricing make no sense once the provider
    // becomes Anthropic. Left alone, an admin who picks a model, then
    // changes their mind on provider without re-picking, would silently
    // submit a mismatched provider/native_model_id pair.
    if (!isInitialRun) {
      setNativeModelId("");
      setInputPrice("");
      setOutputPrice("");
    }
    listAvailableModels(provider)
      .then((entries) => {
        if (cancelled) return;
        setAvailability({ kind: "ready", entries });
        // Editing an existing row, still on its original provider (or a
        // fresh registration pre-filled for this same provider): reflect
        // what's actually configured/prefilled instead of an unselected
        // "Select a model...". If the live catalog no longer lists it, fall
        // back to manual entry so the existing value stays visible (in the
        // text input) rather than hidden behind a dropdown that can't show it.
        const targetNativeId = initial
          ? initial.provider === provider
            ? initial.native_model_id
            : null
          : prefill && prefill.provider === provider
            ? prefill.native_model_id
            : null;
        if (isInitialRun && targetNativeId) {
          const match = entries.find((entry) => entry.native_model_id === targetNativeId);
          if (match) {
            setSelectedFromList(match.native_model_id);
          } else {
            setManualEntry(true);
          }
        }
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.code === "provider_not_configured") {
          setAvailability({ kind: "not_configured" });
        } else if (err instanceof ApiError && err.code === "custom_model_live_listing_unsupported") {
          // Expected/documented for vertex_ai - not an error state.
          setAvailability({ kind: "manual_vertex" });
        } else {
          setAvailability({
            kind: "error",
            message: err instanceof ApiError ? err.message : "Failed to load the live model catalog.",
          });
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider, initial, prefill]);

  const showDropdown = availability.kind === "ready" && !manualEntry;

  async function handleSave() {
    setError(null);
    if (!name.trim() || !nativeModelId.trim() || !inputPrice.trim()) {
      setError("Name, native model id, and input price are required.");
      return;
    }
    if (capability === "chat" && !outputPrice.trim()) {
      setError("Output price is required for chat-capability models.");
      return;
    }
    setBusy(true);
    try {
      let saved: CustomModelResponse;
      if (initial) {
        saved = await editCustomModel(initial.id, {
          name: name.trim(),
          provider,
          native_model_id: nativeModelId.trim(),
          capability,
          input_price_per_million_usd: inputPrice.trim(),
          // Explicit null (not omitted) clears a previously-required price
          // when editing capability from "chat" to "embeddings" - the
          // backend distinguishes "omitted" from "explicit null".
          output_price_per_million_usd: capability === "chat" ? outputPrice.trim() : null,
          pricing_source: pricingSource.trim() || null,
          // Always sent (even []) - the backend distinguishes "omitted"
          // (unchanged) from "provided" via model_fields_set, and clearing
          // a chain back to [] must be an explicit, sent value.
          fallback_model_names: fallbackChain,
        });
      } else {
        saved = await registerCustomModel({
          name: name.trim(),
          provider,
          native_model_id: nativeModelId.trim(),
          capability,
          input_price_per_million_usd: inputPrice.trim(),
          // Must be omitted entirely (never sent, not even null) for
          // "embeddings" on create - the backend's write-time guard/DB
          // CHECK rejects a mismatch either way.
          ...(capability === "chat" ? { output_price_per_million_usd: outputPrice.trim() } : {}),
          pricing_source: pricingSource.trim() || null,
          fallback_model_names: fallbackChain,
        });
      }
      onSaved(saved);
    } catch (err) {
      // 422 (static-registry collision, self-hosted collision, embeddings/
      // provider mismatch, fallback chain too long/self-reference/
      // duplicate/unresolvable) and 409 (name conflict) all carry a
      // specific backend message - surfaced verbatim, never collapsed into
      // a generic failure.
      setError(err instanceof ApiError ? err.message : "Failed to save custom model.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={initial ? `Edit "${initial.name}"` : "Register custom model"} onClose={onClose} width={560}>
      <div className="field">
        <label>Name</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. gpt-5.5-preview"
        />
        <div className="field-hint">
          The gateway-facing model name your requests will use. Must not collide with an existing
          Gatekey model id or one already claimed by a self-hosted endpoint.
        </div>
      </div>

      <div className="field">
        <label>Provider</label>
        <select value={provider} onChange={(e) => setProvider(e.target.value as CustomModelProvider)}>
          {CUSTOM_MODEL_PROVIDERS.map((p) => (
            <option key={p} value={p}>
              {PROVIDER_LABELS[p]}
            </option>
          ))}
        </select>
        <div className="field-hint">
          Routes through the BYOK key already on file for this provider - no new credential is
          entered here. Ollama models are registered under Self-Hosted Models on the{" "}
          <Link href="/providers">Providers</Link> screen instead.
        </div>
      </div>

      <div className="field">
        <label>Model</label>

        {availability.kind === "loading" ? (
          <div className="skeleton skeleton-text" style={{ marginBottom: 8 }} />
        ) : null}

        {availability.kind === "not_configured" ? (
          <div className="banner banner-info" style={{ marginBottom: 8 }}>
            No API key configured for {PROVIDER_LABELS[provider]} yet - add one under{" "}
            <Link href="/providers">Providers</Link> first. You can still register this model now
            by typing its native model id below, but verification will fail until a key is
            configured.
          </div>
        ) : null}

        {availability.kind === "manual_vertex" ? (
          <div className="field-hint" style={{ marginBottom: 8 }}>
            Vertex AI models are entered manually - live catalog listing isn&apos;t available for
            this provider.
          </div>
        ) : null}

        {availability.kind === "error" ? (
          <div className="banner banner-error" style={{ marginBottom: 8 }}>
            Could not load the live model catalog: {availability.message}. You can still enter the
            native model id manually below.
          </div>
        ) : null}

        {showDropdown ? (
          <>
            <select
              value={selectedFromList}
              onChange={(e) => {
                const entry = (availability as { kind: "ready"; entries: AvailableModelEntry[] }).entries.find(
                  (x) => x.native_model_id === e.target.value
                );
                setSelectedFromList(e.target.value);
                if (entry) {
                  setNativeModelId(entry.native_model_id);
                  setInputPrice(entry.input_price_per_million_usd ?? "");
                  if (capability === "chat") setOutputPrice(entry.output_price_per_million_usd ?? "");
                }
              }}
            >
              <option value="">Select a model...</option>
              {(availability as { kind: "ready"; entries: AvailableModelEntry[] }).entries.map((entry) => (
                <option key={entry.native_model_id} value={entry.native_model_id}>
                  {entry.native_model_id} - {entry.display_name}
                </option>
              ))}
            </select>
            <div className="field-hint">
              Pricing prefills from the live catalog when known - still editable below.{" "}
              <button type="button" className="btn-link" onClick={() => setManualEntry(true)}>
                Can&apos;t find it? Enter the native model id manually.
              </button>
            </div>
          </>
        ) : (
          <>
            <input
              type="text"
              value={nativeModelId}
              onChange={(e) => setNativeModelId(e.target.value)}
              placeholder="the literal id sent to the provider's own API"
            />
            {availability.kind === "ready" && manualEntry ? (
              <div className="field-hint">
                <button type="button" className="btn-link" onClick={() => setManualEntry(false)}>
                  Pick from the live catalog instead
                </button>
              </div>
            ) : null}
          </>
        )}
      </div>

      <div className="field">
        <label>Capability</label>
        <select
          value={capability}
          onChange={(e) => {
            const next = e.target.value as CustomModelCapability;
            setCapability(next);
            if (next === "embeddings") setOutputPrice("");
          }}
        >
          <option value="chat">Chat</option>
          <option value="embeddings">Embeddings</option>
        </select>
        {capability === "embeddings" ? (
          <div className="field-hint">
            Embeddings capability is only supported for OpenAI and Vertex AI - the server rejects
            any other provider/capability combination.
          </div>
        ) : null}
      </div>

      <div className="field">
        <label>Input price (USD per million tokens)</label>
        <input
          type="text"
          value={inputPrice}
          onChange={(e) => setInputPrice(e.target.value)}
          placeholder="e.g. 2.50"
        />
      </div>
      {capability === "chat" ? (
        <div className="field">
          <label>Output price (USD per million tokens)</label>
          <input
            type="text"
            value={outputPrice}
            onChange={(e) => setOutputPrice(e.target.value)}
            placeholder="e.g. 10.00"
          />
          <div className="field-hint">Required for chat-capability models.</div>
        </div>
      ) : null}
      <div className="field">
        <label>Pricing source (optional)</label>
        <input
          type="text"
          value={pricingSource}
          onChange={(e) => setPricingSource(e.target.value)}
          placeholder="e.g. a link to the provider's pricing page"
        />
      </div>

      <FallbackChainPicker
        value={fallbackChain}
        onChange={setFallbackChain}
        candidates={fallbackCandidates.filter((c) => c !== name.trim())}
      />

      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button className="btn btn-primary" onClick={handleSave} disabled={busy}>
          {busy ? "Saving..." : "Save"}
        </button>
      </div>
    </Modal>
  );
}
