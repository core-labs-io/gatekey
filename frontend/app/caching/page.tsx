"use client";

/**
 * Caching Settings admin console (Phase 4, Reliability & Cost Efficiency).
 * Org Admin only.
 *
 * Two-layer model (backend session notes, see src/lib/api.ts's
 * `TeamCacheSettingsResponse` doc comment): this org-level screen is a KILL
 * SWITCH - when disabled here, no team's caching runs regardless of its own
 * setting. The real per-team opt-in (AC4.3.2/AC4.3.3, default OFF) lives on
 * each team's own page (Org Admin: Teams -> team detail is out of Phase 4
 * scope to touch; Team Lead: /team/reliability). This screen makes that
 * relationship explicit so an admin doesn't mistake the org toggle for a
 * global "caching is on" switch.
 */

import { useEffect, useState } from "react";
import { ConsoleShell } from "@/components/ConsoleShell";
import { ConfirmDialog, DataTable, useToast } from "@/components/ui";
import {
  ApiError,
  clearCache,
  getCachingSettings,
  listCacheEntries,
  updateCachingSettings,
  type CacheEntryTeaser,
  type CachingSettingsResponse,
} from "@/lib/api";

export default function CachingSettingsPage() {
  const toast = useToast();
  const [settings, setSettings] = useState<CachingSettingsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);

  const [entries, setEntries] = useState<CacheEntryTeaser[]>([]);
  const [entriesLoading, setEntriesLoading] = useState(true);
  const [entriesError, setEntriesError] = useState<string | null>(null);
  const [clearing, setClearing] = useState(false);
  const [clearBusy, setClearBusy] = useState(false);

  useEffect(() => {
    loadSettings();
    loadEntries();
  }, []);

  async function loadSettings() {
    setLoading(true);
    setError(null);
    try {
      const data = await getCachingSettings();
      setSettings(data);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load caching settings.");
    } finally {
      setLoading(false);
    }
  }

  function loadEntries() {
    setEntriesLoading(true);
    setEntriesError(null);
    listCacheEntries()
      .then(setEntries)
      .catch((err) => setEntriesError(err instanceof ApiError ? err.message : "Failed to load cache entries."))
      .finally(() => setEntriesLoading(false));
  }

  async function handleSave() {
    if (!settings) return;
    setSaving(true);
    setError(null);
    try {
      const result = await updateCachingSettings({
        enabled: settings.enabled,
        ttl_seconds: settings.ttl_seconds,
      });
      setSettings(result);
      setDirty(false);
      toast.push("success", "Caching settings saved.");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to save settings.");
    } finally {
      setSaving(false);
    }
  }

  async function handleClearCache() {
    setClearBusy(true);
    try {
      const result = await clearCache(); // org-wide (no team_id)
      toast.push("success", `Cache cleared org-wide (${result.entries_cleared} entries).`);
      setClearing(false);
      loadEntries();
    } catch (err) {
      toast.push("error", err instanceof ApiError ? err.message : "Failed to clear cache.");
    } finally {
      setClearBusy(false);
    }
  }

  return (
    <ConsoleShell>
      <div className="page">
        <div className="page-title">Caching Settings</div>
        <div className="page-subtitle">
          Exact-match response caching (AC4.3.1) with TTL, for improved performance and cost
          efficiency.
        </div>

        {error ? <div className="banner banner-error">{error}</div> : null}

        <div className="banner banner-info">
          This org-level toggle is a <strong>kill switch</strong>: when disabled here, caching is
          off for every team regardless of that team&apos;s own setting. Each team must separately
          opt in (default off) and set its own TTL (1 minute - 24 hours) on its team page - see
          Team Lead &rarr; Reliability &amp; Cost for a team you lead.
        </div>

        {loading ? (
          <div className="skeleton skeleton-text" style={{ height: 150 }} />
        ) : settings ? (
          <div className="panel">
            <div className="panel-title">Org-wide kill switch</div>

            <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <input
                type="checkbox"
                id="cache-enabled"
                style={{ width: "auto" }}
                checked={settings.enabled}
                onChange={(e) => {
                  setSettings({ ...settings, enabled: e.target.checked });
                  setDirty(true);
                }}
              />
              <label htmlFor="cache-enabled" style={{ margin: 0 }}>
                Allow caching org-wide
              </label>
            </div>
            <div className="field-hint" style={{ marginTop: -8, marginBottom: 14 }}>
              When off, no team&apos;s requests are cached, even if that team has its own caching
              enabled.
            </div>

            <div className="field">
              <label>Default cache TTL (seconds)</label>
              <input
                type="number"
                value={settings.ttl_seconds}
                onChange={(e) => {
                  setSettings({ ...settings, ttl_seconds: parseInt(e.target.value, 10) || 0 });
                  setDirty(true);
                }}
                min={60}
                max={86400}
              />
              <div className="field-hint">
                Range: 60s - 24h (86400s). A team&apos;s own TTL (1-1440 minutes) takes
                precedence for that team&apos;s traffic once it has opted in.
              </div>
            </div>

            <div className="page-header-row">
              <span className="text-muted">
                {settings.enabled ? "Caching is allowed org-wide" : "Caching is disabled org-wide (kill switch on)"}
              </span>
              <button className="btn btn-primary" onClick={handleSave} disabled={saving || !dirty}>
                {saving ? "Saving..." : "Save Settings"}
              </button>
            </div>
          </div>
        ) : null}

        <div className="panel" style={{ marginTop: 20 }}>
          <div className="page-header-row">
            <div className="panel-title">Cached entries (teaser)</div>
            <button className="btn btn-danger" onClick={() => setClearing(true)}>
              Clear cache (org-wide)
            </button>
          </div>
          <p className="text-muted">
            Metadata only, per AC4.3.9 - the cached prompt/response body is never shown here, only
            enough context to identify an entry.
          </p>
          {entriesError ? <div className="banner banner-error">{entriesError}</div> : null}
          <DataTable
            loading={entriesLoading}
            rows={entries}
            rowKey={(e) => e.key_preview}
            emptyState="No cache entries (or caching is disabled/not yet warmed)."
            columns={[
              { key: "key", header: "Key preview", render: (e) => <span className="mono">{e.key_preview}</span> },
              { key: "provider", header: "Provider", render: (e) => e.provider ?? <span className="text-muted">&mdash;</span> },
              { key: "model", header: "Model", render: (e) => e.model ?? <span className="text-muted">&mdash;</span> },
              {
                key: "tokens",
                header: "Tokens (in/out)",
                align: "right",
                render: (e) => `${e.input_tokens} / ${e.output_tokens}`,
              },
              {
                key: "created",
                header: "Created",
                render: (e) => (e.created_at ? new Date(e.created_at).toLocaleString() : "—"),
              },
              {
                key: "expires",
                header: "Expires",
                render: (e) => (e.expires_at ? new Date(e.expires_at).toLocaleString() : "—"),
              },
            ]}
          />
        </div>

        {clearing ? (
          <ConfirmDialog
            title="Clear the cache org-wide?"
            consequence="Every team's cached responses will be invalidated (soft clear via a sentinel value, per AC4.3.8) - the next matching request from any team will miss and re-fetch from the provider."
            confirmLabel="Clear cache"
            busy={clearBusy}
            onCancel={() => setClearing(false)}
            onConfirm={handleClearCache}
          />
        ) : null}
      </div>
    </ConsoleShell>
  );
}
