"use client";

/**
 * Audit entries view (Phase 2 FE-8/FE-9): filters + paginated table over
 * GET /v1/admin/audit-entries. Extracted from app/audit-log so the Auditor
 * Org Logs screen (non-admin UI doc section 8.2) renders the exact same
 * component instead of a second implementation. Backend auth:
 * require_role(org_admin, auditor) - break-glass token also accepted.
 *
 * Phase 5 (5.2 Hash-Chained Audit Ledger, AC5.2.5/AC5.2.8): adds the
 * hash-chain integrity badge + "Verify now" button (ui doc section 10.3),
 * and an Export (CSV/JSON) action that automatically includes chain columns
 * once chaining is enabled - both org-admin and auditor sessions can call
 * these (same `require_admin_or_auditor` gate as the underlying reads).
 */

import { Fragment, useEffect, useState } from "react";
import {
  ApiError,
  AUDIT_ACTIONS,
  downloadBlob,
  exportAuditEntries,
  listAuditEntries,
  verifyAuditChain,
  type AuditEntriesPageResponse,
  type AuditVerifyResponse,
} from "@/lib/api";
import { Badge, useToast, type BadgeTone } from "@/components/ui";
import { useCallerRole } from "@/components/differentiators";

function ChainBadge() {
  const toast = useToast();
  const role = useCallerRole();
  const [result, setResult] = useState<AuditVerifyResponse | null>(null);
  const [checking, setChecking] = useState(true);

  function runVerify(manual: boolean) {
    setChecking(true);
    verifyAuditChain()
      .then((res) => {
        setResult(res);
        if (manual && res.status === "intact") {
          toast.push("success", `Chain intact - ${res.entries_verified ?? 0} entries verified.`);
        } else if (manual && res.status === "broken") {
          toast.push(
            "error",
            `Chain broken at entry ${res.broken_at_entry_id} (chain_seq ${res.broken_at_chain_seq}).`
          );
        }
      })
      .catch((err) => {
        if (manual) toast.push("error", err instanceof ApiError ? err.message : "Verification failed.");
      })
      .finally(() => setChecking(false));
  }

  useEffect(() => runVerify(false), []); // eslint-disable-line react-hooks/exhaustive-deps

  if (!result) {
    return checking ? <span className="text-muted">Checking hash chain...</span> : null;
  }

  if (result.status === "not_enabled") {
    return (
      <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <Badge tone="gray">Hash chain not enabled</Badge>
        {/* Compliance Settings (where chain_enabled is toggled) is an
         * Org-Admin-only surface - never link an Auditor session to a page
         * their own GET there would 403 on. */}
        {role === "org_admin" ? (
          <a href="/compliance/settings" className="btn-link">
            Enable &rarr;
          </a>
        ) : null}
      </span>
    );
  }

  const tone: BadgeTone = result.status === "intact" ? "green" : "red";
  return (
    <span style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <Badge tone={tone}>
        {result.status === "intact"
          ? `Verified - chain intact (${result.entries_verified ?? 0} entries)`
          : `Chain broken at entry ${result.broken_at_entry_id}`}
      </Badge>
      <button className="btn-link" onClick={() => runVerify(true)} disabled={checking}>
        {checking ? "Verifying..." : "Verify now"}
      </button>
    </span>
  );
}

function ExportButtons({
  action,
  actor,
  from,
  to,
}: {
  action: string;
  actor: string;
  from: string;
  to: string;
}) {
  const toast = useToast();
  const [exporting, setExporting] = useState(false);

  async function handleExport(format: "csv" | "json") {
    setExporting(true);
    try {
      const { blob, filename } = await exportAuditEntries({
        action: action || undefined,
        actor: actor || undefined,
        from: from ? `${from}T00:00:00Z` : undefined,
        to: to ? toExclusiveEnd(to) : undefined,
        format,
      });
      downloadBlob(blob, filename);
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to export audit entries.");
    } finally {
      setExporting(false);
    }
  }

  return (
    <span style={{ display: "flex", gap: 8 }}>
      <button className="btn btn-secondary" onClick={() => handleExport("csv")} disabled={exporting}>
        Export CSV
      </button>
      <button className="btn btn-secondary" onClick={() => handleExport("json")} disabled={exporting}>
        Export JSON
      </button>
    </span>
  );
}

function ValueBlock({ label, value }: { label: string; value: Record<string, unknown> | null }) {
  return (
    <div style={{ flex: 1, minWidth: 0 }}>
      <div className="stat-tile-label">{label}</div>
      {value === null ? (
        <span className="text-muted">(none)</span>
      ) : (
        <pre
          style={{
            margin: 0,
            padding: 8,
            background: "var(--surface, #f6f6f6)",
            border: "1px solid var(--border, #ddd)",
            borderRadius: 6,
            fontSize: 12,
            overflowX: "auto",
          }}
        >
          {JSON.stringify(value, null, 2)}
        </pre>
      )}
    </div>
  );
}

/** Add one day so a date picked in the `to` field is inclusive against the
 * backend's half-open `created_at < to` filter. */
function toExclusiveEnd(date: string): string {
  const d = new Date(`${date}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + 1);
  return d.toISOString();
}

export function AuditEntriesView() {
  const [action, setAction] = useState("");
  const [actor, setActor] = useState("");
  const [actorApplied, setActorApplied] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<AuditEntriesPageResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    listAuditEntries({
      action: action || undefined,
      actor: actorApplied || undefined,
      from: from ? `${from}T00:00:00Z` : undefined,
      to: to ? toExclusiveEnd(to) : undefined,
      page,
    })
      .then((result) => {
        if (!cancelled) setData(result);
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof ApiError ? err.message : "Failed to load audit entries.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [action, actorApplied, from, to, page]);

  const totalPages = data ? Math.max(Math.ceil(data.total / data.page_size), 1) : 1;

  return (
    <>
      <div className="page-header-row">
        <ChainBadge />
        <ExportButtons action={action} actor={actorApplied} from={from} to={to} />
      </div>

      <div style={{ display: "flex", gap: 12, flexWrap: "wrap", alignItems: "flex-end" }}>
        <div className="field">
          <label>Action</label>
          <select
            value={action}
            onChange={(e) => {
              setAction(e.target.value);
              setPage(1);
            }}
          >
            <option value="">All actions</option>
            {AUDIT_ACTIONS.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>Actor (name, email, or user id)</label>
          <input
            type="text"
            value={actor}
            onChange={(e) => setActor(e.target.value)}
            onBlur={() => {
              setActorApplied(actor.trim());
              setPage(1);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                setActorApplied(actor.trim());
                setPage(1);
              }
            }}
            placeholder="ana@acme.co"
          />
        </div>
        <div className="field">
          <label>From</label>
          <input
            type="date"
            value={from}
            onChange={(e) => {
              setFrom(e.target.value);
              setPage(1);
            }}
          />
        </div>
        <div className="field">
          <label>To</label>
          <input
            type="date"
            value={to}
            onChange={(e) => {
              setTo(e.target.value);
              setPage(1);
            }}
          />
        </div>
      </div>

      {error ? <div className="banner banner-error">{error}</div> : null}

      <table className="data-table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Actor</th>
            <th>Action</th>
            <th>Target</th>
            <th className="align-right">Details</th>
          </tr>
        </thead>
        <tbody>
          {loading ? (
            Array.from({ length: 5 }).map((_, i) => (
              <tr key={`skeleton-${i}`}>
                {Array.from({ length: 5 }).map((_, j) => (
                  <td key={j}>
                    <div className="skeleton skeleton-text" />
                  </td>
                ))}
              </tr>
            ))
          ) : !data || data.entries.length === 0 ? (
            <tr>
              <td colSpan={5} className="empty-state-cell">
                No audit entries match these filters.
              </td>
            </tr>
          ) : (
            data.entries.map((entry) => (
              <Fragment key={entry.id}>
                <tr>
                  <td>{new Date(entry.created_at).toLocaleString()}</td>
                  <td>{entry.actor_label}</td>
                  <td>{entry.action}</td>
                  <td>
                    <span className="text-muted">{entry.target_type}</span> {entry.target_id}
                  </td>
                  <td className="align-right">
                    <button
                      className="btn-link"
                      onClick={() => setExpanded(expanded === entry.id ? null : entry.id)}
                    >
                      {expanded === entry.id ? "Hide" : "Show"}
                    </button>
                  </td>
                </tr>
                {expanded === entry.id ? (
                  <tr>
                    <td colSpan={5}>
                      <div style={{ display: "flex", gap: 16 }}>
                        <ValueBlock label="Old value" value={entry.old_value} />
                        <ValueBlock label="New value" value={entry.new_value} />
                      </div>
                    </td>
                  </tr>
                ) : null}
              </Fragment>
            ))
          )}
        </tbody>
      </table>

      <div className="page-header-row" style={{ marginTop: 12 }}>
        <span className="text-muted">
          {data ? `${data.total} entries - page ${data.page} of ${totalPages}` : ""}
        </span>
        <span>
          <button
            className="btn btn-secondary"
            disabled={loading || page <= 1}
            onClick={() => setPage(page - 1)}
          >
            Previous
          </button>{" "}
          <button
            className="btn btn-secondary"
            disabled={loading || page >= totalPages}
            onClick={() => setPage(page + 1)}
          >
            Next
          </button>
        </span>
      </div>
    </>
  );
}
