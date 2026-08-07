"use client";

/**
 * Team Lead - Join Requests queue (Phase 2 FE-5, non-admin UI doc section
 * 7.1). Pending requests with the shared approve/reject modals (the same
 * components the Org Admin escalation queue uses), plus a recently-resolved
 * list. Scoped by the team switcher to teams the session actually leads
 * (from /v1/auth/me) - the backend additionally enforces require_team_role.
 */

import { useCallback, useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import {
  ApproveJoinRequestModal,
  RejectJoinRequestModal,
  TeamSwitcher,
  computeHeadroom,
  fmtUsd,
  useLeadTeams,
} from "@/components/team-management";
import {
  ApiError,
  getTeam,
  listTeamJoinRequests,
  type JoinRequestResponse,
  type TeamDetailResponse,
} from "@/lib/api";

export default function JoinRequestsPage() {
  const { teams, selected, select, loading: teamsLoading, error: teamsError } = useLeadTeams();
  const [requests, setRequests] = useState<JoinRequestResponse[]>([]);
  const [detail, setDetail] = useState<TeamDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [approving, setApproving] = useState<JoinRequestResponse | null>(null);
  const [rejecting, setRejecting] = useState<JoinRequestResponse | null>(null);

  const refresh = useCallback(() => {
    if (!selected) return;
    setError(null);
    Promise.all([listTeamJoinRequests(selected), getTeam(selected)])
      .then(([rows, teamDetail]) => {
        setRequests(rows);
        setDetail(teamDetail);
      })
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Failed to load join requests.")
      );
  }, [selected]);

  useEffect(refresh, [refresh]);

  const pending = requests.filter((r) => r.status === "pending");
  const resolved = requests.filter((r) => r.status !== "pending");
  const headroom = detail ? computeHeadroom(detail) : null;

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Join Requests</div>
        <TeamSwitcher teams={teams} selected={selected} onSelect={select} />
        {teamsError && !teamsLoading ? (
          <div className="banner banner-error">{teamsError}</div>
        ) : null}
        {error ? <div className="banner banner-error">{error}</div> : null}

        {detail && headroom ? (
          <p className="text-muted">
            {headroom.ceiling === null
              ? "This team has no budget ceiling."
              : `Team unallocated budget: $${headroom.unallocated!.toFixed(2)} of $${headroom.ceiling.toFixed(2)} ceiling`}
          </p>
        ) : null}

        {selected ? (
          <>
            <div className="panel">
              <div className="panel-title">Pending</div>
              {pending.length === 0 ? (
                <p className="text-muted">No pending requests.</p>
              ) : (
                pending.map((r) => (
                  <div key={r.id} className="page-header-row" style={{ marginBottom: 8 }}>
                    <span>
                      {r.requester_name} &middot;{" "}
                      <span className="text-muted">
                        requested {new Date(r.requested_at).toLocaleString()}
                      </span>
                    </span>
                    <span>
                      <button className="btn btn-secondary" onClick={() => setRejecting(r)}>
                        Reject
                      </button>{" "}
                      <button className="btn btn-primary" onClick={() => setApproving(r)}>
                        Approve
                      </button>
                    </span>
                  </div>
                ))
              )}
            </div>

            {resolved.length > 0 ? (
              <div className="panel">
                <div className="panel-title">Recently resolved</div>
                {resolved.map((r) => (
                  <div key={r.id} style={{ marginBottom: 6 }}>
                    {r.requester_name} &middot;{" "}
                    {r.status === "approved" ? (
                      <>
                        approved{" "}
                        {r.resolved_at ? new Date(r.resolved_at).toLocaleDateString() : ""} &middot;{" "}
                        {fmtUsd(r.approved_budget_usd)} allocated
                      </>
                    ) : (
                      <>
                        rejected{" "}
                        {r.resolved_at ? new Date(r.resolved_at).toLocaleDateString() : ""}
                        {r.rejection_reason ? (
                          <span className="text-muted"> &middot; &quot;{r.rejection_reason}&quot;</span>
                        ) : null}
                      </>
                    )}
                  </div>
                ))}
              </div>
            ) : null}
          </>
        ) : null}

        {approving && selected ? (
          <ApproveJoinRequestModal
            teamId={selected}
            request={approving}
            onClose={() => setApproving(null)}
            onApproved={() => {
              setApproving(null);
              refresh();
            }}
          />
        ) : null}
        {rejecting && selected ? (
          <RejectJoinRequestModal
            teamId={selected}
            request={rejecting}
            onClose={() => setRejecting(null)}
            onRejected={() => {
              setRejecting(null);
              refresh();
            }}
          />
        ) : null}
      </div>
    </ConsoleShell>
  );
}
