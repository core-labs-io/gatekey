"use client";

/**
 * Shared bits for Phase 5's Differentiators screens (Drift Detector,
 * Self-Hosted Governance, Shadow AI Discovery) - each of those is a single
 * page shared by both Org Admin (full read/write) and Auditor (read-only)
 * sessions, matching the design doc's `require_admin_or_auditor` RBAC for
 * every read surface there. `useCallerRole` resolves which one a session
 * is, so each page can hide (never just disable) admin-only controls for
 * an Auditor - mirrors `ConsoleShell`'s own "a role only ever sees its own
 * nav entries" discipline, applied within a shared page instead of via a
 * separate route per role.
 *
 * `ShadowAiReportTable`/`ShadowAiPolicyModal` are reused by both the
 * org-wide Shadow AI screen (`app/differentiators/shadow-ai`) and the Team
 * Lead's own team-scoped view (`app/team/shadow-ai`) - same convention
 * `AuditEntriesView` already established for the Audit Log / Org Logs split.
 */

import { useEffect, useState } from "react";
import { Badge, DataTable, Modal } from "@/components/ui";
import { getMe, getStoredToken, type ShadowAiReportRowResponse } from "@/lib/api";

/** `null` while resolving. Break-glass token sessions are treated as
 * `"org_admin"` (the token is org_admin-equivalent on every admin surface -
 * same precedent `ConsoleShell` uses for nav selection). */
export function useCallerRole(): "org_admin" | "auditor" | "other" | null {
  const [role, setRole] = useState<"org_admin" | "auditor" | "other" | null>(null);

  useEffect(() => {
    if (getStoredToken()) {
      setRole("org_admin");
      return;
    }
    getMe()
      .then((me) => {
        setRole(me.org_role === "org_admin" ? "org_admin" : me.org_role === "auditor" ? "auditor" : "other");
      })
      .catch(() => setRole("other"));
  }, []);

  return role;
}

export function ShadowAiReportTable({
  rows,
  loading,
}: {
  rows: ShadowAiReportRowResponse[];
  loading: boolean;
}) {
  return (
    <DataTable
      loading={loading}
      rows={rows}
      rowKey={(r) => `${r.user_identifier}:${r.destination_host}`}
      emptyState="No unsanctioned-tool usage detected in this range."
      columns={[
        {
          key: "user",
          header: "User",
          render: (r) => (
            <span>
              {r.user_identifier}
              {!r.linked ? (
                <span className="text-muted" style={{ marginLeft: 6, fontSize: 12 }}>
                  (not linked to a Gatekey user)
                </span>
              ) : null}
              {r.repeat_violator ? (
                <span style={{ marginLeft: 6 }}>
                  <Badge tone="red">Repeat violator</Badge>
                </span>
              ) : null}
            </span>
          ),
        },
        {
          key: "tool",
          header: "Unsanctioned tool",
          render: (r) => (
            <span>
              {r.tool_label} <span className="text-muted">({r.destination_host})</span>
            </span>
          ),
        },
        {
          key: "frequency",
          header: "Frequency",
          align: "right",
          render: (r) => `${r.frequency_per_week.toFixed(1)}x/week`,
        },
        {
          key: "last_seen",
          header: "Last seen",
          align: "right",
          render: (r) => new Date(r.last_seen).toLocaleString(),
        },
      ]}
    />
  );
}

export function ShadowAiPolicyModal({ onClose }: { onClose: () => void }) {
  return (
    <Modal title="Shadow AI - data-handling policy" onClose={onClose} width={640}>
      <div style={{ maxHeight: "70vh", overflowY: "auto", fontSize: 13.5, lineHeight: 1.6 }}>
        <p>
          Review this before enabling Shadow AI Discovery - treat it with the same weight as a
          legal consent screen. Full text: <span className="mono">docs/policy/shadow-ai-data-handling.md</span>.
        </p>
        <h4>What this feature is</h4>
        <p>
          Passive log ingestion, not active monitoring. Gatekey does not watch your network
          traffic itself - your org&apos;s own SASE/proxy tool exports its own connection logs, and
          a lightweight transform sends a normalized batch of events to a Gatekey endpoint. Gatekey
          never installs a browser extension or network agent, and never intercepts live traffic.
        </p>
        <h4>Exactly what is collected</h4>
        <p>
          Only for events whose destination hostname matches the curated allowlist below: the
          <strong> user identifier</strong> your tool reports, the <strong>destination host</strong>{" "}
          (e.g. <span className="mono">chat.openai.com</span>), the <strong>timestamp</strong>, and
          which detection mechanism reported it. Optionally, non-content connection metadata.
        </p>
        <p>
          <strong>Never</strong> collected: full URLs, query strings, or request/response bodies -
          this is a structural guarantee (no database column exists that could hold them), not a
          configuration option. This feature only ever knows <em>that</em> a connection happened,
          never <em>what</em> was in it.
        </p>
        <h4>Data minimization</h4>
        <p>
          Any submitted event whose destination host does not match an enabled row on the known
          AI-tool hostname allowlist is dropped in memory and never written to the database - not
          even in a log line.
        </p>
        <h4>Why this data is collected</h4>
        <p>
          To detect employees bypassing Gatekey&apos;s own governed gateway by connecting directly
          to an unsanctioned AI tool - one that isn&apos;t subject to your org&apos;s budget, DLP,
          residency, or audit policies.
        </p>
        <h4>Retention</h4>
        <p>
          A dedicated, always-finite retention window (default 90 days), separate from audit-log
          and usage-log retention, purged automatically on a schedule. There is no soft-delete - a
          purged row is gone.
        </p>
        <h4>What this feature cannot do</h4>
        <p>
          It cannot perform true inline network blocking or redirection - Gatekey has no presence
          in your SASE/proxy tool&apos;s traffic path. Two opt-in mechanisms exist instead, both
          off by default and both requiring explicit confirmation to enable: an automated{" "}
          <strong>notification</strong> email to the flagged user/their Team Lead, and an outbound{" "}
          <strong>webhook</strong> your own SASE/SOAR tooling can use to enact an actual block on
          its end.
        </p>
        <h4>Who can see this data</h4>
        <ul>
          <li>Org Admin - full org-wide read + all configuration.</li>
          <li>Auditor - full org-wide read-only.</li>
          <li>Team Lead - read-only, scoped to their own team&apos;s members only.</li>
          <li>Member - no access.</li>
        </ul>
        <h4>How to disable</h4>
        <p>
          Stop sending data from your SASE/proxy tool, rotate (invalidate) the ingestion token,
          set enforcement back to detect-only, and existing rows age out via the retention window
          above.
        </p>
      </div>
      <div className="modal-actions">
        <button className="btn btn-primary" onClick={onClose}>
          Close
        </button>
      </div>
    </Modal>
  );
}
