"use client";

/**
 * Policy Viewer (Phase 2 FE-9, non-admin UI doc section 8.3) - read-only,
 * org-wide policy surface for Auditor and Org Admin sessions (and the
 * break-glass token): the org model-policy baseline plus every team's
 * further restriction, so an auditor reviews the whole precedence stack
 * without impersonating a user. DLP / access-schedule columns are Phase 3 -
 * deliberately absent. Nothing here is editable by design.
 *
 * Data: GET /v1/admin/model-policy (org baseline) + GET /v1/teams and each
 * team's detail (team_restriction).
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { DataTable } from "@/components/ui";
import {
  ApiError,
  getModelPolicy,
  getTeam,
  listTeams,
  type ModelPolicyResponse,
} from "@/lib/api";

interface TeamPolicyRow {
  id: string;
  name: string;
  restriction: string[] | null;
}

export default function PolicyViewerPage() {
  const [policy, setPolicy] = useState<ModelPolicyResponse | null>(null);
  const [teams, setTeams] = useState<TeamPolicyRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      getModelPolicy(),
      listTeams().then((teamList) =>
        Promise.all(
          teamList.map((t) =>
            getTeam(t.id).then((detail) => ({
              id: t.id,
              name: t.name,
              restriction: detail.team_restriction,
            }))
          )
        )
      ),
    ])
      .then(([policyResult, teamRows]) => {
        if (cancelled) return;
        setPolicy(policyResult);
        setTeams(teamRows);
      })
      .catch((err) => {
        if (!cancelled)
          setError(
            err instanceof ApiError && (err.status === 401 || err.status === 403)
              ? "You do not have access to the org policy surface. This screen is for Auditors and Org Admins."
              : err instanceof ApiError
                ? err.message
                : "Failed to load policy state."
          );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const baselineLabel =
    policy === null
      ? ""
      : policy.mode === "unconfigured"
        ? "Unconfigured - all known models allowed."
        : policy.mode === "allowlist"
          ? `Allowlist - only: ${policy.models.join(", ")}`
          : `Denylist - blocked: ${policy.models.join(", ")}`;

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Policy Viewer</div>
        <div className="page-subtitle">
          The full model-policy precedence stack: org baseline, then per-team restrictions.
          Read-only.
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}

        <div className="panel" style={{ marginBottom: 16 }}>
          <div className="panel-title">Org baseline</div>
          {loading ? (
            <div className="skeleton skeleton-text" />
          ) : (
            <p style={{ margin: 0 }}>{baselineLabel}</p>
          )}
        </div>

        <div className="panel">
          <div className="panel-title">Per-team restrictions</div>
          <DataTable
            loading={loading}
            rows={teams}
            rowKey={(t) => t.id}
            emptyState="No teams yet."
            columns={[
              { key: "team", header: "Team", render: (t) => t.name },
              {
                key: "restriction",
                header: "Further restricts to",
                render: (t) =>
                  t.restriction === null ? (
                    <span className="text-muted">(no further restriction - org baseline applies)</span>
                  ) : t.restriction.length === 0 ? (
                    <span className="text-muted">(all models blocked for this team)</span>
                  ) : (
                    <span className="mono">{t.restriction.join(", ")}</span>
                  ),
              },
            ]}
          />
        </div>
      </div>
    </ConsoleShell>
  );
}
