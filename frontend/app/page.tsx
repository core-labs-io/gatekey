"use client";

/**
 * Root landing - also the SSO post-callback target (the backend's
 * /v1/auth/sso/callback 302s to "/" for a resolved user; see backend
 * api/v1/auth.py's shared frontend-route contract). Routing order:
 * stored admin token wins (Phase 1 behavior unchanged), then the session
 * is asked via GET /v1/auth/me and the user is routed by onboarding_status
 * and role. No session and no token -> /login.
 */

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { getMe, getStoredToken, type MeResponse } from "@/lib/api";

function landingPathFor(me: MeResponse): string {
  if (me.onboarding_status === "pending_profile") return "/onboarding/profile";
  if (me.onboarding_status === "pending_approval") return "/onboarding/pending";
  if (me.onboarding_status === "pending_alert_email") return "/onboarding/alert-email";
  if (me.org_role === "org_admin") return "/dashboard";
  if (me.org_role === "auditor") return "/org-usage";
  return "/my-usage";
}

export default function RootPage() {
  const router = useRouter();
  useEffect(() => {
    let cancelled = false;
    if (getStoredToken()) {
      router.replace("/dashboard");
      return;
    }
    getMe()
      .then((me) => {
        if (!cancelled) router.replace(landingPathFor(me));
      })
      .catch(() => {
        if (!cancelled) router.replace("/login");
      });
    return () => {
      cancelled = true;
    };
  }, [router]);
  return null;
}
