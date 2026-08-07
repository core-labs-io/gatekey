"use client";

/**
 * Compliance & DLP (Phase 3, BD-2/BD-5, design doc sections 9.2/9.4).
 * Org-wide DLP policy (detector toggles, default action, raw-content /
 * inbound-response toggles), custom regex patterns CRUD, and the
 * content-aware routing rules. Team-level DLP action override lives on
 * each team's detail page (app/teams/[teamId]).
 *
 * Phase 5 (5.3 Content-Classification-Aware Routing, AC5.3.1/AC5.3.4/
 * AC5.3.6): all four categories (pii/source_code/financial_data/legal) are
 * now functionally equivalent - the "not yet enforced" copy for the latter
 * three is gone. The "Classification source" control from the ui mock
 * (an exclusive Gatekey/Purview/Google-DLP radio group) is reframed per
 * AC5.3.6 as an ADDITIVE `sensitivity_label_mappings` management table -
 * Gatekey's own classifier always runs as the fallback; a matched mapping
 * only short-circuits its own single mapped category, never an
 * either/or choice between vendors.
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { Badge, ConfirmDialog, DataTable, FieldError, Modal, useToast } from "@/components/ui";
import {
  ApiError,
  CONTENT_AWARE_CATEGORIES,
  MODELS_BY_PROVIDER,
  createDlpCustomPattern,
  createSensitivityLabelMapping,
  deleteDlpCustomPattern,
  deleteSensitivityLabelMapping,
  getContentAwareRules,
  getDlpPolicy,
  listDlpCustomPatterns,
  listSelfHostedProviders,
  listSensitivityLabelMappings,
  putContentAwareRules,
  putDlpPolicy,
  updateDlpCustomPattern,
  updateSensitivityLabelMapping,
  type ContentAwareCategory,
  type ContentAwareRuleResponse,
  type DlpAction,
  type DlpCustomPatternResponse,
  type DlpPolicyResponse,
  type SensitivityLabelMappingResponse,
} from "@/lib/api";

const ACTION_LABELS: Record<DlpAction, string> = {
  log: "Log only",
  redact: "Redact",
  block: "Block",
};

const CATEGORY_LABELS: Record<ContentAwareCategory, string> = {
  pii: "PII detected",
  source_code: "Source code detected",
  financial_data: "Financial data detected",
  legal: "Legal content detected",
};

// --- DLP policy panel ----------------------------------------------------------

function DlpPolicyPanel({
  policy,
  onSaved,
}: {
  policy: DlpPolicyResponse;
  onSaved: (updated: DlpPolicyResponse) => void;
}) {
  const toast = useToast();
  const [form, setForm] = useState(policy);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      // scan_inbound_responses is not yet implemented (toggle removed above) -
      // always submit false so a pre-existing true value doesn't 422 this save.
      const result = await putDlpPolicy({ ...form, scan_inbound_responses: false });
      onSaved(result);
      toast.push("success", "DLP policy saved.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save DLP policy.");
    } finally {
      setBusy(false);
    }
  }

  function toggle(key: keyof DlpPolicyResponse) {
    setForm((prev) => ({ ...prev, [key]: !prev[key] }));
  }

  return (
    <div className="panel">
      <div className="panel-title">DLP policy (org-wide)</div>
      <p className="text-muted">
        Built-in detectors scan every request (and, if enabled, every response) for sensitive
        content. A finding triggers the default action below unless a team has its own override.
      </p>
      <div className="field">
        <label>Built-in detectors</label>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          {(
            [
              ["ssn_detector_enabled", "Social Security Numbers"],
              ["credit_card_detector_enabled", "Credit card numbers"],
              ["email_detector_enabled", "Email addresses"],
              ["phone_detector_enabled", "Phone numbers"],
            ] as [keyof DlpPolicyResponse, string][]
          ).map(([key, label]) => (
            <label key={key} style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <input
                type="checkbox"
                checked={form[key] as boolean}
                onChange={() => toggle(key)}
                style={{ width: "auto" }}
              />
              {label}
            </label>
          ))}
        </div>
      </div>
      <div className="field">
        <label>Default action</label>
        <select
          value={form.default_action}
          onChange={(e) => setForm({ ...form, default_action: e.target.value as DlpAction })}
        >
          {(Object.keys(ACTION_LABELS) as DlpAction[]).map((a) => (
            <option key={a} value={a}>
              {ACTION_LABELS[a]}
            </option>
          ))}
        </select>
        <div className="field-hint">
          Log: record the finding, allow the request. Redact: strip the flagged content before
          it leaves Gatekey. Block: reject the request outright.
        </div>
      </div>
      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input type="checkbox" id="dlp-inbound" checked={false} disabled style={{ width: "auto" }} />
        <label htmlFor="dlp-inbound" style={{ margin: 0 }}>
          Also scan provider responses (not just inbound prompts)
        </label>
        <Badge tone="gray">Not yet implemented</Badge>
      </div>
      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          id="dlp-store-raw"
          checked={form.store_raw_flagged_content}
          onChange={() => toggle("store_raw_flagged_content")}
          style={{ width: "auto" }}
        />
        <label htmlFor="dlp-store-raw" style={{ margin: 0 }}>
          Store the raw flagged content alongside the finding
        </label>
      </div>
      <div className="field-hint" style={{ marginTop: -10 }}>
        Off by default - leave this off unless your compliance program specifically requires
        retaining the sensitive text itself, not just the fact that something was flagged.
      </div>
      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-primary" onClick={handleSave} disabled={busy}>
          {busy ? "Saving..." : "Save DLP policy"}
        </button>
      </div>
    </div>
  );
}

// --- Custom pattern modal --------------------------------------------------------

function CustomPatternModal({
  initial,
  onClose,
  onSaved,
}: {
  initial: DlpCustomPatternResponse | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(initial?.name ?? "");
  const [pattern, setPattern] = useState(initial?.pattern ?? "");
  const [action, setAction] = useState<DlpAction>(initial?.action ?? "log");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      if (initial) {
        await updateDlpCustomPattern(initial.id, { name: name.trim(), pattern, action });
      } else {
        await createDlpCustomPattern({ name: name.trim(), pattern, action });
      }
      toast.push("success", initial ? "Pattern updated." : "Pattern created.");
      onSaved();
    } catch (err) {
      // 422 invalid_dlp_custom_pattern_regex / 409 name conflict pass
      // through verbatim.
      setError(err instanceof ApiError ? err.message : "Failed to save pattern.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={initial ? `Edit pattern - ${initial.name}` : "New custom pattern"} onClose={onClose}>
      <div className="field">
        <label>Name</label>
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. internal-project-code" />
      </div>
      <div className="field">
        <label>Regex pattern</label>
        <input
          type="text"
          className="mono"
          value={pattern}
          onChange={(e) => setPattern(e.target.value)}
          placeholder="e.g. PROJ-\d{4}"
        />
        <div className="field-hint">Validated as a compilable regex on save.</div>
      </div>
      <div className="field">
        <label>Action</label>
        <select value={action} onChange={(e) => setAction(e.target.value as DlpAction)}>
          {(Object.keys(ACTION_LABELS) as DlpAction[]).map((a) => (
            <option key={a} value={a}>
              {ACTION_LABELS[a]}
            </option>
          ))}
        </select>
      </div>
      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button className="btn btn-primary" onClick={handleSave} disabled={busy || !name.trim() || !pattern.trim()}>
          {busy ? "Saving..." : "Save"}
        </button>
      </div>
    </Modal>
  );
}

function CustomPatternsPanel() {
  const toast = useToast();
  const [rows, setRows] = useState<DlpCustomPatternResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<null | "new" | DlpCustomPatternResponse>(null);
  const [deleting, setDeleting] = useState<DlpCustomPatternResponse | null>(null);
  const [busy, setBusy] = useState(false);

  function refresh() {
    setLoading(true);
    listDlpCustomPatterns()
      .then(setRows)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load patterns."))
      .finally(() => setLoading(false));
  }

  useEffect(refresh, []);

  async function handleDelete(row: DlpCustomPatternResponse) {
    setBusy(true);
    try {
      await deleteDlpCustomPattern(row.id);
      toast.push("success", `${row.name} deleted.`);
      setDeleting(null);
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to delete pattern.");
      setDeleting(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="page-header-row">
        <div className="panel-title">Custom patterns</div>
        <button className="btn btn-primary" onClick={() => setEditing("new")}>
          + Add pattern
        </button>
      </div>
      {error ? <div className="banner banner-error">{error}</div> : null}
      <DataTable
        loading={loading}
        rows={rows}
        rowKey={(r) => r.id}
        emptyState="No custom patterns yet."
        columns={[
          { key: "name", header: "Name", render: (r) => r.name },
          { key: "pattern", header: "Pattern", render: (r) => <span className="mono">{r.pattern}</span> },
          { key: "action", header: "Action", render: (r) => <Badge tone="gray">{ACTION_LABELS[r.action]}</Badge> },
          {
            key: "actions",
            header: "",
            align: "right",
            render: (r) => (
              <>
                <button className="btn-link" onClick={() => setEditing(r)}>
                  Edit
                </button>{" "}
                <button className="btn-link" style={{ color: "var(--red)" }} onClick={() => setDeleting(r)}>
                  Delete
                </button>
              </>
            ),
          },
        ]}
      />
      {editing ? (
        <CustomPatternModal
          initial={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            refresh();
          }}
        />
      ) : null}
      {deleting ? (
        <ConfirmDialog
          title={`Delete pattern ${deleting.name}?`}
          consequence="This cannot be undone."
          confirmLabel="Delete"
          busy={busy}
          onCancel={() => setDeleting(null)}
          onConfirm={() => handleDelete(deleting)}
        />
      ) : null}
    </div>
  );
}

// --- Content-aware routing rules -------------------------------------------------

function ContentAwareRulesPanel() {
  const toast = useToast();
  const [rows, setRows] = useState<ContentAwareRuleResponse[]>([]);
  const [selfHostedModels, setSelfHostedModels] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function refresh() {
    setLoading(true);
    Promise.all([getContentAwareRules(), listSelfHostedProviders()])
      .then(([ruleRows, providers]) => {
        setRows(ruleRows);
        // Only verified providers' models are ever routable (AC5.5.5) - an
        // unverified endpoint's models can't be selected here either, same
        // constraint the Model Policy static tab applies.
        setSelfHostedModels(providers.filter((p) => p.verified).flatMap((p) => p.models));
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load content-aware rules."))
      .finally(() => setLoading(false));
  }

  useEffect(refresh, []);

  const allModels = [...Object.values(MODELS_BY_PROVIDER).flat(), ...selfHostedModels];

  function setEnabled(category: string, enabled: boolean) {
    setRows((prev) => prev.map((r) => (r.category === category ? { ...r, enabled } : r)));
  }

  function toggleModel(category: string, model: string) {
    setRows((prev) =>
      prev.map((r) =>
        r.category === category
          ? {
              ...r,
              allowed_models: r.allowed_models.includes(model)
                ? r.allowed_models.filter((m) => m !== model)
                : [...r.allowed_models, model],
            }
          : r
      )
    );
  }

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      const result = await putContentAwareRules(rows);
      setRows(result);
      toast.push("success", "Content-aware rules saved.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save content-aware rules.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="panel-title">Content-aware routing</div>
      <p className="text-muted">
        When a category is triggered on a request, only its listed models may serve it - an
        enabled category with zero models selected blocks that category&apos;s traffic entirely.
        All four categories below are enforced from a real classifier signal. If a request
        triggers more than one enabled category, only models allowed by every matched category
        may serve it (the intersection) - this runs after the static org/team baseline, so a
        model already blocked there stays blocked regardless of these rules. Self-hosted models
        only appear below once their endpoint is registered and verified on the Providers screen.
      </p>
      {error ? <div className="banner banner-error">{error}</div> : null}
      {loading ? (
        <div className="skeleton skeleton-text" />
      ) : (
        rows
          .slice()
          .sort((a, b) => CONTENT_AWARE_CATEGORIES.indexOf(a.category as ContentAwareCategory) - CONTENT_AWARE_CATEGORIES.indexOf(b.category as ContentAwareCategory))
          .map((row) => (
            <div key={row.category} className="model-group">
              <div className="page-header-row" style={{ marginBottom: 8 }}>
                <label style={{ display: "flex", alignItems: "center", gap: 8, fontWeight: 600 }}>
                  <input
                    type="checkbox"
                    checked={row.enabled}
                    onChange={(e) => setEnabled(row.category, e.target.checked)}
                    style={{ width: "auto" }}
                  />
                  {CATEGORY_LABELS[row.category as ContentAwareCategory] ?? row.category}
                </label>
                {row.enabled && row.allowed_models.length === 0 ? (
                  <Badge tone="amber">Blocks all traffic in this category</Badge>
                ) : null}
              </div>
              <div className="model-checkbox-grid">
                {allModels.map((model) => (
                  <label key={model} className="model-checkbox">
                    <input
                      type="checkbox"
                      checked={row.allowed_models.includes(model)}
                      onChange={() => toggleModel(row.category, model)}
                      disabled={!row.enabled}
                    />
                    {model}
                  </label>
                ))}
              </div>
            </div>
          ))
      )}
      <div className="modal-actions">
        <button className="btn btn-primary" onClick={handleSave} disabled={busy || loading}>
          {busy ? "Saving..." : "Save content-aware rules"}
        </button>
      </div>
    </div>
  );
}

// --- Sensitivity-label mappings (Phase 5, 5.3, AC5.3.5/AC5.3.6) ------------------
//
// Reframed per AC5.3.6 from the ui mock's exclusive "Classification source"
// radio (Gatekey built-in / Purview / Google DLP) into an ADDITIVE mapping
// table: Gatekey's own classifier always runs as the fallback for every
// category; a matched external label only short-circuits its own single
// mapped category, never an either/or choice between vendors.

function SensitivityLabelMappingModal({
  initial,
  onClose,
  onSaved,
}: {
  initial: SensitivityLabelMappingResponse | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [externalLabel, setExternalLabel] = useState(initial?.external_label ?? "");
  const [category, setCategory] = useState<ContentAwareCategory>(
    (initial?.gatekey_category as ContentAwareCategory) ?? CONTENT_AWARE_CATEGORIES[0]
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      if (initial) {
        await updateSensitivityLabelMapping(initial.id, {
          external_label: externalLabel.trim(),
          gatekey_category: category,
        });
      } else {
        await createSensitivityLabelMapping({
          external_label: externalLabel.trim(),
          gatekey_category: category,
        });
      }
      toast.push("success", initial ? "Mapping updated." : "Mapping created.");
      onSaved();
    } catch (err) {
      // 409 sensitivity_label_mapping_conflict passes through verbatim on a
      // duplicate external_label.
      setError(err instanceof ApiError ? err.message : "Failed to save mapping.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={initial ? `Edit mapping - ${initial.external_label}` : "New sensitivity-label mapping"} onClose={onClose}>
      <div className="field">
        <label>External label</label>
        <input
          type="text"
          value={externalLabel}
          onChange={(e) => setExternalLabel(e.target.value)}
          placeholder="e.g. Microsoft Purview: Highly Confidential"
        />
        <div className="field-hint">
          The exact label value your upstream tool (Purview, Google DLP, or any other pre-set
          labeling system) sends via the <span className="mono">X-Gatekey-Sensitivity-Label</span>{" "}
          request header.
        </div>
      </div>
      <div className="field">
        <label>Trusted as category</label>
        <select value={category} onChange={(e) => setCategory(e.target.value as ContentAwareCategory)}>
          {CONTENT_AWARE_CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {CATEGORY_LABELS[c]}
            </option>
          ))}
        </select>
        <div className="field-hint">
          A request carrying this exact label is treated as already classified into this one
          category, without Gatekey re-running its own classifier for it - Gatekey&apos;s
          classifiers still run for every other category.
        </div>
      </div>
      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button className="btn btn-primary" onClick={handleSave} disabled={busy || !externalLabel.trim()}>
          {busy ? "Saving..." : "Save"}
        </button>
      </div>
    </Modal>
  );
}

function SensitivityLabelMappingsPanel() {
  const toast = useToast();
  const [rows, setRows] = useState<SensitivityLabelMappingResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<null | "new" | SensitivityLabelMappingResponse>(null);
  const [deleting, setDeleting] = useState<SensitivityLabelMappingResponse | null>(null);
  const [busy, setBusy] = useState(false);

  function refresh() {
    setLoading(true);
    listSensitivityLabelMappings()
      .then(setRows)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load sensitivity-label mappings."))
      .finally(() => setLoading(false));
  }

  useEffect(refresh, []);

  async function handleDelete(row: SensitivityLabelMappingResponse) {
    setBusy(true);
    try {
      await deleteSensitivityLabelMapping(row.id);
      toast.push("success", `${row.external_label} deleted.`);
      setDeleting(null);
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to delete mapping.");
      setDeleting(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="page-header-row">
        <div className="panel-title">Classification source - sensitivity-label mappings</div>
        <button className="btn btn-primary" onClick={() => setEditing("new")}>
          + Add mapping
        </button>
      </div>
      <p className="text-muted">
        If your org already applies its own sensitivity labels upstream (e.g. Microsoft Purview or
        Google DLP) and sends one via the <span className="mono">X-Gatekey-Sensitivity-Label</span>{" "}
        request header, map that exact label to a Gatekey category here so Gatekey trusts it
        instead of re-running its own classifier for that one category. This is additive, not
        exclusive - Gatekey&apos;s built-in classifier always keeps running as the fallback for
        every category with no matching label, and for any label Gatekey doesn&apos;t recognize.
      </p>
      {error ? <div className="banner banner-error">{error}</div> : null}
      <DataTable
        loading={loading}
        rows={rows}
        rowKey={(r) => r.id}
        emptyState="No sensitivity-label mappings configured - Gatekey's own classifier runs for every category."
        columns={[
          { key: "external_label", header: "External label", render: (r) => <span className="mono">{r.external_label}</span> },
          {
            key: "category",
            header: "Trusted as",
            render: (r) => CATEGORY_LABELS[r.gatekey_category as ContentAwareCategory] ?? r.gatekey_category,
          },
          {
            key: "actions",
            header: "",
            align: "right",
            render: (r) => (
              <>
                <button className="btn-link" onClick={() => setEditing(r)}>
                  Edit
                </button>{" "}
                <button className="btn-link" style={{ color: "var(--red)" }} onClick={() => setDeleting(r)}>
                  Delete
                </button>
              </>
            ),
          },
        ]}
      />
      {editing ? (
        <SensitivityLabelMappingModal
          initial={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            refresh();
          }}
        />
      ) : null}
      {deleting ? (
        <ConfirmDialog
          title={`Delete mapping ${deleting.external_label}?`}
          consequence="Requests carrying this label will fall through to Gatekey's own classifier again."
          confirmLabel="Delete"
          busy={busy}
          onCancel={() => setDeleting(null)}
          onConfirm={() => handleDelete(deleting)}
        />
      ) : null}
    </div>
  );
}

// --- Page ------------------------------------------------------------------------

export default function DlpCompliancePage() {
  const [policy, setPolicy] = useState<DlpPolicyResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getDlpPolicy()
      .then(setPolicy)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load DLP policy."));
  }, []);

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Compliance &amp; DLP</div>
        <div className="page-subtitle">
          Org-wide data-loss-prevention scanning and content-aware model routing. Teams can
          narrow the DLP action on their own detail page, never widen it.
        </div>
        {error ? <div className="banner banner-error">{error}</div> : null}
        {policy ? (
          <DlpPolicyPanel policy={policy} onSaved={setPolicy} />
        ) : !error ? (
          <div className="skeleton skeleton-text" style={{ height: 160 }} />
        ) : null}
        <CustomPatternsPanel />
        <ContentAwareRulesPanel />
        <SensitivityLabelMappingsPanel />
      </div>
    </ConsoleShell>
  );
}
