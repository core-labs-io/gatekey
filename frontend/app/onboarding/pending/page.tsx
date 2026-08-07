"use client";

/**
 * Onboarding holding state (ui-requirements-non-admin.md section 2.2, FE-2).
 * Replaces any dashboard/nav until the join request resolves. Shows
 * routed-to copy (Team Lead vs. org-admin fallback), updates in place on
 * rejection with the approver's reason and a "Choose a different team"
 * path back to the profile screen. Approved -> route into the app via "/".
 */

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, getOnboardingStatus, logoutSession, type JoinRequestResponse } from "@/lib/api";

export default function OnboardingPendingPage() {
  const router = useRouter();
  const [request, setRequest] = useState<JoinRequestResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getOnboardingStatus()
      .then((req) => {
        if (cancelled) return;
        if (req.status === "approved") {
          // Membership exists now - "/" routes by /v1/auth/me into the app.
          router.replace("/");
          return;
        }
        setRequest(req);
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          router.replace("/login");
          return;
        }
        if (err instanceof ApiError && err.status === 404) {
          // Never submitted a request - back to profile/team selection.
          router.replace("/onboarding/profile");
          return;
        }
        setError(err instanceof ApiError ? err.message : "Failed to load your request status.");
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  async function handleSignOut() {
    try {
      await logoutSession();
    } catch {
      // Session may already be gone - proceed to login regardless.
    }
    router.replace("/login");
  }

  const teamName = request?.team_name ?? "your selected team";
  const approverCopy =
    request?.routed_to === "org_admin" ? "your org admin" : "that team's Team Lead";

  return (
    <div className="auth-shell">
      <div className="wizard-card" style={{ textAlign: "center" }}>
        {error ? <div className="banner banner-error">{error}</div> : null}
        {!request && !error ? (
          <div className="skeleton skeleton-text" style={{ width: "100%", height: 60 }} />
        ) : null}
        {request?.status === "pending" ? (
          <>
            <div className="auth-brand">⏳ Request submitted</div>
            <p>
              Your request to join <strong>{teamName}</strong> is awaiting approval from{" "}
              {approverCopy}.
            </p>
            <p className="text-muted">
              You&apos;ll get access as soon as it&apos;s approved — no action needed from you right
              now.
            </p>
            <p className="text-muted" style={{ fontSize: 12 }}>
              Submitted {new Date(request.requested_at).toLocaleString()}
            </p>
          </>
        ) : null}
        {request?.status === "rejected" ? (
          <>
            <div className="auth-brand">Request not approved</div>
            <p>
              Your request to join <strong>{teamName}</strong> was rejected.
            </p>
            {request.rejection_reason ? (
              <p className="text-muted">Reason: &ldquo;{request.rejection_reason}&rdquo;</p>
            ) : null}
            <button
              className="btn btn-primary"
              style={{ marginTop: 8 }}
              onClick={() => router.push("/onboarding/profile")}
            >
              Choose a different team
            </button>
          </>
        ) : null}
        <div style={{ marginTop: 20 }}>
          <button type="button" className="btn btn-link" onClick={handleSignOut}>
            Sign out
          </button>
        </div>
      </div>
    </div>
  );
}
