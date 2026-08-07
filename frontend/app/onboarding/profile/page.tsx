"use client";

/**
 * First-time setup: profile & team selection (ui-requirements-non-admin.md
 * section 2.1, FE-2). SSO callback lands here when the user has no role, no
 * membership, and no pending join request. Full name pre-fills from the IdP
 * claim (editable); team is a single-select of admin-managed teams only.
 * Submitting creates a pending join request (always Member role - role
 * promotion is Org-Admin-only) and moves to /onboarding/pending.
 */

import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import {
  ApiError,
  getMe,
  listOnboardingTeams,
  logoutSession,
  submitJoinRequest,
  type OnboardingTeam,
} from "@/lib/api";

export default function OnboardingProfilePage() {
  const router = useRouter();
  const [loading, setLoading] = useState(true);
  const [teams, setTeams] = useState<OnboardingTeam[]>([]);
  const [fullName, setFullName] = useState("");
  const [teamId, setTeamId] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    Promise.all([getMe(), listOnboardingTeams()])
      .then(([me, teamList]) => {
        if (cancelled) return;
        // Route by live state, never a cached decision (design doc 2.1 step 5).
        if (me.onboarding_status === "resolved") {
          router.replace("/");
          return;
        }
        if (me.onboarding_status === "pending_approval") {
          router.replace("/onboarding/pending");
          return;
        }
        setFullName(me.name);
        setTeams(teamList);
        setLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          router.replace("/login");
          return;
        }
        setError(err instanceof ApiError ? err.message : "Failed to load onboarding data.");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!fullName.trim() || !teamId) return;
    setSubmitting(true);
    setError(null);
    try {
      await submitJoinRequest({ full_name: fullName.trim(), team_id: teamId });
      router.replace("/onboarding/pending");
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // Already have a pending request (AC6.4) - the holding screen is
        // the right place to be, not an error.
        router.replace("/onboarding/pending");
        return;
      }
      setError(err instanceof ApiError ? err.message : "Failed to submit your request.");
      setSubmitting(false);
    }
  }

  async function handleSignOut() {
    try {
      await logoutSession();
    } catch {
      // Session may already be gone - proceed to login regardless.
    }
    router.replace("/login");
  }

  return (
    <div className="auth-shell">
      <div className="wizard-card">
        <div className="auth-brand">Welcome to Gatekey</div>
        <div className="auth-subtitle">A couple of details before you can get started.</div>
        {error ? <div className="banner banner-error">{error}</div> : null}
        {loading ? (
          <div className="skeleton skeleton-text" style={{ width: "100%", height: 80 }} />
        ) : (
          <form onSubmit={handleSubmit}>
            <div className="field">
              <label htmlFor="full-name">Full name</label>
              <input
                id="full-name"
                type="text"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                required
              />
            </div>
            <div className="field">
              <label htmlFor="team">Team</label>
              <select id="team" value={teamId} onChange={(e) => setTeamId(e.target.value)} required>
                <option value="" disabled>
                  Select your team...
                </option>
                {teams.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.name}
                  </option>
                ))}
              </select>
              <div className="field-hint">
                This list is set by your org admin. Don&apos;t see your team? Ask your admin to add it
                first.
              </div>
            </div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: 18 }}>
              <button type="button" className="btn btn-link" onClick={handleSignOut}>
                Sign out
              </button>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={submitting || !fullName.trim() || !teamId}
              >
                {submitting ? "Submitting..." : "Submit for approval →"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
