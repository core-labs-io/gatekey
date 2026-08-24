"use client";

/**
 * First-SSO-login org_admin onboarding: register the org's budget-alert
 * recipient email (added by backend migration `0048`). SSO callback lands
 * an org_admin here when `org_settings.alert_recipient_email` is still
 * unset - see `api/v1/auth.py`'s `_resolve_post_login_redirect`. Product
 * owner's explicit ask: steer toward a group inbox (e.g.
 * ai-alerts@company.com), not the admin's own personal address - the
 * whole point is that alerts keep reaching someone even if this admin
 * changes roles later. Not enforced server-side (no reliable way to tell
 * a group inbox from a personal one), so the steering lives in this
 * copy, not validation.
 */

import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { ApiError, getMe, logoutSession, setOrgAlertEmail } from "@/lib/api";

export default function OnboardingAlertEmailPage() {
  const router = useRouter();
  const [loading, setLoading] = useState(true);
  const [email, setEmail] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getMe()
      .then((me) => {
        if (cancelled) return;
        // Route by live state, never a cached decision (same discipline
        // /onboarding/profile follows).
        if (me.onboarding_status !== "pending_alert_email") {
          router.replace("/");
          return;
        }
        setLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          router.replace("/login");
          return;
        }
        setError(err instanceof ApiError ? err.message : "Failed to load your account.");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!email.trim()) return;
    setSubmitting(true);
    setError(null);
    try {
      await setOrgAlertEmail(email.trim());
      router.replace("/");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save the alert email.");
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
        <div className="auth-subtitle">
          One more thing before you get started: where should budget alerts go?
        </div>
        {error ? <div className="banner banner-error">{error}</div> : null}
        {loading ? (
          <div className="skeleton skeleton-text" style={{ width: "100%", height: 80 }} />
        ) : (
          <form onSubmit={handleSubmit}>
            <div className="field">
              <label htmlFor="alert-email">Alert recipient email</label>
              <input
                id="alert-email"
                type="email"
                placeholder="ai-alerts@yourcompany.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                required
              />
              <div className="field-hint">
                Team and org budget-threshold alerts (50%, 75%, 100%) are sent here. We
                recommend a shared group address rather than your own inbox - alerts should
                keep reaching your team even if you change roles later. You can update this
                anytime from Org Settings.
              </div>
            </div>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                marginTop: 18,
              }}
            >
              <button type="button" className="btn btn-link" onClick={handleSignOut}>
                Sign out
              </button>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={submitting || !email.trim()}
              >
                {submitting ? "Saving..." : "Continue →"}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
