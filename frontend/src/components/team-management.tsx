"use client";

/**
 * Shared team-management components (Phase 2 FE-4/FE-5).
 *
 * The non-admin UI doc (section 7.3/7.4) explicitly requires the Team Lead
 * screens to reuse the Org Admin Teams screen's members table, reassignment
 * modal, join-request approve/reject modals, and model-restrictions card
 * "as-is" rather than redesigning them - so they all live here once and are
 * rendered from both app/teams/[teamId] (admin) and app/team/* (team lead).
 *
 * Budget-ceiling enforcement is surfaced, not re-implemented: the input
 * shows the live unallocated headroom as a hint, and a backend 422
 * (budget_ceiling_exceeded - message carries the authoritative headroom,
 * re-derived under the row lock) is displayed verbatim as the field error.
 */

import { useEffect, useState } from "react";
import {
  ApiError,
  addTeamMember,
  approveJoinRequest,
  clearCache,
  createTeamRateLimitRule,
  deleteTeamAccessSchedule,
  deleteTeamRateLimitRule,
  deleteTeamResidencyRule,
  getMe,
  getTeam,
  getTeamAccessSchedule,
  getTeamCacheSettings,
  getTeamDegradationPolicy,
  getTeamDlpOverride,
  getTeamFailoverOverride,
  getMemberModelRestrictions,
  getTeamModelRestrictions,
  getTeamResidencyRule,
  listRemovedTeamMembers,
  listTeamRateLimitRules,
  putMemberModelRestrictions,
  putTeamAccessSchedule,
  putTeamCacheSettings,
  putTeamDlpOverride,
  putTeamFailoverOverride,
  putTeamModelRestrictions,
  putTeamResidencyRule,
  reassignTeamBudget,
  rejectJoinRequest,
  removeTeamMember,
  restoreTeamMember,
  updateTeamDegradationPolicy,
  updateTeamMember,
  updateTeamRateLimitRule,
  type DlpAction,
  type JoinRequestResponse,
  type MeTeam,
  type RateLimitOnLimit,
  type RateLimitRuleResponse,
  type RemovedTeamMemberResponse,
  type ResidencyViolationBehavior,
  type TeamDetailResponse,
  type TeamMemberResponse,
  type TeamRole,
  type UserResponse,
} from "@/lib/api";
import { Badge, ConfirmDialog, DataTable, FieldError, Modal, useToast } from "@/components/ui";
import { ManageMemberKeysModal } from "@/components/personal-keys";
import { AccessScheduleForm } from "@/components/access-schedule";

// --- Formatting / headroom helpers -------------------------------------------

export function fmtUsd(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return "Unmetered";
  return `$${Number(value).toFixed(2)}`;
}

export interface TeamHeadroom {
  ceiling: number | null;
  allocated: number;
  /** null = no ceiling (unconstrained). */
  unallocated: number | null;
}

/** Client-side headroom estimate for hints only - the backend re-derives the
 * authoritative figure under a row lock and its 422 message wins. */
export function computeHeadroom(detail: TeamDetailResponse): TeamHeadroom {
  const allocated = detail.members.reduce(
    (sum, m) => sum + (m.budget_usd === null ? 0 : Number(m.budget_usd)),
    0
  );
  const ceiling = detail.budget_ceiling_usd === null ? null : Number(detail.budget_ceiling_usd);
  return {
    ceiling,
    allocated,
    unallocated: ceiling === null ? null : Math.max(ceiling - allocated, 0),
  };
}

function headroomHint(headroom: TeamHeadroom): string {
  if (headroom.ceiling === null) return "No team ceiling set - any budget is allowed.";
  return `Max: $${headroom.unallocated!.toFixed(2)} (team has $${headroom.unallocated!.toFixed(
    2
  )} unallocated of its $${headroom.ceiling.toFixed(2)} ceiling)`;
}

function memberStatus(budget: string | null, spend: string) {
  if (budget === null) return <Badge tone="green">Active</Badge>;
  const s = Number(spend);
  const b = Number(budget);
  if (s >= b) return <Badge tone="red">Budget exhausted</Badge>;
  if (b > 0 && s / b >= 0.8) return <Badge tone="amber">Near limit</Badge>;
  return <Badge tone="green">Active</Badge>;
}

// --- Member add/edit modal ----------------------------------------------------

export function MemberFormModal({
  teamId,
  detail,
  initial,
  users,
  onClose,
  onSaved,
}: {
  teamId: string;
  detail: TeamDetailResponse;
  /** null = add mode. */
  initial: TeamMemberResponse | null;
  /** Org-user list for the picker (admin path). Team Leads have no
   * org-user-listing endpoint, so they pass null and get a plain user-id
   * input - their normal add path is join-request approval anyway. */
  users: UserResponse[] | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [userId, setUserId] = useState(initial?.user_id ?? "");
  const [role, setRole] = useState<TeamRole>(initial?.role ?? "member");
  const [unmetered, setUnmetered] = useState(initial ? initial.budget_usd === null : false);
  const [budget, setBudget] = useState(initial?.budget_usd ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const headroom = computeHeadroom(detail);
  const memberIds = new Set(detail.members.map((m) => m.user_id));
  const candidates = users?.filter((u) => !memberIds.has(u.id)) ?? null;

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      const budget_usd = unmetered ? null : budget || "0";
      if (initial) {
        await updateTeamMember(teamId, initial.user_id, { role, budget_usd });
      } else {
        await addTeamMember(teamId, { user_id: userId, role, budget_usd });
      }
      toast.push("success", initial ? "Member updated." : "Member added.");
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save member.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={initial ? `Edit ${initial.name}` : "Add member"} onClose={onClose}>
      {initial ? null : candidates ? (
        <div className="field">
          <label>User</label>
          <select value={userId} onChange={(e) => setUserId(e.target.value)}>
            <option value="">Select a user...</option>
            {candidates.map((u) => (
              <option key={u.id} value={u.id}>
                {u.name}
              </option>
            ))}
          </select>
        </div>
      ) : (
        <div className="field">
          <label>User ID</label>
          <input
            type="text"
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
            placeholder="user UUID"
          />
          <div className="field-hint">
            Members usually join via a join request - approve it from the Join Requests
            screen instead if one is pending.
          </div>
        </div>
      )}
      <div className="field">
        <label>Role</label>
        {/* Only member/team_lead are expressible here, mirroring the
            backend's Literal-typed role field (AC1.5) - org-wide roles are
            never assignable from a team screen. */}
        <select value={role} onChange={(e) => setRole(e.target.value as TeamRole)}>
          <option value="member">Member</option>
          <option value="team_lead">Team Lead</option>
        </select>
      </div>
      <div className="field">
        <label>Budget (USD)</label>
        <input
          type="text"
          value={unmetered ? "" : budget}
          onChange={(e) => setBudget(e.target.value)}
          disabled={unmetered}
          placeholder="100.00"
        />
        <div className="field-hint">{headroomHint(headroom)}</div>
      </div>
      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          id="member-unmetered"
          checked={unmetered}
          onChange={(e) => setUnmetered(e.target.checked)}
          style={{ width: "auto" }}
        />
        <label htmlFor="member-unmetered" style={{ margin: 0 }}>
          Unmetered (no budget for this membership)
        </label>
      </div>
      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button
          className="btn btn-primary"
          onClick={handleSave}
          disabled={busy || (!initial && !userId.trim())}
        >
          {busy ? "Saving..." : "Save"}
        </button>
      </div>
    </Modal>
  );
}

// --- Budget reassignment modal ------------------------------------------------

export function ReassignBudgetModal({
  teamId,
  members,
  onClose,
  onSaved,
}: {
  teamId: string;
  members: TeamMemberResponse[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [fromId, setFromId] = useState("");
  const [toId, setToId] = useState("");
  const [amount, setAmount] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const from = members.find((m) => m.user_id === fromId);
  const fromMax = from?.budget_usd === null || from === undefined ? null : Number(from.budget_usd);

  async function handleReassign() {
    setBusy(true);
    setError(null);
    try {
      const result = await reassignTeamBudget(teamId, {
        from_user_id: fromId,
        to_user_id: toId,
        amount_usd: amount,
      });
      toast.push(
        "success",
        `Reassigned ${fmtUsd(result.amount_usd)} - new budgets: ${fmtUsd(
          result.from_new_budget_usd
        )} / ${fmtUsd(result.to_new_budget_usd)}.`
      );
      onSaved();
    } catch (err) {
      // 422 budget_ceiling_exceeded carries the live headroom in its message.
      setError(err instanceof ApiError ? err.message : "Failed to reassign budget.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Reassign budget between members" onClose={onClose}>
      <div className="field">
        <label>From member</label>
        <select value={fromId} onChange={(e) => setFromId(e.target.value)}>
          <option value="">Select...</option>
          {members.map((m) => (
            <option key={m.user_id} value={m.user_id}>
              {m.name} ({fmtUsd(m.budget_usd)})
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label>To member</label>
        <select value={toId} onChange={(e) => setToId(e.target.value)}>
          <option value="">Select...</option>
          {members
            .filter((m) => m.user_id !== fromId)
            .map((m) => (
              <option key={m.user_id} value={m.user_id}>
                {m.name} ({fmtUsd(m.budget_usd)})
              </option>
            ))}
        </select>
      </div>
      <div className="field">
        <label>Amount (USD)</label>
        <input
          type="text"
          value={amount}
          onChange={(e) => setAmount(e.target.value)}
          placeholder="50.00"
        />
        {fromMax !== null && fromMax !== undefined ? (
          <div className="field-hint">Max: ${fromMax.toFixed(2)} (source member&apos;s budget)</div>
        ) : null}
      </div>
      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button
          className="btn btn-primary"
          onClick={handleReassign}
          disabled={busy || !fromId || !toId || !amount.trim()}
        >
          {busy ? "Reassigning..." : "Reassign"}
        </button>
      </div>
    </Modal>
  );
}

// --- Members table section ----------------------------------------------------

export function MembersSection({
  teamId,
  detail,
  users,
  onChanged,
}: {
  teamId: string;
  detail: TeamDetailResponse;
  users: UserResponse[] | null;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [formOpen, setFormOpen] = useState<null | "new" | TeamMemberResponse>(null);
  const [keysFor, setKeysFor] = useState<TeamMemberResponse | null>(null);
  const [modelAccessFor, setModelAccessFor] = useState<TeamMemberResponse | null>(null);
  const [reassignOpen, setReassignOpen] = useState(false);
  const [removing, setRemoving] = useState<TeamMemberResponse | null>(null);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);

  const headroom = computeHeadroom(detail);

  const [removedOpen, setRemovedOpen] = useState(false);
  const [removedMembers, setRemovedMembers] = useState<RemovedTeamMemberResponse[] | null>(null);
  const [restoreBusy, setRestoreBusy] = useState<string | null>(null);

  async function handleRemove(member: TeamMemberResponse) {
    setRemoveBusy(true);
    setRemoveError(null);
    try {
      await removeTeamMember(teamId, member.user_id);
      toast.push("success", `${member.name} removed from the team. Their access is cut off immediately - restore them anytime from "Removed members" below.`);
      setRemoving(null);
      onChanged();
    } catch (err) {
      setRemoveError(err instanceof ApiError ? err.message : "Failed to remove member.");
    } finally {
      setRemoveBusy(false);
    }
  }

  async function loadRemovedMembers() {
    try {
      setRemovedMembers(await listRemovedTeamMembers(teamId));
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to load removed members.");
    }
  }

  async function handleRestore(member: RemovedTeamMemberResponse) {
    setRestoreBusy(member.user_id);
    try {
      await restoreTeamMember(teamId, member.user_id);
      toast.push("success", `${member.name} restored - their existing keys work again immediately.`);
      await loadRemovedMembers();
      onChanged();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to restore member.");
    } finally {
      setRestoreBusy(null);
    }
  }

  return (
    <div className="panel">
      <div className="page-header-row">
        <div className="panel-title">Members</div>
        <button className="btn btn-primary" onClick={() => setFormOpen("new")}>
          + Add member
        </button>
      </div>
      {headroom.ceiling !== null ? (
        <p className="text-muted">
          Allocated to members: ${headroom.allocated.toFixed(2)} of $
          {headroom.ceiling.toFixed(2)} ceiling
        </p>
      ) : null}
      <DataTable
        rows={detail.members}
        rowKey={(m) => m.user_id}
        emptyState="No members yet."
        columns={[
          { key: "name", header: "Name", render: (m) => m.name },
          {
            key: "role",
            header: "Role",
            render: (m) =>
              m.role === "team_lead" ? <Badge tone="gray">Team Lead</Badge> : "Member",
          },
          { key: "budget", header: "Budget", align: "right", render: (m) => fmtUsd(m.budget_usd) },
          {
            key: "spent",
            header: "Spent",
            align: "right",
            render: (m) => fmtUsd(m.current_spend_usd),
          },
          {
            key: "status",
            header: "Status",
            align: "right",
            render: (m) => memberStatus(m.budget_usd, m.current_spend_usd),
          },
          {
            key: "actions",
            header: "Actions",
            align: "right",
            render: (m) => (
              <>
                <button className="btn-link" onClick={() => setFormOpen(m)}>
                  Edit
                </button>{" "}
                {/* Delegated key management (UI doc section 7.3): opens the
                    same key surface as My API Keys, scoped to this member
                    via the /v1/teams/../members/../keys endpoints. */}
                <button className="btn-link" onClick={() => setKeysFor(m)}>
                  Manage API keys
                </button>{" "}
                <button className="btn-link" onClick={() => setModelAccessFor(m)}>
                  Model access
                </button>{" "}
                <button
                  className="btn-link"
                  style={{ color: "var(--red)" }}
                  onClick={() => {
                    setRemoveError(null);
                    setRemoving(m);
                  }}
                >
                  Remove
                </button>
              </>
            ),
          },
        ]}
      />
      <div style={{ marginTop: 12 }}>
        <button
          className="btn btn-secondary"
          onClick={() => setReassignOpen(true)}
          disabled={detail.members.length < 2}
        >
          Reassign budget between members
        </button>
      </div>

      {formOpen ? (
        <MemberFormModal
          teamId={teamId}
          detail={detail}
          initial={formOpen === "new" ? null : formOpen}
          users={users}
          onClose={() => setFormOpen(null)}
          onSaved={() => {
            setFormOpen(null);
            onChanged();
          }}
        />
      ) : null}
      {keysFor ? (
        <ManageMemberKeysModal
          teamId={teamId}
          teamName={detail.name}
          userId={keysFor.user_id}
          memberName={keysFor.name}
          onClose={() => setKeysFor(null)}
        />
      ) : null}
      {modelAccessFor ? (
        <MemberModelAccessModal
          teamId={teamId}
          userId={modelAccessFor.user_id}
          memberName={modelAccessFor.name}
          onClose={() => setModelAccessFor(null)}
        />
      ) : null}
      {reassignOpen ? (
        <ReassignBudgetModal
          teamId={teamId}
          members={detail.members}
          onClose={() => setReassignOpen(false)}
          onSaved={() => {
            setReassignOpen(false);
            onChanged();
          }}
        />
      ) : null}
      {removing ? (
        <ConfirmDialog
          title={`Remove ${removing.name} from this team?`}
          consequence={
            removeError ??
            "Their keys stop working immediately. This is reversible - restore them anytime from \"Removed members\" below, with the same role, budget, and spend history."
          }
          confirmLabel="Remove member"
          busy={removeBusy}
          onCancel={() => setRemoving(null)}
          onConfirm={() => handleRemove(removing)}
        />
      ) : null}

      <div style={{ marginTop: 20 }}>
        <button
          className="btn-link"
          onClick={() => {
            const next = !removedOpen;
            setRemovedOpen(next);
            if (next && removedMembers === null) void loadRemovedMembers();
          }}
        >
          {removedOpen ? "▾" : "▸"} Removed members
        </button>
        {removedOpen ? (
          <DataTable
            rows={removedMembers ?? []}
            rowKey={(m) => m.user_id}
            emptyState="No removed members."
            columns={[
              { key: "name", header: "Name", render: (m) => m.name },
              {
                key: "removed_at",
                header: "Removed",
                render: (m) => new Date(m.removed_at).toLocaleString(),
              },
              {
                key: "actions",
                header: "Actions",
                align: "right",
                render: (m) => (
                  <button
                    className="btn-link"
                    disabled={restoreBusy === m.user_id}
                    onClick={() => handleRestore(m)}
                  >
                    {restoreBusy === m.user_id ? "Restoring..." : "Restore"}
                  </button>
                ),
              },
            ]}
          />
        ) : null}
      </div>
    </div>
  );
}

// --- Model restrictions card --------------------------------------------------

export function ModelRestrictionsCard({ teamId }: { teamId: string }) {
  const toast = useToast();
  const [baseline, setBaseline] = useState<string[]>([]);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getTeamModelRestrictions(teamId)
      .then((data) => {
        if (cancelled) return;
        setBaseline(data.org_baseline);
        // null restriction = org baseline applies unchanged -> all checked.
        setChecked(new Set(data.team_restriction ?? data.org_baseline));
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof ApiError ? err.message : "Failed to load model restrictions.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [teamId]);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      const result = await putTeamModelRestrictions(teamId, { models: [...checked] });
      setBaseline(result.org_baseline);
      setChecked(new Set(result.team_restriction ?? result.org_baseline));
      toast.push("success", "Team model restrictions saved.");
    } catch (err) {
      // 422 team_model_restricts_org_denied_model passes through verbatim.
      setError(err instanceof ApiError ? err.message : "Failed to save restrictions.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="panel-title">Team Model Restrictions</div>
      <p className="text-muted">
        A team can only narrow the org baseline, never re-enable a model the Org Admin
        has denied - models outside the baseline are not shown at all.
      </p>
      {loading ? (
        <div className="skeleton skeleton-text" />
      ) : (
        <div className="model-checkbox-grid">
          {baseline.map((model) => (
            <label key={model} className="model-checkbox">
              <input
                type="checkbox"
                checked={checked.has(model)}
                onChange={(e) => {
                  const next = new Set(checked);
                  if (e.target.checked) next.add(model);
                  else next.delete(model);
                  setChecked(next);
                }}
              />
              {model}
            </label>
          ))}
        </div>
      )}
      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-primary" onClick={handleSave} disabled={busy || loading}>
          {busy ? "Saving..." : "Save restrictions"}
        </button>
      </div>
    </div>
  );
}

// --- Per-member model access (third layer, below ModelRestrictionsCard) -----

/** A team lead narrows ONE member's access within the team's own effective
 * set (org baseline intersected with the team's restriction, if any) -
 * mirrors `ModelRestrictionsCard` one layer down: same "fetch baseline +
 * current restriction, checkbox grid, null = everything checked, full-
 * replace on save" shape, scoped to `teamBaseline` instead of `org_baseline`
 * and to one `(teamId, userId)` pair instead of the whole team. */
export function MemberModelAccessModal({
  teamId,
  userId,
  memberName,
  onClose,
}: {
  teamId: string;
  userId: string;
  memberName: string;
  onClose: () => void;
}) {
  const toast = useToast();
  const [baseline, setBaseline] = useState<string[]>([]);
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    getMemberModelRestrictions(teamId, userId)
      .then((data) => {
        if (cancelled) return;
        setBaseline(data.team_baseline);
        // null restriction = the team baseline applies to them unchanged -> all checked.
        setChecked(new Set(data.member_restriction ?? data.team_baseline));
      })
      .catch((err) => {
        if (!cancelled)
          setError(err instanceof ApiError ? err.message : "Failed to load model access.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [teamId, userId]);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      const result = await putMemberModelRestrictions(teamId, userId, { models: [...checked] });
      setBaseline(result.team_baseline);
      setChecked(new Set(result.member_restriction ?? result.team_baseline));
      toast.push("success", `Model access saved for ${memberName}.`);
      onClose();
    } catch (err) {
      // 422 member_model_restricts_team_denied_model passes through verbatim.
      setError(err instanceof ApiError ? err.message : "Failed to save model access.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={`Model access - ${memberName}`} onClose={onClose}>
      <p className="text-muted" style={{ marginTop: 0 }}>
        Choose which of this team&apos;s own models {memberName} can use. This can only narrow
        what the team already allows, never widen it - models outside the team&apos;s set are not
        shown at all.
      </p>
      {loading ? (
        <div className="skeleton skeleton-text" />
      ) : baseline.length === 0 ? (
        <div className="text-muted">
          This team has no models enabled yet - set the team&apos;s own model restrictions first.
        </div>
      ) : (
        <div className="model-checkbox-grid">
          {baseline.map((model) => (
            <label key={model} className="model-checkbox">
              <input
                type="checkbox"
                checked={checked.has(model)}
                onChange={(e) => {
                  const next = new Set(checked);
                  if (e.target.checked) next.add(model);
                  else next.delete(model);
                  setChecked(next);
                }}
              />
              {model}
            </label>
          ))}
        </div>
      )}
      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button className="btn btn-primary" onClick={handleSave} disabled={busy || loading}>
          {busy ? "Saving..." : "Save model access"}
        </button>
      </div>
    </Modal>
  );
}

// --- DLP action override (Phase 3, BD-2) ---------------------------------------

const DLP_ACTION_LABELS: Record<DlpAction, string> = {
  log: "Log only",
  redact: "Redact",
  block: "Block",
};

export function TeamDlpOverrideCard({ teamId }: { teamId: string }) {
  const toast = useToast();
  const [action, setAction] = useState<DlpAction | null>(null);
  const [selected, setSelected] = useState<DlpAction>("log");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getTeamDlpOverride(teamId)
      .then((data) => {
        if (cancelled) return;
        setAction(data.action);
        if (data.action) setSelected(data.action);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : "Failed to load DLP override.");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [teamId]);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      const result = await putTeamDlpOverride(teamId, selected);
      setAction(result.action);
      toast.push("success", "Team DLP override saved.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save DLP override.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="panel-title">Team DLP action override</div>
      <p className="text-muted">
        Overrides only the action a finding triggers - detectors and custom patterns stay
        org-wide, set from <a href="/compliance/dlp">Compliance &amp; DLP</a>.
      </p>
      {loading ? (
        <div className="skeleton skeleton-text" />
      ) : (
        <>
          {action === null ? (
            <div className="banner banner-info">No override set - the org default action applies.</div>
          ) : null}
          <div className="field">
            <label>Action for this team</label>
            <select value={selected} onChange={(e) => setSelected(e.target.value as DlpAction)}>
              {(Object.keys(DLP_ACTION_LABELS) as DlpAction[]).map((a) => (
                <option key={a} value={a}>
                  {DLP_ACTION_LABELS[a]}
                </option>
              ))}
            </select>
          </div>
          <FieldError message={error} />
          <div className="modal-actions">
            <button className="btn btn-primary" onClick={handleSave} disabled={busy}>
              {busy ? "Saving..." : "Save override"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

// --- Residency rule narrowing (Phase 3, BD-4) -----------------------------------

function regionsToText(regions: string[]): string {
  return regions.join(", ");
}

function textToRegions(text: string): string[] {
  return text
    .split(",")
    .map((r) => r.trim())
    .filter(Boolean);
}

export function TeamResidencyRuleCard({ teamId }: { teamId: string }) {
  const toast = useToast();
  const [rule, setRule] = useState<{ allowed_regions: string[]; violation_behavior: ResidencyViolationBehavior } | null>(null);
  const [regionsText, setRegionsText] = useState("");
  const [behavior, setBehavior] = useState<ResidencyViolationBehavior>("hard_block");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function refresh() {
    setLoading(true);
    getTeamResidencyRule(teamId)
      .then((data) => {
        setRule(data);
        setRegionsText(data ? regionsToText(data.allowed_regions) : "");
        setBehavior(data?.violation_behavior ?? "hard_block");
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load residency rule."))
      .finally(() => setLoading(false));
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(refresh, [teamId]);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      const result = await putTeamResidencyRule(teamId, {
        allowed_regions: textToRegions(regionsText),
        violation_behavior: behavior,
      });
      setRule(result);
      toast.push("success", "Team residency rule saved.");
    } catch (err) {
      // 422 residency_rule_widens_org_rule passes through verbatim.
      setError(err instanceof ApiError ? err.message : "Failed to save residency rule.");
    } finally {
      setBusy(false);
    }
  }

  async function handleRemove() {
    setBusy(true);
    setError(null);
    try {
      await deleteTeamResidencyRule(teamId);
      toast.push("success", "Team override removed - the org-wide rule applies again.");
      refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to remove team override.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="panel-title">Team residency narrowing</div>
      <p className="text-muted">
        A team rule can only narrow the org-wide{" "}
        <a href="/compliance/residency">residency rule</a>, never widen it.
      </p>
      {loading ? (
        <div className="skeleton skeleton-text" />
      ) : (
        <>
          {rule === null ? (
            <div className="banner banner-info">No team override - the org-wide rule applies unchanged.</div>
          ) : null}
          <div className="field">
            <label>Allowed regions (comma-separated)</label>
            <input
              type="text"
              value={regionsText}
              onChange={(e) => setRegionsText(e.target.value)}
              placeholder="e.g. us-east-1"
            />
          </div>
          <div className="field">
            <label>On a violation</label>
            <select value={behavior} onChange={(e) => setBehavior(e.target.value as ResidencyViolationBehavior)}>
              <option value="hard_block">Hard block the request</option>
              <option value="warn">Warn only (allow, but log)</option>
            </select>
          </div>
          <FieldError message={error} />
          <div className="modal-actions" style={{ justifyContent: rule ? "space-between" : "flex-end" }}>
            {rule ? (
              <button className="btn btn-secondary" onClick={handleRemove} disabled={busy}>
                Remove override
              </button>
            ) : null}
            <button className="btn btn-primary" onClick={handleSave} disabled={busy || !regionsText.trim()}>
              {busy ? "Saving..." : "Save narrowing"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

// --- Access schedule narrowing (Phase 3, BD-16/17) -------------------------------

export function TeamAccessScheduleCard({ teamId }: { teamId: string }) {
  const [schedule, setSchedule] = useState<Awaited<ReturnType<typeof getTeamAccessSchedule>>>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  function refresh() {
    setLoading(true);
    getTeamAccessSchedule(teamId)
      .then(setSchedule)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load access schedule."))
      .finally(() => setLoading(false));
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(refresh, [teamId]);

  return (
    <div className="panel">
      <div className="panel-title">Team access-schedule narrowing</div>
      {error ? <div className="banner banner-error">{error}</div> : null}
      {loading ? (
        <div className="skeleton skeleton-text" />
      ) : (
        <AccessScheduleForm
          key={schedule ? "configured" : "unrestricted"}
          schedule={schedule}
          narrowingHint="Can only narrow the org default (never widen it) - see Scheduled Access Windows for the org-wide default."
          onSave={async (body) => {
            const result = await putTeamAccessSchedule(teamId, body);
            setSchedule(result);
            return result;
          }}
          onRemoveOverride={async () => {
            await deleteTeamAccessSchedule(teamId);
            setSchedule(null);
          }}
        />
      )}
    </div>
  );
}

// --- Join-request approve/reject modals ---------------------------------------
//
// The identical control surface serves both the Team Lead queue and the Org
// Admin escalation queue (admin UI doc section 8's explicit "don't build two
// independent implementations of the same approval logic").

export function ApproveJoinRequestModal({
  teamId,
  request: joinRequest,
  onClose,
  onApproved,
}: {
  teamId: string;
  request: JoinRequestResponse;
  onClose: () => void;
  onApproved: () => void;
}) {
  const toast = useToast();
  const [budget, setBudget] = useState("");
  const [hint, setHint] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getTeam(teamId)
      .then((detail) => {
        if (!cancelled) setHint(headroomHint(computeHeadroom(detail)));
      })
      .catch(() => {
        // Hint only - the backend enforces the ceiling either way.
      });
    return () => {
      cancelled = true;
    };
  }, [teamId]);

  async function handleApprove() {
    setBusy(true);
    setError(null);
    try {
      await approveJoinRequest(teamId, joinRequest.id, { budget_usd: budget });
      toast.push("success", `${joinRequest.requester_name} approved and budgeted.`);
      onApproved();
    } catch (err) {
      // 422 budget_ceiling_exceeded: message carries the live headroom
      // re-derived by the backend under the team lock.
      setError(err instanceof ApiError ? err.message : "Failed to approve request.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={`Approve ${joinRequest.requester_name}`} onClose={onClose}>
      <div className="field">
        <label>Budget for this member (USD) *</label>
        <input
          type="text"
          value={budget}
          onChange={(e) => setBudget(e.target.value)}
          placeholder="100.00"
        />
        {hint ? <div className="field-hint">{hint}</div> : null}
      </div>
      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button
          className="btn btn-primary"
          onClick={handleApprove}
          disabled={busy || !budget.trim()}
        >
          {busy ? "Approving..." : "Approve & allocate"}
        </button>
      </div>
    </Modal>
  );
}

export function RejectJoinRequestModal({
  teamId,
  request: joinRequest,
  onClose,
  onRejected,
}: {
  teamId: string;
  request: JoinRequestResponse;
  onClose: () => void;
  onRejected: () => void;
}) {
  const toast = useToast();
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleReject() {
    setBusy(true);
    setError(null);
    try {
      await rejectJoinRequest(teamId, joinRequest.id, { reason: reason.trim() || null });
      toast.push("success", `${joinRequest.requester_name}'s request rejected.`);
      onRejected();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to reject request.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={`Reject ${joinRequest.requester_name}'s request`} onClose={onClose}>
      <div className="field">
        <label>Reason (optional - shown to the requester)</label>
        <textarea
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          rows={3}
          style={{ width: "100%" }}
        />
      </div>
      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button className="btn btn-danger" onClick={handleReject} disabled={busy}>
          {busy ? "Rejecting..." : "Reject request"}
        </button>
      </div>
    </Modal>
  );
}

// --- Team Lead team switcher --------------------------------------------------

const LEAD_TEAM_STORAGE_KEY = "gatekey_lead_team";

/** Teams the session leads, plus a persisted selection - a Team Lead may
 * lead multiple teams (AC1.2). The list comes from /v1/auth/me, so the
 * switcher can never offer a team the session doesn't actually lead. */
export function useLeadTeams(): {
  teams: MeTeam[];
  selected: string | null;
  select: (teamId: string) => void;
  loading: boolean;
  error: string | null;
} {
  const [teams, setTeams] = useState<MeTeam[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getMe()
      .then((me) => {
        if (cancelled) return;
        const lead = me.teams.filter((t) => t.role === "team_lead");
        setTeams(lead);
        const stored = window.localStorage.getItem(LEAD_TEAM_STORAGE_KEY);
        const initial = lead.find((t) => t.team_id === stored)?.team_id ?? lead[0]?.team_id ?? null;
        setSelected(initial);
        if (lead.length === 0) setError("You do not lead any team.");
      })
      .catch(() => {
        if (!cancelled) setError("Failed to resolve your teams. Are you signed in?");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return {
    teams,
    selected,
    select: (teamId: string) => {
      window.localStorage.setItem(LEAD_TEAM_STORAGE_KEY, teamId);
      setSelected(teamId);
    },
    loading,
    error,
  };
}

// --- Phase 4: team-scoped Reliability & Cost cards -----------------------------
//
// Mirrors the Phase 3 narrowing cards above (TeamDlpOverrideCard etc.) -
// same load/save/error pattern, wired to the Phase 4 team-scoped endpoints
// (`require_team_role("team_lead", ...)` - Org Admin OR that team's own
// Team Lead).

export function TeamCacheSettingsCard({ teamId }: { teamId: string }) {
  const toast = useToast();
  const [enabled, setEnabled] = useState(false);
  const [ttlMinutes, setTtlMinutes] = useState(5);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function refresh() {
    setLoading(true);
    getTeamCacheSettings(teamId)
      .then((data) => {
        setEnabled(data.cache_enabled);
        setTtlMinutes(data.cache_ttl_minutes);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load cache settings."))
      .finally(() => setLoading(false));
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(refresh, [teamId]);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      const result = await putTeamCacheSettings(teamId, { cache_enabled: enabled, cache_ttl_minutes: ttlMinutes });
      setEnabled(result.cache_enabled);
      setTtlMinutes(result.cache_ttl_minutes);
      toast.push("success", "Team cache settings saved.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save cache settings.");
    } finally {
      setBusy(false);
    }
  }

  async function handleClear() {
    setClearing(true);
    try {
      const result = await clearCache(teamId);
      toast.push("success", `Cache cleared for this team (${result.entries_cleared} entries).`);
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to clear cache.");
    } finally {
      setClearing(false);
    }
  }

  return (
    <div className="panel">
      <div className="panel-title">Caching</div>
      <p className="text-muted">
        Opt-in per team (default off). The org-wide kill switch (Reliability &amp; Cost &rarr;
        Caching Settings) can still disable caching for everyone regardless of this setting.
      </p>
      {loading ? (
        <div className="skeleton skeleton-text" />
      ) : (
        <>
          <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="checkbox"
              id={`cache-enabled-${teamId}`}
              style={{ width: "auto" }}
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            <label htmlFor={`cache-enabled-${teamId}`} style={{ margin: 0 }}>
              Enable caching for this team
            </label>
          </div>
          <div className="field">
            <label>TTL (minutes)</label>
            <input
              type="number"
              min={1}
              max={1440}
              value={ttlMinutes}
              onChange={(e) => setTtlMinutes(parseInt(e.target.value, 10) || 1)}
            />
            <div className="field-hint">1 minute - 24 hours (1440 minutes). Default: 5.</div>
          </div>
          <FieldError message={error} />
          <div className="modal-actions" style={{ justifyContent: "space-between" }}>
            <button className="btn btn-secondary" onClick={handleClear} disabled={clearing}>
              {clearing ? "Clearing..." : "Clear this team's cache"}
            </button>
            <button className="btn btn-primary" onClick={handleSave} disabled={busy}>
              {busy ? "Saving..." : "Save"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

export function TeamDegradationPolicyCard({ teamId }: { teamId: string }) {
  const toast = useToast();
  const [enabled, setEnabled] = useState(false);
  const [threshold, setThreshold] = useState("10");
  const [fallbackModel, setFallbackModel] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function refresh() {
    setLoading(true);
    setError(null);
    getTeamDegradationPolicy(teamId)
      .then((data) => {
        setEnabled(data.enabled);
        setThreshold(data.threshold_pct_of_budget);
        setFallbackModel(data.downgrade_target_model);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load degradation policy."))
      .finally(() => setLoading(false));
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(refresh, [teamId]);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      const result = await updateTeamDegradationPolicy(teamId, {
        enabled,
        threshold_pct_of_budget: Number(threshold),
        downgrade_target_model: fallbackModel,
      });
      setEnabled(result.enabled);
      setThreshold(result.threshold_pct_of_budget);
      setFallbackModel(result.downgrade_target_model);
      toast.push("success", "Team degradation policy saved.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save degradation policy.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="panel-title">Graceful Degradation</div>
      <p className="text-muted">
        Auto-downgrade chat-completion requests to a cheaper model when this team&apos;s
        remaining budget falls below the threshold below - overrides the org default for this
        team only.
      </p>
      {loading ? (
        <div className="skeleton skeleton-text" />
      ) : (
        <>
          <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="checkbox"
              id={`degradation-enabled-${teamId}`}
              style={{ width: "auto" }}
              checked={enabled}
              onChange={(e) => setEnabled(e.target.checked)}
            />
            <label htmlFor={`degradation-enabled-${teamId}`} style={{ margin: 0 }}>
              Enable for this team
            </label>
          </div>
          <div className="field">
            <label>Budget threshold (%)</label>
            <input type="number" min={1} max={99} value={threshold} onChange={(e) => setThreshold(e.target.value)} />
          </div>
          <div className="field">
            <label>Downgrade target model</label>
            <input
              type="text"
              value={fallbackModel}
              onChange={(e) => setFallbackModel(e.target.value)}
              placeholder="e.g., gpt-4o-mini"
            />
            <div className="field-hint">Must be a model from this team&apos;s allowed model list.</div>
          </div>
          <FieldError message={error} />
          <div className="modal-actions">
            <button className="btn btn-primary" onClick={handleSave} disabled={busy || !fallbackModel.trim()}>
              {busy ? "Saving..." : "Save"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

export function TeamFailoverOverrideCard({ teamId }: { teamId: string }) {
  const toast = useToast();
  const [disabled, setDisabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function refresh() {
    setLoading(true);
    getTeamFailoverOverride(teamId)
      .then((data) => setDisabled(data.failover_disabled))
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load failover override."))
      .finally(() => setLoading(false));
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(refresh, [teamId]);

  async function handleSave(next: boolean) {
    setBusy(true);
    setError(null);
    try {
      const result = await putTeamFailoverOverride(teamId, next);
      setDisabled(result.failover_disabled);
      toast.push("success", "Team failover override saved.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save failover override.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="panel-title">Failover</div>
      <p className="text-muted">
        Narrowing only - this can turn OFF automatic failover for this team even when it&apos;s
        enabled at the org/key level, but can never force failover ON if it&apos;s off there.
      </p>
      {loading ? (
        <div className="skeleton skeleton-text" />
      ) : (
        <>
          <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="checkbox"
              id={`failover-disabled-${teamId}`}
              style={{ width: "auto" }}
              checked={disabled}
              onChange={(e) => setDisabled(e.target.checked)}
            />
            <label htmlFor={`failover-disabled-${teamId}`} style={{ margin: 0 }}>
              Disable automatic failover for this team
            </label>
          </div>
          <FieldError message={error} />
          <div className="modal-actions">
            <button className="btn btn-primary" onClick={() => handleSave(disabled)} disabled={busy}>
              {busy ? "Saving..." : "Save"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

const RATE_LIMIT_ON_LIMIT_LABELS: Record<RateLimitOnLimit, string> = {
  reject: "Reject immediately",
  queue_and_retry: "Queue and retry",
};

export function TeamRateLimitCard({ teamId }: { teamId: string }) {
  const toast = useToast();
  const [rule, setRule] = useState<RateLimitRuleResponse | null>(null);
  const [requestsPerMinute, setRequestsPerMinute] = useState("");
  const [tokensPerMinute, setTokensPerMinute] = useState("");
  const [onLimit, setOnLimit] = useState<RateLimitOnLimit>("reject");
  const [maxQueueWait, setMaxQueueWait] = useState(30);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function refresh() {
    setLoading(true);
    setError(null);
    listTeamRateLimitRules(teamId)
      .then((data) => {
        const existing = data.rules[0] ?? null;
        setRule(existing);
        if (existing) {
          setRequestsPerMinute(existing.requests_per_minute?.toString() ?? "");
          setTokensPerMinute(existing.tokens_per_minute?.toString() ?? "");
          setOnLimit(existing.on_limit);
          setMaxQueueWait(existing.max_queue_wait_seconds);
        }
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load rate limit rule."))
      .finally(() => setLoading(false));
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(refresh, [teamId]);

  async function handleSave() {
    setError(null);
    const requests_per_minute = requestsPerMinute.trim() ? parseInt(requestsPerMinute, 10) : null;
    const tokens_per_minute = tokensPerMinute.trim() ? parseInt(tokensPerMinute, 10) : null;
    if (requests_per_minute === null && tokens_per_minute === null) {
      setError("At least one of requests/minute or tokens/minute must be set.");
      return;
    }
    setBusy(true);
    try {
      const body = { requests_per_minute, tokens_per_minute, on_limit: onLimit, max_queue_wait_seconds: maxQueueWait };
      const result = rule
        ? await updateTeamRateLimitRule(teamId, rule.id, body)
        : await createTeamRateLimitRule(teamId, body);
      setRule(result);
      toast.push("success", "Team rate limit rule saved.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save rate limit rule.");
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete() {
    if (!rule) return;
    setBusy(true);
    try {
      await deleteTeamRateLimitRule(teamId, rule.id);
      setRule(null);
      setRequestsPerMinute("");
      setTokensPerMinute("");
      toast.push("success", "Team rate limit rule removed.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to remove rate limit rule.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="panel">
      <div className="panel-title">Rate Limiting</div>
      <p className="text-muted">
        Team-wide request/token pool. A member&apos;s personal limit (org-level, set by an Org
        Admin) is additive to this pool (AC4.2.9).
      </p>
      {loading ? (
        <div className="skeleton skeleton-text" />
      ) : (
        <>
          <div className="field">
            <label>Requests per minute</label>
            <input
              type="number"
              value={requestsPerMinute}
              onChange={(e) => setRequestsPerMinute(e.target.value)}
              placeholder="Unlimited"
            />
          </div>
          <div className="field">
            <label>Tokens per minute</label>
            <input
              type="number"
              value={tokensPerMinute}
              onChange={(e) => setTokensPerMinute(e.target.value)}
              placeholder="Unlimited"
            />
          </div>
          <div className="field">
            <label>On limit</label>
            <select value={onLimit} onChange={(e) => setOnLimit(e.target.value as RateLimitOnLimit)}>
              {(Object.keys(RATE_LIMIT_ON_LIMIT_LABELS) as RateLimitOnLimit[]).map((o) => (
                <option key={o} value={o}>
                  {RATE_LIMIT_ON_LIMIT_LABELS[o]}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label>Max queue wait (seconds)</label>
            <input
              type="number"
              min={10}
              max={300}
              value={maxQueueWait}
              onChange={(e) => setMaxQueueWait(parseInt(e.target.value, 10) || 10)}
            />
          </div>
          <FieldError message={error} />
          <div className="modal-actions" style={{ justifyContent: rule ? "space-between" : "flex-end" }}>
            {rule ? (
              <button className="btn btn-secondary" onClick={handleDelete} disabled={busy}>
                Remove rule
              </button>
            ) : null}
            <button className="btn btn-primary" onClick={handleSave} disabled={busy}>
              {busy ? "Saving..." : "Save"}
            </button>
          </div>
        </>
      )}
    </div>
  );
}

export function TeamSwitcher({
  teams,
  selected,
  onSelect,
}: {
  teams: MeTeam[];
  selected: string | null;
  onSelect: (teamId: string) => void;
}) {
  if (teams.length <= 1) {
    return teams.length === 1 ? <div className="page-subtitle">{teams[0].team_name}</div> : null;
  }
  return (
    <div className="field" style={{ maxWidth: 320 }}>
      <label>Team</label>
      <select value={selected ?? ""} onChange={(e) => onSelect(e.target.value)}>
        {teams.map((t) => (
          <option key={t.team_id} value={t.team_id}>
            {t.team_name}
          </option>
        ))}
      </select>
    </div>
  );
}
