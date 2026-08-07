"use client";

/**
 * Backup Groups admin console (Phase 4, Reliability & Cost Efficiency,
 * AC4.1.1/AC4.1.2). Org Admin only. Modeled closely on
 * app/rate-limiting/page.tsx's CRUD structure (ConsoleShell, useToast,
 * shared Modal/ConfirmDialog/DataTable).
 *
 * "Member keys" are entered as provider-key LABELS, not ids - the backend
 * has no `keys` column on `backup_groups` at all; membership is tracked on
 * each `ProviderKey.backup_group_id`, and association happens by matching a
 * submitted label against an existing key's label (see
 * `api/v1/admin/backup_groups.py`'s module docstring, mirrored in
 * src/lib/api.ts). A label with no matching key yet is not an error - it's
 * a declared, not-yet-realized member.
 *
 * `listProviderKeys()` (Phase 4, previously missing) now backs a real
 * checkbox picker of actual configured `(provider, label)` pairs instead of
 * free text - a typo can no longer silently declare a member that will
 * never exist. Free text is still offered alongside the picker for
 * declaring a not-yet-configured key up front (a real, documented use case
 * per the backend module docstring above), clearly labeled as such.
 *
 * One real sharp edge, confirmed against the actual backend (not guessed):
 * `label` is unique per `(org_id, provider)`, NOT globally - two different
 * providers can share a label (e.g. both an OpenAI and an Anthropic key
 * labeled "primary"). Checking a key in the picker submits its label, and
 * the backend associates EVERY key across every provider that shares that
 * label (intentional - AC4.1.2 explicitly allows a group to span
 * providers). The picker shows provider alongside label so this is visible
 * before submitting, and checking two rows that happen to share a label is
 * harmless (submitting a label twice is a no-op on the backend).
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { ConfirmDialog, DataTable, Modal, useToast } from "@/components/ui";
import {
  ApiError,
  createBackupGroup,
  deleteBackupGroup,
  listBackupGroups,
  listProviderKeys,
  PROVIDER_LABELS,
  type BackupGroupResponse,
  type ProviderKeyListItem,
} from "@/lib/api";

export default function BackupGroupsPage() {
  const toast = useToast();
  const [groups, setGroups] = useState<BackupGroupResponse[]>([]);
  const [availableKeys, setAvailableKeys] = useState<ProviderKeyListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [selectedLabels, setSelectedLabels] = useState<Set<string>>(new Set());
  const [declaredLabelsText, setDeclaredLabelsText] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [deleting, setDeleting] = useState<BackupGroupResponse | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  function refresh() {
    setLoading(true);
    setError(null);
    Promise.all([listBackupGroups(), listProviderKeys()])
      .then(([groupRows, keyRows]) => {
        setGroups(groupRows);
        setAvailableKeys(keyRows);
      })
      .catch((err) => setError(err instanceof ApiError ? err.message : "Failed to load backup groups."))
      .finally(() => setLoading(false));
  }

  useEffect(refresh, []);

  function openCreate() {
    setName("");
    setSelectedLabels(new Set());
    setDeclaredLabelsText("");
    setFormError(null);
    setCreating(true);
  }

  function toggleLabel(label: string) {
    setSelectedLabels((prev) => {
      const next = new Set(prev);
      if (next.has(label)) next.delete(label);
      else next.add(label);
      return next;
    });
  }

  async function handleCreate() {
    setFormError(null);
    if (!name.trim()) {
      setFormError("Name is required.");
      return;
    }
    const declared = declaredLabelsText
      .split(",")
      .map((k) => k.trim())
      .filter(Boolean);
    // De-duplicate - the same label may appear both checked in the real-key
    // picker and typed into the declared-labels free text.
    const keys = Array.from(new Set([...selectedLabels, ...declared]));

    setSaving(true);
    try {
      const result = await createBackupGroup({ name: name.trim(), keys });
      setGroups((prev) => [...prev, result]);
      setCreating(false);
      toast.push("success", `Backup group "${result.name}" created.`);
    } catch (err) {
      setFormError(err instanceof ApiError ? err.message : "Failed to create backup group.");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete(group: BackupGroupResponse) {
    setDeleteBusy(true);
    try {
      await deleteBackupGroup(group.id);
      setGroups((prev) => prev.filter((g) => g.id !== group.id));
      toast.push("success", `Backup group "${group.name}" deleted.`);
      setDeleting(null);
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to delete backup group.");
    } finally {
      setDeleteBusy(false);
    }
  }

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Backup Groups</div>
        <div className="page-subtitle">
          Provider keys sharing a backup group can serve as failover targets for each other
          (AC4.1.1/AC4.1.2). A group may span multiple providers offering the same model set.
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}

        <div className="page-header-row">
          <div />
          <button className="btn btn-primary" onClick={openCreate}>
            + Create Backup Group
          </button>
        </div>

        <DataTable
          loading={loading}
          rows={groups}
          rowKey={(g) => g.id}
          emptyState="No backup groups configured yet."
          columns={[
            { key: "name", header: "Name", render: (g) => g.name },
            {
              key: "keys",
              header: "Member key labels",
              render: (g) =>
                g.keys.length > 0 ? (
                  <span className="mono">{g.keys.join(", ")}</span>
                ) : (
                  <span className="text-muted">No keys declared</span>
                ),
            },
            {
              key: "actions",
              header: "Actions",
              align: "right",
              render: (g) => (
                <button className="btn-link" style={{ color: "var(--red)" }} onClick={() => setDeleting(g)}>
                  Delete
                </button>
              ),
            },
          ]}
        />

        {creating ? (
          <Modal title="Create Backup Group" onClose={() => setCreating(false)} width={560}>
            <div className="field">
              <label>Name</label>
              <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g., openai-prod-pool" />
            </div>
            <div className="field">
              <label>Member keys</label>
              {availableKeys.length === 0 ? (
                <div className="field-hint">
                  No provider keys are configured yet - add keys on the Providers screen first, or
                  declare a not-yet-configured key by label below.
                </div>
              ) : (
                <div
                  style={{
                    border: "1px solid var(--border)",
                    borderRadius: "var(--radius)",
                    maxHeight: 200,
                    overflowY: "auto",
                  }}
                >
                  {availableKeys.map((k) => (
                    <label
                      key={k.id}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 8,
                        padding: "8px 12px",
                        borderBottom: "1px solid var(--border)",
                        cursor: "pointer",
                      }}
                    >
                      <input
                        type="checkbox"
                        checked={selectedLabels.has(k.label)}
                        onChange={() => toggleLabel(k.label)}
                      />
                      <span className="mono">{k.label}</span>
                      <span className="text-muted">({PROVIDER_LABELS[k.provider]})</span>
                      {k.backup_group_id ? <span className="text-muted">already in a group</span> : null}
                    </label>
                  ))}
                </div>
              )}
              <div className="field-hint">
                Checking a label associates every configured key across every provider that shares
                it (labels are unique per provider, not globally - AC4.1.2 allows a group to span
                providers by design).
              </div>
            </div>
            <div className="field">
              <label>Also declare not-yet-configured keys (comma-separated labels, optional)</label>
              <input
                type="text"
                value={declaredLabelsText}
                onChange={(e) => setDeclaredLabelsText(e.target.value)}
                placeholder="e.g., failover-key-2"
              />
              <div className="field-hint">
                A group can be declared before every member key exists - these labels are recorded
                now and linked automatically once a matching key is added.
              </div>
            </div>
            {formError ? <div className="field-error">{formError}</div> : null}
            <div className="modal-actions">
              <button className="btn btn-secondary" onClick={() => setCreating(false)} disabled={saving}>
                Cancel
              </button>
              <button className="btn btn-primary" onClick={handleCreate} disabled={saving}>
                {saving ? "Creating..." : "Create"}
              </button>
            </div>
          </Modal>
        ) : null}

        {deleting ? (
          <ConfirmDialog
            title={`Delete backup group "${deleting.name}"?`}
            consequence="Every member key's backup_group_id is cleared (they stop participating in this group's failover) - the keys themselves are not deleted."
            confirmLabel="Delete group"
            busy={deleteBusy}
            onCancel={() => setDeleting(null)}
            onConfirm={() => handleDelete(deleting)}
          />
        ) : null}
      </div>
    </ConsoleShell>
  );
}
