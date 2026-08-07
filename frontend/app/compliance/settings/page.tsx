"use client";

/**
 * Compliance Settings (Phase 3, BD-10, design doc section 9.1): audit
 * retention (AC1.6 - null/blank means never auto-purge, a client must
 * explicitly opt in to a finite window), usage/prompt-log retention
 * (default 30 days, independent of audit retention), and the org timezone
 * used to resolve every scheduled access window.
 *
 * Phase 5 (5.2 Hash-Chained Audit Ledger, AC5.2.2/AC5.2.7): adds the
 * `chain_enabled` toggle. Mutually exclusive with a finite audit retention
 * window (deleting a row structurally breaks a hash chain) - the backend
 * rejects a PUT that sets both, and this screen disables/greys out
 * whichever control conflicts with the other's current state, with an
 * inline reason, so the rejection is never a surprise 422.
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { FieldError, useToast } from "@/components/ui";
import {
  ApiError,
  getComplianceSettings,
  putComplianceSettings,
  type ComplianceSettingsResponse,
} from "@/lib/api";

// A short, common IANA timezone list - the backend validates any IANA name
// server-side (this is a convenience picker, not an exhaustive one).
const COMMON_TIMEZONES = [
  "UTC",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "Europe/London",
  "Europe/Berlin",
  "Europe/Paris",
  "Asia/Kolkata",
  "Asia/Singapore",
  "Asia/Tokyo",
  "Australia/Sydney",
];

export default function ComplianceSettingsPage() {
  const toast = useToast();
  const [loaded, setLoaded] = useState(false);
  const [neverPurgeAudit, setNeverPurgeAudit] = useState(true);
  const [auditDays, setAuditDays] = useState("");
  const [logDays, setLogDays] = useState("30");
  const [timezone, setTimezone] = useState("UTC");
  const [chainEnabled, setChainEnabled] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getComplianceSettings()
      .then((data: ComplianceSettingsResponse) => {
        setNeverPurgeAudit(data.audit_retention_days === null);
        setAuditDays(data.audit_retention_days?.toString() ?? "");
        setLogDays(data.log_prompt_retention_days.toString());
        setTimezone(data.access_schedule_timezone);
        setChainEnabled(data.chain_enabled);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load compliance settings."))
      .finally(() => setLoaded(true));
  }, []);

  // AC5.2.7: a finite retention window and the hash chain are mutually
  // exclusive - the backend rejects a PUT that sets both, so this UI
  // disables whichever control conflicts with the other's current state
  // rather than letting the admin discover the conflict as a 422.
  const retentionIsFinite = !neverPurgeAudit;

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      await putComplianceSettings({
        audit_retention_days: neverPurgeAudit ? null : Number(auditDays),
        log_prompt_retention_days: Number(logDays) || 30,
        access_schedule_timezone: timezone,
        chain_enabled: chainEnabled,
      });
      toast.push("success", "Compliance settings saved.");
    } catch (err) {
      // The timezone validator's "not a recognized IANA timezone" message,
      // and the chain/purge mutual-exclusivity 422, both pass through
      // verbatim on a rejected save.
      setError(err instanceof ApiError ? err.message : "Failed to save compliance settings.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Compliance Settings</div>
        <div className="page-subtitle">
          Independent retention windows for the audit trail and for usage/prompt logs, plus the
          org timezone every scheduled access window resolves against.
        </div>
        {error ? <div className="banner banner-error">{error}</div> : null}
        {!loaded ? (
          <div className="skeleton skeleton-text" style={{ height: 200 }} />
        ) : (
          <div className="panel">
            <div className="field">
              <label>Audit trail retention</label>
              <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <input
                  type="checkbox"
                  checked={neverPurgeAudit}
                  onChange={(e) => setNeverPurgeAudit(e.target.checked)}
                  disabled={chainEnabled}
                  style={{ width: "auto" }}
                />
                Never auto-purge audit entries (default)
              </label>
              {!neverPurgeAudit ? (
                <input
                  type="text"
                  value={auditDays}
                  onChange={(e) => setAuditDays(e.target.value)}
                  placeholder="e.g. 365"
                  disabled={chainEnabled}
                  style={{ marginTop: 8 }}
                />
              ) : null}
              <div className="banner banner-warning" style={{ marginTop: 8 }}>
                Setting a value here enables a scheduled job that <strong>permanently, irreversibly
                deletes</strong> audit entries older than the window - this is the one sanctioned
                exception to the audit trail&apos;s normal append-only, never-delete design. Leave
                this unset unless your compliance program specifically requires it.
              </div>
              {chainEnabled ? (
                <div className="field-hint" style={{ marginTop: 8 }}>
                  Locked to &quot;never auto-purge&quot; while the hash chain (below) is enabled -
                  deleting a row would structurally break chain verification. Disable the hash
                  chain first if you need a finite retention window.
                </div>
              ) : null}
            </div>

            <div className="field">
              <label>Hash-chained audit ledger</label>
              <label style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <input
                  type="checkbox"
                  checked={chainEnabled}
                  onChange={(e) => setChainEnabled(e.target.checked)}
                  disabled={retentionIsFinite}
                  style={{ width: "auto" }}
                />
                Enable tamper-evident hash chaining over the audit trail
              </label>
              <div className="field-hint" style={{ marginTop: 8 }}>
                Each audit entry is chained to the one before it via a SHA-256 hash, so any
                retroactive modification to a historical entry becomes detectable via
                &quot;Verify now&quot; on the Audit Log tab. Turning this on for the first time
                backfills the chain over every existing audit entry, back to this org&apos;s true
                first-ever entry - this can take a while on a large audit table and holds a lock
                for its duration, so prefer a low-traffic window.
              </div>
              {retentionIsFinite ? (
                <div className="field-hint" style={{ marginTop: 8 }}>
                  Unavailable while a finite audit retention window is set above - a scheduled
                  purge would structurally break the chain. Set audit retention back to &quot;never
                  auto-purge&quot; first if you need the hash chain.
                </div>
              ) : null}
            </div>
            <div className="field">
              <label>Usage / prompt log retention (days)</label>
              <input type="text" value={logDays} onChange={(e) => setLogDays(e.target.value)} />
              <div className="field-hint">
                Independent of audit retention above - defaults to 30 days. Governs usage logs and
                any retained prompt content, not the audit trail.
              </div>
            </div>
            <div className="field">
              <label>Org timezone</label>
              <select value={timezone} onChange={(e) => setTimezone(e.target.value)}>
                {[...new Set([timezone, ...COMMON_TIMEZONES])].map((tz) => (
                  <option key={tz} value={tz}>
                    {tz}
                  </option>
                ))}
              </select>
              <input
                type="text"
                value={timezone}
                onChange={(e) => setTimezone(e.target.value)}
                placeholder="Any IANA timezone name, e.g. America/Sao_Paulo"
                style={{ marginTop: 6 }}
              />
              <div className="field-hint">
                Used to resolve every scheduled access window (org/team/key) and holiday date -
                never inferred from a request&apos;s IP.
              </div>
            </div>
            <FieldError message={error} />
            <div className="modal-actions">
              <button className="btn btn-primary" onClick={handleSave} disabled={busy}>
                {busy ? "Saving..." : "Save compliance settings"}
              </button>
            </div>
          </div>
        )}
      </div>
    </ConsoleShell>
  );
}
