"use client";

/**
 * Team detail - Org Admin (Phase 2 FE-4, admin UI doc section 8's team
 * detail page): edit name/ceiling, period config, members table + budget
 * reassignment, model restrictions, alert thresholds (org-admin-only per
 * ADR-fork 8), delete team.
 *
 * Members/restrictions/join-request components are the shared ones from
 * team-management.tsx - the Team Lead screens render the same components.
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { ConsoleShell } from "@/components/ConsoleShell";
import { ConfirmDialog, FieldError, Modal, useToast } from "@/components/ui";
import {
  MembersSection,
  ModelRestrictionsCard,
  TeamAccessScheduleCard,
  TeamDlpOverrideCard,
  TeamResidencyRuleCard,
  computeHeadroom,
  fmtUsd,
} from "@/components/team-management";
import {
  ApiError,
  deleteTeam,
  getTeam,
  listUsers,
  putTeamAlertConfig,
  updateTeam,
  updateTeamPeriodConfig,
  type TeamDetailResponse,
  type UserResponse,
} from "@/lib/api";

const DELETE_BLOCK_REASONS: Record<string, string> = {
  team_has_members: "The team still has members - remove or reassign them first.",
  team_has_join_requests:
    "The team has join-request history attached and cannot be deleted.",
  team_in_use:
    "The team is still referenced by one or more (possibly revoked) API keys and cannot be deleted.",
};

function EditTeamModal({
  detail,
  onClose,
  onSaved,
}: {
  detail: TeamDetailResponse;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(detail.name);
  const [noCeiling, setNoCeiling] = useState(detail.budget_ceiling_usd === null);
  const [ceiling, setCeiling] = useState(detail.budget_ceiling_usd ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      await updateTeam(detail.id, {
        name: name.trim(),
        budget_ceiling_usd: noCeiling ? null : ceiling || "0",
      });
      toast.push("success", "Team updated.");
      onSaved();
    } catch (err) {
      // 422 budget_ceiling_below_current_allocation / budget_ceiling_exceeded
      // surface verbatim (messages carry the live figures).
      setError(err instanceof ApiError ? err.message : "Failed to update team.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Edit team" onClose={onClose}>
      <div className="field">
        <label>Name</label>
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
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
          id="edit-no-ceiling"
          checked={noCeiling}
          onChange={(e) => setNoCeiling(e.target.checked)}
          style={{ width: "auto" }}
        />
        <label htmlFor="edit-no-ceiling" style={{ margin: 0 }}>
          No ceiling (unconstrained)
        </label>
      </div>
      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button className="btn btn-primary" onClick={handleSave} disabled={busy || !name.trim()}>
          {busy ? "Saving..." : "Save"}
        </button>
      </div>
    </Modal>
  );
}

function PeriodConfigCard({
  detail,
  onChanged,
}: {
  detail: TeamDetailResponse;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [periodType, setPeriodType] = useState(detail.period_type);
  const [onPeriodEnd, setOnPeriodEnd] = useState(detail.on_period_end);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      await updateTeamPeriodConfig(detail.id, {
        period_type: periodType,
        on_period_end: onPeriodEnd,
      });
      toast.push("success", "Period configuration saved.");
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save period config.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="panel-title">Budget period</div>
      <p className="text-muted">
        Current period started {new Date(detail.current_period_started_at).toLocaleDateString()}.
      </p>
      <div className="field">
        <label>Period type</label>
        <select
          value={periodType}
          onChange={(e) => setPeriodType(e.target.value as "monthly" | "quarterly")}
        >
          <option value="monthly">Monthly</option>
          <option value="quarterly">Quarterly</option>
        </select>
      </div>
      <div className="field">
        <label>On period end</label>
        <select
          value={onPeriodEnd}
          onChange={(e) => setOnPeriodEnd(e.target.value as "rollover" | "reset")}
        >
          <option value="rollover">Roll over unused budget</option>
          <option value="reset">Reset to zero</option>
        </select>
      </div>
      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-primary" onClick={handleSave} disabled={busy}>
          {busy ? "Saving..." : "Save period config"}
        </button>
      </div>
    </div>
  );
}

function AlertConfigCard({
  detail,
  onChanged,
}: {
  detail: TeamDetailResponse;
  onChanged: () => void;
}) {
  const toast = useToast();
  const cfg = detail.alert_config;
  const [t80, setT80] = useState(cfg.threshold_80_enabled);
  const [t100, setT100] = useState(cfg.threshold_100_enabled);
  const [email, setEmail] = useState(cfg.email_enabled);
  const [webhook, setWebhook] = useState(cfg.webhook_enabled);
  const [webhookUrl, setWebhookUrl] = useState("");
  const [clearUrl, setClearUrl] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      // webhook_url is WRITE-ONLY: omit the key to keep the stored URL,
      // send a string to replace, explicit null to clear. The URL is never
      // readable back - only webhook_configured.
      const body: Parameters<typeof putTeamAlertConfig>[1] = {
        threshold_80_enabled: t80,
        threshold_100_enabled: t100,
        webhook_enabled: webhook,
        email_enabled: email,
      };
      if (clearUrl) body.webhook_url = null;
      else if (webhookUrl.trim()) body.webhook_url = webhookUrl.trim();
      await putTeamAlertConfig(detail.id, body);
      toast.push("success", "Alert configuration saved.");
      setWebhookUrl("");
      setClearUrl(false);
      onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save alert config.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="panel-title">Alert thresholds</div>
      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          id="alert-80"
          checked={t80}
          onChange={(e) => setT80(e.target.checked)}
          style={{ width: "auto" }}
        />
        <label htmlFor="alert-80" style={{ margin: 0 }}>
          Notify at 80% of team spend
        </label>
      </div>
      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          id="alert-100"
          checked={t100}
          onChange={(e) => setT100(e.target.checked)}
          style={{ width: "auto" }}
        />
        <label htmlFor="alert-100" style={{ margin: 0 }}>
          Notify at 100% of team spend
        </label>
      </div>
      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          id="alert-email"
          checked={email}
          onChange={(e) => setEmail(e.target.checked)}
          style={{ width: "auto" }}
        />
        <label htmlFor="alert-email" style={{ margin: 0 }}>
          Email delivery
        </label>
      </div>
      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          id="alert-webhook"
          checked={webhook}
          onChange={(e) => setWebhook(e.target.checked)}
          style={{ width: "auto" }}
        />
        <label htmlFor="alert-webhook" style={{ margin: 0 }}>
          Webhook delivery
        </label>
      </div>
      <div className="field">
        <label>Webhook URL (write-only)</label>
        <input
          type="text"
          value={webhookUrl}
          onChange={(e) => setWebhookUrl(e.target.value)}
          disabled={clearUrl}
          placeholder={
            cfg.webhook_configured
              ? "A webhook URL is configured (never shown) - enter a new one to replace it"
              : "https://hooks.slack.com/..."
          }
        />
        <div className="field-hint">
          {cfg.webhook_configured
            ? "Configured. Leave blank to keep the stored URL."
            : "Not configured."}
        </div>
      </div>
      {cfg.webhook_configured ? (
        <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <input
            type="checkbox"
            id="alert-clear-url"
            checked={clearUrl}
            onChange={(e) => setClearUrl(e.target.checked)}
            style={{ width: "auto" }}
          />
          <label htmlFor="alert-clear-url" style={{ margin: 0 }}>
            Clear the stored webhook URL
          </label>
        </div>
      ) : null}
      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-primary" onClick={handleSave} disabled={busy}>
          {busy ? "Saving..." : "Save alert config"}
        </button>
      </div>
    </div>
  );
}

export default function TeamDetailPage() {
  const params = useParams<{ teamId: string }>();
  const teamId = params.teamId;
  const router = useRouter();
  const toast = useToast();

  const [detail, setDetail] = useState<TeamDetailResponse | null>(null);
  const [users, setUsers] = useState<UserResponse[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  const refresh = useCallback(() => {
    getTeam(teamId)
      .then(setDetail)
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Failed to load team.")
      );
    // The user list feeds the add-member picker; a failure just downgrades
    // the modal to a raw user-id input.
    listUsers()
      .then(setUsers)
      .catch(() => setUsers(null));
  }, [teamId]);

  useEffect(refresh, [refresh]);

  async function handleDelete() {
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await deleteTeam(teamId);
      toast.push("success", "Team deleted.");
      router.replace("/teams");
    } catch (err) {
      if (err instanceof ApiError && DELETE_BLOCK_REASONS[err.code]) {
        setDeleteError(DELETE_BLOCK_REASONS[err.code]);
      } else {
        setDeleteError(err instanceof ApiError ? err.message : "Failed to delete team.");
      }
    } finally {
      setDeleteBusy(false);
    }
  }

  if (error) {
    return (
      <ConsoleShell>
        <div className="page">
          <div className="banner banner-error">{error}</div>
          <Link href="/teams">&larr; Back to Teams</Link>
        </div>
      </ConsoleShell>
    );
  }

  if (!detail) {
    return (
      <ConsoleShell>
        <div className="page">
          <div className="skeleton skeleton-text" style={{ width: "40%" }} />
        </div>
      </ConsoleShell>
    );
  }

  const headroom = computeHeadroom(detail);

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-header-row">
          <div>
            <Link href="/teams">&larr; Teams</Link>
            <div className="page-title">{detail.name}</div>
          </div>
          <span>
            <button className="btn btn-secondary" onClick={() => setEditOpen(true)}>
              Edit team
            </button>{" "}
            <button
              className="btn btn-danger"
              onClick={() => {
                setDeleteError(null);
                setDeleting(true);
              }}
            >
              Delete team
            </button>
          </span>
        </div>

        <p className="text-muted">
          Ceiling:{" "}
          {detail.budget_ceiling_usd === null
            ? "none"
            : `${fmtUsd(detail.budget_ceiling_usd)} / ${detail.period_type}`}{" "}
          &middot; Spend this period: {fmtUsd(detail.current_spend_usd)}
          {headroom.ceiling !== null
            ? ` - allocated to members: $${headroom.allocated.toFixed(2)} of $${headroom.ceiling.toFixed(2)}`
            : ""}
        </p>

        <MembersSection teamId={teamId} detail={detail} users={users} onChanged={refresh} />
        <ModelRestrictionsCard teamId={teamId} />
        <PeriodConfigCard detail={detail} onChanged={refresh} />
        <AlertConfigCard detail={detail} onChanged={refresh} />
        <TeamDlpOverrideCard teamId={teamId} />
        <TeamResidencyRuleCard teamId={teamId} />
        <TeamAccessScheduleCard teamId={teamId} />

        {editOpen ? (
          <EditTeamModal
            detail={detail}
            onClose={() => setEditOpen(false)}
            onSaved={() => {
              setEditOpen(false);
              refresh();
            }}
          />
        ) : null}
        {deleting ? (
          <ConfirmDialog
            title={`Delete team ${detail.name}?`}
            consequence={deleteError ?? "This cannot be undone."}
            confirmLabel="Delete team"
            busy={deleteBusy}
            onCancel={() => setDeleting(false)}
            onConfirm={handleDelete}
          />
        ) : null}
      </div>
    </ConsoleShell>
  );
}
