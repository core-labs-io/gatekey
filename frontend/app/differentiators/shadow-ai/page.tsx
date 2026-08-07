"use client";

/**
 * Shadow AI Discovery (Phase 5, 5.1, ui doc section 12.1). Org-wide screen -
 * Org Admin gets full config/CRUD/token-gen, Auditor gets the identical
 * read-only view (`useCallerRole` hides every write control for an
 * Auditor session, never just disables them). Team Lead's own team-scoped
 * report lives on a separate screen (`app/team/shadow-ai`), matching this
 * codebase's existing Audit Log / Org Logs split convention.
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { Badge, ConfirmDialog, DataTable, FieldError, useToast } from "@/components/ui";
import { SecretRevealModal } from "@/components/personal-keys";
import { ShadowAiPolicyModal, ShadowAiReportTable, useCallerRole } from "@/components/differentiators";
import {
  ApiError,
  addKnownAiToolHostname,
  getShadowAiConfig,
  getShadowAiReport,
  listKnownAiToolHostnames,
  listTeams,
  putShadowAiConfig,
  removeKnownAiToolHostname,
  rotateShadowAiIngestToken,
  type KnownAiToolHostnameResponse,
  type ShadowAiConfigResponse,
  type ShadowAiReportRowResponse,
  type TeamResponse,
} from "@/lib/api";

const ENFORCEMENT_LABELS: Record<ShadowAiConfigResponse["enforcement_mode"], string> = {
  detect_only: "Detect only (recommended)",
  notification: "Notification - email the flagged user + their Team Lead",
  webhook: "Webhook - POST to your own SASE/SOAR automation",
};

function ConfigPanel({ canEdit }: { canEdit: boolean }) {
  const toast = useToast();
  const [config, setConfig] = useState<ShadowAiConfigResponse | null>(null);
  const [detectionSource, setDetectionSource] = useState<"sase_log" | "proxy_log">("sase_log");
  const [enforcementMode, setEnforcementMode] = useState<ShadowAiConfigResponse["enforcement_mode"]>("detect_only");
  const [webhookUrl, setWebhookUrl] = useState("");
  const [retentionDays, setRetentionDays] = useState("90");
  const [pendingIntrusive, setPendingIntrusive] = useState<ShadowAiConfigResponse["enforcement_mode"] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [rotating, setRotating] = useState(false);
  const [reveal, setReveal] = useState<{ token: string } | null>(null);
  const [showPolicy, setShowPolicy] = useState(false);

  function refresh() {
    getShadowAiConfig()
      .then((data) => {
        setConfig(data);
        setDetectionSource(data.detection_source);
        setEnforcementMode(data.enforcement_mode);
        setRetentionDays(String(data.shadow_ai_retention_days));
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load Shadow AI configuration."));
  }

  useEffect(refresh, []);

  async function doSave(confirm: boolean) {
    setBusy(true);
    setError(null);
    try {
      const result = await putShadowAiConfig({
        detection_source: detectionSource,
        enforcement_mode: enforcementMode,
        webhook_url: enforcementMode === "webhook" ? webhookUrl.trim() || null : null,
        shadow_ai_retention_days: Number(retentionDays) || 90,
        confirm,
      });
      setConfig(result);
      toast.push("success", "Shadow AI configuration saved.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save Shadow AI configuration.");
    } finally {
      setBusy(false);
      setPendingIntrusive(null);
    }
  }

  function handleSave() {
    // The backend never echoes a previously-saved webhook_url back (GET
    // only reports `webhook_configured: boolean`, same non-echo discipline
    // as `Team.webhook_url`) and this is a full-replace PUT - so the URL
    // must be re-entered on EVERY save while enforcement is "webhook", not
    // just the first time, even if one was already configured.
    if (enforcementMode === "webhook" && !webhookUrl.trim()) {
      setError(
        "Re-enter the webhook URL to save - it's never echoed back after being set, and this save would otherwise clear it."
      );
      return;
    }
    const isIntrusive = enforcementMode !== "detect_only";
    const isTransition = config?.enforcement_mode !== enforcementMode;
    if (isIntrusive && isTransition) {
      setPendingIntrusive(enforcementMode);
      return;
    }
    doSave(false);
  }

  async function handleRotate() {
    setRotating(true);
    try {
      const result = await rotateShadowAiIngestToken();
      setReveal({ token: result.token });
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to generate ingestion token.");
    } finally {
      setRotating(false);
    }
  }

  if (!config) {
    return error ? <div className="banner banner-error">{error}</div> : <div className="skeleton skeleton-text" style={{ height: 160 }} />;
  }

  return (
    <div className="panel">
      <div className="page-header-row">
        <div className="panel-title">Setup</div>
        <button className="btn-link" onClick={() => setShowPolicy(true)}>
          View policy
        </button>
      </div>
      <div className="banner banner-info">
        This feature collects connection metadata described in the data-handling policy (never
        full URLs, query strings, or request/response bodies). Review it before enabling.{" "}
        <button className="btn-link" onClick={() => setShowPolicy(true)}>
          View policy
        </button>
      </div>

      <div className="field">
        <label>Detection source</label>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="radio"
              name="detection-source"
              checked={detectionSource === "sase_log"}
              onChange={() => setDetectionSource("sase_log")}
              disabled={!canEdit}
              style={{ width: "auto" }}
            />
            SASE log ingestion
          </label>
          <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="radio"
              name="detection-source"
              checked={detectionSource === "proxy_log"}
              onChange={() => setDetectionSource("proxy_log")}
              disabled={!canEdit}
              style={{ width: "auto" }}
            />
            Proxy log ingestion
          </label>
          <label style={{ display: "flex", alignItems: "center", gap: 8, opacity: 0.6 }}>
            <input type="radio" name="detection-source" checked={false} disabled style={{ width: "auto" }} />
            Browser extension <Badge tone="gray">Coming later</Badge>
          </label>
        </div>
      </div>

      <div className="field">
        <label>Ingestion token</label>
        <div>
          {config.ingestion_configured ? (
            <span className="text-muted">
              Configured{config.token_created_at ? ` - issued ${new Date(config.token_created_at).toLocaleString()}` : ""} (never shown again)
            </span>
          ) : (
            <span className="text-muted">
              Not generated yet - the ingestion endpoint rejects all traffic until this exists.
            </span>
          )}
        </div>
        {canEdit ? (
          <button className="btn btn-secondary" style={{ marginTop: 8 }} onClick={handleRotate} disabled={rotating}>
            {rotating ? "Generating..." : config.ingestion_configured ? "Rotate token" : "Generate token"}
          </button>
        ) : null}
        <div className="field-hint" style={{ marginTop: 6 }}>
          Rotating immediately invalidates the previous token - no overlap window. Point your
          SASE/proxy tool&apos;s export transform at{" "}
          <span className="mono">POST /v1/admin/shadow-ai/ingest</span> with this token.
        </div>
      </div>

      <div className="field">
        <label>Enforcement</label>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {(Object.keys(ENFORCEMENT_LABELS) as (keyof typeof ENFORCEMENT_LABELS)[]).map((mode) => (
            <label key={mode} style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <input
                type="radio"
                name="enforcement-mode"
                checked={enforcementMode === mode}
                onChange={() => setEnforcementMode(mode)}
                disabled={!canEdit}
                style={{ width: "auto" }}
              />
              {ENFORCEMENT_LABELS[mode]}
            </label>
          ))}
        </div>
        {enforcementMode === "webhook" ? (
          <>
            <input
              type="text"
              value={webhookUrl}
              onChange={(e) => setWebhookUrl(e.target.value)}
              placeholder="https://..."
              disabled={!canEdit}
              style={{ marginTop: 8 }}
            />
            <div className="field-hint" style={{ marginTop: 6 }}>
              {config.webhook_configured
                ? "A webhook URL is already configured, but it's never shown again - re-enter it here on every save while enforcement is set to webhook, even one that only changes an unrelated field."
                : "Required while enforcement mode is set to webhook."}
            </div>
          </>
        ) : null}
        <div className="field-hint" style={{ marginTop: 6 }}>
          Neither is true inline network blocking - Gatekey has no presence in your SASE/proxy
          tool&apos;s traffic path. Both are off by default; switching to either requires
          confirming this is intrusive.
        </div>
      </div>

      <div className="field">
        <label>Retention (days)</label>
        <input
          type="text"
          value={retentionDays}
          onChange={(e) => setRetentionDays(e.target.value)}
          disabled={!canEdit}
        />
        <div className="field-hint">
          Always finite (unlike audit-log retention) - separate from every other retention window
          in this app.
        </div>
      </div>

      <FieldError message={error} />
      {canEdit ? (
        <div className="modal-actions">
          <button className="btn btn-primary" onClick={handleSave} disabled={busy}>
            {busy ? "Saving..." : "Save Shadow AI configuration"}
          </button>
        </div>
      ) : null}

      {pendingIntrusive ? (
        <ConfirmDialog
          title="This is intrusive - are you sure?"
          consequence={
            pendingIntrusive === "notification"
              ? "Every newly-detected event will trigger an automated email to the flagged user and their Team Lead."
              : "Every newly-detected event will fire an outbound webhook to your configured URL, which your own tooling can use to act on."
          }
          confirmLabel="Enable"
          busy={busy}
          onCancel={() => setPendingIntrusive(null)}
          onConfirm={() => doSave(true)}
        />
      ) : null}

      {reveal ? (
        <SecretRevealModal
          title="Save this ingestion token now"
          secret={reveal.token}
          onDone={() => setReveal(null)}
        />
      ) : null}

      {showPolicy ? <ShadowAiPolicyModal onClose={() => setShowPolicy(false)} /> : null}
    </div>
  );
}

function KnownHostnamesPanel({ canEdit }: { canEdit: boolean }) {
  const toast = useToast();
  const [rows, setRows] = useState<KnownAiToolHostnameResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [hostname, setHostname] = useState("");
  const [toolLabel, setToolLabel] = useState("");
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState<KnownAiToolHostnameResponse | null>(null);

  function refresh() {
    setLoading(true);
    listKnownAiToolHostnames()
      .then(setRows)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load known hostnames."))
      .finally(() => setLoading(false));
  }

  useEffect(refresh, []);

  async function handleAdd() {
    setBusy(true);
    try {
      await addKnownAiToolHostname({ hostname: hostname.trim(), tool_label: toolLabel.trim() });
      toast.push("success", `${hostname} added.`);
      setAdding(false);
      setHostname("");
      setToolLabel("");
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to add hostname.");
    } finally {
      setBusy(false);
    }
  }

  async function handleRemove(row: KnownAiToolHostnameResponse) {
    setBusy(true);
    try {
      await removeKnownAiToolHostname(row.hostname);
      toast.push("success", `${row.hostname} removed.`);
      setRemoving(null);
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to remove hostname.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="page-header-row">
        <div className="panel-title">Known AI-tool hostnames</div>
        {canEdit ? (
          <button className="btn btn-primary" onClick={() => setAdding((v) => !v)}>
            {adding ? "Cancel" : "+ Add hostname"}
          </button>
        ) : null}
      </div>
      <p className="text-muted">
        Only ingested events whose destination host matches an enabled row here are ever stored -
        everything else in a submitted batch is dropped, not persisted.
      </p>
      {error ? <div className="banner banner-error">{error}</div> : null}
      {adding ? (
        <div style={{ display: "flex", gap: 8, alignItems: "flex-end", marginBottom: 12 }}>
          <div className="field" style={{ marginBottom: 0, flex: 1 }}>
            <label>Hostname</label>
            <input type="text" value={hostname} onChange={(e) => setHostname(e.target.value)} placeholder="chat.example.com" />
          </div>
          <div className="field" style={{ marginBottom: 0, flex: 1 }}>
            <label>Tool label</label>
            <input type="text" value={toolLabel} onChange={(e) => setToolLabel(e.target.value)} placeholder="Example AI" />
          </div>
          <button className="btn btn-primary" onClick={handleAdd} disabled={busy || !hostname.trim() || !toolLabel.trim()}>
            Add
          </button>
        </div>
      ) : null}
      <DataTable
        loading={loading}
        rows={rows}
        rowKey={(r) => r.hostname}
        emptyState="No known hostnames configured."
        columns={[
          { key: "hostname", header: "Hostname", render: (r) => <span className="mono">{r.hostname}</span> },
          { key: "tool", header: "Tool", render: (r) => r.tool_label },
          { key: "enabled", header: "Status", render: (r) => <Badge tone={r.enabled ? "green" : "gray"}>{r.enabled ? "Enabled" : "Disabled"}</Badge> },
          {
            key: "actions",
            header: "",
            align: "right",
            render: (r) =>
              canEdit ? (
                <button className="btn-link" style={{ color: "var(--red)" }} onClick={() => setRemoving(r)}>
                  Remove
                </button>
              ) : null,
          },
        ]}
      />
      {removing ? (
        <ConfirmDialog
          title={`Remove ${removing.hostname}?`}
          consequence="Future events to this hostname will no longer be detected as shadow AI usage."
          confirmLabel="Remove"
          busy={busy}
          onCancel={() => setRemoving(null)}
          onConfirm={() => handleRemove(removing)}
        />
      ) : null}
    </div>
  );
}

function ReportPanel() {
  const [teams, setTeams] = useState<TeamResponse[]>([]);
  const [teamId, setTeamId] = useState("");
  const [rows, setRows] = useState<ShadowAiReportRowResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listTeams()
      .then(setTeams)
      .catch(() => setTeams([]));
  }, []);

  useEffect(() => {
    setLoading(true);
    getShadowAiReport({ teamId: teamId || undefined })
      .then(setRows)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load the Shadow AI report."))
      .finally(() => setLoading(false));
  }, [teamId]);

  return (
    <div className="panel">
      <div className="page-header-row">
        <div className="panel-title">Report</div>
        <select value={teamId} onChange={(e) => setTeamId(e.target.value)}>
          <option value="">All teams</option>
          {teams.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
      </div>
      {error ? <div className="banner banner-error">{error}</div> : null}
      <ShadowAiReportTable rows={rows} loading={loading} />
    </div>
  );
}

export default function ShadowAiPage() {
  const role = useCallerRole();
  const canEdit = role === "org_admin";

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Shadow AI</div>
        <div className="page-subtitle">
          Detect employee use of unsanctioned AI tools that bypass Gatekey entirely, via your own
          SASE/proxy log export.
        </div>
        <ConfigPanel canEdit={canEdit} />
        <KnownHostnamesPanel canEdit={canEdit} />
        <ReportPanel />
      </div>
    </ConsoleShell>
  );
}
