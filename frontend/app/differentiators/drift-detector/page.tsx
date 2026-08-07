"use client";

/**
 * Drift Detector (Phase 5, 5.4, ui doc section 12.2). A daily canary suite
 * (fixed, cheap, code-seeded prompts - read-only here, AC5.4.1) runs
 * against every actively-used model; this screen surfaces per-model
 * status/trend, expandable plain-language alert detail, canary history, and
 * "Export to audit log". RBAC: Org Admin configures per-model enable/
 * disable; Org Admin + Auditor view everything (`useCallerRole` hides the
 * enable/disable toggle and export action for an Auditor session, never
 * just disables them).
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { Badge, DataTable, Modal, useToast, type BadgeTone } from "@/components/ui";
import { useCallerRole } from "@/components/differentiators";
import {
  ApiError,
  exportDriftAlert,
  getDriftStatus,
  listCanaryHistory,
  listCanaryPrompts,
  listDriftAlerts,
  setCanaryModelSetting,
  type CanaryPromptResponse,
  type CanaryRunResponse,
  type DriftAlertResponse,
  type DriftModelStatusResponse,
} from "@/lib/api";

function statusFor(row: DriftModelStatusResponse): { label: string; tone: BadgeTone } {
  if (row.open_alerts_count > 0) return { label: "Drift detected", tone: "red" };
  if (row.last_run_at === null) return { label: "Not yet run", tone: "gray" };
  if (row.baselines_established === 0) return { label: "Establishing baseline", tone: "gray" };
  return { label: "Stable", tone: "green" };
}

function CanaryHistoryModal({ model, onClose }: { model: string; onClose: () => void }) {
  const [rows, setRows] = useState<CanaryRunResponse[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listCanaryHistory({ model, limit: 50 })
      .then(setRows)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load canary history."));
  }, [model]);

  return (
    <Modal title={`Canary history - ${model}`} onClose={onClose} width={720}>
      {error ? <div className="banner banner-error">{error}</div> : null}
      <DataTable
        loading={rows === null && !error}
        rows={rows ?? []}
        rowKey={(r) => r.id}
        emptyState="No canary runs recorded for this model yet."
        columns={[
          { key: "run_at", header: "Run at", render: (r) => new Date(r.run_at).toLocaleString() },
          { key: "latency", header: "Latency", align: "right", render: (r) => `${r.latency_ms}ms` },
          {
            key: "refusal",
            header: "Refusal",
            render: (r) => (r.refusal_detected ? <Badge tone="amber">Yes</Badge> : <Badge tone="gray">No</Badge>),
          },
          {
            key: "similarity",
            header: "Similarity vs. baseline",
            align: "right",
            render: (r) => (r.similarity_score_vs_baseline === null ? "—" : Number(r.similarity_score_vs_baseline).toFixed(2)),
          },
          { key: "cost", header: "Canary cost", align: "right", render: (r) => `$${Number(r.cost_usd).toFixed(6)}` },
        ]}
      />
    </Modal>
  );
}

function ModelAlerts({
  model,
  canEdit,
  onExported,
}: {
  model: string;
  canEdit: boolean;
  onExported: () => void;
}) {
  const toast = useToast();
  const [alerts, setAlerts] = useState<DriftAlertResponse[] | null>(null);
  const [historyFor, setHistoryFor] = useState<string | null>(null);
  const [exportingId, setExportingId] = useState<string | null>(null);

  useEffect(() => {
    listDriftAlerts({ model, status: "open" })
      .then(setAlerts)
      .catch(() => setAlerts([]));
  }, [model]);

  async function handleExport(alert: DriftAlertResponse) {
    setExportingId(alert.id);
    try {
      await exportDriftAlert(alert.id);
      toast.push("success", "Alert exported to the audit log.");
      onExported();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to export alert.");
    } finally {
      setExportingId(null);
    }
  }

  return (
    <div style={{ padding: "8px 16px 16px", background: "var(--gray-bg, #f6f6f6)" }}>
      {alerts === null ? (
        <div className="skeleton skeleton-text" />
      ) : alerts.length === 0 ? (
        <span className="text-muted">No open alerts for this model.</span>
      ) : (
        alerts.map((a) => (
          <div key={a.id} style={{ marginBottom: 8 }}>
            <div>&#9656; {a.message}</div>
            <div style={{ display: "flex", gap: 12, marginTop: 4 }}>
              <button className="btn-link" onClick={() => setHistoryFor(model)}>
                View canary history
              </button>
              {canEdit ? (
                <button className="btn-link" onClick={() => handleExport(a)} disabled={exportingId === a.id}>
                  {exportingId === a.id ? "Exporting..." : "Export to audit log"}
                </button>
              ) : null}
            </div>
          </div>
        ))
      )}
      {historyFor ? <CanaryHistoryModal model={historyFor} onClose={() => setHistoryFor(null)} /> : null}
    </div>
  );
}

export default function DriftDetectorPage() {
  const role = useCallerRole();
  const canEdit = role === "org_admin";
  const toast = useToast();
  const [rows, setRows] = useState<DriftModelStatusResponse[]>([]);
  const [prompts, setPrompts] = useState<CanaryPromptResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [togglingModel, setTogglingModel] = useState<string | null>(null);

  function refresh() {
    setLoading(true);
    Promise.all([getDriftStatus(), listCanaryPrompts()])
      .then(([status, promptRows]) => {
        setRows(status);
        setPrompts(promptRows);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load drift detector status."))
      .finally(() => setLoading(false));
  }

  useEffect(refresh, []);

  async function handleToggle(row: DriftModelStatusResponse) {
    setTogglingModel(row.model);
    try {
      await setCanaryModelSetting(row.model, !row.canary_enabled);
      toast.push("success", `Canary suite ${!row.canary_enabled ? "enabled" : "disabled"} for ${row.model}.`);
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to update canary setting.");
    } finally {
      setTogglingModel(null);
    }
  }

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Drift Detector</div>
        <div className="page-subtitle">
          A fixed, cheap canary prompt suite runs daily against every actively-used model, so a
          silent provider-side model change behind a stable API/version name gets caught early.
          Canary cost is tracked separately (below) and never billed to any team/user/org budget.
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}

        <div className="panel" style={{ marginBottom: 16 }}>
          <div className="panel-title">Canary prompt suite (read-only)</div>
          <p className="text-muted">
            Fixed, hand-curated set - not admin-editable in this phase.
          </p>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {prompts.map((p) => (
              <li key={p.id} style={{ fontSize: 13 }}>
                <Badge tone="gray">{p.label}</Badge>{" "}
                <span className="mono">{p.prompt_text}</span>{" "}
                <span className="text-muted">(max {p.max_tokens} tokens)</span>
              </li>
            ))}
          </ul>
        </div>

        <div className="panel">
          <div className="panel-title">Per-model status</div>
          <DataTable
            loading={loading}
            rows={rows}
            rowKey={(r) => r.model}
            emptyState="No canary data yet - the daily scheduler job populates this once models are actively used."
            columns={[
              {
                key: "model",
                header: "Model",
                render: (r) => (
                  <button className="btn-link" onClick={() => setExpanded(expanded === r.model ? null : r.model)}>
                    {expanded === r.model ? "▾" : "▸"} <span className="mono">{r.model}</span>
                  </button>
                ),
              },
              {
                key: "status",
                header: "Status",
                render: (r) => {
                  const s = statusFor(r);
                  return <Badge tone={s.tone}>{s.label}</Badge>;
                },
              },
              {
                key: "last_checked",
                header: "Last checked",
                render: (r) => (r.last_run_at ? new Date(r.last_run_at).toLocaleString() : "Never"),
              },
              {
                key: "baselines",
                header: "Baselines established",
                align: "right",
                render: (r) => r.baselines_established,
              },
              {
                key: "enabled",
                header: "Canary suite",
                render: (r) =>
                  canEdit ? (
                    <label style={{ display: "flex", alignItems: "center", gap: 6 }}>
                      <input
                        type="checkbox"
                        checked={r.canary_enabled}
                        onChange={() => handleToggle(r)}
                        disabled={togglingModel === r.model}
                        style={{ width: "auto" }}
                      />
                      Enabled
                    </label>
                  ) : (
                    <Badge tone={r.canary_enabled ? "green" : "gray"}>{r.canary_enabled ? "Enabled" : "Disabled"}</Badge>
                  ),
              },
            ]}
          />
          {expanded ? <ModelAlerts model={expanded} canEdit={canEdit} onExported={refresh} /> : null}
        </div>
      </div>
    </ConsoleShell>
  );
}
