"use client";

/**
 * Login (UI spec section 7.1; Phase 2 FE-1 adds SSO).
 *
 * Two coexisting credentials:
 * - SSO (Phase 2): a plain link to the backend's /v1/auth/sso/login, which
 *   302s to the IdP and, on callback, sets an httpOnly session cookie and
 *   redirects back into this app. The button only renders if a probe shows
 *   SSO is actually configured (the backend 404s otherwise).
 * - Admin token (Phase 1, unchanged): shared bearer token provisioned via
 *   the backend's GATEKEY_ADMIN_TOKEN env var. Validated against a real
 *   authenticated endpoint (GET /v1/admin/providers) and stored
 *   client-side on success.
 */

import { useEffect, useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { ApiError, SSO_LOGIN_URL, listProviders, probeSsoConfigured, setStoredToken } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [token, setToken] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [ssoAvailable, setSsoAvailable] = useState(false);

  useEffect(() => {
    let cancelled = false;
    probeSsoConfigured().then((available) => {
      if (!cancelled) setSsoAvailable(available);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const providers = await listProviders(token);
      setStoredToken(token);
      const anyConfigured = providers.some((p) => p.configured);
      router.replace(anyConfigured ? "/dashboard" : "/setup");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        setError("Invalid admin token");
      } else {
        setError(err instanceof Error ? err.message : "Something went wrong.");
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="auth-shell">
      <div className="auth-card">
        <div className="auth-brand">Gatekey</div>
        <div className="auth-subtitle">Sign in to your gateway</div>
        {ssoAvailable ? (
          <>
            <a
              className="btn btn-primary"
              href={SSO_LOGIN_URL}
              style={{ display: "block", width: "100%", textAlign: "center", textDecoration: "none", boxSizing: "border-box" }}
            >
              Sign in with SSO
            </a>
            <div className="auth-subtitle" style={{ margin: "14px 0" }}>
              or use an admin token
            </div>
          </>
        ) : null}
        <form onSubmit={handleSubmit}>
          <div className="field">
            <label htmlFor="admin-token">Admin token</label>
            <input
              id="admin-token"
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoFocus
              required
              style={error ? { borderColor: "var(--red)" } : undefined}
            />
            {error ? <div className="field-error">{error}</div> : null}
          </div>
          <button type="submit" className="btn btn-primary" style={{ width: "100%" }} disabled={submitting}>
            {submitting ? "Signing in..." : "Sign in"}
          </button>
        </form>
      </div>
    </div>
  );
}
