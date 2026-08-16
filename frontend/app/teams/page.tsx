"use client";

/**
 * Teams & Users - Org Admin (Phase 2 FE-4, admin UI doc section 8).
 *
 * Top-to-bottom per the doc's layout: escalated join requests needing
 * org-admin action (target team has no Team Lead, or pending >= 5 business
 * days), the team list, and the org-wide settings section (ceiling +
 * personal-key settings, admin UI doc section 15's org-level toggles).
 *
 * Session-only surface: /v1/teams and /v1/admin/org-settings never accept
 * the Phase 1 break-glass bearer, so this route only appears in the
 * org_admin session nav.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { ConsoleShell } from "@/components/ConsoleShell";
import { Badge, DataTable, FieldError, Modal, useToast } from "@/components/ui";
import {
  ApproveJoinRequestModal,
  RejectJoinRequestModal,
  fmtUsd,
} from "@/components/team-management";
import {
  ApiError,
  createTeam,
  getAdminJoinRequestQueue,
  getOrgSettings,
  listTeams,
  putOrgSettings,
  type AdminJoinRequestQueueEntry,
  type OrgSettingsResponse,
  type TeamResponse,
} from "@/lib/api";

const ESCALATION_LABELS: Record<AdminJoinRequestQueueEntry["escalation_reason"], string> = {
  no_team_lead: "no Team Lead assigned yet",
  pending_over_5_business_days: "pending over 5 business days",
};

function CreateTeamModal({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [ceiling, setCeiling] = useState("");
  const [noCeiling, setNoCeiling] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleCreate() {
    setBusy(true);
    setError(null);
    try {
      await createTeam({
        name: name.trim(),
        budget_ceiling_usd: noCeiling ? null : ceiling || "0",
      });
      toast.push("success", `Team "${name.trim()}" created.`);
      onSaved();
    } catch (err) {
      // 422 budget_ceiling_exceeded (org ceiling headroom) surfaces verbatim.
      setError(err instanceof ApiError ? err.message : "Failed to create team.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Add team" onClose={onClose}>
      <div className="field">
        <label>Name</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. ml-platform"
        />
      </div>
      <div className="field">
        <label>Budget ceiling (USD)</label>
        <input
          type="text"
          value={noCeiling ? "" : ceiling}
          onChange={(e) => setCeiling(e.target.value)}
          disabled={noCeiling}
          placeholder="2500.00"
        />
      </div>
      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          id="no-ceiling"
          checked={noCeiling}
          onChange={(e) => setNoCeiling(e.target.checked)}
          style={{ width: "auto" }}
        />
        <label htmlFor="no-ceiling" style={{ margin: 0 }}>
          No ceiling (unconstrained)
        </label>
      </div>
      <p className="field-hint">
        Model restrictions, members, alerts and period config are edited on the team&apos;s
        detail page after creation.
      </p>
      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button className="btn btn-primary" onClick={handleCreate} disabled={busy || !name.trim()}>
          {busy ? "Creating..." : "Create team"}
        </button>
      </div>
    </Modal>
  );
}

function OrgSettingsSection() {
  const toast = useToast();
  const [settings, setSettings] = useState<OrgSettingsResponse | null>(null);
  const [ceiling, setCeiling] = useState("");
  const [noCeiling, setNoCeiling] = useState(false);
  const [softCap, setSoftCap] = useState("3");
  const [maxExpiry, setMaxExpiry] = useState("");
  const [autoProvision, setAutoProvision] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getOrgSettings()
      .then((data) => {
        if (cancelled) return;
        setSettings(data);
        setCeiling(data.budget_ceiling_usd ?? "");
        setNoCeiling(data.budget_ceiling_usd === null);
        setSoftCap(String(data.personal_key_soft_cap));
        setMaxExpiry(
          data.max_self_serve_key_expiration_days === null
            ? ""
            : String(data.max_self_serve_key_expiration_days)
        );
        setAutoProvision(data.auto_provision_personal_key_on_approval);
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof ApiError ? err.message : "Failed to load org settings.");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      const saved = await putOrgSettings({
        budget_ceiling_usd: noCeiling ? null : ceiling || "0",
        currency: "USD",
        max_self_serve_key_expiration_days: maxExpiry.trim() === "" ? null : Number(maxExpiry),
        personal_key_soft_cap: Number(softCap) || 1,
        auto_provision_personal_key_on_approval: autoProvision,
      });
      setSettings(saved);
      toast.push("success", "Org settings saved.");
    } catch (err) {
      // 422 budget_ceiling_below_current_allocation surfaces verbatim - the
      // message names the current sum of team ceilings.
      setError(err instanceof ApiError ? err.message : "Failed to save org settings.");
    } finally {
      setBusy(false);
    }
  }

  if (settings === null && error === null) return null;

  return (
    <div className="panel">
      <div className="panel-title">Org settings</div>
      {settings ? (
        <>
          <div className="field">
            <label>Org-wide budget ceiling (USD / period)</label>
            <input
              type="text"
              value={noCeiling ? "" : ceiling}
              onChange={(e) => setCeiling(e.target.value)}
              disabled={noCeiling}
              placeholder="5000.00"
            />
          </div>
          <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="checkbox"
              id="org-no-ceiling"
              checked={noCeiling}
              onChange={(e) => setNoCeiling(e.target.checked)}
              style={{ width: "auto" }}
            />
            <label htmlFor="org-no-ceiling" style={{ margin: 0 }}>
              No org ceiling
            </label>
          </div>
          <div className="field">
            <label>Personal keys per user (soft cap)</label>
            <input
              type="number"
              min={1}
              value={softCap}
              onChange={(e) => setSoftCap(e.target.value)}
            />
          </div>
          <div className="field">
            <label>Max self-serve key expiration (days, blank = no max)</label>
            <input
              type="number"
              min={1}
              value={maxExpiry}
              onChange={(e) => setMaxExpiry(e.target.value)}
            />
          </div>
          <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="checkbox"
              id="auto-provision"
              checked={autoProvision}
              onChange={(e) => setAutoProvision(e.target.checked)}
              style={{ width: "auto" }}
            />
            <label htmlFor="auto-provision" style={{ margin: 0 }}>
              Auto-provision a personal key when a join request is approved
            </label>
          </div>
        </>
      ) : null}
      <FieldError message={error} />
      {settings ? (
        <div className="modal-actions">
          <button className="btn btn-primary" onClick={handleSave} disabled={busy}>
            {busy ? "Saving..." : "Save org settings"}
          </button>
        </div>
      ) : null}
    </div>
  );
}

export default function TeamsPage() {
  const [teams, setTeams] = useState<TeamResponse[]>([]);
  const [queue, setQueue] = useState<AdminJoinRequestQueueEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [approving, setApproving] = useState<AdminJoinRequestQueueEntry | null>(null);
  const [rejecting, setRejecting] = useState<AdminJoinRequestQueueEntry | null>(null);

  const refresh = useCallback(() => {
    setLoading(true);
    setError(null);
    Promise.all([listTeams(), getAdminJoinRequestQueue()])
      .then(([teamRows, queueRows]) => {
        setTeams(teamRows);
        setQueue(queueRows);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load teams."))
      .finally(() => setLoading(false));
  }, []);

  useEffect(refresh, [refresh]);

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-header-row">
          <div className="page-title">Teams &amp; Users</div>
          <button className="btn btn-primary" onClick={() => setCreateOpen(true)}>
            + Add team
          </button>
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}

        {queue.length > 0 ? (
          <div className="panel">
            <div className="panel-title">Join requests needing your action</div>
            <p className="text-muted">
              Shown here only when the target team has no Team Lead (or the request has
              waited too long) - otherwise the team&apos;s own Team Lead handles it.
            </p>
            {queue.map((entry) => (
              <div key={entry.id} className="page-header-row" style={{ marginBottom: 8 }}>
                <span>
                  {entry.requester_name} &rarr; {entry.team_name ?? entry.team_id} &middot;{" "}
                  <span className="text-muted">{ESCALATION_LABELS[entry.escalation_reason]}</span>
                </span>
                <span>
                  <button className="btn btn-secondary" onClick={() => setRejecting(entry)}>
                    Reject
                  </button>{" "}
                  <button className="btn btn-primary" onClick={() => setApproving(entry)}>
                    Approve &amp; allocate
                  </button>
                </span>
              </div>
            ))}
          </div>
        ) : null}

        <DataTable
          loading={loading}
          rows={teams}
          rowKey={(t) => t.id}
          emptyState="No teams yet. Add one to start delegating budgets."
          searchText={(t) => t.name}
          searchPlaceholder="Filter teams..."
          initialSort={{ key: "name", dir: "asc" }}
          columns={[
            {
              key: "name",
              header: "Team",
              sortValue: (t) => t.name,
              render: (t) => <Link href={`/teams/${t.id}`}>{t.name}</Link>,
            },
            {
              key: "spend",
              header: "Spend",
              align: "right",
              render: (t) => fmtUsd(t.current_spend_usd),
            },
            {
              key: "ceiling",
              header: "Ceiling",
              align: "right",
              render: (t) =>
                t.budget_ceiling_usd === null ? "No ceiling" : fmtUsd(t.budget_ceiling_usd),
            },
            {
              key: "period",
              header: "Period",
              render: (t) => (
                <>
                  <Badge tone="gray">{t.period_type}</Badge>{" "}
                  <span className="text-muted">
                    {t.on_period_end === "rollover" ? "rolls over" : "resets"}
                  </span>
                </>
              ),
            },
            {
              key: "detail",
              header: "",
              align: "right",
              render: (t) => <Link href={`/teams/${t.id}`}>Manage &rarr;</Link>,
            },
          ]}
        />
        <p className="text-muted">
          Member counts, allocation and restrictions live on each team&apos;s detail page.
        </p>

        <OrgSettingsSection />

        {createOpen ? (
          <CreateTeamModal
            onClose={() => setCreateOpen(false)}
            onSaved={() => {
              setCreateOpen(false);
              refresh();
            }}
          />
        ) : null}
        {approving ? (
          <ApproveJoinRequestModal
            teamId={approving.team_id}
            request={approving}
            onClose={() => setApproving(null)}
            onApproved={() => {
              setApproving(null);
              refresh();
            }}
          />
        ) : null}
        {rejecting ? (
          <RejectJoinRequestModal
            teamId={rejecting.team_id}
            request={rejecting}
            onClose={() => setRejecting(null)}
            onRejected={() => {
              setRejecting(null);
              refresh();
            }}
          />
        ) : null}
      </div>
    </ConsoleShell>
  );
}
