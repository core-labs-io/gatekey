"use client";

/** Users screen (UI spec section 7.5). Real /v1/admin/users endpoints. */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { Modal, ConfirmDialog, DataTable, Badge, BudgetBar, FieldError, useToast } from "@/components/ui";
import {
  ApiError,
  createUser,
  deleteUser,
  listUsers,
  patchUserOrgRole,
  updateUser,
  type OrgRole,
  type UserResponse,
} from "@/lib/api";

const ORG_ROLE_LABELS: Record<OrgRole, string> = { org_admin: "Org Admin", auditor: "Auditor" };

/** Org-wide role editor (Phase 2, AC1.5) - the ONLY place org_admin/auditor
 * can be granted or cleared. Granting org_admin gets an explicit confirm
 * step; backend errors surface verbatim. */
function OrgRoleModal({
  user,
  onClose,
  onSaved,
}: {
  user: UserResponse;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [role, setRole] = useState<OrgRole | "">(user.org_role ?? "");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await patchUserOrgRole(user.id, role === "" ? null : role);
      toast.push("success", `${user.name}'s org role updated.`);
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to update org role.");
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  if (confirming) {
    return (
      <ConfirmDialog
        title={`Grant ${user.name} the Org Admin role?`}
        consequence="Org Admin has full control over the org: providers, budgets, policies, every team, and granting further org roles. This is the highest privilege in Gatekey."
        confirmLabel="Grant Org Admin"
        destructive={false}
        busy={busy}
        onCancel={() => setConfirming(false)}
        onConfirm={save}
      />
    );
  }

  return (
    <Modal title={`Org role - ${user.name}`} onClose={onClose}>
      <div className="field">
        <label>Org-wide role</label>
        <select value={role} onChange={(e) => setRole(e.target.value as OrgRole | "")}>
          <option value="">None (member/team roles only)</option>
          <option value="org_admin">Org Admin</option>
          <option value="auditor">Auditor (read-only, org-wide)</option>
        </select>
        <div className="field-hint">
          Team-level roles (Member / Team Lead) are managed per team, not here.
        </div>
      </div>
      <FieldError message={error} />
      <div className="modal-actions">
        <button className="btn btn-secondary" onClick={onClose} disabled={busy}>
          Cancel
        </button>
        <button
          className="btn btn-primary"
          onClick={() =>
            role === "org_admin" && user.org_role !== "org_admin" ? setConfirming(true) : save()
          }
          disabled={busy || role === (user.org_role ?? "")}
        >
          {busy ? "Saving..." : "Save"}
        </button>
      </div>
    </Modal>
  );
}

function UserFormModal({
  initial,
  onClose,
  onSaved,
}: {
  initial: UserResponse | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(initial?.name ?? "");
  const [unmetered, setUnmetered] = useState(initial ? initial.budget_usd === null : true);
  const [budget, setBudget] = useState(initial?.budget_usd ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setBusy(true);
    setError(null);
    try {
      const budget_usd = unmetered ? null : budget || "0";
      if (initial) {
        await updateUser(initial.id, { name, budget_usd });
      } else {
        await createUser({ name, budget_usd });
      }
      toast.push("success", initial ? "User updated." : "User created.");
      onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save user.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={initial ? "Edit user" : "Add user"} onClose={onClose}>
      <div className="field">
        <label>Name</label>
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. ana@acme.co" />
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
      </div>
      <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <input
          type="checkbox"
          id="unmetered"
          checked={unmetered}
          onChange={(e) => setUnmetered(e.target.checked)}
          style={{ width: "auto" }}
        />
        <label htmlFor="unmetered" style={{ margin: 0 }}>
          Unmetered (no spend cutoff)
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

export default function UsersPage() {
  const toast = useToast();
  const [rows, setRows] = useState<UserResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState<null | "new" | UserResponse>(null);
  const [roleFor, setRoleFor] = useState<UserResponse | null>(null);
  const [menuFor, setMenuFor] = useState<string | null>(null);
  const [removing, setRemoving] = useState<UserResponse | null>(null);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const [removeBusy, setRemoveBusy] = useState(false);

  function refresh() {
    setLoading(true);
    setError(null);
    listUsers()
      .then(setRows)
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load users."))
      .finally(() => setLoading(false));
  }

  useEffect(refresh, []);

  async function handleRemove(user: UserResponse) {
    setRemoveBusy(true);
    setRemoveError(null);
    try {
      await deleteUser(user.id);
      toast.push("success", `${user.name} removed.`);
      setRemoving(null);
      refresh();
    } catch (err) {
      if (err instanceof ApiError && err.code === "user_in_use") {
        setRemoveError(
          `Can't remove this user - it's still referenced by a service account key. Revoke or reassign it first.`
        );
      } else {
        setRemoveError(err instanceof ApiError ? err.message : "Failed to remove user.");
      }
    } finally {
      setRemoveBusy(false);
    }
  }

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-header-row">
          <div className="page-title">Users</div>
          <button className="btn btn-primary" onClick={() => setFormOpen("new")}>
            + Add user
          </button>
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}

        <DataTable
          loading={loading}
          rows={rows}
          rowKey={(r) => r.id}
          emptyState="No users yet. Add one to start tracking budget/spend."
          searchText={(r) => `${r.name} ${r.org_role ?? ""}`}
          searchPlaceholder="Filter users..."
          initialSort={{ key: "name", dir: "asc" }}
          columns={[
            { key: "name", header: "Name", render: (r) => r.name, sortValue: (r) => r.name },
            {
              key: "org_role",
              header: "Org role",
              sortValue: (r) => r.org_role,
              render: (r) =>
                r.org_role ? (
                  <Badge tone={r.org_role === "org_admin" ? "amber" : "gray"}>
                    {ORG_ROLE_LABELS[r.org_role]}
                  </Badge>
                ) : (
                  <span className="text-muted">&mdash;</span>
                ),
            },
            {
              key: "budget",
              header: "Budget",
              align: "right",
              sortValue: (r) => (r.budget_usd === null ? null : Number(r.budget_usd)),
              render: (r) => (r.budget_usd === null ? "Unmetered" : `$${Number(r.budget_usd).toFixed(2)}`),
            },
            {
              key: "spent",
              header: "Spent",
              align: "right",
              sortValue: (r) => Number(r.current_spend_usd),
              render: (r) => `$${Number(r.current_spend_usd).toFixed(2)}`,
            },
            {
              key: "status",
              header: "Status",
              align: "right",
              render: (r) => {
                if (r.budget_usd === null) return <Badge tone="green">Active</Badge>;
                const spend = Number(r.current_spend_usd);
                const budget = Number(r.budget_usd);
                if (spend >= budget) return <Badge tone="red">Budget exhausted</Badge>;
                if (budget > 0 && spend / budget >= 0.9) return <Badge tone="amber">Near limit</Badge>;
                return <Badge tone="green">Active</Badge>;
              },
            },
            {
              key: "actions",
              header: "Actions",
              align: "right",
              render: (r) => (
                <span style={{ position: "relative" }}>
                  <button className="btn-link" onClick={() => setMenuFor(menuFor === r.id ? null : r.id)}>
                    &#8942;
                  </button>
                  {menuFor === r.id ? (
                    <div
                      style={{
                        position: "absolute",
                        right: 0,
                        top: 20,
                        background: "var(--surface)",
                        border: "1px solid var(--border)",
                        borderRadius: 6,
                        boxShadow: "0 6px 20px rgba(0,0,0,0.12)",
                        zIndex: 10,
                        minWidth: 110,
                      }}
                    >
                      <button
                        className="btn"
                        style={{ display: "block", width: "100%", border: "none", textAlign: "left" }}
                        onClick={() => {
                          setFormOpen(r);
                          setMenuFor(null);
                        }}
                      >
                        Edit
                      </button>
                      <button
                        className="btn"
                        style={{ display: "block", width: "100%", border: "none", textAlign: "left" }}
                        onClick={() => {
                          setRoleFor(r);
                          setMenuFor(null);
                        }}
                      >
                        Org role...
                      </button>
                      <button
                        className="btn"
                        style={{ display: "block", width: "100%", border: "none", textAlign: "left", color: "var(--red)" }}
                        onClick={() => {
                          setRemoving(r);
                          setRemoveError(null);
                          setMenuFor(null);
                        }}
                      >
                        Remove
                      </button>
                    </div>
                  ) : null}
                </span>
              ),
            },
          ]}
        />

        {formOpen ? (
          <UserFormModal
            initial={formOpen === "new" ? null : formOpen}
            onClose={() => setFormOpen(null)}
            onSaved={() => {
              setFormOpen(null);
              refresh();
            }}
          />
        ) : null}

        {roleFor ? (
          <OrgRoleModal
            user={roleFor}
            onClose={() => setRoleFor(null)}
            onSaved={() => {
              setRoleFor(null);
              refresh();
            }}
          />
        ) : null}

        {removing ? (
          <ConfirmDialog
            title={`Remove user ${removing.name}?`}
            consequence={removeError ?? "This cannot be undone."}
            confirmLabel="Delete user"
            busy={removeBusy}
            onCancel={() => setRemoving(null)}
            onConfirm={() => handleRemove(removing)}
          />
        ) : null}
      </div>
    </ConsoleShell>
  );
}
