"use client";

/**
 * Personal API key components (Phase 2 FE-6, non-admin UI doc section 6).
 *
 * One key list/create/regenerate/revoke surface, used twice per the UI
 * doc's explicit reuse instruction (section 7.3): self-serve on /my-keys,
 * and delegated (Team Lead / Org Admin managing a member's keys) inside
 * ManageMemberKeysModal. The caller wires the endpoint set (self vs
 * delegated) via the `api` prop; the component never decides auth.
 *
 * Secret hygiene: plaintext secrets exist only inside SecretRevealModal's
 * props for the lifetime of that modal - never in any list state.
 */

import { useEffect, useState } from "react";
import {
  ApiError,
  createMemberKey,
  listMemberKeys,
  regenerateMemberKey,
  revokeMemberKey,
  type PersonalApiKeyCreateResponse,
  type PersonalApiKeyResponse,
} from "@/lib/api";
import { Badge, ConfirmDialog, DataTable, FieldError, Modal, useToast } from "@/components/ui";

export interface KeyTeamOption {
  team_id: string;
  team_name: string;
}

export interface PersonalKeyApi {
  create: (body: {
    name: string;
    team_id: string;
    expires_at: string | null;
  }) => Promise<PersonalApiKeyCreateResponse>;
  regenerate: (keyId: string) => Promise<PersonalApiKeyCreateResponse>;
  revoke: (keyId: string) => Promise<void>;
}

// --- One-time secret reveal ---------------------------------------------------

const GATEWAY_BASE = (process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000").replace(
  /\/$/,
  ""
);

/** Same two-step, one-time-reveal pattern as admin-minted service accounts
 * (UI doc section 6's explicit instruction). No close-on-backdrop, no X -
 * the only exit is the explicit "I've saved it" button. */
export function SecretRevealModal({
  title,
  secret,
  onDone,
}: {
  title: string;
  secret: string;
  onDone: () => void;
}) {
  const [copied, setCopied] = useState<"secret" | "snippet" | null>(null);
  // Generic env-var names on purpose (UI doc section 6's compatibility
  // caveat) - no CLI-specific naming until one is confirmed to work.
  const snippet = `export GATEKEY_BASE_URL=${GATEWAY_BASE}/v1\nexport GATEKEY_API_KEY=${secret}`;

  function copy(kind: "secret" | "snippet", value: string) {
    navigator.clipboard?.writeText(value);
    setCopied(kind);
    setTimeout(() => setCopied(null), 1500);
  }

  return (
    <Modal title={title} onClose={null}>
      <div className="banner banner-warning">
        This is the only time you&apos;ll see this secret. Save it now.
      </div>
      <div className="secret-box">
        <span style={{ flex: 1, wordBreak: "break-all" }} className="mono">
          {secret}
        </span>
        <button className="btn" onClick={() => copy("secret", secret)}>
          {copied === "secret" ? "Copied" : "Copy"}
        </button>
      </div>
      <div className="field" style={{ marginTop: 12 }}>
        <label>Quick start</label>
        <pre
          className="mono"
          style={{
            margin: 0,
            padding: 8,
            background: "var(--surface, #f6f6f6)",
            border: "1px solid var(--border, #ddd)",
            borderRadius: 6,
            fontSize: 12,
            overflowX: "auto",
          }}
        >
          {snippet}
        </pre>
        <button
          className="btn btn-secondary"
          style={{ marginTop: 8 }}
          onClick={() => copy("snippet", snippet)}
        >
          {copied === "snippet" ? "Copied" : "Copy snippet"}
        </button>
      </div>
      <div className="modal-actions">
        <button className="btn btn-primary" onClick={onDone}>
          I&apos;ve saved it, close
        </button>
      </div>
    </Modal>
  );
}

// --- Create modal -------------------------------------------------------------

type ExpiryChoice = "none" | "90d" | "custom";

function expiryToIso(choice: ExpiryChoice, customDate: string): string | null {
  if (choice === "none") return null;
  if (choice === "90d") return new Date(Date.now() + 90 * 24 * 60 * 60 * 1000).toISOString();
  return customDate ? `${customDate}T00:00:00Z` : null;
}

/** Step 1 of the create flow. Team is auto-selected (no dropdown) when
 * exactly one option exists; the parent handles the reveal step via
 * onCreated. Backend 422s (personal-key soft cap, org max expiry) surface
 * verbatim as the field error. */
export function KeyCreateModal({
  teams,
  api,
  onClose,
  onCreated,
}: {
  teams: KeyTeamOption[];
  api: PersonalKeyApi;
  onClose: () => void;
  onCreated: (created: PersonalApiKeyCreateResponse) => void;
}) {
  const [name, setName] = useState("");
  const [teamId, setTeamId] = useState(teams.length === 1 ? teams[0].team_id : "");
  const [expiry, setExpiry] = useState<ExpiryChoice>("none");
  const [customDate, setCustomDate] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleCreate() {
    setBusy(true);
    setError(null);
    try {
      const created = await api.create({
        name: name.trim(),
        team_id: teamId,
        expires_at: expiryToIso(expiry, customDate),
      });
      onCreated(created);
    } catch (err) {
      // 422 soft-cap / max-expiry messages pass through verbatim.
      setError(err instanceof ApiError ? err.message : "Failed to create key.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Create API key" onClose={onClose}>
      <div className="field">
        <label>Name</label>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. claude-cli"
        />
      </div>
      {teams.length === 1 ? (
        <div className="field">
          <label>Team</label>
          <input type="text" value={teams[0].team_name} disabled />
        </div>
      ) : (
        <div className="field">
          <label>Team</label>
          <select value={teamId} onChange={(e) => setTeamId(e.target.value)}>
            <option value="">Select a team...</option>
            {teams.map((t) => (
              <option key={t.team_id} value={t.team_id}>
                {t.team_name}
              </option>
            ))}
          </select>
        </div>
      )}
      <div className="field">
        <label>Expires</label>
        {(
          [
            ["none", "No expiration"],
            ["90d", "90 days"],
            ["custom", "Custom date"],
          ] as [ExpiryChoice, string][]
        ).map(([value, label]) => (
          <label key={value} style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <input
              type="radio"
              name="key-expiry"
              checked={expiry === value}
              onChange={() => setExpiry(value)}
              style={{ width: "auto" }}
            />
            {label}
          </label>
        ))}
        {expiry === "custom" ? (
          <input
            type="date"
            value={customDate}
            onChange={(e) => setCustomDate(e.target.value)}
            style={{ marginTop: 6 }}
          />
        ) : null}
        <div className="field-hint">
          Your org may cap the maximum expiration - the server enforces it on create.
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
          disabled={busy || !name.trim() || !teamId || (expiry === "custom" && !customDate)}
        >
          {busy ? "Creating..." : "Create"}
        </button>
      </div>
    </Modal>
  );
}

// --- Key list + full manage surface -------------------------------------------

export function keyStatusBadge(row: PersonalApiKeyResponse) {
  if (row.revoked_at)
    return <Badge tone="gray">Revoked {new Date(row.revoked_at).toLocaleDateString()}</Badge>;
  if (!row.active) return <Badge tone="amber">Expired</Badge>;
  return <Badge tone="green">Active</Badge>;
}

export function PersonalKeyManager({
  keys,
  loading,
  api,
  createTeams,
  teamNameFor,
  showTeam = true,
  ownerName,
  onChanged,
}: {
  keys: PersonalApiKeyResponse[];
  loading: boolean;
  api: PersonalKeyApi;
  /** Teams offered in the create form - exactly one means auto-selected. */
  createTeams: KeyTeamOption[];
  teamNameFor?: (teamId: string) => string;
  showTeam?: boolean;
  /** Set when managing someone else's keys (delegated) - changes confirm copy. */
  ownerName?: string;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [creating, setCreating] = useState(false);
  const [reveal, setReveal] = useState<{ title: string; secret: string } | null>(null);
  const [regenTarget, setRegenTarget] = useState<PersonalApiKeyResponse | null>(null);
  const [revokeTarget, setRevokeTarget] = useState<PersonalApiKeyResponse | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleRegenerate(row: PersonalApiKeyResponse) {
    setBusy(true);
    try {
      const result = await api.regenerate(row.id);
      setRegenTarget(null);
      setReveal({ title: `New secret for ${result.name}`, secret: result.secret });
      onChanged();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to regenerate key.");
      setRegenTarget(null);
    } finally {
      setBusy(false);
    }
  }

  async function handleRevoke(row: PersonalApiKeyResponse) {
    setBusy(true);
    try {
      await api.revoke(row.id);
      toast.push("success", `${row.name} revoked.`);
      setRevokeTarget(null);
      onChanged();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to revoke key.");
      setRevokeTarget(null);
    } finally {
      setBusy(false);
    }
  }

  const columns = [
    {
      key: "name",
      header: "Name",
      render: (r: PersonalApiKeyResponse) => (
        <span style={!r.active ? { color: "var(--text-muted)" } : undefined}>{r.name}</span>
      ),
    },
    ...(showTeam
      ? [
          {
            key: "team",
            header: "Team",
            render: (r: PersonalApiKeyResponse) => teamNameFor?.(r.team_id) ?? r.team_id,
          },
        ]
      : []),
    {
      key: "key",
      header: "Key",
      render: (r: PersonalApiKeyResponse) => (
        <span className="mono">gk_pk_{r.key_prefix}&hellip;</span>
      ),
    },
    {
      key: "created",
      header: "Created",
      render: (r: PersonalApiKeyResponse) => new Date(r.created_at).toLocaleDateString(),
    },
    {
      key: "expires",
      header: "Expires",
      render: (r: PersonalApiKeyResponse) =>
        r.expires_at ? new Date(r.expires_at).toLocaleDateString() : "Never",
    },
    {
      key: "status",
      header: "Status",
      align: "right" as const,
      render: keyStatusBadge,
    },
    {
      key: "actions",
      header: "",
      align: "right" as const,
      render: (r: PersonalApiKeyResponse) =>
        r.revoked_at ? null : (
          <>
            <button className="btn-link" onClick={() => setRegenTarget(r)}>
              Regenerate
            </button>{" "}
            <button
              className="btn-link"
              style={{ color: "var(--red)" }}
              onClick={() => setRevokeTarget(r)}
            >
              Revoke
            </button>
          </>
        ),
    },
  ];

  return (
    <>
      <div className="page-header-row">
        <div className="panel-title">{ownerName ? `${ownerName}'s API keys` : "Keys"}</div>
        <button className="btn btn-primary" onClick={() => setCreating(true)}>
          + Create key
        </button>
      </div>
      <DataTable
        loading={loading}
        rows={keys}
        rowKey={(r) => r.id}
        emptyState="No API keys yet."
        columns={columns}
      />

      {creating ? (
        <KeyCreateModal
          teams={createTeams}
          api={api}
          onClose={() => setCreating(false)}
          onCreated={(created) => {
            setCreating(false);
            setReveal({ title: "Save this key now", secret: created.secret });
            onChanged();
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
      {regenTarget ? (
        <ConfirmDialog
          title={`Regenerate ${regenTarget.name}?`}
          consequence={
            ownerName
              ? `This regenerates ${ownerName}'s own key - the current secret stops working immediately and they'll need to update anything using it.`
              : `Regenerating ${regenTarget.name} invalidates the current secret immediately - anywhere it's still configured will start failing until you update it.`
          }
          confirmLabel="Regenerate"
          busy={busy}
          onCancel={() => setRegenTarget(null)}
          onConfirm={() => handleRegenerate(regenTarget)}
        />
      ) : null}
      {revokeTarget ? (
        <ConfirmDialog
          title={`Revoke ${revokeTarget.name}?`}
          consequence="Anything using this key will immediately start failing authentication. This cannot be undone."
          confirmLabel="Revoke"
          busy={busy}
          onCancel={() => setRevokeTarget(null)}
          onConfirm={() => handleRevoke(revokeTarget)}
        />
      ) : null}
    </>
  );
}

// --- Delegated per-member modal (UI doc section 7.3) --------------------------

/** "Manage API keys" for one team member, via the delegated
 * /v1/teams/{team_id}/members/{user_id}/keys endpoints - the backend scopes
 * the listing to (owner, team), so a lead never sees keys the member holds
 * on other teams. */
export function ManageMemberKeysModal({
  teamId,
  teamName,
  userId,
  memberName,
  onClose,
}: {
  teamId: string;
  teamName: string;
  userId: string;
  memberName: string;
  onClose: () => void;
}) {
  const [keys, setKeys] = useState<PersonalApiKeyResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  function refresh() {
    listMemberKeys(teamId, userId)
      .then(setKeys)
      .catch((err) =>
        setError(err instanceof ApiError ? err.message : "Failed to load this member's keys.")
      )
      .finally(() => setLoading(false));
  }

  useEffect(refresh, [teamId, userId]);

  return (
    <Modal title={`Manage API keys - ${memberName}`} onClose={onClose} width={640}>
      {error ? <div className="banner banner-error">{error}</div> : null}
      <PersonalKeyManager
        keys={keys}
        loading={loading}
        createTeams={[{ team_id: teamId, team_name: teamName }]}
        showTeam={false}
        ownerName={memberName}
        onChanged={refresh}
        api={{
          // team_id is implied by the delegated path - body carries name/expiry only.
          create: ({ name, expires_at }) => createMemberKey(teamId, userId, { name, expires_at }),
          regenerate: (keyId) => regenerateMemberKey(teamId, userId, keyId),
          revoke: (keyId) => revokeMemberKey(teamId, userId, keyId),
        }}
      />
    </Modal>
  );
}
