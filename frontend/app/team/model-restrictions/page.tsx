"use client";

/**
 * Team Lead - Model Restrictions (Phase 2 FE-5, non-admin UI doc section
 * 7.4): the same ModelRestrictionsCard the admin team detail page renders -
 * narrow-only within the org baseline; org-denied models are absent, not
 * shown-disabled. A widening attempt is rejected by the backend
 * (422 team_model_restricts_org_denied_model) and surfaced verbatim.
 */

import { ConsoleShell } from "@/components/ConsoleShell";
import { ModelRestrictionsCard, TeamSwitcher, useLeadTeams } from "@/components/team-management";

export default function TeamModelRestrictionsPage() {
  const { teams, selected, select, loading, error } = useLeadTeams();

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Model Restrictions</div>
        <TeamSwitcher teams={teams} selected={selected} onSelect={select} />
        {error && !loading ? <div className="banner banner-error">{error}</div> : null}
        {selected ? <ModelRestrictionsCard key={selected} teamId={selected} /> : null}
      </div>
    </ConsoleShell>
  );
}
