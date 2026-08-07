"use client";

/**
 * Providers screen (UI spec section 7.4). Real endpoints - no mock data.
 *
 * Phase 4 (AC4.1.1/AC4.1.2/AC4.1.6/AC4.1.7): per-key health status, backup-
 * group membership, and a per-key "Check now" trigger are now wired via
 * `listProviderKeys()` (`GET /v1/admin/provider-keys`) - the individual
 * `ProviderKey`-row view that was missing on the last pass (see prior
 * handoff report). Each provider can now hold more than one labeled key -
 * adding a second (or third...) key uses `ProviderKeyForm`'s label input,
 * and an individual key can be deleted on its own via
 * `deleteProviderKeyById` without touching any other key for that provider.
 * `deleteProviderKey` (deletes EVERY key for a provider) is still used, but
 * only where that's actually the intent: the "Remove key" action on a
 * provider that currently has exactly one key configured.
 *
 * Phase 4 (security-review fix round, Fix 1): a per-key "Failover" action
 * opens `FailoverConfigModal`, the only admin surface that actually turns on
 * reactive failover for a key (`PUT .../failover-config`). Because
 * `listProviderKeys()` has no `failover_enabled`/`failover_target_id`
 * fields (no GET returns them - see `updateProviderKeyFailoverConfig`'s
 * doc comment in `lib/api.ts`), this screen only ever knows a key's
 * failover config once it's been read back from that PUT's response - a
 * `failoverConfigs` map below caches that, session-scoped, and also powers
 * the reverse "Backup for: ..." annotation on whichever key is the
 * configured target.
 *
 * Phase 5 (5.5 Unified Governance, AC5.5.9): a "Self-Hosted Models" card
 * (ui doc section 6) registers/edits/removes vLLM/Ollama-style self-hosted
 * endpoints under the same governed pipeline as any BYOK provider - a
 * separate `self_hosted_providers` table, not an overload of the fixed
 * `provider` cards above. `bearer_token` is write-only - never echoed back
 * by any GET, so the edit form always starts blank and only overwrites the
 * stored credential if a new value is actually typed.
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { Badge, ConfirmDialog, DataTable, FieldError, Modal, useToast, type BadgeTone } from "@/components/ui";
import Link from "next/link";
import { ProviderKeyForm } from "@/components/ProviderKeyForm";
import { RotationPolicyForm } from "@/components/rotation";
import {
  ApiError,
  checkProviderKeyHealth,
  deleteProviderKey,
  deleteProviderKeyById,
  editSelfHostedProvider,
  getProviderKeyRotationPolicy,
  listProviderKeys,
  listProviders,
  listSelfHostedProviders,
  putProviderKeyRotationPolicy,
  registerSelfHostedProvider,
  removeSelfHostedProvider,
  reverifySelfHostedProvider,
  updateProviderKeyFailoverConfig,
  PROVIDER_LABELS,
  type ProviderKeyFailoverConfigResponse,
  type ProviderKeyListItem,
  type ProviderKeyResponse,
  type ProviderName,
  type RotationPolicyResponse,
  type SelfHostedProviderResponse,
} from "@/lib/api";

const PROVIDERS: ProviderName[] = ["openai", "anthropic", "vertex_ai", "ollama", "openrouter"];

function healthTone(status: string): BadgeTone {
  switch (status) {
    case "healthy":
      return "green";
    case "degraded":
      return "amber";
    case "down":
    case "unavailable":
      return "red";
    default:
      return "gray";
  }
}

function formatAvailability(availability24h: number | null): string {
  if (availability24h === null) return "—";
  return `${(availability24h * 100).toFixed(1)}%`;
}

// --- Rotation (Phase 3, BD-15, AC7.7): reminder policy + guided rotate --------

function ProviderRotationModal({ provider, onClose }: { provider: ProviderName; onClose: () => void }) {
  const [policy, setPolicy] = useState<RotationPolicyResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rotating, setRotating] = useState(false);

  useEffect(() => {
    getProviderKeyRotationPolicy(provider)
      .then(setPolicy)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load rotation policy."));
  }, [provider]);

  if (rotating) {
    return (
      <Modal title={`Rotate ${PROVIDER_LABELS[provider]} key`} onClose={() => setRotating(false)}>
        <ProviderKeyForm
          provider={provider}
          mode="rotate"
          onCancel={() => setRotating(false)}
          onSaved={() => {
            setRotating(false);
            onClose();
          }}
        />
      </Modal>
    );
  }

  return (
    <Modal title={`Rotation - ${PROVIDER_LABELS[provider]}`} onClose={onClose}>
      <p className="text-muted">
        Provider keys always rotate through the guided manual flow below - a live key backs
        potentially many teams/apps at once, so there is no fully-automatic option here.
      </p>
      {error ? <div className="banner banner-error">{error}</div> : null}
      {policy ? (
        <>
          <div className="field-hint" style={{ marginBottom: 8 }}>
            The toggle/interval below only control the reminder email - actually rotating the key
            still requires pasting the new one in below.
          </div>
          <RotationPolicyForm
            policy={policy}
            onSave={(body) => putProviderKeyRotationPolicy(provider, body)}
            onSaved={setPolicy}
          />
        </>
      ) : !error ? (
        <div className="skeleton skeleton-text" />
      ) : null}
      <div className="modal-actions">
        <button className="btn btn-primary" onClick={() => setRotating(true)}>
          Rotate key now (guided)
        </button>
      </div>
    </Modal>
  );
}

// --- Failover config (Phase 4, security-review fix round, Fix 1) -------------

function FailoverConfigModal({
  providerKey,
  siblingKeys,
  knownConfig,
  onClose,
  onSaved,
}: {
  providerKey: ProviderKeyListItem;
  /** Other keys for the SAME provider, this key excluded - the only valid
   * failover targets per AC4.1.9 (backend enforces same-provider; the UI
   * never offers a choice it would reject). */
  siblingKeys: ProviderKeyListItem[];
  /** Only known if this key's config has already been read back from a
   * prior save this session - see module-level doc comment above. */
  knownConfig?: ProviderKeyFailoverConfigResponse;
  onClose: () => void;
  onSaved: (config: ProviderKeyFailoverConfigResponse) => void;
}) {
  const [enabled, setEnabled] = useState(knownConfig?.failover_enabled ?? false);
  const [targetId, setTargetId] = useState(knownConfig?.failover_target_id ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      const result = await updateProviderKeyFailoverConfig(providerKey.id, {
        failover_enabled: enabled,
        failover_target_id: targetId || null,
      });
      onSaved(result);
    } catch (err) {
      // 422 failover_target_invalid (own id, or a different provider's key)
      // and 404 not_found both carry a specific backend message already -
      // rendered verbatim, never collapsed into a generic failure.
      setError(err instanceof ApiError ? err.message : "Failed to save failover configuration.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={`Failover - "${providerKey.label}"`} onClose={onClose}>
      <p className="text-muted">
        When enabled, a request that fails on this key automatically retries against the target
        key below instead of failing outright - only other {PROVIDER_LABELS[providerKey.provider]}{" "}
        keys are offered as a target, since a backup key must support the same model(s) as this
        one (AC4.1.9), which the backend enforces at the provider level.
      </p>
      {siblingKeys.length === 0 ? (
        <div className="field-hint">
          {PROVIDER_LABELS[providerKey.provider]} has only this one key configured - add a second
          key for this provider to configure failover.
        </div>
      ) : (
        <>
          <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="checkbox"
              id="failover-enabled"
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
              style={{ width: "auto" }}
            />
            <label htmlFor="failover-enabled" style={{ margin: 0 }}>
              Enable failover for this key
            </label>
          </div>
          <div className="field">
            <label>Failover target</label>
            <select value={targetId} onChange={(e) => setTargetId(e.target.value)} disabled={!enabled}>
              <option value="">None</option>
              {siblingKeys.map((k) => (
                <option key={k.id} value={k.id}>
                  {k.label}
                </option>
              ))}
            </select>
          </div>
        </>
      )}
      {error ? <div className="field-error">{error}</div> : null}
      <div className="modal-actions">
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
          {siblingKeys.length === 0 ? "Close" : "Cancel"}
        </button>
        {siblingKeys.length > 0 ? (
          <button className="btn btn-primary" onClick={handleSave} disabled={busy}>
            {busy ? "Saving..." : "Save failover config"}
          </button>
        ) : null}
      </div>
    </Modal>
  );
}

// --- Per-key list (Phase 4, AC4.1.6/AC4.1.7) ----------------------------------

function ProviderKeysTable({
  provider,
  keys,
  loading,
  checkingKeyId,
  failoverConfigs,
  backupForByTarget,
  onCheckNow,
  onEdit,
  onDelete,
  onConfigureFailover,
}: {
  provider: ProviderName;
  keys: ProviderKeyListItem[];
  loading: boolean;
  checkingKeyId: string | null;
  /** Session-scoped cache of whatever this key's failover config has been
   * read back as (see module-level doc comment - no GET exposes this). */
  failoverConfigs: Map<string, ProviderKeyFailoverConfigResponse>;
  /** Reverse of `failoverConfigs`: keyed by TARGET key id, value = the
   * key(s) that currently name it as their failover target (also only as
   * complete as `failoverConfigs` itself is). */
  backupForByTarget: Map<string, ProviderKeyListItem[]>;
  onCheckNow: (key: ProviderKeyListItem) => void;
  onEdit: (key: ProviderKeyListItem) => void;
  onDelete: (key: ProviderKeyListItem) => void;
  onConfigureFailover: (key: ProviderKeyListItem) => void;
}) {
  return (
    <DataTable
      loading={loading}
      rows={keys}
      rowKey={(k) => k.id}
      emptyState="No keys configured yet."
      columns={[
        {
          key: "label",
          header: "Label",
          render: (k) => (
            <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span className={`status-dot ${k.health_status === "healthy" ? "status-dot-on" : "status-dot-off"}`} />
              <span className="mono">{k.label}</span>
              {k.is_primary ? <Badge tone="green">Primary</Badge> : null}
              {k.backup_group_id ? <Badge tone="gray">In backup group</Badge> : null}
            </span>
          ),
        },
        {
          key: "health",
          header: "Health",
          render: (k) => <Badge tone={healthTone(k.health_status)}>{k.health_status}</Badge>,
        },
        {
          key: "availability",
          header: "24h availability",
          align: "right",
          render: (k) => formatAvailability(k.availability_24h),
        },
        {
          key: "last_check",
          header: "Last checked",
          render: (k) => (k.last_health_check ? new Date(k.last_health_check).toLocaleString() : "Never"),
        },
        {
          key: "last_error",
          header: "Last error",
          render: (k) =>
            k.last_error ? <span className="text-muted">{k.last_error}</span> : <span className="text-muted">—</span>,
        },
        {
          key: "failover",
          header: "Failover",
          render: (k) => {
            const config = failoverConfigs.get(k.id);
            const backupFor = backupForByTarget.get(k.id) ?? [];
            const targetLabel = config?.failover_target_id
              ? keys.find((other) => other.id === config.failover_target_id)?.label ?? "unknown key"
              : null;
            return (
              <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                {!config ? (
                  <span className="text-muted">Not configured this session</span>
                ) : config.failover_enabled && targetLabel ? (
                  <Badge tone="green">Failover &rarr; {targetLabel}</Badge>
                ) : config.failover_enabled ? (
                  <Badge tone="amber">Enabled, no target</Badge>
                ) : (
                  <Badge tone="gray">Failover off</Badge>
                )}
                {backupFor.length > 0 ? (
                  <span className="text-muted" style={{ fontSize: 12 }}>
                    Backup for: {backupFor.map((b) => b.label).join(", ")}
                  </span>
                ) : null}
              </div>
            );
          },
        },
        {
          key: "actions",
          header: "Actions",
          align: "right",
          render: (k) => (
            <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
              <button className="btn-link" onClick={() => onCheckNow(k)} disabled={checkingKeyId === k.id}>
                {checkingKeyId === k.id ? "Checking..." : "Check now"}
              </button>
              <button className="btn-link" onClick={() => onEdit(k)}>
                Edit
              </button>
              <button className="btn-link" onClick={() => onConfigureFailover(k)}>
                Failover
              </button>
              {keys.length > 1 ? (
                <button className="btn-link" style={{ color: "var(--red)" }} onClick={() => onDelete(k)}>
                  Delete
                </button>
              ) : null}
            </div>
          ),
        },
      ]}
    />
  );
}

// --- Self-Hosted Models (Phase 5, 5.5, AC5.5.1/AC5.5.9) -----------------------

function SelfHostedProviderForm({
  initial,
  onClose,
  onSaved,
}: {
  initial: SelfHostedProviderResponse | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(initial?.name ?? "");
  const [baseUrl, setBaseUrl] = useState(initial?.base_url ?? "");
  const [bearerToken, setBearerToken] = useState("");
  const [costBasis, setCostBasis] = useState(initial?.cost_basis_per_gpu_hour ?? "");
  const [modelsText, setModelsText] = useState(initial?.models.join("\n") ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setError(null);
    const models = modelsText
      .split(/[\n,]/)
      .map((m) => m.trim())
      .filter(Boolean);
    if (!name.trim() || !baseUrl.trim() || !costBasis.trim() || models.length === 0) {
      setError("Name, base URL, cost basis, and at least one model id are required.");
      return;
    }
    setBusy(true);
    try {
      if (initial) {
        await editSelfHostedProvider(initial.id, {
          name: name.trim(),
          base_url: baseUrl.trim(),
          ...(bearerToken ? { bearer_token: bearerToken } : {}),
          cost_basis_per_gpu_hour: costBasis.trim(),
          models,
        });
      } else {
        await registerSelfHostedProvider({
          name: name.trim(),
          base_url: baseUrl.trim(),
          bearer_token: bearerToken || null,
          cost_basis_per_gpu_hour: costBasis.trim(),
          models,
        });
      }
      onSaved();
    } catch (err) {
      // 409 self_hosted_provider_name_conflict, 422
      // self_hosted_model_registry_collision / self_hosted_model_already_claimed
      // all pass through verbatim.
      setError(err instanceof ApiError ? err.message : "Failed to save self-hosted endpoint.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={initial ? `Edit "${initial.name}"` : "Register self-hosted endpoint"} onClose={onClose}>
      <div className="field">
        <label>Name</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. vllm-internal-llama3"
        />
      </div>
      <div className="field">
        <label>Base URL</label>
        <input
          type="text"
          value={baseUrl}
          onChange={(e) => setBaseUrl(e.target.value)}
          placeholder="http://vllm.internal:8000"
        />
        <div className="field-hint">
          Must expose an OpenAI-compatible <span className="mono">/v1/chat/completions</span> +{" "}
          <span className="mono">/v1/models</span> surface (vLLM and Ollama both do). Chat only in
          this phase - not routable for completions/embeddings.
        </div>
      </div>
      <div className="field">
        <label>Bearer token {initial ? "(leave blank to keep the current one)" : "(optional)"}</label>
        <input type="password" value={bearerToken} onChange={(e) => setBearerToken(e.target.value)} />
        <div className="field-hint">
          Only needed if this endpoint sits behind an authenticating reverse proxy. Never shown
          again after saving.
        </div>
      </div>
      <div className="field">
        <label>Cost basis (USD per GPU-hour)</label>
        <input
          type="text"
          value={costBasis}
          onChange={(e) => setCostBasis(e.target.value)}
          placeholder="e.g. 2.10"
        />
        <div className="field-hint">
          Used to estimate spend for requests to this endpoint (rate x wall-clock latency) - an
          interim proxy, always shown as an estimate, never invoice-grade like a BYOK provider&apos;s
          token pricing.
        </div>
      </div>
      <div className="field">
        <label>Model ids served by this endpoint</label>
        <textarea
          rows={3}
          value={modelsText}
          onChange={(e) => setModelsText(e.target.value)}
          placeholder={"One per line, e.g.\nllama3.1-70b-instruct"}
        />
        <div className="field-hint">
          Must not collide with an existing Gatekey model id or one already claimed by another
          self-hosted endpoint.
        </div>
      </div>
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

function SelfHostedModelsCard() {
  const toast = useToast();
  const [rows, setRows] = useState<SelfHostedProviderResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<null | "new" | SelfHostedProviderResponse>(null);
  const [removing, setRemoving] = useState<SelfHostedProviderResponse | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);
  const [verifyingId, setVerifyingId] = useState<string | null>(null);

  function refresh() {
    setLoading(true);
    listSelfHostedProviders()
      .then(setRows)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load self-hosted endpoints."))
      .finally(() => setLoading(false));
  }

  useEffect(refresh, []);

  async function handleRemove(row: SelfHostedProviderResponse) {
    setRemoveBusy(true);
    try {
      await removeSelfHostedProvider(row.id);
      toast.push("success", `"${row.name}" removed.`);
      setRemoving(null);
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to remove endpoint.");
    } finally {
      setRemoveBusy(false);
    }
  }

  async function handleVerify(row: SelfHostedProviderResponse) {
    setVerifyingId(row.id);
    try {
      const result = await reverifySelfHostedProvider(row.id);
      toast.push(
        result.verified ? "success" : "error",
        result.verified ? `"${row.name}" verified.` : `"${row.name}" could not be verified - check the base URL and token.`
      );
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Verification failed.");
    } finally {
      setVerifyingId(null);
    }
  }

  return (
    <div className="provider-card" style={{ flexDirection: "column", alignItems: "stretch" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div>
          <div className="provider-card-name">Self-Hosted Models</div>
          <div className="provider-card-meta">
            vLLM/Ollama-style endpoints, governed under the same policy/budget/DLP/audit pipeline
            as any BYOK provider.
          </div>
        </div>
        <button className="btn" onClick={() => setEditing("new")}>
          + Register model
        </button>
      </div>
      {error ? <div className="banner banner-error" style={{ marginTop: 12 }}>{error}</div> : null}
      {loading ? (
        <div className="skeleton skeleton-text" style={{ marginTop: 12 }} />
      ) : rows.length === 0 ? (
        <div className="text-muted" style={{ marginTop: 12 }}>
          No self-hosted endpoints registered yet.
        </div>
      ) : (
        <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 10 }}>
          {rows.map((row) => (
            <div key={row.id} style={{ border: "1px solid var(--border)", borderRadius: 6, padding: 12 }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
                  <span className={`status-dot ${row.verified ? "status-dot-on" : "status-dot-off"}`} />
                  <span className="mono">{row.name}</span>
                  <Badge tone={row.verified ? "green" : "gray"}>{row.verified ? "Verified" : "Not verified"}</Badge>
                </span>
                <span style={{ display: "flex", gap: 8 }}>
                  <button className="btn-link" onClick={() => handleVerify(row)} disabled={verifyingId === row.id}>
                    {verifyingId === row.id ? "Verifying..." : "Re-verify"}
                  </button>
                  <button className="btn-link" onClick={() => setEditing(row)}>
                    Edit
                  </button>
                  <button className="btn-link" style={{ color: "var(--red)" }} onClick={() => setRemoving(row)}>
                    Remove
                  </button>
                </span>
              </div>
              <div className="text-muted" style={{ fontSize: 12, marginTop: 4 }}>
                {row.base_url} · Cost basis: ${row.cost_basis_per_gpu_hour}/GPU-hour (estimated, not
                an invoice figure) · Models: {row.models.join(", ")}
              </div>
            </div>
          ))}
        </div>
      )}
      {editing ? (
        <SelfHostedProviderForm
          initial={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            toast.push("success", editing === "new" ? "Endpoint registered." : "Endpoint updated.");
            setEditing(null);
            refresh();
          }}
        />
      ) : null}
      {removing ? (
        <ConfirmDialog
          title={`Remove "${removing.name}"?`}
          consequence="Any request routed to one of this endpoint's models will start failing immediately. Historical usage records are unaffected."
          confirmLabel="Remove"
          busy={removeBusy}
          onCancel={() => setRemoving(null)}
          onConfirm={() => handleRemove(removing)}
        />
      ) : null}
    </div>
  );
}

export default function ProvidersPage() {
  const toast = useToast();
  const [rows, setRows] = useState<ProviderKeyResponse[] | null>(null);
  const [keys, setKeys] = useState<ProviderKeyListItem[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [formState, setFormState] = useState<{ provider: ProviderName; editingLabel?: string } | null>(null);
  const [removing, setRemoving] = useState<ProviderName | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);
  const [deletingKey, setDeletingKey] = useState<ProviderKeyListItem | null>(null);
  const [deleteKeyBusy, setDeleteKeyBusy] = useState(false);
  const [rotating, setRotating] = useState<ProviderName | null>(null);
  const [checkingKeyId, setCheckingKeyId] = useState<string | null>(null);
  const [configuringFailover, setConfiguringFailover] = useState<ProviderKeyListItem | null>(null);
  // Session-scoped cache of failover config as read back from
  // `updateProviderKeyFailoverConfig()` - see module-level doc comment for
  // why there's no GET to seed this from instead. Intentionally NOT reset
  // by `refresh()` below, so it survives a key-list reload.
  const [failoverConfigs, setFailoverConfigs] = useState<Map<string, ProviderKeyFailoverConfigResponse>>(new Map());

  function refresh() {
    setLoading(true);
    setError(null);
    Promise.all([listProviders(), listProviderKeys()])
      .then(([providerRows, keyRows]) => {
        setRows(providerRows);
        setKeys(keyRows);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load providers."))
      .finally(() => setLoading(false));
  }

  useEffect(refresh, []);

  const byProvider = new Map((rows ?? []).map((r) => [r.provider, r]));
  const keysByProvider = new Map<ProviderName, ProviderKeyListItem[]>();
  for (const key of keys ?? []) {
    const list = keysByProvider.get(key.provider) ?? [];
    list.push(key);
    keysByProvider.set(key.provider, list);
  }

  // Reverse of `failoverConfigs`: keyed by TARGET key id -> the key(s) that
  // currently name it as their failover target, for the "Backup for: ..."
  // annotation. Only as complete as `failoverConfigs` itself (session-
  // scoped, no GET to seed it from - see that state's doc comment).
  const backupForByTarget = new Map<string, ProviderKeyListItem[]>();
  for (const config of failoverConfigs.values()) {
    if (!config.failover_enabled || !config.failover_target_id) continue;
    const sourceKey = (keys ?? []).find((k) => k.id === config.id);
    if (!sourceKey) continue;
    const list = backupForByTarget.get(config.failover_target_id) ?? [];
    list.push(sourceKey);
    backupForByTarget.set(config.failover_target_id, list);
  }

  async function handleRemoveAll(provider: ProviderName) {
    setRemoveBusy(true);
    try {
      await deleteProviderKey(provider);
      toast.push("success", `${PROVIDER_LABELS[provider]} key removed.`);
      setRemoving(null);
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to remove key.");
    } finally {
      setRemoveBusy(false);
    }
  }

  async function handleDeleteKey(key: ProviderKeyListItem) {
    setDeleteKeyBusy(true);
    try {
      await deleteProviderKeyById(key.provider, key.id);
      toast.push("success", `Key "${key.label}" deleted.`);
      setDeletingKey(null);
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to delete key.");
    } finally {
      setDeleteKeyBusy(false);
    }
  }

  async function handleCheckNow(key: ProviderKeyListItem) {
    setCheckingKeyId(key.id);
    try {
      const result = await checkProviderKeyHealth(key.id);
      if (result.status === "ok") {
        toast.push("success", `"${key.label}" is healthy (${result.latency_ms}ms).`);
      } else {
        toast.push("error", `"${key.label}" health check failed: ${result.error ?? "unknown error"}`);
      }
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Health check failed.");
    } finally {
      setCheckingKeyId(null);
    }
  }

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Providers</div>
        <div className="page-subtitle">
          Bring your own API keys. Gatekey never performs inference itself - it routes to these
          providers under your policy.
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}

        <div className="panel" style={{ marginBottom: 16 }}>
          <div className="panel-title">Reliability (Phase 4)</div>
          <p className="text-muted">
            A provider can hold more than one labeled key - health, availability, and backup-group
            membership for each are shown per key below. Multi-key failover groups and their
            event history are managed on dedicated screens.
          </p>
          <div style={{ display: "flex", gap: 16 }}>
            <Link href="/backup-groups">Backup Groups &rarr;</Link>
            <Link href="/failover-events">Failover Events &rarr;</Link>
          </div>
        </div>

        {PROVIDERS.map((provider) => {
          const row = byProvider.get(provider);
          const configured = row?.configured ?? false;
          const providerKeys = keysByProvider.get(provider) ?? [];
          return (
            <div key={provider} className="provider-card" style={{ flexDirection: "column", alignItems: "stretch" }}>
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <div>
                  <div className="provider-card-name">
                    <span className={`status-dot ${configured ? "status-dot-on" : "status-dot-off"}`} />
                    {PROVIDER_LABELS[provider]}
                  </div>
                  <div className="provider-card-meta">
                    {loading
                      ? "Loading..."
                      : configured
                      ? `Validated ${row?.validated_at ? new Date(row.validated_at).toLocaleString() : "recently"} · ${providerKeys.length} key${providerKeys.length === 1 ? "" : "s"} configured`
                      : "No key on file yet."}
                  </div>
                </div>
                <div style={{ display: "flex", gap: 8 }}>
                  <button className="btn" onClick={() => setFormState({ provider })}>
                    + Add key
                  </button>
                  {configured ? (
                    <button className="btn btn-secondary" onClick={() => setRotating(provider)}>
                      Rotation
                    </button>
                  ) : null}
                  {configured && providerKeys.length <= 1 ? (
                    <button className="btn btn-danger" onClick={() => setRemoving(provider)}>
                      Remove
                    </button>
                  ) : null}
                </div>
              </div>

              {configured && providerKeys.length > 0 ? (
                <div style={{ marginTop: 12 }}>
                  <ProviderKeysTable
                    provider={provider}
                    keys={providerKeys}
                    loading={loading}
                    checkingKeyId={checkingKeyId}
                    failoverConfigs={failoverConfigs}
                    backupForByTarget={backupForByTarget}
                    onCheckNow={handleCheckNow}
                    onEdit={(k) => setFormState({ provider, editingLabel: k.label })}
                    onDelete={setDeletingKey}
                    onConfigureFailover={setConfiguringFailover}
                  />
                </div>
              ) : null}
            </div>
          );
        })}

        <SelfHostedModelsCard />

        {formState ? (
          <Modal
            title={
              formState.editingLabel
                ? `Edit "${formState.editingLabel}" - ${PROVIDER_LABELS[formState.provider]}`
                : `Add ${PROVIDER_LABELS[formState.provider]} key`
            }
            onClose={() => setFormState(null)}
          >
            <ProviderKeyForm
              provider={formState.provider}
              editingLabel={formState.editingLabel}
              hasExistingKeys={(keysByProvider.get(formState.provider)?.length ?? 0) > 0}
              onCancel={() => setFormState(null)}
              onSaved={() => {
                toast.push("success", `${PROVIDER_LABELS[formState.provider]} key saved.`);
                setFormState(null);
                refresh();
              }}
            />
          </Modal>
        ) : null}

        {removing ? (
          <ConfirmDialog
            title={`Remove the ${PROVIDER_LABELS[removing]} key?`}
            consequence={`Any request routed to a ${PROVIDER_LABELS[removing]} model will start failing immediately.`}
            confirmLabel="Remove key"
            busy={removeBusy}
            onCancel={() => setRemoving(null)}
            onConfirm={() => handleRemoveAll(removing)}
          />
        ) : null}

        {deletingKey ? (
          <ConfirmDialog
            title={`Delete key "${deletingKey.label}"?`}
            consequence={`Only this labeled key for ${PROVIDER_LABELS[deletingKey.provider]} is removed - other keys for this provider are untouched.${deletingKey.backup_group_id ? " It will also be removed from its backup group." : ""}`}
            confirmLabel="Delete key"
            busy={deleteKeyBusy}
            onCancel={() => setDeletingKey(null)}
            onConfirm={() => handleDeleteKey(deletingKey)}
          />
        ) : null}

        {rotating ? (
          <ProviderRotationModal
            provider={rotating}
            onClose={() => {
              setRotating(null);
              refresh();
            }}
          />
        ) : null}

        {configuringFailover ? (
          <FailoverConfigModal
            providerKey={configuringFailover}
            siblingKeys={(keysByProvider.get(configuringFailover.provider) ?? []).filter(
              (k) => k.id !== configuringFailover.id
            )}
            knownConfig={failoverConfigs.get(configuringFailover.id)}
            onClose={() => setConfiguringFailover(null)}
            onSaved={(config) => {
              setFailoverConfigs((prev) => {
                const next = new Map(prev);
                next.set(config.id, config);
                return next;
              });
              toast.push("success", `Failover configuration saved for "${config.label}".`);
              setConfiguringFailover(null);
              refresh();
            }}
          />
        ) : null}
      </div>
    </ConsoleShell>
  );
}
