"use client";

/**
 * Dismissible post-setup checklist on the Dashboard: the four steps between
 * "console loads" and "first proxied request", each derived from live data
 * (never a manually-ticked box) and linking to the screen that completes it.
 * Hidden permanently once dismissed, and hidden automatically once every
 * step is done.
 */

import { useEffect, useState } from "react";
import Link from "next/link";
import { listProviders, listServiceAccounts, listTeams, listUsers } from "@/lib/api";
import { useApiQuery } from "@/lib/useApiQuery";

const DISMISS_KEY = "gatekey-onboarding-checklist-dismissed";

interface Step {
  label: string;
  href: string;
  linkText: string;
  done: boolean;
}

export function OnboardingChecklist({ requestCount }: { requestCount: number | null }) {
  const [dismissed, setDismissed] = useState(true); // assume hidden until read
  useEffect(() => {
    try {
      setDismissed(localStorage.getItem(DISMISS_KEY) === "1");
    } catch {
      setDismissed(false);
    }
  }, []);

  const providers = useApiQuery(() => listProviders(), []);
  const users = useApiQuery(() => listUsers(), []);
  const teams = useApiQuery(() => listTeams(), []);
  const serviceAccounts = useApiQuery(() => listServiceAccounts(), []);

  if (dismissed) return null;
  // Wait for the data before rendering, so steps never flicker from
  // unchecked to checked.
  if (providers.data === null || users.data === null || teams.data === null || serviceAccounts.data === null) {
    return null;
  }

  const steps: Step[] = [
    {
      label: "Connect your first provider key",
      href: "/providers",
      linkText: "Providers",
      done: providers.data.length > 0,
    },
    {
      label: "Create a user and a team (with a member budget)",
      href: "/teams",
      linkText: "Teams",
      done: users.data.length > 0 && teams.data.length > 0,
    },
    {
      label: "Create a service-account key for an app",
      href: "/service-accounts",
      linkText: "Service Accounts",
      done: serviceAccounts.data.length > 0,
    },
    {
      label: "Make your first proxied request",
      href: "/service-accounts",
      linkText: "how-to",
      done: (requestCount ?? 0) > 0,
    },
  ];

  if (steps.every((s) => s.done)) return null;

  function dismiss() {
    try {
      localStorage.setItem(DISMISS_KEY, "1");
    } catch {
      // Non-fatal - it just reappears next visit.
    }
    setDismissed(true);
  }

  const doneCount = steps.filter((s) => s.done).length;

  return (
    <div className="checklist-card">
      <div className="checklist-head">
        <span className="checklist-title">
          Getting started &middot; {doneCount}/{steps.length} done
        </span>
        <button type="button" className="btn-link" style={{ fontSize: 12 }} onClick={dismiss}>
          Dismiss
        </button>
      </div>
      {steps.map((s) => (
        <div key={s.label} className={`checklist-item ${s.done ? "done" : ""}`}>
          <span className="checklist-mark" aria-hidden>
            {s.done ? "✓" : ""}
          </span>
          <span className="checklist-text">
            {s.label}
            {!s.done ? (
              <>
                {" "}
                &middot; <Link href={s.href}>{s.linkText}</Link>
              </>
            ) : null}
          </span>
        </div>
      ))}
    </div>
  );
}
