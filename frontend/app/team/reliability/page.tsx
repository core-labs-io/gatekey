"use client";

/**
 * Team Lead - Reliability & Cost (Phase 4). Own-team-scoped equivalent of
 * the Org Admin's Rate Limiting / Caching Settings / Degradation Policy /
 * failover-override screens - same pattern as
 * app/team/model-restrictions/page.tsx (TeamSwitcher + Card components).
 */

import { ConsoleShell } from "@/components/ConsoleShell";
import {
  TeamCacheSettingsCard,
  TeamDegradationPolicyCard,
  TeamFailoverOverrideCard,
  TeamRateLimitCard,
  TeamSwitcher,
  useLeadTeams,
} from "@/components/team-management";

export default function TeamReliabilityPage() {
  const { teams, selected, select, loading, error } = useLeadTeams();

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Reliability &amp; Cost</div>
        <div className="page-subtitle">
          Failover, rate limiting, caching, and graceful degradation for this team only.
        </div>
        <TeamSwitcher teams={teams} selected={selected} onSelect={select} />
        {error && !loading ? <div className="banner banner-error">{error}</div> : null}
        {selected ? (
          <div className="panel-grid" style={{ gridTemplateColumns: "1fr 1fr" }}>
            <TeamFailoverOverrideCard key={`failover-${selected}`} teamId={selected} />
            <TeamRateLimitCard key={`ratelimit-${selected}`} teamId={selected} />
            <TeamCacheSettingsCard key={`cache-${selected}`} teamId={selected} />
            <TeamDegradationPolicyCard key={`degradation-${selected}`} teamId={selected} />
          </div>
        ) : null}
      </div>
    </ConsoleShell>
  );
}
