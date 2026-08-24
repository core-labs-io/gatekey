"use client";

/**
 * Model Catalog (Model Catalog + Cross-Provider Fallback Chains technical
 * design doc, section 1.6 "Frontend flow" / section 6 frontend-developer
 * tasks 11-14). First-ever dedicated admin UI for the Custom Model Registry
 * (`services/custom_models.py`, `api/v1/admin/custom_models.py`) plus its
 * two additive slices:
 *
 * - Part A: a live "what models does this provider actually have" lookup
 *   (`GET /v1/admin/custom-models/available/{provider}`), so registering a
 *   custom model no longer requires already knowing the provider's exact
 *   model id string.
 * - Part B: an optional, ordered, max-5-entry fallback chain per custom
 *   model - other models (registry / other verified custom models /
 *   verified self-hosted model ids) Gatekey automatically tries, in order,
 *   if the primary model's own provider call fails.
 *
 * RBAC is enforced in this component, not just trusted to the backend
 * (`useCallerRole`, same convention `app/providers/page.tsx`'s Custom
 * Models card and the Phase 5 Differentiators screens already establish):
 * Org Admin gets full CRUD + verify + fallback-chain editing; Auditor gets
 * the identical read-only list, no mutating control rendered at all; a
 * Team Lead/Member session (role === "other") never sees this page's data
 * or controls at all.
 *
 * Ollama / self-hosted endpoints are NOT registered here - they keep their
 * own "Self-Hosted Models" card on the Providers screen; this page's
 * provider dropdown deliberately only offers the 4 BYOK providers
 * (openai/anthropic/vertex_ai/openrouter).
 */

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ConsoleShell } from "@/components/ConsoleShell";
import { useCallerRole } from "@/components/differentiators";
import { CustomModelForm } from "@/components/custom-model-form";
import { Badge, ConfirmDialog, DataTable, useToast } from "@/components/ui";
import {
  ApiError,
  listCustomModels,
  listRegistryModelNames,
  listSelfHostedProviders,
  removeCustomModel,
  verifyCustomModel,
  PROVIDER_LABELS,
  type CustomModelResponse,
  type ProviderName,
} from "@/lib/api";

// --- Table (task 13) -----------------------------------------------------------

function ModelCatalogTable({
  rows,
  loading,
  canWrite,
  verifyingId,
  onVerify,
  onEdit,
  onRemove,
}: {
  rows: CustomModelResponse[];
  loading: boolean;
  canWrite: boolean;
  verifyingId: string | null;
  onVerify: (row: CustomModelResponse) => void;
  onEdit: (row: CustomModelResponse) => void;
  onRemove: (row: CustomModelResponse) => void;
}) {
  return (
    <DataTable
      loading={loading}
      rows={rows}
      rowKey={(m) => m.id}
      emptyState="No custom models registered yet."
      searchText={(m) => `${m.name} ${m.native_model_id} ${m.provider}`}
      searchPlaceholder="Filter by name, native id, or provider..."
      columns={[
        {
          key: "name",
          header: "Name",
          sortValue: (m) => m.name,
          render: (m) => (
            <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span className={`status-dot ${m.verified ? "status-dot-on" : "status-dot-off"}`} />
              <span className="mono">{m.name}</span>
            </span>
          ),
        },
        {
          key: "provider",
          header: "Provider",
          sortValue: (m) => m.provider,
          render: (m) => PROVIDER_LABELS[m.provider as ProviderName] ?? m.provider,
        },
        {
          key: "native_model_id",
          header: "Native model id",
          sortValue: (m) => m.native_model_id,
          render: (m) => <span className="mono">{m.native_model_id}</span>,
        },
        {
          key: "capability",
          header: "Capability",
          sortValue: (m) => m.capability,
          render: (m) => m.capability,
        },
        {
          key: "pricing",
          header: "Pricing (per M tokens)",
          render: (m) => (
            <span>
              ${m.input_price_per_million_usd} in
              {m.output_price_per_million_usd ? <> / ${m.output_price_per_million_usd} out</> : null}
            </span>
          ),
        },
        {
          key: "verified",
          header: "Verified",
          render: (m) => <Badge tone={m.verified ? "green" : "gray"}>{m.verified ? "Verified" : "Not verified"}</Badge>,
        },
        {
          key: "shadowed",
          header: "Shadowed",
          render: (m) =>
            m.shadowed_by_registry ? (
              <Badge
                tone="red"
                title="A newer Gatekey release now defines this same model name - live requests route to that static entry, not this custom mapping. Rename or remove this row."
              >
                Shadowed by registry
              </Badge>
            ) : (
              <span className="text-muted">—</span>
            ),
        },
        {
          key: "fallback",
          header: "Fallback chain",
          render: (m) =>
            m.fallback_model_names.length > 0 ? (
              <Badge tone="gray" title={m.fallback_model_names.map((n, i) => `${i + 1}. ${n}`).join("\n")}>
                {m.fallback_model_names.length} configured
              </Badge>
            ) : (
              <span className="text-muted">—</span>
            ),
        },
        {
          key: "actions",
          header: "Actions",
          align: "right",
          render: (m) =>
            canWrite ? (
              <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                <button className="btn-link" onClick={() => onVerify(m)} disabled={verifyingId === m.id}>
                  {verifyingId === m.id ? "Testing..." : "Test model"}
                </button>
                <button className="btn-link" onClick={() => onEdit(m)}>
                  Edit
                </button>
                <button className="btn-link" style={{ color: "var(--red)" }} onClick={() => onRemove(m)}>
                  Remove
                </button>
              </div>
            ) : (
              <span className="text-muted">—</span>
            ),
        },
      ]}
    />
  );
}

// --- Page ------------------------------------------------------------------

export default function ModelCatalogPage() {
  const role = useCallerRole();
  const toast = useToast();
  const [rows, setRows] = useState<CustomModelResponse[]>([]);
  const [registryNames, setRegistryNames] = useState<string[]>([]);
  const [verifiedSelfHostedModelIds, setVerifiedSelfHostedModelIds] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<null | "new" | CustomModelResponse>(null);
  const [removing, setRemoving] = useState<CustomModelResponse | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);
  const [verifyingId, setVerifyingId] = useState<string | null>(null);

  function refresh() {
    setLoading(true);
    setError(null);
    Promise.all([listCustomModels(), listRegistryModelNames(), listSelfHostedProviders()])
      .then(([customModels, registry, selfHosted]) => {
        setRows(customModels);
        setRegistryNames(registry);
        setVerifiedSelfHostedModelIds(
          Array.from(new Set(selfHosted.filter((p) => p.verified).flatMap((p) => p.models)))
        );
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load the model catalog."))
      .finally(() => setLoading(false));
  }

  // Hooks must run unconditionally - the role-based early return happens
  // below, after every hook has already been declared.
  useEffect(refresh, []);

  const editingId = editing && editing !== "new" ? editing.id : null;
  const fallbackCandidates = useMemo(() => {
    const otherVerifiedCustomModelNames = rows
      .filter((r) => r.verified && r.id !== editingId)
      .map((r) => r.name);
    return Array.from(new Set([...registryNames, ...otherVerifiedCustomModelNames, ...verifiedSelfHostedModelIds]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rows, registryNames, verifiedSelfHostedModelIds, editingId]);

  if (role === "other") {
    return (
      <ConsoleShell>
        <div className="page">
          <div className="page-title">Model Catalog</div>
          <div className="banner banner-error">You do not have access to this page.</div>
        </div>
      </ConsoleShell>
    );
  }

  const canWrite = role === "org_admin";

  async function handleRemove(row: CustomModelResponse) {
    setRemoveBusy(true);
    try {
      await removeCustomModel(row.id);
      toast.push("success", `"${row.name}" removed.`);
      setRemoving(null);
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to remove custom model.");
    } finally {
      setRemoveBusy(false);
    }
  }

  async function handleVerify(row: CustomModelResponse) {
    setVerifyingId(row.id);
    try {
      const result = await verifyCustomModel(row.id);
      toast.push(
        result.verified ? "success" : "error",
        result.verified
          ? `"${row.name}" verified.`
          : `"${row.name}" could not be verified - check the native model id.`
      );
      refresh();
    } catch (err) {
      // Never swallowed: a real provider failure (typo'd native id, no
      // provider_keys row configured yet) surfaces verbatim. The 30s
      // per-row cooldown (429) gets a friendlier, computed "try again in
      // Ns" message when the Retry-After header is present.
      if (err instanceof ApiError && err.code === "custom_model_verify_cooldown") {
        const wait = err.retryAfterSeconds !== undefined ? Math.ceil(err.retryAfterSeconds) : null;
        toast.push(
          "error",
          wait !== null
            ? `Verification for "${row.name}" was attempted too recently - try again in ${wait}s.`
            : err.message
        );
      } else {
        toast.push("error", err instanceof ApiError ? err.message : "Verification failed.");
      }
    } finally {
      setVerifyingId(null);
    }
  }

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-header-row">
          <div>
            <div className="page-title">Model Catalog</div>
            <div className="page-subtitle">
              Register custom BYOK models (OpenAI, Anthropic, Vertex AI, OpenRouter) with admin-set
              pricing so Gatekey can route to and correctly bill for them, and configure automatic
              cross-provider fallback chains for when a model&apos;s own provider call fails. Ollama /
              self-hosted endpoints are registered on the <Link href="/providers">Providers</Link>{" "}
              screen instead.
            </div>
          </div>
          {canWrite ? (
            <button className="btn btn-primary" onClick={() => setEditing("new")}>
              + Register model
            </button>
          ) : null}
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}

        <div className="panel">
          <ModelCatalogTable
            rows={rows}
            loading={loading}
            canWrite={canWrite}
            verifyingId={verifyingId}
            onVerify={handleVerify}
            onEdit={setEditing}
            onRemove={setRemoving}
          />
        </div>

        {canWrite && editing ? (
          <CustomModelForm
            initial={editing === "new" ? null : editing}
            fallbackCandidates={fallbackCandidates}
            onClose={() => setEditing(null)}
            onSaved={() => {
              toast.push("success", editing === "new" ? "Custom model registered." : "Custom model updated.");
              setEditing(null);
              refresh();
            }}
          />
        ) : null}

        {canWrite && removing ? (
          <ConfirmDialog
            title={`Remove "${removing.name}"?`}
            consequence="Any request referencing this model name starts failing (404) immediately. Any other custom model whose fallback chain names this one will simply skip it at request time. Historical usage records referencing it are unaffected."
            confirmLabel="Remove"
            busy={removeBusy}
            onCancel={() => setRemoving(null)}
            onConfirm={() => handleRemove(removing)}
          />
        ) : null}
      </div>
    </ConsoleShell>
  );
}
