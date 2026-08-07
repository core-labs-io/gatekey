"use client";

/**
 * Model Access (Phase 2 FE-7, non-admin UI doc section 5) - "which models
 * you can currently use, and why." Every blocked row names the specific
 * policy layer that blocked it (org baseline vs team restriction), never a
 * bare "blocked" - this is the spec's stated non-admin success bar.
 *
 * Session-only (/v1/model-access mirrors the gateway hot path's own layered
 * resolution). Users with 2+ teams pick which membership to evaluate.
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { Badge } from "@/components/ui";
import {
  ApiError,
  getMe,
  getModelAccess,
  getStoredToken,
  type MeTeam,
  type ModelAccessResponse,
} from "@/lib/api";

function blockedReason(layer: "org" | "team", teamName: string | null): string {
  if (layer === "org") return "Blocked - org-wide policy does not allow this model.";
  return `Blocked - restricted by your team${
    teamName ? ` (${teamName})` : ""
  } beyond the org baseline.`;
}

export default function ModelAccessPage() {
  const [tokenMode] = useState(() => Boolean(getStoredToken()));
  const [teams, setTeams] = useState<MeTeam[]>([]);
  const [selectedTeam, setSelectedTeam] = useState<string | null>(null);
  const [data, setData] = useState<ModelAccessResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Resolve memberships once - the selector only appears for 2+ teams.
  useEffect(() => {
    if (tokenMode) return;
    getMe()
      .then((me) => {
        setTeams(me.teams);
        if (me.teams.length >= 2) setSelectedTeam(me.teams[0].team_id);
      })
      .catch((err) => {
        setError(err instanceof ApiError ? err.message : "Failed to resolve your account.");
        setLoading(false);
      });
  }, [tokenMode]);

  useEffect(() => {
    if (tokenMode || error) return;
    // 2+ teams: wait for the selection; 0-1 teams: server auto-resolves.
    if (teams.length >= 2 && !selectedTeam) return;
    let cancelled = false;
    setLoading(true);
    getModelAccess(teams.length >= 2 ? (selectedTeam ?? undefined) : undefined)
      .then((result) => {
        if (!cancelled) setData(result);
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof ApiError ? err.message : "Failed to load model access.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tokenMode, teams, selectedTeam]);

  const teamName = teams.find((t) => t.team_id === (data?.team_id ?? ""))?.team_name ?? null;

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Model Access</div>
        <div className="page-subtitle">Which models you can currently use, and why.</div>

        {tokenMode ? (
          <div className="banner banner-info">
            This is a personal screen. The admin token has no personal identity - sign in
            with SSO to see your own model access (org policy lives on the Model Policy
            screen).
          </div>
        ) : (
          <>
            {teams.length >= 2 ? (
              <div className="field" style={{ maxWidth: 320 }}>
                <label>Team</label>
                <select
                  value={selectedTeam ?? ""}
                  onChange={(e) => setSelectedTeam(e.target.value)}
                >
                  {teams.map((t) => (
                    <option key={t.team_id} value={t.team_id}>
                      {t.team_name}
                    </option>
                  ))}
                </select>
              </div>
            ) : null}

            {error ? <div className="banner banner-error">{error}</div> : null}

            <div className="panel">
              {loading ? (
                <div className="skeleton skeleton-text" style={{ height: 120 }} />
              ) : data ? (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th>Model</th>
                      <th>Access</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.models.map((m) => (
                      <tr key={m.model}>
                        <td className="mono">{m.model}</td>
                        <td>
                          {m.allowed ? (
                            <Badge tone="green">Available</Badge>
                          ) : (
                            <span
                              style={{ display: "inline-flex", alignItems: "center", gap: 8 }}
                            >
                              <Badge tone="red">Blocked</Badge>
                              <span className="text-muted">
                                {blockedReason(m.blocking_layer ?? "org", teamName)}
                              </span>
                            </span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : null}
            </div>
          </>
        )}
      </div>
    </ConsoleShell>
  );
}
