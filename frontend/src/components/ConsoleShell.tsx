"use client";

/**
 * Global shell (admin UI spec section 6; Phase 2 FE-3 adds role-based nav
 * per ui-requirements-non-admin.md section 3): persistent left sidebar +
 * top bar, content area on the right. Doubles as the auth guard for every
 * console screen.
 *
 * Two auth paths, checked in order:
 * 1. Stored admin token (Phase 1 break-glass) - the backend accepts it as
 *    org_admin-equivalent on every admin/team endpoint, so token-mode nav
 *    is the full Org Admin nav (incl. Teams / Audit Log / Identity). It has
 *    no personal identity, so the personal screens (My Usage / My API Keys
 *    / Model Access) never appear in token mode.
 * 2. Session cookie (Phase 2 SSO) - GET /v1/auth/me resolves identity and
 *    roles; nav variant is computed from org_role + teams[].role and a
 *    non-resolved onboarding_status bounces to the onboarding screens
 *    (a pending user has no console access - UI doc section 2.2).
 * Neither -> /login.
 *
 * RBAC in the UI: a role only ever sees its own nav entries - Team Lead
 * extras render only when the session actually holds a team_lead
 * membership; Auditor gets the read-only org nav with no member/admin
 * entries; controls a role shouldn't see are never rendered.
 */

import { useEffect, useState, type ReactNode } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { clearStoredToken, getMe, getStoredToken, logoutSession } from "@/lib/api";
import { ThemeToggle } from "@/components/ui";

interface NavGroup {
  label?: string;
  items: { href: string; label: string }[];
}

// Org Admin nav - used by both org_admin sessions and the break-glass token
// (token admins must be able to reach every Phase 2 admin screen; personal
// screens are deliberately absent since the token has no personal identity).
const ORG_ADMIN_NAV: NavGroup[] = [
  {
    items: [
      { href: "/dashboard", label: "Dashboard" },
      { href: "/providers", label: "Providers" },
      { href: "/users", label: "Users" },
      { href: "/service-accounts", label: "Service Accounts" },
      { href: "/model-policy", label: "Model Policy" },
      { href: "/teams", label: "Teams" },
      { href: "/audit-log", label: "Audit Log" },
      { href: "/identity", label: "Identity & Access" },
    ],
  },
  // Phase 3 (Security & Compliance) - org-admin-configured controls only.
  {
    label: "Compliance",
    items: [
      { href: "/compliance/dlp", label: "Compliance & DLP" },
      { href: "/compliance/residency", label: "Residency Rules" },
      { href: "/compliance/rotation", label: "Rotation Policy" },
      { href: "/compliance/access-windows", label: "Access Windows" },
      { href: "/compliance/settings", label: "Compliance Settings" },
    ],
  },
  // Phase 4 (Reliability & Cost Efficiency) - org-admin-configured controls.
  {
    label: "Reliability & Cost",
    items: [
      { href: "/rate-limiting", label: "Rate Limiting" },
      { href: "/caching", label: "Caching Settings" },
      { href: "/degradation-policy", label: "Degradation Policy" },
      { href: "/backup-groups", label: "Backup Groups" },
      { href: "/failover-events", label: "Failover Events" },
    ],
  },
  // AI oversight - Shadow AI/Drift Detector config = Org Admin
  // only; Self-Hosted Governance's actual CRUD lives on the Providers
  // screen, this is the read-only cost-normalization cross-link tab.
  {
    label: "AI Oversight",
    items: [
      { href: "/differentiators/shadow-ai", label: "Shadow AI" },
      { href: "/differentiators/drift-detector", label: "Drift Detector" },
      { href: "/differentiators/self-hosted", label: "Self-Hosted Governance" },
    ],
  },
];

const AUDITOR_NAV: NavGroup[] = [
  {
    items: [
      { href: "/org-usage", label: "Org Usage" },
      { href: "/org-logs", label: "Org Logs" },
      { href: "/policy-viewer", label: "Policy Viewer" },
    ],
  },
  // AI oversight - Auditor gets the identical read-only view
  // of each screen (require_admin_or_auditor backend-side); the pages
  // themselves hide every write control for a non-org_admin session.
  {
    label: "AI Oversight",
    items: [
      { href: "/differentiators/shadow-ai", label: "Shadow AI" },
      { href: "/differentiators/drift-detector", label: "Drift Detector" },
      { href: "/differentiators/self-hosted", label: "Self-Hosted Governance" },
    ],
  },
];

const MEMBER_ITEMS = [
  { href: "/my-usage", label: "My Usage" },
  { href: "/model-access", label: "Model Access" },
  { href: "/my-keys", label: "My API Keys" },
];

// Team Lead = Member nav + "My Team" section (additive, per UI doc section
// 1). Access Schedule (P3) and Budget Marketplace (P6) are later phases -
// deliberately omitted.
const TEAM_LEAD_NAV: NavGroup[] = [
  { items: MEMBER_ITEMS },
  {
    label: "My Team",
    items: [
      { href: "/team/join-requests", label: "Join Requests" },
      { href: "/team/dashboard", label: "Team Dashboard" },
      { href: "/team/members", label: "Members & Budgets" },
      { href: "/team/model-restrictions", label: "Model Restrictions" },
      { href: "/team/reliability", label: "Reliability & Cost" },
      // Phase 5 (5.1 Shadow AI, AC5.1.6): read-only, server-side scoped to
      // this Team Lead's own led team(s) only - never another team's data.
      { href: "/team/shadow-ai", label: "Shadow AI" },
    ],
  },
];

const MEMBER_NAV: NavGroup[] = [{ items: MEMBER_ITEMS }];

interface ShellIdentity {
  nav: NavGroup[];
  label: string;
  session: boolean;
}

export function ConsoleShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [identity, setIdentity] = useState<ShellIdentity | null>(null);
  // Off-canvas drawer state (only relevant under the 900px breakpoint -
  // above it the sidebar is always visible and this class is inert).
  const [navOpen, setNavOpen] = useState(false);
  // Collapsed nav groups, persisted so an admin who tucks away rarely-used
  // sections keeps that layout across visits.
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  useEffect(() => {
    try {
      const raw = localStorage.getItem("gatekey-nav-collapsed");
      if (raw) setCollapsed(JSON.parse(raw));
    } catch {
      // Corrupt/unavailable storage - fall back to everything expanded.
    }
  }, []);

  function toggleGroup(label: string) {
    setCollapsed((prev) => {
      const next = { ...prev, [label]: !prev[label] };
      try {
        localStorage.setItem("gatekey-nav-collapsed", JSON.stringify(next));
      } catch {
        // Non-fatal.
      }
      return next;
    });
  }

  // Navigating always closes the drawer (mobile).
  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  useEffect(() => {
    let cancelled = false;
    if (getStoredToken()) {
      setIdentity({ nav: ORG_ADMIN_NAV, label: "admin", session: false });
      return;
    }
    getMe()
      .then((me) => {
        if (cancelled) return;
        if (me.onboarding_status === "pending_profile") {
          router.replace("/onboarding/profile");
          return;
        }
        if (me.onboarding_status === "pending_approval") {
          router.replace("/onboarding/pending");
          return;
        }
        const nav =
          me.org_role === "org_admin"
            ? ORG_ADMIN_NAV
            : me.org_role === "auditor"
              ? AUDITOR_NAV
              : me.teams.some((t) => t.role === "team_lead")
                ? TEAM_LEAD_NAV
                : MEMBER_NAV;
        setIdentity({ nav, label: me.name || me.email || "user", session: true });
      })
      .catch(() => {
        if (!cancelled) router.replace("/login");
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  async function handleSignOut() {
    if (identity?.session) {
      try {
        await logoutSession();
      } catch {
        // Session may already be expired - proceed to login regardless.
      }
    } else {
      clearStoredToken();
    }
    router.replace("/login");
  }

  if (!identity) return null;

  return (
    <div className={`shell ${navOpen ? "sidebar-open" : ""}`}>
      <aside className="sidebar">
        <div className="sidebar-brand">
          <span aria-hidden>&#9860;</span> Gatekey
        </div>
        <nav className="sidebar-nav" aria-label="Main navigation">
          {identity.nav.map((group, i) => {
            const isCollapsed = group.label ? Boolean(collapsed[group.label]) : false;
            return (
              <div key={group.label ?? i}>
                {group.label ? (
                  <button
                    type="button"
                    className="sidebar-section"
                    aria-expanded={!isCollapsed}
                    onClick={() => toggleGroup(group.label!)}
                  >
                    {group.label}
                    <span className="chevron" aria-hidden>
                      &#9662;
                    </span>
                  </button>
                ) : null}
                {isCollapsed
                  ? null
                  : group.items.map((item) => (
                      <Link
                        key={item.href}
                        href={item.href}
                        className={`sidebar-link ${pathname?.startsWith(item.href) ? "active" : ""}`}
                        aria-current={pathname?.startsWith(item.href) ? "page" : undefined}
                      >
                        {item.label}
                      </Link>
                    ))}
              </div>
            );
          })}
        </nav>
        <div className="sidebar-footer">v0.1.0</div>
      </aside>
      {navOpen ? (
        <button
          type="button"
          className="sidebar-backdrop"
          aria-label="Close navigation"
          onClick={() => setNavOpen(false)}
        />
      ) : null}
      <div className="content">
        <div className="topbar">
          <button
            type="button"
            className="icon-btn nav-toggle"
            aria-label={navOpen ? "Close navigation" : "Open navigation"}
            aria-expanded={navOpen}
            onClick={() => setNavOpen((v) => !v)}
          >
            <span aria-hidden>&#9776;</span>
          </button>
          <span style={{ flex: 1 }} className="nav-toggle-spacer" />
          <span className="topbar-identity">{identity.label}</span>
          <ThemeToggle />
          <button className="btn btn-secondary" onClick={handleSignOut}>
            Sign out
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
