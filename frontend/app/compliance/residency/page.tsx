"use client";

/**
 * Residency Rules (Phase 3, BD-4, design doc section 9.3). Org-wide rule:
 * an allowlist of regions plus hard-block-vs-warn on a violation - default
 * behavior on creation is hard_block (AC3.2: a client must explicitly opt
 * down to warn). Team-level narrowing lives on each team's detail page.
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { ConfirmDialog, FieldError, useToast } from "@/components/ui";
import {
  ApiError,
  deleteOrgResidencyRule,
  getOrgResidencyRule,
  putOrgResidencyRule,
  type ResidencyRuleResponse,
  type ResidencyViolationBehavior,
} from "@/lib/api";

function regionsToText(regions: string[]): string {
  return regions.join(", ");
}

function textToRegions(text: string): string[] {
  return text
    .split(",")
    .map((r) => r.trim())
    .filter(Boolean);
}

export default function ResidencyRulesPage() {
  const toast = useToast();
  const [rule, setRule] = useState<ResidencyRuleResponse | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [regionsText, setRegionsText] = useState("");
  const [behavior, setBehavior] = useState<ResidencyViolationBehavior>("hard_block");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [deleting, setDeleting] = useState(false);

  function refresh() {
    getOrgResidencyRule()
      .then((data) => {
        setRule(data);
        setRegionsText(data ? regionsToText(data.allowed_regions) : "");
        setBehavior(data?.violation_behavior ?? "hard_block");
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load residency rule."))
      .finally(() => setLoaded(true));
  }

  useEffect(refresh, []);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      const allowed_regions = textToRegions(regionsText);
      const result = await putOrgResidencyRule({ allowed_regions, violation_behavior: behavior });
      setRule(result);
      toast.push("success", "Residency rule saved.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save residency rule.");
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete() {
    setBusy(true);
    try {
      await deleteOrgResidencyRule();
      toast.push("success", "Residency rule removed - no region restriction applies org-wide.");
      setDeleting(false);
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to remove rule.");
      setDeleting(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Residency Rules</div>
        <div className="page-subtitle">
          Restrict which regions your org&apos;s traffic may be routed to. Teams can further
          narrow this org-wide rule on their own detail page, never widen it.
        </div>
        {error ? <div className="banner banner-error">{error}</div> : null}
        {!loaded ? (
          <div className="skeleton skeleton-text" style={{ height: 160 }} />
        ) : (
          <div className="panel">
            <div className="panel-title">Org-wide rule</div>
            {!rule ? (
              <div className="banner banner-info">
                No residency rule configured - traffic is not restricted by region.
              </div>
            ) : null}
            <div className="field">
              <label>Allowed regions (comma-separated)</label>
              <input
                type="text"
                value={regionsText}
                onChange={(e) => setRegionsText(e.target.value)}
                placeholder="e.g. us-east-1, eu-west-1"
              />
            </div>
            <div className="field">
              <label>On a violation</label>
              <select
                value={behavior}
                onChange={(e) => setBehavior(e.target.value as ResidencyViolationBehavior)}
              >
                <option value="hard_block">Hard block the request</option>
                <option value="warn">Warn only (allow, but log)</option>
              </select>
              <div className="field-hint">
                New rules default to hard block - switching an existing hard-block rule down to
                warn-only is recorded distinctly in the audit trail.
              </div>
            </div>
            <FieldError message={error} />
            <div className="modal-actions" style={{ justifyContent: rule ? "space-between" : "flex-end" }}>
              {rule ? (
                <button className="btn btn-secondary" onClick={() => setDeleting(true)} disabled={busy}>
                  Remove rule
                </button>
              ) : null}
              <button
                className="btn btn-primary"
                onClick={handleSave}
                disabled={busy || !regionsText.trim()}
              >
                {busy ? "Saving..." : "Save residency rule"}
              </button>
            </div>
          </div>
        )}
        {deleting ? (
          <ConfirmDialog
            title="Remove the org-wide residency rule?"
            consequence="Traffic will no longer be restricted by region org-wide. Any existing team-level narrowing rules stay in place and keep applying to their own team."
            confirmLabel="Remove rule"
            busy={busy}
            onCancel={() => setDeleting(false)}
            onConfirm={handleDelete}
          />
        ) : null}
      </div>
    </ConsoleShell>
  );
}
