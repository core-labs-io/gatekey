"use client";

/**
 * My API Keys (Phase 2 FE-6, non-admin UI doc section 6) - self-serve
 * create/regenerate/revoke of the caller's own personal keys via the
 * session-only /v1/keys routes. Team is auto-selected when the caller holds
 * exactly one membership, else a dropdown from /v1/auth/me. Backend 422s
 * (soft cap, org max expiry) surface verbatim. CLI Auto-Sync (section 6.1)
 * is Phase 3.7a - deliberately absent.
 */

import { useCallback, useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { PersonalKeyManager } from "@/components/personal-keys";
import {
  ApiError,
  createMyKey,
  getMe,
  getStoredToken,
  listMyKeys,
  regenerateMyKey,
  revokeMyKey,
  type MeTeam,
  type PersonalApiKeyResponse,
} from "@/lib/api";

export default function MyKeysPage() {
  const [tokenMode] = useState(() => Boolean(getStoredToken()));
  const [teams, setTeams] = useState<MeTeam[]>([]);
  const [keys, setKeys] = useState<PersonalApiKeyResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(() => {
    if (tokenMode) return;
    setError(null);
    Promise.all([listMyKeys(), getMe()])
      .then(([keyRows, me]) => {
        setKeys(keyRows);
        setTeams(me.teams);
      })
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Failed to load your API keys.")
      )
      .finally(() => setLoading(false));
  }, [tokenMode]);

  useEffect(refresh, [refresh]);

  const teamNames = Object.fromEntries(teams.map((t) => [t.team_id, t.team_name]));

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">My API Keys</div>
        <div className="page-subtitle">
          Keys are bound by your account&apos;s model access and budget - creating a key never
          grants more than you already have.
        </div>

        {tokenMode ? (
          <div className="banner banner-info">
            This is a personal screen. The admin token has no personal identity - sign in
            with SSO to manage your own keys.
          </div>
        ) : (
          <>
            {error ? <div className="banner banner-error">{error}</div> : null}
            {!loading && teams.length === 0 ? (
              <div className="banner banner-info">
                You need a team membership before you can create a key - keys inherit their
                team&apos;s budget and policy context.
              </div>
            ) : null}
            <div className="panel">
              <PersonalKeyManager
                keys={keys}
                loading={loading}
                createTeams={teams.map((t) => ({ team_id: t.team_id, team_name: t.team_name }))}
                teamNameFor={(id) => teamNames[id] ?? id}
                onChanged={refresh}
                api={{
                  create: createMyKey,
                  regenerate: regenerateMyKey,
                  revoke: revokeMyKey,
                }}
              />
            </div>
          </>
        )}
      </div>
    </ConsoleShell>
  );
}
