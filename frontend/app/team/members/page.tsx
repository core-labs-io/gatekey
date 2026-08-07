"use client";

/**
 * Team Lead - Members & Budgets (Phase 2 FE-5, non-admin UI doc section
 * 7.3): the Team-Lead-scoped subset of the admin Teams screen. Identical
 * members table + reassignment modal (shared MembersSection from
 * team-management.tsx, per the doc's explicit reuse instruction), just
 * pre-filtered to one led team and without the Add-team / ceiling-edit
 * affordances only an Org Admin has.
 *
 * `users={null}`: there is no org-user-listing endpoint a Team Lead can
 * call, so the add-member modal degrades to a raw user-id input - the
 * normal Team Lead add path is approving a join request.
 */

import { useCallback, useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { MembersSection, TeamSwitcher, useLeadTeams } from "@/components/team-management";
import { ApiError, getTeam, type TeamDetailResponse } from "@/lib/api";

export default function TeamMembersPage() {
  const { teams, selected, select, loading: teamsLoading, error: teamsError } = useLeadTeams();
  const [detail, setDetail] = useState<TeamDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    if (!selected) return;
    setError(null);
    getTeam(selected)
      .then(setDetail)
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Failed to load team members.")
      );
  }, [selected]);

  useEffect(refresh, [refresh]);

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Members &amp; Budgets</div>
        <TeamSwitcher teams={teams} selected={selected} onSelect={select} />
        {teamsError && !teamsLoading ? (
          <div className="banner banner-error">{teamsError}</div>
        ) : null}
        {error ? <div className="banner banner-error">{error}</div> : null}

        {detail && selected ? (
          <MembersSection teamId={selected} detail={detail} users={null} onChanged={refresh} />
        ) : null}
      </div>
    </ConsoleShell>
  );
}
