"use client";

/**
 * Service Accounts & Keys (Phase 1 UI spec section 7.6 + Phase 2 admin UI
 * doc section 9): org-wide oversight over EVERY key regardless of who
 * created it, via the unified GET /v1/admin/keys?type=app|personal|all
 * listing. App and personal keys are distinguished by the Owner column;
 * the filter narrows to one type.
 *
 * Rules from the doc: "+ Create app key" mints app credentials only -
 * personal keys never originate here (they come from My API Keys or a Team
 * Lead's delegated flow). Revoke works on either type; Regenerate on
 * someone else's personal key carries a stronger confirm since a human
 * depends on that exact secret. One-time secret reveal reuses the shared
 * SecretRevealModal.
 *
 * Works for the break-glass token and org_admin sessions alike (adminAuth
 * precedence in the API client). The app-key create flow needs the Phase 1
 * /v1/admin/users listing; if that call fails (e.g. it rejects this
 * credential), the listing still renders and only creation is disabled.
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { Modal, ConfirmDialog, DataTable, Badge, FieldError, useToast } from "@/components/ui";
import { SecretRevealModal } from "@/components/personal-keys";
import { RotationPolicyForm } from "@/components/rotation";
import { AccessScheduleForm } from "@/components/access-schedule";
import {
  ApiError,
  adminRegenerateKey,
  adminRevokeKey,
  createServiceAccount,
  getKeyAccessSchedule,
  getKeyRotationPolicy,
  grantEmergencyOverride,
  listAdminKeys,
  listTeams,
  listUsers,
  putKeyAccessSchedule,
  putKeyRotationPolicy,
  revokeEmergencyOverride,
  rotateKeyNow,
  type AccessScheduleResponse,
  type AdminKeyResponse,
  type AdminKeyType,
  type EmergencyOverrideResponse,
  type RotationPolicyResponse,
  type ServiceAccountKeyCreateResponse,
  type TeamResponse,
  type UserResponse,
} from "@/lib/api";

// --- Rotation (Phase 3, BD-15): per-key override + "Rotate now" ---------------
// App keys only (backed by the ServiceAccountKey table) - personal keys have
// no rotation-policy/access-schedule/emergency-override surface.

function RotationModal({ row, onClose }: { row: AdminKeyResponse; onClose: () => void }) {
  const toast = useToast();
  const [policy, setPolicy] = useState<RotationPolicyResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmingRotate, setConfirmingRotate] = useState(false);
  const [busy, setBusy] = useState(false);
  const [reveal, setReveal] = useState<{ secret: string; overlapExpiresAt: string } | null>(null);

  useEffect(() => {
    getKeyRotationPolicy(row.id)
      .then(setPolicy)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load rotation policy."));
  }, [row.id]);

  async function handleRotateNow() {
    setBusy(true);
    try {
      const result = await rotateKeyNow(row.id);
      setConfirmingRotate(false);
      setReveal({ secret: result.secret, overlapExpiresAt: result.overlap_expires_at });
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to rotate key.");
      setConfirmingRotate(false);
    } finally {
      setBusy(false);
    }
  }

  if (reveal) {
    return (
      <SecretRevealModal
        title={`New secret for ${row.name} - old key valid until ${new Date(
          reveal.overlapExpiresAt
        ).toLocaleString()}`}
        secret={reveal.secret}
        onDone={() => {
          setReveal(null);
          onClose();
        }}
      />
    );
  }

  return (
    <Modal title={`Rotation - ${row.name}`} onClose={onClose} width={560}>
      {error ? <div className="banner banner-error">{error}</div> : null}
      {policy ? (
        <>
          <RotationPolicyForm
            policy={policy}
            onSave={(body) => putKeyRotationPolicy(row.id, body)}
            onSaved={setPolicy}
            inheritHint="Blank interval inherits the org default (Rotation Policy screen)."
          />
          <div className="field" style={{ marginTop: 8 }}>
            <label>Manual rotation</label>
            <div className="field-hint" style={{ marginBottom: 8 }}>
              Issues a new secret immediately, keeping the old one valid for a short overlap
              window (never an instant cutover) so in-flight callers don&apos;t break.
            </div>
            <button className="btn btn-secondary" onClick={() => setConfirmingRotate(true)}>
              Rotate now
            </button>
          </div>
        </>
      ) : !error ? (
        <div className="skeleton skeleton-text" />
      ) : null}
      {confirmingRotate ? (
        <ConfirmDialog
          title={`Rotate ${row.name} now?`}
          consequence="A new secret is issued now; the current one keeps working for the configured overlap buffer, then stops."
          confirmLabel="Rotate now"
          destructive={false}
          busy={busy}
          onCancel={() => setConfirmingRotate(false)}
          onConfirm={handleRotateNow}
        />
      ) : null}
    </Modal>
  );
}

// --- Access schedule + emergency override (Phase 3, BD-16/17/18) -------------

function AccessScheduleModal({ row, onClose }: { row: AdminKeyResponse; onClose: () => void }) {
  const toast = useToast();
  const [schedule, setSchedule] = useState<AccessScheduleResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [expiresAt, setExpiresAt] = useState("");
  const [grantBusy, setGrantBusy] = useState(false);
  const [grantError, setGrantError] = useState<string | null>(null);
  // No backend GET for active overrides (POST/DELETE only) - this list is
  // seeded from grants/revokes made in this session, not a full history.
  const [overrides, setOverrides] = useState<EmergencyOverrideResponse[]>([]);

  useEffect(() => {
    getKeyAccessSchedule(row.id)
      .then(setSchedule)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load access schedule."))
      .finally(() => setLoading(false));
  }, [row.id]);

  async function handleGrant() {
    if (!row.team_id) return;
    setGrantBusy(true);
    setGrantError(null);
    try {
      const iso = expiresAt ? new Date(expiresAt).toISOString() : "";
      const result = await grantEmergencyOverride(row.team_id, row.id, { reason, expires_at: iso });
      setOverrides((prev) => [result, ...prev]);
      setReason("");
      setExpiresAt("");
      toast.push("success", "Emergency override granted.");
    } catch (err) {
      setGrantError(err instanceof ApiError ? err.message : "Failed to grant override.");
    } finally {
      setGrantBusy(false);
    }
  }

  async function handleRevoke(override: EmergencyOverrideResponse) {
    if (!row.team_id) return;
    try {
      await revokeEmergencyOverride(row.team_id, row.id, override.id);
      setOverrides((prev) =>
        prev.map((o) => (o.id === override.id ? { ...o, revoked_at: new Date().toISOString() } : o))
      );
      toast.push("success", "Override revoked.");
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to revoke override.");
    }
  }

  return (
    <Modal title={`Access schedule - ${row.name}`} onClose={onClose} width={560}>
      {error ? <div className="banner banner-error">{error}</div> : null}
      {!loading ? (
        <AccessScheduleForm
          key={schedule ? "configured" : "unrestricted"}
          schedule={schedule}
          narrowingHint="Can only narrow the key's resolved team/org schedule (never widen it)."
          onSave={async (body) => {
            const result = await putKeyAccessSchedule(row.id, body);
            setSchedule(result);
            return result;
          }}
        />
      ) : (
        <div className="skeleton skeleton-text" />
      )}

      <div className="panel" style={{ marginTop: 16 }}>
        <div className="panel-title">Emergency override</div>
        <p className="text-muted">
          Time-boxed bypass of this key&apos;s resolved schedule. Requires a non-empty reason,
          enforced server-side.
        </p>
        {!row.team_id ? (
          <div className="banner banner-info">
            This is a legacy key with no team binding - emergency overrides require a team.
          </div>
        ) : (
          <>
            <div className="field">
              <label>Reason</label>
              <textarea rows={2} value={reason} onChange={(e) => setReason(e.target.value)} style={{ width: "100%" }} />
            </div>
            <div className="field">
              <label>Until</label>
              <input type="datetime-local" value={expiresAt} onChange={(e) => setExpiresAt(e.target.value)} />
            </div>
            <FieldError message={grantError} />
            <div className="modal-actions">
              <button
                className="btn btn-primary"
                onClick={handleGrant}
                disabled={grantBusy || !reason.trim() || !expiresAt}
              >
                {grantBusy ? "Granting..." : "Grant override"}
              </button>
            </div>
            {overrides.length > 0 ? (
              <table className="data-table" style={{ marginTop: 8 }}>
                <thead>
                  <tr>
                    <th>Reason</th>
                    <th>Until</th>
                    <th>Status</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {overrides.map((o) => (
                    <tr key={o.id}>
                      <td>{o.reason}</td>
                      <td>{new Date(o.expires_at).toLocaleString()}</td>
                      <td>
                        {o.revoked_at ? (
                          <Badge tone="gray">Revoked</Badge>
                        ) : new Date(o.expires_at) < new Date() ? (
                          <Badge tone="gray">Expired</Badge>
                        ) : (
                          <Badge tone="amber">Active</Badge>
                        )}
                      </td>
                      <td className="align-right">
                        {!o.revoked_at ? (
                          <button className="btn-link" style={{ color: "var(--red)" }} onClick={() => handleRevoke(o)}>
                            Revoke
                          </button>
                        ) : null}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="field-hint">
                Overrides granted in this browser session are listed here - the backend does not
                expose a full history listing for this Phase.
              </p>
            )}
          </>
        )}
      </div>
    </Modal>
  );
}

function CreateAppKeyFlow({
  users,
  teams,
  onClose,
  onCreated,
}: {
  users: UserResponse[];
  teams: TeamResponse[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [userId, setUserId] = useState(users[0]?.id ?? "");
  const [teamId, setTeamId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<ServiceAccountKeyCreateResponse | null>(null);

  async function handleCreate() {
    setBusy(true);
    setError(null);
    try {
      const result = await createServiceAccount({ name, user_id: userId, team_id: teamId });
      setCreated(result);
    } catch (err) {
      // H-1: 404 "team membership not found" passes through verbatim - the
      // attributed user must already be a member of the chosen team.
      setError(err instanceof ApiError ? err.message : "Failed to create service account.");
    } finally {
      setBusy(false);
    }
  }

  if (created) {
    return (
      <SecretRevealModal
        title={`Save this secret now - ${created.name}`}
        secret={created.secret}
        onDone={onCreated}
      />
    );
  }

  return (
    <Modal title="Create app key" onClose={onClose}>
      <div className="field">
        <label>Name</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. billing-service"
        />
      </div>
      <div className="field">
        <label>Attributed user</label>
        <select value={userId} onChange={(e) => setUserId(e.target.value)}>
          {users.length === 0 ? <option value="">No users yet - create one first</option> : null}
          {users.map((u) => (
            <option key={u.id} value={u.id}>
              {u.name}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label>Team *</label>
        <select value={teamId} onChange={(e) => setTeamId(e.target.value)}>
          <option value="">Select a team...</option>
          {teams.map((t) => (
            <option key={t.id} value={t.id}>
              {t.name}
            </option>
          ))}
        </select>
        <div className="field-hint">
          The attributed user must already be a member of this team - the key inherits that
          team&apos;s budget and policy context.
        </div>
      </div>
      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button
          className="btn btn-primary"
          onClick={handleCreate}
          disabled={busy || !name.trim() || !userId || !teamId}
        >
          {busy ? "Creating..." : "Create"}
        </button>
      </div>
    </Modal>
  );
}

const FILTER_LABELS: Record<AdminKeyType | "all", string> = {
  all: "All keys",
  app: "App keys",
  personal: "Personal keys",
};

function keyPrefixDisplay(row: AdminKeyResponse) {
  const scheme = row.key_type === "app" ? "gk_sk_" : "gk_pk_";
  return (
    <span className="mono">
      {scheme}
      {row.key_prefix}&hellip;
    </span>
  );
}

export default function ServiceAccountsPage() {
  const toast = useToast();
  const [filter, setFilter] = useState<AdminKeyType | "all">("all");
  const [rows, setRows] = useState<AdminKeyResponse[]>([]);
  const [users, setUsers] = useState<UserResponse[] | null>(null);
  const [teams, setTeams] = useState<TeamResponse[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<AdminKeyResponse | null>(null);
  const [regenerating, setRegenerating] = useState<AdminKeyResponse | null>(null);
  const [reveal, setReveal] = useState<{ title: string; secret: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [rotationFor, setRotationFor] = useState<AdminKeyResponse | null>(null);
  const [scheduleFor, setScheduleFor] = useState<AdminKeyResponse | null>(null);

  function refresh() {
    setLoading(true);
    setError(null);
    listAdminKeys(filter)
      .then(setRows)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load keys."))
      .finally(() => setLoading(false));
  }

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(refresh, [filter]);

  useEffect(() => {
    // App-key creation needs the user listing + team picker (H-1: team_id
    // is required on create); a failure here only disables creation, never
    // the oversight listing.
    listUsers()
      .then(setUsers)
      .catch(() => setUsers(null));
    listTeams()
      .then(setTeams)
      .catch(() => setTeams(null));
  }, []);

  async function handleRevoke(row: AdminKeyResponse) {
    setBusy(true);
    try {
      await adminRevokeKey(row.id);
      toast.push("success", `${row.name} revoked.`);
      setRevoking(null);
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to revoke.");
      setRevoking(null);
    } finally {
      setBusy(false);
    }
  }

  async function handleRegenerate(row: AdminKeyResponse) {
    setBusy(true);
    try {
      const result = await adminRegenerateKey(row.id);
      setRegenerating(null);
      setReveal({ title: `New secret for ${result.name}`, secret: result.secret });
      refresh();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to regenerate.");
      setRegenerating(null);
    } finally {
      setBusy(false);
    }
  }

  const sorted = [...rows].sort((a, b) => Number(a.active === b.active ? 0 : a.active ? -1 : 1));

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-header-row">
          <div>
            <div className="page-title">Service Accounts &amp; Keys</div>
            <div className="page-subtitle">
              Every credential in the org - admin-minted app keys and self-serve personal
              keys. Personal keys are created from My API Keys or by a Team Lead, never here.
            </div>
          </div>
          <span style={{ display: "flex", gap: 8 }}>
            <select
              value={filter}
              onChange={(e) => setFilter(e.target.value as AdminKeyType | "all")}
            >
              {Object.entries(FILTER_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
            <button
              className="btn btn-primary"
              onClick={() => setCreating(true)}
              disabled={!users || users.length === 0 || !teams || teams.length === 0}
            >
              + Create app key
            </button>
          </span>
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}
        {users !== null && users.length === 0 && !loading ? (
          <div className="banner banner-info">
            Create a user first (Users screen) before issuing app keys.
          </div>
        ) : null}
        {teams !== null && teams.length === 0 && !loading ? (
          <div className="banner banner-info">
            App keys are team-bound - create a team first (Teams screen) before issuing one.
          </div>
        ) : null}

        <DataTable
          loading={loading}
          rows={sorted}
          rowKey={(r) => r.id}
          emptyState="No keys yet."
          searchText={(r) =>
            `${r.name} ${r.owner_name} ${r.key_type} ${teams?.find((t) => t.id === r.team_id)?.name ?? ""}`
          }
          searchPlaceholder="Filter keys..."
          columns={[
            {
              key: "name",
              header: "Name",
              sortValue: (r) => r.name,
              render: (r) => (
                <span style={!r.active ? { color: "var(--text-muted)" } : undefined}>{r.name}</span>
              ),
            },
            { key: "key", header: "Key", render: keyPrefixDisplay },
            {
              key: "owner",
              header: "Owner",
              sortValue: (r) => `${r.key_type} ${r.owner_name}`,
              render: (r) => (
                <>
                  <Badge tone="gray">{r.key_type === "app" ? "App" : "Personal"}</Badge>{" "}
                  {r.owner_name}
                </>
              ),
            },
            {
              key: "team",
              header: "Team",
              sortValue: (r) =>
                r.team_id === null ? null : (teams?.find((t) => t.id === r.team_id)?.name ?? r.team_id),
              // Legacy pre-Phase-2 keys have no team binding (team_id null).
              render: (r) =>
                r.team_id === null ? (
                  <span className="text-muted">&mdash; (legacy)</span>
                ) : (
                  (teams?.find((t) => t.id === r.team_id)?.name ?? r.team_id)
                ),
            },
            {
              key: "created",
              header: "Created",
              sortValue: (r) => r.created_at,
              render: (r) => new Date(r.created_at).toLocaleDateString(),
            },
            {
              key: "expires",
              header: "Expires",
              sortValue: (r) => r.expires_at ?? "9999",
              render: (r) => (r.expires_at ? new Date(r.expires_at).toLocaleDateString() : "Never"),
            },
            {
              key: "status",
              header: "Status",
              align: "right",
              render: (r) =>
                r.active ? (
                  <Badge tone="green">Active</Badge>
                ) : r.revoked_at ? (
                  <Badge tone="gray">
                    Revoked {new Date(r.revoked_at).toLocaleDateString()}
                  </Badge>
                ) : (
                  <Badge tone="amber">Expired</Badge>
                ),
            },
            {
              key: "actions",
              header: "",
              align: "right",
              render: (r) =>
                r.revoked_at ? null : (
                  <>
                    <button className="btn-link" onClick={() => setRegenerating(r)}>
                      Regenerate
                    </button>{" "}
                    {/* Rotation policy/access-schedule/emergency-override are
                        Phase 3 app-key-only surfaces (backed by the
                        ServiceAccountKey table) - personal keys don't have them. */}
                    {r.key_type === "app" ? (
                      <>
                        <button className="btn-link" onClick={() => setRotationFor(r)}>
                          Rotation
                        </button>{" "}
                        <button className="btn-link" onClick={() => setScheduleFor(r)}>
                          Schedule
                        </button>{" "}
                      </>
                    ) : null}
                    <button
                      className="btn-link"
                      style={{ color: "var(--red)" }}
                      onClick={() => setRevoking(r)}
                    >
                      Revoke
                    </button>
                  </>
                ),
            },
          ]}
        />

        {creating && users && teams ? (
          <CreateAppKeyFlow
            users={users}
            teams={teams}
            onClose={() => setCreating(false)}
            onCreated={() => {
              setCreating(false);
              refresh();
            }}
          />
        ) : null}

        {reveal ? (
          <SecretRevealModal
            title={reveal.title}
            secret={reveal.secret}
            onDone={() => setReveal(null)}
          />
        ) : null}

        {regenerating ? (
          <ConfirmDialog
            title={`Regenerate ${regenerating.name}?`}
            consequence={
              regenerating.key_type === "personal"
                ? `This regenerates ${regenerating.owner_name}'s own personal key - they'll need to update anything using it themselves.`
                : "The current secret stops working immediately - any app using it will start failing until it's updated."
            }
            confirmLabel="Regenerate"
            busy={busy}
            onCancel={() => setRegenerating(null)}
            onConfirm={() => handleRegenerate(regenerating)}
          />
        ) : null}

        {revoking ? (
          <ConfirmDialog
            title={`Revoke ${revoking.name}?`}
            consequence="Anything using this key will immediately start failing authentication."
            confirmLabel="Revoke"
            busy={busy}
            onCancel={() => setRevoking(null)}
            onConfirm={() => handleRevoke(revoking)}
          />
        ) : null}

        {rotationFor ? <RotationModal row={rotationFor} onClose={() => setRotationFor(null)} /> : null}
        {scheduleFor ? <AccessScheduleModal row={scheduleFor} onClose={() => setScheduleFor(null)} /> : null}
      </div>
    </ConsoleShell>
  );
}
